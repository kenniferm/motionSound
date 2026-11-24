"""Local Starlette service for the React production workspace."""
from contextlib import asynccontextmanager
from functools import partial
import json
import logging
import os
from pathlib import Path
from sqlite3 import Error as DatabaseError
from subprocess import SubprocessError
from threading import RLock

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import production_layers, questionnaire, sound_first, structured_api
from .cancellation import GenerationCancelled
from .catalog import Catalog, MissingRecord, RETRYABLE_ROUND_STATES, artifact, read_record, waveform
from .jobs import Jobs, job_route
from .refinement import NO_EDITS
from .room_delete import deletion_requested, finish_room_deletion, request_room_deletion
from .rooms import room_title
from .service import Studio
from .sound_first import MAX_COUNT
from .store import Store, uid, write_json

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger(__name__)


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, strict=True)


class Instruction(Input):
    prompt: str = Field(min_length=1)


class Generate(Instruction):
    duration: int = Field(default=60, ge=5, le=240)
    count: int = Field(default=1, ge=1, le=MAX_COUNT)
    compare: bool = False


class Improve(Instruction):
    duration: int = Field(ge=5, le=240)
    count: int = Field(default=1, ge=1, le=MAX_COUNT)
    remake: bool = False


class Polish(Instruction):
    refinement: bool = False


class Rename(Input):
    name: str = Field(min_length=1, max_length=120)


class Retry(Input):
    position: int | None = Field(default=None, ge=0)


class Review(Input):
    model_config = ConfigDict(str_strip_whitespace=False)
    text: str = ''
    answers: dict[str, str] = Field(default_factory=dict)
    token: str | None = None


class Preview(Input):
    layerId: str


class Busy(ValueError):
    pass


class Production:
    def __init__(self, root, jobs):
        self.store = Store(root)
        self.jobs = jobs
        self.studio = Studio(self.store)
        self.catalog = Catalog(self.store, jobs)
        self.lock = RLock()
        self.deletion_errors = {}

    def active(self, room_id):
        return [job for job in self.jobs.snapshot()
                if job_route(job)[2] == room_id and not job.future.done()]

    def submit(self, kind, room_id, rid, prompt, work, reserve=None):
        with self.lock:
            if kind != 'room':
                self.catalog.require_room(room_id)
            active = self.active(room_id)
            if active and (kind not in ('improve', 'render') or len(active) > 1
                           or job_route(active[0])[0] == 'redo'):
                raise Busy('この部屋の処理が終わってから実行してください')
            key = f'{kind}:{rid}:{room_id}'
            if any(job.key == key for job in active):
                raise Busy('同じ処理はすでに登録されています')

            def execute():
                try:
                    return work()
                except GenerationCancelled:
                    self.set_incomplete(rid, 'cancelled', None)
                    return rid
                except Exception as exc:
                    if kind != 'preview':
                        self.set_incomplete(rid, 'failed', structured_api.redact(str(exc)))
                    raise

            jid = self.jobs.submit(prompt, execute, key=key,
                                   after=active[0].future if active else None, reserve=reserve)
            job = next(job for job in self.jobs.snapshot() if job.id == jid)
            job.future.add_done_callback(partial(self.completed, room_id, rid))
            return {'roomId': room_id, 'roundId': rid, 'jobId': jid}

    def set_incomplete(self, rid, status, error):
        self.store.execute("""UPDATE rounds SET error=?, status=CASE
            WHEN status IN ('queued','planning','rendering') THEN ? ELSE status END
            WHERE id=? AND status!='superseded'
            AND (? IS NOT NULL OR status IN ('queued','planning','rendering'))""", (error, status, rid, error))

    def completed(self, room_id, rid, future):
        with self.lock:
            if future.cancelled() or isinstance(future.exception(), GenerationCancelled):
                self.set_incomplete(rid, 'cancelled', None)
            if deletion_requested(self.store.root, room_id) and not self.active(room_id):
                self.finish_delete(room_id)

    def finish_delete(self, room_id):
        try:
            finish_room_deletion(self.studio, room_id)
            self.deletion_errors.pop(room_id, None)
        except (ValueError, OSError, DatabaseError) as exc:
            self.deletion_errors[room_id] = structured_api.redact(str(exc))
            LOG.warning('Room %s could not be deleted: %s', room_id, self.deletion_errors[room_id])

    def rooms(self):
        with self.lock:
            failures = []
            if self.deletion_errors:
                names = {row['id']: room_title(row) for row in self.catalog.history.list()}
                failures = [{'roomId': rid, 'title': names.get(rid) or '部屋', 'error': error}
                            for rid, error in self.deletion_errors.items()]
            return {'rooms': self.catalog.room_list(), 'failedDeletions': failures}

    def room(self, room_id):
        return self.catalog.room(room_id)

    def saved(self):
        tracks = []
        for row in self.store.saved_audio():
            try:
                tracks.append(self.catalog.final(row['id']).public())
            except MissingRecord:
                continue
        return {'tracks': tracks}

    def submit_generation(self, kind, room_id, rid, prompt, reserve):
        return self.submit(kind, room_id, rid, prompt,
                           partial(self.studio.generate_reserved_room, rid), reserve=reserve)

    def generate(self, data):
        rid = uid()
        reserve = partial(self.studio.reserve_generation, None, data.prompt, data.duration,
                          round_id=rid, candidate_count=data.count, compare_claude=data.compare)
        return self.submit_generation('room', rid, rid, data.prompt, reserve)

    def improve(self, audio_id, data):
        with self.lock:
            track = self.catalog.final(audio_id)
            if not data.remake:
                if not track.can_partial_improve:
                    raise ValueError('個別素材が見つかりません。作品全体を作り直してください')
                if track.duration != data.duration:
                    raise ValueError('部分改善では元の作品と同じ長さを指定してください')
            for job in self.active(track.room_id):
                kind, rid, _ = job_route(job)
                if kind != 'improve' or job.label != data.prompt:
                    continue
                context = json.loads(self.store.one('SELECT context FROM rounds WHERE id=?', (rid,))['context'])
                if (context.get('refinement_run_id') == audio_id
                        and context.get('selective_refinement') == (not data.remake)
                        and context.get('candidate_count') == data.count
                        and context['render_context']['duration_seconds'] == data.duration):
                    return {'roomId': track.room_id, 'roundId': rid, 'jobId': job.id}
            rid = uid()
            reserve = partial(self.studio.reserve_improvement, track.room_id, track.round_id, track.id,
                              data.prompt, rid, duration=data.duration, candidate_count=data.count,
                              remake=data.remake)
            return self.submit_generation('improve', track.room_id, rid, data.prompt, reserve)

    def redo(self, room_id, round_id, data):
        self.catalog.require_round(room_id, round_id)
        rid = uid()
        reserve = partial(self.studio.reserve_redo, room_id, round_id, data.prompt, rid)
        return self.submit_generation('redo', room_id, rid, data.prompt, reserve)

    def undo(self, room_id, round_id):
        with self.lock:
            self.catalog.require_round(room_id, round_id)
            if self.active(room_id):
                raise Busy('この部屋の処理が終わってから実行してください')
            self.studio.undo_redo_prompt(room_id, round_id)
            return {}

    def retry(self, room_id, round_id, data):
        with self.lock:
            row = self.catalog.require_round(room_id, round_id)
            context = json.loads(row['context'])
            if row['error'] == NO_EDITS:
                raise ValueError(NO_EDITS)
            saved = self.store.rows('SELECT position FROM candidates WHERE round_id=?', (round_id,))
            positions = sound_first.candidate_positions(context, (item['position'] for item in saved))
            ready = self.studio.ready_positions(round_id)
            if data.position is not None and (data.position not in positions or data.position in ready):
                raise Busy('この候補は再開できません')
            if row['status'] in ('queued', 'planning', 'rendering'):
                if data.position is None or not any(
                    candidate['position'] == data.position and candidate['error']
                    for candidate in self.catalog.round_output(row, None)['candidates']
                ):
                    raise Busy('この回を生成中です')
            elif row['status'] not in RETRYABLE_ROUND_STATES or (positions and positions <= ready):
                raise Busy('この回は再開できません')
            if context.get('reserved'):
                work = partial(self.studio.generate_reserved_room, round_id, position=data.position)
            else:
                work = partial(self.studio.render_saved, round_id,
                               retry=row['status'] != 'awaiting_approval', position=data.position)
            return self.submit('render', room_id, round_id, row['prompt'], work)

    def cancel(self, room_id, round_id):
        with self.lock:
            self.catalog.require_room(room_id)
            active = [job for job in self.active(room_id) if job_route(job)[1] == round_id]
            for job in active:
                self.jobs.cancel(job)
            return {}

    def delete_room(self, room_id):
        with self.lock:
            if not deletion_requested(self.store.root, room_id):
                self.catalog.require_room(room_id)
                request_room_deletion(self.store.root, room_id)
            for job in self.active(room_id):
                self.jobs.cancel(job)
            if not self.active(room_id):
                self.finish_delete(room_id)
            return {}

    def rename_room(self, room_id, data):
        with self.lock:
            self.catalog.require_room(room_id)
            self.catalog.history.rename(room_id, data.name)
            return {}

    def rename_audio(self, audio_id, data):
        track = self.catalog.final(audio_id)
        self.store.rename('audio', track.candidate_id, data.name)
        return {}

    def review_fields(self):
        return {'fields': [{'id': key, 'label': questionnaire.QUESTIONS[key][0],
                            'options': questionnaire.QUESTIONS[key][1]}
                           for key in questionnaire.PROFILES['production']]}

    def reviews(self, audio_id):
        track = self.catalog.final(audio_id)
        rows = self.store.rows("""SELECT id,text,context,created,run_id FROM reviews
            WHERE candidate_id=? AND (run_id=? OR run_id IS NULL) ORDER BY created DESC,id DESC""",
                               (track.candidate_id, audio_id))
        return {'reviews': [{**row, 'context': json.loads(row.get('context') or '{}')} for row in rows]}

    def review(self, audio_id, data):
        track = self.catalog.final(audio_id)
        context = {'review_stage': 'spatial'}
        if data.answers:
            context['questionnaire'] = {'version': questionnaire.VERSION, 'profile': 'production',
                                        'answers': data.answers}
        review_id = self.store.review(track.round_id, track.candidate_id, data.text, data.token or uid(),
                                      context, run_id=audio_id)
        return {'id': review_id}

    def save(self, audio_id):
        track = self.catalog.final(audio_id)
        self.store.save_audio(track.id)
        return {}

    def polish(self, data):
        return {'prompt': self.studio.polish_prompt(data.prompt, refinement=data.refinement)}

    def suggestions(self, audio_id):
        self.catalog.final(audio_id)
        return {'prompts': self.studio.suggest_improvements(audio_id)}

    def audio_status(self, audio_id):
        return self.catalog.track(audio_id).public()

    def audio(self, audio_id, request):
        track = self.catalog.track(audio_id)
        download = request.query_params.get('download') == '1'
        return FileResponse(track.path, media_type=track.media_type,
                            filename=track.title + track.path.suffix if download else None,
                            headers={'Cache-Control': 'no-cache'})

    def peaks(self, audio_id):
        track = self.catalog.track(audio_id)
        try:
            stat = track.path.stat()
            return waveform(track.path, stat.st_mtime_ns, stat.st_size)
        except (OSError, SubprocessError, ValueError, EOFError):
            # The same audio URL remains playable if waveform extraction is unavailable.
            return {'peaks': [], 'duration': track.duration}

    def spatial(self, audio_id):
        return self.catalog.spatial(self.catalog.track(audio_id))

    def prepare_spatial_preview(self, candidate_id, data):
        with self.lock:
            candidate = self.catalog.candidate(candidate_id)
            layer = self.catalog.spatial_layer(candidate_id, data.layerId)
            room_id, rid = candidate['room_id'], candidate['round_id']

            def render():
                with self.studio.operation(room_id):
                    self.catalog.candidate(candidate_id)
                    try:
                        production_layers.make_spatial_preview(
                            self.store.root/'candidates'/candidate_id, layer, self.store.root)
                    except GenerationCancelled:
                        raise
                    except Exception as exc:
                        error = structured_api.redact(str(exc))
                        write_json(layer/f'preview_error_{uid()}.json', {'error': error})
                    return rid

            return self.submit('preview', room_id, rid, '空間設計の試聴音', render)

    def codes(self, audio_id):
        track = self.catalog.final(audio_id)
        root = self.store.root
        run = root/'runs'/audio_id
        base = root/'candidates'/track.candidate_id
        layers = read_record(run/'result.json').get('layers', {})
        paths = [('完成音源', run/'source.py', run)]
        if sound := layers.get('sound'):
            paths = [('原音', base/'layers'/'sound'/Path(sound).name/'execution'/'source.py', base),
                     ('空間ミックス（共通）', ROOT/'soundlab'/'spatial'/'work.py', ROOT/'soundlab'/'spatial'),
                     ('方向処理（共通）', ROOT/'soundlab'/'spatial'/'render.py', ROOT/'soundlab'/'spatial'),
                     *paths]
        return {'codes': [{'label': label, 'source': saved.read_text()}
                          for label, path, directory in paths if (saved := artifact(path, directory))]}


def endpoint(action, model=None, *, status=200, with_request=False):
    async def route(request):
        kwargs = dict(request.path_params)
        if model:
            if request.headers.get('content-type', '').split(';', 1)[0].lower() != 'application/json':
                raise HTTPException(415, 'JSONを送信してください')
            try:
                data = model.model_validate(await request.json())
                if model is not Input:
                    kwargs['data'] = data
            except (ValidationError, ValueError):
                raise HTTPException(422, '指示や設定を確認してください') from None
        if with_request:
            kwargs['request'] = request
        value = await run_in_threadpool(action, request.app.state.production, **kwargs)
        if isinstance(value, Response):
            return value
        return JSONResponse(value, status_code=status, headers={'Cache-Control': 'no-store'})
    return route


async def problem(_request, exc):
    if isinstance(exc, HTTPException):
        status, message = exc.status_code, exc.detail
    else:
        status = 404 if isinstance(exc, MissingRecord) else 409 if isinstance(exc, Busy) else 400
        message = str(exc)
    return JSONResponse({'error': structured_api.redact(str(message))}, status_code=status,
                        headers={'Cache-Control': 'no-store'})


def create_app(data_dir=None, *, frontend=None, jobs=None):
    dist = Path(frontend) if frontend is not None else ROOT/'frontend'/'dist'
    root = Path(data_dir) if data_dir is not None else Path(os.environ.get('SOUNDLAB_DATA_DIR', ROOT/'data'))

    @asynccontextmanager
    async def lifespan(app):
        production = Production(root, jobs if jobs is not None else Jobs())
        with production.studio.server_session():
            await run_in_threadpool(production.studio.recover)
            for marker in (production.store.root/'.room_deletions').glob('*'):
                await run_in_threadpool(production.finish_delete, marker.name)
            app.state.production = production
            try:
                yield
            finally:
                for job in production.jobs.snapshot():
                    production.jobs.cancel(job)
                await run_in_threadpool(production.jobs.executor.shutdown)

    def home(_request):
        if not (dist/'index.html').is_file():
            return JSONResponse({'error': './start で画面をビルドしてください'}, status_code=503)
        return FileResponse(dist/'index.html', headers={'Cache-Control': 'no-cache'})

    return Starlette(lifespan=lifespan, exception_handlers={HTTPException: problem, ValueError: problem}, routes=[
        Route('/api/health', lambda request: JSONResponse({'status': 'ok', 'app': 'motionSound'})),
        Route('/api/rooms', endpoint(Production.rooms)),
        Route('/api/rooms', endpoint(Production.generate, Generate, status=202), methods=['POST']),
        Route('/api/rooms/{room_id}', endpoint(Production.room)),
        Route('/api/rooms/{room_id}', endpoint(Production.rename_room, Rename), methods=['PATCH']),
        Route('/api/rooms/{room_id}', endpoint(Production.delete_room, status=202), methods=['DELETE']),
        Route('/api/rooms/{room_id}/rounds/{round_id}/redo', endpoint(Production.redo, Instruction, status=202), methods=['POST']),
        Route('/api/rooms/{room_id}/rounds/{round_id}/undo', endpoint(Production.undo, Input), methods=['POST']),
        Route('/api/rooms/{room_id}/rounds/{round_id}/retry', endpoint(Production.retry, Retry, status=202), methods=['POST']),
        Route('/api/rooms/{room_id}/rounds/{round_id}/cancel', endpoint(Production.cancel, Input, status=202), methods=['POST']),
        Route('/api/saved', endpoint(Production.saved)),
        Route('/api/review-fields', endpoint(Production.review_fields)),
        Route('/api/prompt/polish', endpoint(Production.polish, Polish), methods=['POST']),
        Route('/api/candidates/{candidate_id}/spatial-preview', endpoint(Production.prepare_spatial_preview, Preview, status=202), methods=['POST']),
        Route('/api/audio/{audio_id}', endpoint(Production.audio, with_request=True)),
        Route('/api/audio/{audio_id}', endpoint(Production.rename_audio, Rename), methods=['PATCH']),
        Route('/api/audio/{audio_id}/status', endpoint(Production.audio_status)),
        Route('/api/audio/{audio_id}/waveform', endpoint(Production.peaks)),
        Route('/api/audio/{audio_id}/spatial', endpoint(Production.spatial)),
        Route('/api/audio/{audio_id}/codes', endpoint(Production.codes)),
        Route('/api/audio/{audio_id}/reviews', endpoint(Production.reviews)),
        Route('/api/audio/{audio_id}/reviews', endpoint(Production.review, Review), methods=['POST']),
        Route('/api/audio/{audio_id}/save', endpoint(Production.save, Input), methods=['POST']),
        Route('/api/audio/{audio_id}/improve', endpoint(Production.improve, Improve, status=202), methods=['POST']),
        Route('/api/audio/{audio_id}/suggestions', endpoint(Production.suggestions, Input), methods=['POST']),
        Mount('/assets', app=StaticFiles(directory=dist/'assets', check_dir=False)),
        Route('/', home),
    ])
