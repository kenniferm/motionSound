import hashlib
import importlib.metadata
import json
import os
import platform
import time
import shutil
import signal
import subprocess
import sys
from pathlib import Path
import numpy as np
from tempfile import TemporaryDirectory, TemporaryFile
from .cancellation import GenerationCancelled, check
from .store import now, write_json


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(code, request):
    return hashlib.sha256(code + json.dumps(request, sort_keys=True).encode()).hexdigest()


def safe_artifact(path, base):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(base.resolve()):
        raise ValueError('成果物のパスが不正です')
    return path


def published_audio_path(root, run):
    """Resolve a recorded MP3 or WAV in its run directory, including moved workspaces."""
    info = json.loads(run['result'] or '{}').get('audio') or {}
    base = root/'runs'/run['id']
    recorded = Path(info.get('path') or 'work.mp3')
    if recorded.suffix.lower() not in ('.mp3', '.wav'):
        return None
    for path in (base/recorded.name, recorded):
        try:
            if safe_artifact(path, base).stat().st_size:
                return path
        except (OSError, ValueError):
            continue
    return None


def mp3_encoder():
    executable = shutil.which('ffmpeg')
    if executable is None:
        raise ValueError('MP3出力にはFFmpegが必要です。brew install ffmpeg を実行してください。')
    return executable


def convert(path, base, request, *, expected_frames=None):
    safe_artifact(path, base)
    frames = expected_frames if expected_frames is not None else request['duration_seconds'] * request['sample_rate']
    if path.stat().st_size > frames * 2 * 8 + 10000:
        raise ValueError('音声配列が大きすぎます')
    a = np.load(path, mmap_mode='r', allow_pickle=False)
    if a.dtype not in (np.dtype('float32'), np.dtype('float64')) or a.shape != (frames, 2):
        raise ValueError('音声配列の形式・長さ・チャンネル数が不正です')
    if not np.isfinite(a).all():
        raise ValueError('音声にNaNまたは無限大があります')
    peak = float(np.max(np.abs(a)))
    if peak > 1:
        raise ValueError('振幅が範囲[-1,1]を超えています')
    rate = request['sample_rate']
    target = path.with_suffix('.mp3')
    with TemporaryDirectory(dir=base) as temporary:
        encoded = Path(temporary) / target.name
        check()
        command = [mp3_encoder(), '-hide_banner', '-loglevel', 'error', '-f', 'f32le',
                   '-ar', str(rate), '-ac', '2', '-i', 'pipe:0',
                   '-c:a', 'libmp3lame', '-b:a', '320k', str(encoded)]
        with TemporaryFile(dir=temporary) as pcm:
            a.astype('<f4', copy=False).tofile(pcm)
            pcm.seek(0)
            check()
            with subprocess.Popen(command, stdin=pcm, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE) as encoder:
                deadline = time.monotonic() + 60
                try:
                    while True:
                        check()
                        if time.monotonic() >= deadline:
                            raise ValueError('MP3の書き出しがタイムアウトしました')
                        try:
                            _, stderr = encoder.communicate(timeout=.2)
                            break
                        except subprocess.TimeoutExpired:
                            continue
                finally:
                    if encoder.poll() is None:
                        encoder.kill()
                        encoder.communicate()
                if encoder.returncode:
                    raise ValueError('MP3の書き出しに失敗しました：' + stderr.decode(errors='replace').strip())
        check()
        if encoded.stat().st_size == 0:
            raise ValueError('MP3の書き出し結果が空です')
        target.hardlink_to(encoded)
    rms = float(np.sqrt(np.mean(a.astype(np.float64)**2)))
    dc = np.mean(a, axis=0)
    # A short summary at fixed intervals supports diagnosis, not perceptual scoring.
    windows = []
    for chunk in np.array_split(a, min(12, frames), axis=0):
        check()
        channel_rms = np.sqrt(np.mean(chunk.astype(np.float64)**2, axis=0))
        mono = chunk[:4096].mean(axis=1)
        magnitude = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
        frequency = np.fft.rfftfreq(len(mono), 1 / 48000)
        centroid = float(np.dot(frequency, magnitude) / magnitude.sum()) if magnitude.any() else None
        windows.append({'channel_rms': channel_rms.tolist(), 'spectral_centroid_hz': centroid})
    diagnostics = {
        'windows': windows,
        'near_silent': rms < 1e-4,
        'dc_offset_by_channel': dc.tolist(),
        'dc_warning': bool(np.max(np.abs(dc)) > .01),
        'endpoint_peak': float(np.max(np.abs(a[[0, -1]]))),
        'endpoint_warning': bool(np.max(np.abs(a[[0, -1]])) > .05),
        'peak_at_full_scale': peak >= .999,
    }
    return {'path': str(target), 'peak': peak, 'rms': rms, 'diagnostics': diagnostics,
            'silent': peak == 0, 'frames': frames, 'sample_rate': rate, 'channels': 2, 'format': 'mp3', 'bitrate_kbps': 320, 'sha256': file_hash(target)}


def manifest_entries(run_dir):
    # Inspect only known artifact locations; never follow generated symbolic links.
    paths = [run_dir / name for name in ('source.py', 'request.json', 'environment.json',
        'process.json', 'run.log', 'result.json', 'work.mp3', 'reused_materials.json')]
    folder = run_dir / 'events'
    if folder.is_dir() and not folder.is_symlink():
        paths.extend(sorted(folder.glob('*.npy')))
    entries, excluded = [], []
    for path in paths:
        if not path.exists() and not path.is_symlink():
            continue
        try:
            safe_artifact(path, run_dir)
        except ValueError:
            excluded.append(str(path.relative_to(run_dir)))
            continue
        entries.append({'path': str(path.relative_to(run_dir)), 'bytes': path.stat().st_size,
                        'sha256': file_hash(path)})
    return entries, excluded


def execute(code_path, request, run_dir, approval, timeout=120, reuse=None, *,
            stage='final', materials=None, provenance=None, retained=None):
    """Direct execution is NOT a sandbox. Only called after an explicit user action."""
    started_at, started = now(), time.perf_counter()
    run_dir = Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    code = Path(code_path).read_bytes()
    if fingerprint(code, request) != approval:
        raise ValueError('承認後にコードまたは設定が変わりました')
    source = run_dir / 'source.py'
    if code:
        source.write_bytes(code)
    actual = {**request, 'output_dir': str(run_dir)}
    if materials is not None:
        actual['input_dir'] = str(run_dir / 'input')
    request_path = run_dir / 'request.json'
    if retained is not None:
        retained_ids = retained[2].keys()
        editable = [event for event in request['events'] if event['id'] not in retained_ids]
    write_json(request_path, actual)
    write_json(run_dir / 'environment.json', {'python': sys.version, 'executable': sys.executable,
        'request_argument': 'json_path',
        'platform': platform.platform(), 'machine': platform.machine(),
        'dependencies': {p: importlib.metadata.version(p) for p in ['numpy','scipy']}, 'approval': approval})
    result = {'status': 'failed', 'error': None, 'audio': None,
              'started_at': started_at, 'manifest': str(run_dir / 'manifest.json')}
    if provenance is not None:
        result['layers'] = provenance
    result['layer'] = stage
    proc = None
    try:
        from .spatial.work import dry_mix, validate_clips, mix_work

        check()
        if materials is not None:
            original, events, hashes = materials
            validate_clips(original, events, hashes)
            inputs = run_dir / 'input' / 'events'
            inputs.mkdir(parents=True)
            (run_dir / 'events').mkdir()
            final_frames = {event['id']: event['frames'] for event in request['events']}
            for event in events:
                check()
                name = event['id'] + '.npy'
                shutil.copyfile(original / 'events' / name, inputs / name)
                if final_frames.get(event['id']) == event['frames']:
                    shutil.copyfile(inputs / name, run_dir / 'events' / name)

        if reuse:
            previous, hashes = reuse
            validate_clips(previous, request['events'], hashes)
            (run_dir/'events').mkdir()
            for event_id in hashes:
                check()
                shutil.copyfile(previous/'events'/f'{event_id}.npy', run_dir/'events'/f'{event_id}.npy')
            validate_clips(run_dir, request['events'], hashes)
            write_json(run_dir/'reused_materials.json', {
                'source_directory': str(previous), 'hashes': hashes,
            })
        else:
            if not code:
                raise ValueError('実行するPythonコードがありません')
            if retained is not None:
                request_path = run_dir/'python_request.json'
                write_json(request_path, {**actual, 'events': editable,
                                          'source_events': materials[1] if materials else []})
            env = {'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8', 'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1'}
            with (run_dir / 'run.log').open('xb') as log:
                proc = subprocess.Popen([sys.executable, '-I', str(source), str(request_path)],
                    cwd=run_dir, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                write_json(run_dir / 'process.json', {'pid': proc.pid})
                deadline = time.monotonic() + timeout
                while True:
                    check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f'実行時間が{timeout}秒を超えました')
                    try:
                        proc.wait(timeout=min(.2, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if proc.returncode != 0:
                    result['python_returncode'] = proc.returncode
                    raise ValueError(f'Pythonが終了コード{proc.returncode}で失敗しました')
            # Finish ordinary descendants before inspecting their output files.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if retained is not None and reuse is None:
            clips = run_dir/'events'
            validate_clips(run_dir, editable)
            if {path.name for path in clips.iterdir()} != {e['id'] + '.npy' for e in editable}:
                raise ValueError('変更対象外の素材が書き出されました')
            original, kept_events, hashes = retained
            validate_clips(original, kept_events, hashes)
            for event_id in hashes:
                check()
                shutil.copyfile(original/'events'/f'{event_id}.npy', clips/f'{event_id}.npy')
        events = request['events']
        frames = request['duration_seconds'] * 48000
        result['event_hashes'] = validate_clips(run_dir, events)
        if retained is not None and any(result['event_hashes'][k] != v for k, v in retained[2].items()):
            raise ValueError('保持する素材が変更されました')
        if stage == 'sound':
            result['audio'] = dry_mix(run_dir, events, frames)
            result['renderer'] = 'dry_reference'
        elif stage == 'final':
            result['audio'], result['dataset'] = mix_work(run_dir, run_dir/'events', events, frames, run_dir.parent.parent)
            result['renderer'] = 'binaural_work_v02'
        result['status'] = 'succeeded'
    except GenerationCancelled as exc:
        result.update(status='cancelled', error=str(exc), audio=None)
    except Exception as exc:
        result['error'] = str(exc)
    finally:
        # Stop ordinary descendants even when the script's main process exited first.
        if proc:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        if materials is not None and (run_dir / 'input').is_dir() and not (run_dir / 'input').is_symlink():
            try:
                shutil.rmtree(run_dir / 'input')
            except OSError as exc:
                result['status'] = 'failed'
                result['error'] = str(exc)
        result['finished_at'] = now()
        result['elapsed_seconds'] = time.perf_counter() - started
        write_json(run_dir / 'result.json', result)
        entries, excluded = manifest_entries(run_dir)
        write_json(run_dir / 'manifest.json', {'schema_version': 1, 'engine': 'python',
            'run_id': run_dir.name, 'status': result['status'], 'approval': approval,
            'started_at': started_at, 'finished_at': result['finished_at'],
            'elapsed_seconds': result['elapsed_seconds'], 'render_context': request.get('render_context'),
            'renderer': result.get('renderer'), 'layer': stage,
            'artifacts': entries, 'excluded_unsafe_paths': excluded})
    return result
