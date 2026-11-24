"""Resolve published audio by ID and serialize read-only production state."""
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory

import numpy as np
from scipy.io import wavfile

from . import refinement, runner, sound_first
from .cancellation import GenerationCancelled
from .job_progress import job_progress
from .jobs import job_route
from .production_layers import saved_spatial_preview, spatial_preview_request
from .read_model import read_round_output
from .room_delete import deletion_requested
from .rooms import RoomHistory, room_title
from .spatial.work import DIRECT_MAX_GAIN, DIRECT_REFERENCE_DISTANCE
from .spatial_view import source_descriptions, trajectory_data
from .spatial_view_bass import add_audio_features
from .store import ID_PATTERN, read_json
from .structured_api import redact

MEDIA_TYPES = {'.mp3': 'audio/mpeg', '.wav': 'audio/wav'}
RETRYABLE_ROUND_STATES = frozenset({'awaiting_approval', 'failed', 'partial', 'interrupted', 'cancelled', 'completed'})


class MissingRecord(ValueError):
    pass


def read_record(path):
    try:
        return read_json(path)
    except (OSError, ValueError):
        return {}


def artifact(path, directory):
    try:
        return runner.safe_artifact(path, directory)
    except (OSError, ValueError):
        return None


def latest(paths):
    existing = []
    for path in paths:
        try:
            existing.append((path.stat().st_mtime_ns, path))
        except FileNotFoundError:
            continue
    return max(existing, default=(0, None))[1]


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    room_id: str
    round_id: str
    candidate_id: str
    stage: str
    path: Path
    duration: float
    layer: Path | None = None
    provider: str = 'openai'
    has_spatial_view: bool = False
    can_partial_improve: bool = False

    @property
    def media_type(self):
        return MEDIA_TYPES[self.path.suffix.lower()]

    def public(self):
        return {'id': self.id, 'title': self.title, 'roomId': self.room_id,
                'roundId': self.round_id, 'candidateId': self.candidate_id, 'stage': self.stage,
                'format': self.path.suffix[1:].lower(), 'duration': self.duration,
                'url': f'/api/audio/{self.id}', 'hasSpatialView': self.has_spatial_view,
                'provider': self.provider, 'canPartialImprove': self.can_partial_improve}


@lru_cache(maxsize=128)
def waveform(path: Path, modified: int, size: int):
    """Peak envelope from the exact file used by the shared player."""
    with TemporaryDirectory() as tmp:
        decoded = Path(tmp)/'native.wav'
        subprocess.run(
            [runner.mp3_encoder(), '-v', 'error', '-nostdin', '-i', str(path),
             '-map', '0:a:0', '-c:a', 'pcm_f32le', str(decoded)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=25, check=True,
        )
        rate, frames = wavfile.read(decoded, mmap=True)
        duration = len(frames) / rate
        peaks = []
        if len(frames):
            for chunk in np.array_split(frames, min(144, len(frames))):
                peak = float(np.max(np.abs(chunk)))
                peaks.append(peak if np.isfinite(peak) else 0.0)
        del frames
    scale = max(peaks, default=0) or 1
    return {'peaks': [round(value / scale, 4) for value in peaks], 'duration': duration}


class Catalog:
    def __init__(self, store, jobs):
        self.store, self.jobs = store, jobs
        self.history = RoomHistory(store)

    def require_room(self, rid):
        if not re.fullmatch(ID_PATTERN, rid) or deletion_requested(self.store.root, rid):
            raise MissingRecord('部屋が見つかりません')
        if not self.store.rows("""SELECT 1 FROM rounds r LEFT JOIN room_history h ON h.round_id=r.id
                WHERE r.id=? AND json_extract(r.context,'$.inline_room_id') IS NULL
                  AND h.trashed_at IS NULL""", (rid,)):
            raise MissingRecord('部屋が見つかりません')

    def require_round(self, room_id, rid):
        self.require_room(room_id)
        row = next((row for row in self.history.steps(room_id) if row['id'] == rid), None)
        if row is None:
            raise MissingRecord('生成回が見つかりません')
        return row

    def candidate(self, cid):
        if not re.fullmatch(ID_PATTERN, cid):
            raise MissingRecord('候補が見つかりません')
        rows = self.store.rows("""SELECT c.*, r.context, r.mode, r.status AS round_status,
            COALESCE(json_extract(r.context,'$.inline_room_id'),r.id) AS room_id
            FROM candidates c JOIN rounds r ON r.id=c.round_id WHERE c.id=?""", (cid,))
        if not rows or rows[0]['round_status'] == 'superseded':
            raise MissingRecord('候補が見つかりません')
        candidate = rows[0]
        self.require_room(candidate['room_id'])
        candidate['context'] = json.loads(candidate['context'] or '{}')
        return candidate

    def final(self, run_id):
        if not re.fullmatch(ID_PATTERN, run_id):
            raise MissingRecord('音源が見つかりません')
        rows = self.store.rows("SELECT * FROM runs WHERE id=? AND status='succeeded'", (run_id,))
        if not rows:
            raise MissingRecord('音源が見つかりません')
        run = rows[0]
        return self.final_track(self.candidate(run['candidate_id']), run)

    def final_track(self, candidate, run):
        run_id = run['id']
        if not re.fullmatch(ID_PATTERN, run_id) or not re.fullmatch(ID_PATTERN, candidate['id']):
            raise MissingRecord('音源が見つかりません')
        info = (json.loads(run['result'] or '{}').get('audio') or {})
        base = self.store.root/'runs'/run_id
        path = runner.published_audio_path(self.store.root, run)
        if path is None:
            raise MissingRecord('音源が見つかりません')
        duration = info['frames']/info['sample_rate'] if (
            info.get('frames') is not None and info.get('sample_rate')) else self.duration(candidate)
        can_partial = True
        try:
            refinement.source(self.store.root, run_id, duration, verify_audio=False)
        except (ValueError, TypeError, AttributeError):
            can_partial = False
        return Track(run_id, candidate['title'], candidate['room_id'], candidate['round_id'],
                     candidate['id'], 'final', path, duration, provider=self.provider(candidate),
                     has_spatial_view=bool(read_record(base/'request.json').get('events')),
                     can_partial_improve=can_partial)

    @staticmethod
    def duration(candidate):
        return candidate['context'].get('render_context', {}).get('duration_seconds', 0)

    @staticmethod
    def provider(candidate):
        if candidate['mode'] == 'demo':
            return 'demo'
        context = candidate['context']
        position = candidate['position']
        if context.get('compare_claude') and not 0 <= position < (context.get('candidate_count') or 0):
            return 'unknown'
        try:
            return sound_first.provider_for_position(context, position)
        except StopIteration:
            return 'unknown'

    def stage(self, stage, cid, attempt, preview=None):
        return self.stage_track(self.candidate(cid), stage, attempt, preview)

    def stage_track(self, candidate, stage, attempt, preview=None):
        cid = candidate['id']
        base = self.store.root/'candidates'/cid/'layers'
        if stage == 'original':
            layer = base/'sound'/attempt
            if not (layer/'material.json').is_file():
                raise MissingRecord('原音が見つかりません')
            path = artifact(layer/'execution'/'work.mp3', base)
        else:
            layer = base/'spatial'/attempt
            if not (layer/'scene.json').is_file():
                raise MissingRecord('空間設計が見つかりません')
            path = artifact(layer/'previews'/preview/'work.mp3', base)
        try:
            available = path is not None and path.stat().st_size > 0
        except OSError:
            available = False
        if not available:
            raise MissingRecord('音源が見つかりません')
        audio_id = '.'.join(filter(None, (stage, cid, attempt, preview)))
        return Track(audio_id, candidate['title'], candidate['room_id'], candidate['round_id'],
                     candidate['id'], stage, path, self.duration(candidate), layer,
                     self.provider(candidate), stage == 'spatial')

    def track(self, audio_id):
        if match := re.fullmatch(rf'original\.({ID_PATTERN})\.({ID_PATTERN})', audio_id):
            return self.stage('original', *match.groups())
        if match := re.fullmatch(rf'spatial\.({ID_PATTERN})\.({ID_PATTERN})\.({ID_PATTERN})', audio_id):
            return self.stage('spatial', *match.groups())
        return self.final(audio_id)

    def spatial_layer(self, cid, attempt):
        base = self.store.root/'candidates'/cid
        if not re.fullmatch(ID_PATTERN, attempt):
            raise MissingRecord('空間設計が見つかりません')
        layer = base/'layers'/'spatial'/attempt
        if not artifact(layer/'scene.json', base):
            raise MissingRecord('空間設計が見つかりません')
        return layer

    def previews(self, candidate, run):
        cid = candidate['id']
        if not re.fullmatch(ID_PATTERN, cid):
            return [], None
        base = self.store.root/'candidates'/cid/'layers'
        origins = json.loads(run['result'] or '{}').get('layers', {}) if run else {}
        spatial = origins.get('spatial')
        sound = origins.get('sound')
        if not spatial and not run:
            scene = latest((base/'spatial').glob('*/scene.json'))
            spatial = scene.parent if scene else None
        if spatial and not sound:
            attempt = read_record(base/'spatial'/Path(spatial).name/'scene.json').get('inputs', {}).get('sound')
            if attempt and re.fullmatch(ID_PATTERN, attempt):
                sound = base/'sound'/attempt
        if not sound and not run:
            material = latest((base/'sound').glob('*/material.json'))
            sound = material.parent if material else None
        tracks, unavailable = [], None
        if sound and re.fullmatch(ID_PATTERN, Path(sound).name):
            try:
                tracks.append(self.stage_track(candidate, 'original', Path(sound).name).public())
            except MissingRecord:
                pass
        if spatial and re.fullmatch(ID_PATTERN, Path(spatial).name):
            attempt = Path(spatial).name
            layer = base/'spatial'/attempt
            path = saved_spatial_preview(layer)
            if path:
                try:
                    tracks.append(self.stage_track(candidate, 'spatial', attempt, path.parent.name).public())
                except MissingRecord:
                    pass
            if not any(track['stage'] == 'spatial' for track in tracks) and (layer/'scene.json').is_file():
                failure = latest(layer.glob('preview_error_*.json'))
                unavailable = {'layerId': attempt,
                               'error': redact(read_record(failure).get('error') or '') if failure else None}
        return tracks, unavailable

    def room_list(self):
        active = {job_route(job)[2] for job in self.jobs.snapshot() if not job.future.done()}
        return [{'id': row['id'], 'title': room_title(row),
                 'created': row['created'], 'active': row['id'] in active}
                for row in self.history.list() if not deletion_requested(self.store.root, row['id'])]

    def room(self, rid):
        self.require_room(rid)
        tasks = {}
        failures = {}
        for job in reversed(self.jobs.snapshot()):
            kind, target, owner = job_route(job)
            if owner != rid:
                continue
            if not job.future.done():
                if target not in tasks or job.future.running():
                    percent, label = job_progress(self.store, job)
                    tasks[target] = {'id': job.id, 'progress': percent, 'label': label,
                                     'stopping': job.stop.is_set()}
            elif not job.future.cancelled() and (error := job.future.exception()) and not isinstance(error, GenerationCancelled):
                failures[kind, target] = redact(str(error))
        steps = self.history.steps(rid)
        rounds = [self.round_output(row, tasks.get(row['id'])) for row in steps]
        known = {row['id'] for row in steps}
        return {'id': rid, 'rounds': rounds, 'active': bool(tasks),
                'errors': [message for (kind, target), message in failures.items()
                           if target not in known or kind == 'preview']}

    def round_output(self, row, task):
        rid = row['id']
        context = json.loads(row['context'])
        output = read_round_output(self.store, rid, context=context)
        candidates = []
        positions = sorted(sound_first.candidate_positions(context, output.views))
        for position in positions:
            candidate, attempts, run = output.views.get(position, ({}, [], None))
            candidate = {**candidate, 'position': position, 'context': context,
                         'mode': row['mode'], 'room_id': context.get('inline_room_id', rid)}
            cid = candidate.get('id')
            track = None
            previews, unavailable = self.previews(candidate, run) if cid else ([], None)
            if run:
                try:
                    track = self.final_track(candidate, run).public()
                except MissingRecord:
                    pass
            last = attempts[0] if attempts else {}
            failure = output.errors.get(position)
            if last.get('status') == 'failed':
                failure = read_record(self.store.root/'runs'/last['id']/'result.json').get('error') or (
                    json.loads(last.get('result') or '{}').get('error'))
            if run and not track and not failure:
                failure = '保存した完成音源が見つかりません。この候補を再開して復旧できます。'
            provider = self.provider(candidate)
            stage = output.checkpoints.get(position)
            planned = output.plans[position] if position < len(output.plans) else None
            candidates.append({'id': cid, 'position': position, 'title': candidate.get('title') or f'候補 {position+1}',
                               'provider': provider, 'plan': planned, 'track': track, 'previews': previews,
                               'unavailableSpatial': unavailable,
                               'canRetry': not track and row['error'] != refinement.NO_EDITS
                               and (row['status'] in RETRYABLE_ROUND_STATES or bool(failure)),
                               'error': failure if not track else None, 'checkpoint': stage,
                               'status': ('ready' if track else 'failed' if failure else
                                          'running' if last.get('status') == 'running' else 'waiting')})
        return {'id': rid, 'prompt': row['prompt'], 'created': row['created'],
                'status': row['status'], 'error': row['error'], 'task': task,
                'count': len(positions), 'duration': context.get('render_context', {}).get('duration_seconds'),
                'previous': context.get('replaces_round_id'), 'parent': row['parent_id'],
                'candidates': candidates}

    def spatial(self, track):
        base = self.store.root/'candidates'/track.candidate_id
        if track.stage == 'original':
            raise MissingRecord('空間情報がありません')
        try:
            if track.stage == 'spatial':
                scene = read_record(track.layer/'scene.json')
                request = spatial_preview_request(base, scene)
                attempt = scene['inputs']['sound']
                if not re.fullmatch(ID_PATTERN, attempt):
                    raise ValueError('不正な保存先')
                clips = base/'layers'/'sound'/attempt/'execution'/'events'
            else:
                request = read_record(self.store.root/'runs'/track.id/'request.json')
                clips = self.store.root/'runs'/track.id/'events'
            data = trajectory_data(request, source_descriptions(base/'plan.json'))
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            raise MissingRecord('空間情報がありません') from None
        if not data['sources']:
            raise MissingRecord('空間情報がありません')
        add_audio_features(data, clips)
        return {**data, 'referenceDistance': DIRECT_REFERENCE_DISTANCE, 'maxDirectGain': DIRECT_MAX_GAIN}
