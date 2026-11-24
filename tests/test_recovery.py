import json
import os
from pathlib import Path
from subprocess import run
from threading import Event

import pytest
from starlette.testclient import TestClient

from conftest import generate_work, insert_candidate
from soundlab import production_layers, service, sound_first
from soundlab.api import create_app
from soundlab.cancellation import current
from soundlab.jobs import Jobs
from soundlab.read_model import read_round_output
from soundlab.store import Store, now, uid, write_json


def test_newer_candidate_failure_invalidates_older_code_checkpoint(tmp_path):
    response = {'output': {'code': '# previous response'}}
    write_json(tmp_path / 'code_1_response.json', response)
    assert sound_first.saved_code_response(tmp_path, 1) == response

    failed = tmp_path / 'retries' / uid()
    failed.mkdir(parents=True)
    write_json(failed / 'candidate_1_error.json', {'error': 'このコードは再試行で失敗しました'})
    assert sound_first.saved_code_response(tmp_path, 1) is None


def test_interrupted_input_publication_is_recoverable(tmp_path, engine, monkeypatch):
    calls = engine()
    write_json = service.write_json

    def disk_error(path, payload):
        if path.name == 'input.json':
            raise OSError('入力の書き込みに失敗')
        return write_json(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(service, 'write_json', disk_error)
        with TestClient(create_app(tmp_path)) as api:
            response = api.post('/api/rooms', json={'prompt': '消えない制作指示', 'duration': 5, 'count': 1})
            assert response.status_code == 202
            rid = response.json()['roomId']
            job, = api.app.state.production.jobs.snapshot()
            assert isinstance(job.future.exception(timeout=10), OSError)
            assert api.get('/api/rooms/' + rid).json()['rounds'][0]['error'] == '入力の書き込みに失敗'
    assert not calls

    store = Store(tmp_path)
    assert not (tmp_path/'rounds'/rid/'input.json').exists()
    # Also exercise recovery of records written by the previous release.
    store.execute("UPDATE rounds SET status='planning',error=NULL,"
                  "context=json_remove(context,'$.contract','$.seed') WHERE id=?", (rid,))
    with TestClient(create_app(tmp_path)) as api:
        assert api.get('/api/rooms/' + rid).json()['rounds'][0]['status'] == 'interrupted'
        assert not api.app.state.production.jobs.snapshot()
        with monkeypatch.context() as patch:
            patch.setattr(service, 'write_json', disk_error)
            assert api.post(f'/api/rooms/{rid}/rounds/{rid}/retry', json={}).status_code == 202
            api.app.state.production.jobs.snapshot()[0].future.result(timeout=10)
            failed = api.get('/api/rooms/' + rid).json()['rounds'][0]
            assert failed['status'] == 'failed' and failed['error'] == '入力の書き込みに失敗'
        assert api.post(f'/api/rooms/{rid}/rounds/{rid}/retry', json={}).status_code == 202
        api.app.state.production.jobs.snapshot()[0].future.result(timeout=20)
        finished = api.get('/api/rooms/' + rid).json()['rounds'][0]
        assert finished['status'] == 'completed'
        assert finished['candidates'][0]['track']
        assert finished['error'] is None
    saved = json.loads((tmp_path/'rounds'/rid/'input.json').read_text())
    assert (saved['prompt'], saved['duration'], saved['seed']) == ('消えない制作指示', 5, 42)


@pytest.mark.parametrize('operation', ['improve', 'redo'])
def test_accepted_round_survives_shutdown_before_its_worker_starts(tmp_path, engine, operation):
    calls = engine()
    store = Store(tmp_path)
    rid = generate_work(service.Studio(store), None, '元の指示', 5, candidate_count=1, seed=307)
    original = read_round_output(store, rid).views[0][2]['id']
    old_call_count = len(calls)
    queue = Jobs(workers=1)
    started = Event()

    def wait_for_shutdown():
        started.set()
        assert current.get().wait(timeout=20)

    with TestClient(create_app(tmp_path, jobs=queue)) as api:
        queue.submit('ほかの処理', wait_for_shutdown)
        assert started.wait(2)
        if operation == 'improve':
            response = api.post(f'/api/audio/{original}/improve',
                                json={'prompt': '保存された指示', 'duration': 6, 'count': 1, 'remake': True})
        else:
            response = api.post(f'/api/rooms/{rid}/rounds/{rid}/redo', json={'prompt': '保存された指示'})
        assert response.status_code == 202
        child = response.json()['roundId']
        assert store.one('SELECT status FROM rounds WHERE id=?', (child,))['status'] == 'queued'
        assert not queue.snapshot()[0].future.running()
    assert len(calls) == old_call_count

    with TestClient(create_app(tmp_path)) as api:
        saved = next(round_ for round_ in api.get('/api/rooms/' + rid).json()['rounds'] if round_['id'] == child)
        assert saved['prompt'] == '保存された指示'
        assert saved['status'] in ('cancelled', 'interrupted')
        assert saved['duration'] == (6 if operation == 'improve' else 5)
        assert not api.app.state.production.jobs.snapshot()
        assert api.post(f'/api/rooms/{rid}/rounds/{child}/retry', json={}).status_code == 202
        api.app.state.production.jobs.snapshot()[0].future.result(timeout=20)
        assert read_round_output(store, child).views[0][2]
        if operation == 'redo':
            assert json.loads((tmp_path/'rounds'/child/'input.json').read_text())['seed'] == 307
            assert api.post(f'/api/rooms/{rid}/rounds/{child}/undo', json={}).status_code == 200
            assert api.get('/api/audio/' + original + '/status').status_code == 200
        else:
            assert store.one('SELECT parent_id FROM rounds WHERE id=?', (child,))['parent_id'] == saved['parent']


@pytest.mark.parametrize('restart', [False, True])
def test_deletion_retains_all_ids_until_missing_files_are_removed(tmp_path, monkeypatch, restart):
    from soundlab import room_delete

    store = Store(tmp_path)
    rid = insert_candidate(store)
    cid = store.one('SELECT id FROM candidates WHERE round_id=?', (rid,))['id']
    run_id = uid()
    audio = tmp_path/'runs'/run_id/'work.mp3'
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b'test media')
    store.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)',
                  (run_id, cid, 'succeeded', 'test', json.dumps({'audio': {'path': str(audio)}}), now()))
    review_id = store.review(rid, cid, '試聴レビュー', uid(), run_id=run_id)
    # Pre-existing selector records must still be removed with their owning room.
    store.execute('INSERT INTO audio_selections VALUES(?,?,?,?,?,?)',
                  (uid(), rid, cid, run_id, review_id, now()))
    store.save_audio(run_id)

    rmtree = room_delete.shutil.rmtree

    def fail_for_run(path):
        if Path(path) == audio.parent:
            raise PermissionError('試験用の権限エラー')
        rmtree(path)

    with TestClient(create_app(tmp_path)) as api:
        with monkeypatch.context() as patch:
            patch.setattr(room_delete.shutil, 'rmtree', fail_for_run)
            assert api.delete('/api/rooms/' + rid).status_code == 202
        assert api.get('/api/rooms/' + rid).status_code == 404
        assert api.get('/api/audio/' + run_id + '/status').status_code == 404
        failure, = api.get('/api/rooms').json()['failedDeletions']
        assert failure['roomId'] == rid and failure['error'] == '試験用の権限エラー'
        assert audio.exists()
        assert store.rows('SELECT id FROM reviews WHERE id=?', (review_id,))
        if not restart:
            assert api.delete('/api/rooms/' + rid).status_code == 202
            assert api.get('/api/rooms').json()['failedDeletions'] == []

    if restart:
        with TestClient(create_app(tmp_path)) as api:
            assert api.get('/api/rooms').json()['failedDeletions'] == []
    assert not audio.parent.exists()
    assert not (tmp_path/'.room_deletions'/rid).exists()
    for table in ('rounds', 'candidates', 'runs', 'reviews', 'audio_selections', 'saved_audio'):
        assert not store.rows(f'SELECT 1 FROM {table}')


def test_missing_audio_can_be_restored_from_saved_layers(tmp_path, engine, monkeypatch):
    calls = engine()
    store = Store(tmp_path)
    rid = generate_work(service.Studio(store), None, '音源復旧', 5, candidate_count=1)
    candidate, _, run = read_round_output(store, rid).views[0]
    result = json.loads(run['result'])
    cid = candidate['id']
    spatial = Path(result['layers']['spatial'])
    preview = next((spatial/'previews').glob('*/work.mp3'))
    preview.unlink()
    review_id = store.review(rid, cid, '元の完成音のレビュー', uid(), run_id=run['id'])
    call_count = len(calls)

    with TestClient(create_app(tmp_path)) as api:
        room_url = '/api/rooms/' + rid
        preview_url = '/api/candidates/' + cid + '/spatial-preview'
        queue = api.app.state.production.jobs
        output = api.get(room_url).json()['rounds'][0]['candidates'][0]
        assert output['unavailableSpatial'] == {'layerId': spatial.name, 'error': None}
        assert not preview.exists() and not queue.snapshot()
        assert api.post(f"/api/audio/{run['id']}/improve", json={
            'prompt': '音色を改善', 'duration': 6, 'count': 1}).status_code == 400

        def unavailable(*args):
            raise OSError('試聴音の書き出しに失敗')

        with monkeypatch.context() as patch:
            patch.setattr(production_layers, 'make_spatial_preview', unavailable)
            assert api.post(preview_url, json={'layerId': spatial.name}).status_code == 202
            queue.snapshot()[0].future.result(timeout=10)
        output = api.get(room_url).json()['rounds'][0]['candidates'][0]
        assert output['unavailableSpatial']['error'] == '試聴音の書き出しに失敗'
        assert api.post(preview_url, json={'layerId': spatial.name}).status_code == 202
        queue.snapshot()[0].future.result(timeout=20)
        output = api.get(room_url).json()['rounds'][0]['candidates'][0]
        assert output['unavailableSpatial'] is None
        assert [track['stage'] for track in output['previews']] == ['original', 'spatial']
        assert output['track']['id'] == run['id']

        Path(result['audio']['path']).unlink()
        output = api.get(room_url).json()['rounds'][0]['candidates'][0]
        assert output['track'] is None and output['canRetry']
        assert '完成音源が見つかりません' in output['error']
        assert api.post(room_url + f'/rounds/{rid}/retry', json={'position': 0}).status_code == 202
        queue.snapshot()[0].future.result(timeout=20)
        output = api.get(room_url).json()['rounds'][0]['candidates'][0]
        assert output['track'] and not output['error']
        assert output['track']['id'] != run['id']
        assert api.get(output['track']['url']).status_code == 200
        regenerated = read_round_output(store, rid).views[0][2]
        assert json.loads(regenerated['result'])['event_hashes'] == result['event_hashes']
        assert store.one('SELECT run_id FROM reviews WHERE id=?', (review_id,))['run_id'] == run['id']
    assert len(calls) == call_count


def test_reserved_round_can_resume_one_candidate(tmp_path, engine):
    engine()
    queue = Jobs(workers=1)
    release = Event()
    with TestClient(create_app(tmp_path, jobs=queue)) as api:
        queue.submit('ほかの処理', release.wait)
        try:
            rid = api.post('/api/rooms', json={'prompt': '三案の指示', 'duration': 5, 'count': 3}).json()['roomId']
            path = f'/api/rooms/{rid}/rounds/{rid}'
            assert api.post(path + '/cancel', json={}).status_code == 202
            assert api.post(path + '/retry', json={'position': 1}).status_code == 202
        finally:
            release.set()
        queue.snapshot()[0].future.result(timeout=20)
        round_ = api.get('/api/rooms/' + rid).json()['rounds'][0]
        assert round_['status'] == 'partial'
        assert [item['status'] for item in round_['candidates']] == ['waiting', 'ready', 'waiting']


def test_same_data_directory_has_one_api_process(tmp_path):
    with TestClient(create_app(tmp_path)) as api:
        with pytest.raises(ValueError, match='このデータフォルダを使うmotionSoundがすでに起動'):
            with TestClient(create_app(tmp_path)):
                pytest.fail('second queue started')
        assert api.get('/api/health').json() == {'status': 'ok', 'app': 'motionSound'}
    with TestClient(create_app(tmp_path)) as next_api:
        assert next_api.get('/api/health').status_code == 200


@pytest.mark.parametrize(('health', 'status', 'message'), [
    ('{"status":"ok","app":"motionSound"}', 0, 'motionSoundはすでに起動しています。'),
    ('{"status":"ok"}', 1, '8501番ポートには別のHTTPサービスが応答しています。'),
    (None, 0, 'uv-stub'),
])
def test_start_checks_default_port_before_building(tmp_path, health, status, message):
    commands = {
        'curl': 'if [ -n "${MOTIONSOUND_TEST_HEALTH-}" ]; then printf "%s" "$MOTIONSOUND_TEST_HEALTH"; else exit 7; fi',
        'npm': "printf '%s\n' npm-stub",
        'uv': "printf '%s\n' uv-stub",
    }
    for name, command in commands.items():
        path = tmp_path / name
        path.write_text('#!/bin/sh\n' + command + '\n')
        path.chmod(0o755)
    env = {**os.environ, 'PATH': os.pathsep.join((str(tmp_path), os.environ['PATH'])),
           'MOTIONSOUND_TEST_HEALTH': health or ''}
    start = Path(__file__).resolve().parents[1] / 'start'
    result = run([start], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == status
    assert message in result.stdout + result.stderr
    assert ('npm-stub' in result.stdout) == (health is None)


def test_waveform_keeps_high_audio_frequencies(tmp_path):
    import numpy as np
    from scipy.io.wavfile import write

    from soundlab.catalog import waveform
    from soundlab.runner import mp3_encoder

    mp3_encoder()
    t = np.arange(48000) / 48000
    signal = np.concatenate([.7 * np.sin(2 * np.pi * f * t) for f in (1000, 8000)])
    path = tmp_path/'frequency.wav'
    write(path, 48000, signal.astype(np.float32))
    stat = path.stat()
    result = waveform(path, stat.st_mtime_ns, stat.st_size)
    assert result['duration'] == 2
    assert np.median(result['peaks'][:72]) > .95
    assert np.median(result['peaks'][72:]) > .8


def test_legacy_generation_without_count_keeps_fourth_audio_playable_and_retryable(tmp_path, engine):
    calls = engine()
    store = Store(tmp_path)
    rid = generate_work(service.Studio(store), None, '旧形式の四案', 5, candidate_count=4)
    run = read_round_output(store, rid).views[3][2]
    filename = Path(json.loads(run['result'])['audio']['path'])
    store.execute("UPDATE rounds SET context=json_remove(context,'$.candidate_count') WHERE id=?", (rid,))
    snapshot = tmp_path/'rounds'/rid/'input.json'
    previous = json.loads(snapshot.read_text())
    previous.pop('candidate_count')
    snapshot.write_text(json.dumps(previous))
    calls_before = len(calls)
    with TestClient(create_app(tmp_path)) as api:
        room_url = '/api/rooms/' + rid
        output = api.get(room_url).json()['rounds'][0]
        assert output['count'] == 4
        assert output['candidates'][3]['track']['id'] == run['id']
        assert api.get('/api/audio/' + run['id'] + '/status').status_code == 200
        filename.unlink()
        assert api.post(room_url + f'/rounds/{rid}/retry', json={'position': 3}).status_code == 202
        api.app.state.production.jobs.snapshot()[0].future.result(timeout=20)
        restored = api.get(room_url).json()['rounds'][0]
        assert restored['status'] == 'completed'
        assert restored['candidates'][3]['track']['id'] != run['id']
    assert len(calls) == calls_before
