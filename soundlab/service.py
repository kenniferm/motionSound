import fcntl
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from threading import Lock
from contextlib import contextmanager, nullcontext
from . import ai, production_layers, runner, sound_first, structured_api
from .cancellation import GenerationCancelled, check
from .memory import context_for
from .store import ID_PATTERN, now, read_json, uid, write_json
from .room_delete import deletion_requested
from .rooms import RoomHistory


class Studio:
    def __init__(self, store):
        self.store = store

    @contextmanager
    def _lock(self, name, mode, *, blocking=False, busy_message='この処理はすでに実行中です'):
        with (self.store.root / name).open('a') as lock:
            try:
                fcntl.flock(lock, mode if blocking else mode | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(busy_message) from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def server_session(self):
        """Allow only one API process to own the background queue for this data directory."""
        return self._lock('.server.lock', fcntl.LOCK_EX,
                          busy_message='このデータフォルダを使うmotionSoundがすでに起動しています。'
                                       '再起動する場合は、先に起動元のターミナルでControl+Cを押してください。')

    @contextmanager
    def operation(self, round_id=None, *, exclusive=False, allow_missing_round=False, shared_round=False):
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        with self._lock('.operation.lock', mode, blocking=not exclusive):
            if round_id is None:
                yield
            else:
                if not re.fullmatch(ID_PATTERN, round_id):
                    raise ValueError('生成IDが不正です')
                lock_dir = self.store.root / '.round_locks'
                lock_dir.mkdir(exist_ok=True)
                round_mode = fcntl.LOCK_SH if shared_round else fcntl.LOCK_EX
                with self._lock(f'.round_locks/{round_id}', round_mode):
                    if not allow_missing_round:
                        self.store.one('SELECT id FROM rounds WHERE id=?', (round_id,))
                    yield

    def reserve_generation(self, pid, prompt, duration, parent_id=None, *, round_id,
                           candidate_count, compare_claude=False, provider='openai',
                           seed=42, refinement=None):
        """Record an accepted request before the worker starts."""
        if not re.fullmatch(ID_PATTERN, round_id):
            raise ValueError('生成IDが不正です')
        created = now()
        context = {'render_context': {'duration_seconds': duration},
                   'candidate_count': candidate_count * (2 if compare_claude else 1),
                   'compare_claude': compare_claude, 'prompt_version': sound_first.VERSION,
                   'provider': provider, 'seed': seed, 'reserved': True, **(refinement or {})}
        with self.operation(), self.store.connect() as db:
            if pid is None:
                pid = uid()
                db.execute('INSERT INTO projects VALUES(?,?,?)', (pid, '生成中', created))
            db.execute('INSERT INTO rounds VALUES(?,?,?,?,?,?,?,?,?)',
                       (round_id, pid, parent_id, prompt, 'openai', 'queued', json.dumps(context), None, created))
            if replaced := context.get('replaces_round_id'):
                self.archive_round(db, replaced)

    def generate_reserved_room(self, rid, *, position=None, client=None):
        row = self.store.one('SELECT * FROM rounds WHERE id=?', (rid,))
        owner = json.loads(row['context']).get('inline_room_id', rid)
        with self.operation(owner):
            if deletion_requested(self.store.root, owner):
                raise GenerationCancelled()
            row = self.store.one('SELECT * FROM rounds WHERE id=?', (rid,))
            reserved = json.loads(row['context'])
            if not reserved.get('reserved'):
                raise ValueError('この生成回は準備済みです')
            return self._generate_reserved(row, reserved, position, client)

    @staticmethod
    def archive_round(db, rid):
        db.execute("""UPDATE rounds SET
            context=json_set(context, '$.status_before_superseded', status), status='superseded'
            WHERE id=?""", (rid,))

    def reserve_redo(self, room_id, previous_id, prompt, new_id):
        if not prompt.strip():
            raise ValueError('音の指示を入力してください')
        with self.operation(room_id):
            if deletion_requested(self.store.root, room_id):
                raise ValueError('この部屋は削除されました')
            previous = next((row for row in RoomHistory(self.store).steps(room_id)
                             if row['id'] == previous_id), None)
            if (previous is None or previous['mode'] != 'openai'
                    or previous['status'] in ('queued', 'planning', 'rendering')):
                raise ValueError('やり直す回を開き直してください')
            context = json.loads(previous['context'])
            settings = context['render_context']
            saved_input = self.store.root / 'rounds' / previous_id / 'input.json'
            seed = context.get('seed', 42)
            if saved_input.is_file():
                seed = read_json(saved_input).get('seed', seed)
            refinement = {'inline_room_id': room_id, 'prompt_slot_id': previous['slot_id'],
                          'replaces_round_id': previous_id}
            if 'refinement_run_id' in context:
                refinement['refinement_run_id'] = context['refinement_run_id']
                refinement['selective_refinement'] = context.get('selective_refinement', False)
            compare = bool(context.get('compare_claude'))
            count = sound_first.generation_groups(context)[0][2]
            self.reserve_generation(previous['project_id'], prompt, settings['duration_seconds'],
                                    previous['parent_id'], round_id=new_id, candidate_count=count,
                                    seed=seed, refinement=refinement, compare_claude=compare,
                                    provider=context.get('provider', 'openai'))

    def undo_redo_prompt(self, room_id, current_id):
        with self.operation(room_id):
            if deletion_requested(self.store.root, room_id):
                raise ValueError('この部屋は削除されました')
            current = next((row for row in RoomHistory(self.store).steps(room_id)
                            if row['id'] == current_id), None)
            if not current or current['status'] in ('queued', 'planning', 'rendering'):
                raise ValueError('戻す回を開き直してください')
            previous_id = json.loads(current['context']).get('replaces_round_id')
            with self.store.connect() as db:
                previous = db.execute("""SELECT id FROM rounds
                    WHERE id=? AND status='superseded'
                    AND json_extract(context, '$.status_before_superseded') IS NOT NULL
                    AND COALESCE(json_extract(context, '$.prompt_slot_id'), id)=?
                    AND COALESCE(json_extract(context, '$.inline_room_id'), id)=?""",
                                      (previous_id, current['slot_id'], room_id)).fetchone()
                if not previous:
                    raise ValueError('ひとつ前の版が見つかりません')
                self.archive_round(db, current_id)
                db.execute("""UPDATE rounds SET
                    status=json_extract(context, '$.status_before_superseded'),
                    context=json_remove(context, '$.status_before_superseded')
                    WHERE id=?""", (previous_id,))

    def polish_prompt(self, draft, *, refinement=False):
        if not draft.strip():
            raise ValueError('先に指示を入力してください。')
        model, client = structured_api.resolve_client(None)
        folder = self.store.root/'prompt_edits'/uid()
        folder.mkdir(parents=True)
        reply, _ = structured_api.request(
            client, model, sound_first.VERSION, folder, 'polish_prompt', ai.PROMPT_POLISH,
            {'draft': draft, 'purpose': 'refine_existing_work' if refinement else 'create_work'},
            ai.PolishedPrompt, 2000,
        )
        if not reply.prompt.strip():
            raise ValueError('AIから改善した指示文を取得できませんでした。')
        return reply.prompt.strip()

    def suggest_improvements(self, run_id):
        run = self.store.one("""
            SELECT u.result, c.id AS candidate_id, r.prompt FROM runs u
            JOIN candidates c ON c.id=u.candidate_id JOIN rounds r ON r.id=c.round_id
            WHERE u.id=? AND u.status='succeeded' AND r.status!='superseded'
        """, (run_id,))
        folder = self.store.root/'runs'/run_id
        arrangement = json.loads(run['result'] or '{}').get('layers', {}).get('arrangement')
        if arrangement and not (Path(arrangement)/'execution'/'source.py').is_file():
            plan_path = self.store.candidate_dir(run['candidate_id'])/'plan.json'
            evidence = {'completed_edit': json.loads(plan_path.read_text())['candidate_plan']['edits']}
        elif arrangement:
            layer_root = self.store.candidate_dir(run['candidate_id'])/'layers'/'arrangement'
            code = runner.safe_artifact(Path(arrangement)/'execution'/'source.py', layer_root)
            evidence = {'final_code': code.read_text()}
        else:
            code = runner.safe_artifact(folder/'source.py', folder)
            evidence = {'final_code': code.read_text()}
        model, client = structured_api.resolve_client(None)
        attempt = folder/'improvement_suggestions'/uid()
        attempt.mkdir(parents=True)
        reply, _ = structured_api.request(
            client, model, sound_first.VERSION, attempt, 'improvement_suggestions',
            ai.IMPROVEMENT_PROMPT, {'prompt': run['prompt'], **evidence},
            ai.ImprovementSuggestions, 4000,
        )
        prompts = [prompt.strip() for prompt in reply.prompts]
        if len(prompts) != 3 or '' in prompts or len(set(prompts)) != 3:
            raise ValueError('異なる改善案を3件取得できませんでした。もう一度お試しください。')
        return prompts

    def reserve_improvement(self, room_id, source_round_id, run_id, prompt, round_id, *, duration,
                            candidate_count, remake=False):
        with self.operation():
            if deletion_requested(self.store.root, room_id):
                raise ValueError('この部屋は削除されました')
            source = self.store.one("""SELECT r.project_id, r.context, c.id AS candidate_id, c.position
                FROM runs u JOIN candidates c ON u.candidate_id=c.id JOIN rounds r ON r.id=c.round_id
                WHERE u.id=? AND u.status='succeeded' AND r.id=? AND r.status!='superseded'""",
                (run_id, source_round_id))
            context = json.loads(source['context'])
            if context.get('inline_room_id', source_round_id) != room_id:
                raise ValueError('この部屋の作品を選んでください')
            self.reserve_generation(source['project_id'], prompt, duration, source['candidate_id'],
                                    round_id=round_id, candidate_count=candidate_count,
                                    provider=sound_first.provider_for_position(context, source['position']),
                                    refinement={'inline_room_id': room_id, 'refinement_run_id': run_id,
                                                'selective_refinement': not remake})

    def render_saved(self, rid, *, retry=False, position=None):
        context = json.loads(self.store.one('SELECT context FROM rounds WHERE id=?', (rid,))['context'])
        owner = context.get('inline_room_id', rid)
        with self.operation(owner, shared_round=True) if owner != rid else nullcontext():
            if deletion_requested(self.store.root, owner):
                raise ValueError('この部屋は削除されました')
            if retry and (context.get('prompt_version') or '').startswith('sound-first-v0.3'):
                self.resume(rid, context, position)
            else:
                self.render_saved_candidates(rid, context, retry=retry, position=position)
        return rid

    def ready_positions(self, rid):
        latest = {}
        for run in self.store.rows("""
            SELECT c.position,u.id,u.result FROM candidates c JOIN runs u ON u.candidate_id=c.id
            WHERE c.round_id=? AND u.status='succeeded' ORDER BY u.created DESC,u.id DESC
        """, (rid,)):
            latest.setdefault(run['position'], run)
        return {position for position, run in latest.items()
                if runner.published_audio_path(self.store.root, run) is not None}

    def write_generation_input(self, rid, prompt, mode, context):
        """Publish the snapshot recorded in SQLite, including after an interrupted write."""
        folder = self.store.root/'rounds'/rid
        path = folder/'input.json'
        default_contract = sound_first.DIRECT if context.get('sound_generation_mode') == 'direct' else sound_first.DIRECTOR
        seed = context.get('seed', 42)
        if 'seed' not in context and (previous := context.get('replaces_round_id')):
            previous_input = self.store.root/'rounds'/previous/'input.json'
            if previous_input.is_file():
                seed = read_json(previous_input).get('seed', seed)
        payload = {**context, 'prompt': prompt, 'mode': mode, 'contract': context.get('contract', default_contract),
                   'duration': context['render_context']['duration_seconds'], 'seed': seed}
        folder.mkdir(parents=True, exist_ok=True)
        write_json(path, payload)
        return payload

    def resume(self, rid, context, position):
        with self.operation(rid):
            row = self.store.one('SELECT * FROM rounds WHERE id=?', (rid,))
            if row['status'] not in ('failed', 'partial', 'interrupted', 'cancelled', 'completed'):
                raise ValueError('この回は再開できません')
            owner = context.get('inline_room_id', rid)
            folder = self.store.root/'rounds'/rid
            candidates = {c['position']: c['id'] for c in self.store.rows(
                'SELECT id,position FROM candidates WHERE round_id=?', (rid,))}
            positions = sound_first.candidate_positions(context, candidates)
            if position is not None and position not in positions:
                raise ValueError('候補番号が不正です')
            ready = self.ready_positions(rid)
            pending = (positions if position is None else {position}) - ready
            if not pending:
                return
            check()
            self.store.execute("UPDATE rounds SET status='rendering',error=NULL WHERE id=?", (rid,))
            status, error = 'failed', None
            ready_lock = Lock()

            def render_candidate(index, cid):
                check()
                if deletion_requested(self.store.root, owner):
                    raise GenerationCancelled()
                if self._render_candidate(cid, self._candidate_approval(cid), 120, retry=True):
                    with ready_lock:
                        ready.add(index)

            def save_candidate(index, proposal, direction):
                check()
                cid = self._save_candidate(rid, index, proposal, direction, saved)
                render_candidate(index, cid)

            try:
                saved_input = folder/'input.json'
                saved = (read_json(saved_input) if saved_input.is_file()
                         else self.write_generation_input(rid, row['prompt'], row['mode'], context))
                sound_first.run_candidates(lambda index: render_candidate(index, candidates[index]),
                                           sorted(pending & candidates.keys()))
                if missing := pending - candidates.keys():
                    batch, _ = sound_first.generate(row['prompt'], saved, folder, save_candidate=save_candidate,
                                                    positions=missing, retry=True)
                    if not row['parent_id'] and batch.project_title and not (folder/'proposal.json').exists():
                        self.store.rename('project', row['project_id'], batch.project_title[:120])
                check()
                status = self._render_status(len(ready), len(positions))
            except GenerationCancelled:
                status = 'cancelled'
            except Exception as exc:
                status = self._render_status(len(ready), len(positions))
                error = structured_api.redact(str(exc))
            finally:
                self.store.execute('UPDATE rounds SET status=?,error=? WHERE id=?', (status, error, rid))

    def _generate_reserved(self, row, reserved, position, client):
        from .spatial.dataset import load_dataset

        check()
        rid, pid, parent_id = row['id'], row['project_id'], row['parent_id']
        prompt, mode, created_at = row['prompt'], row['mode'], row['created']
        duration = reserved['render_context']['duration_seconds']
        provider = reserved.get('provider', 'openai')
        compare_claude = bool(reserved.get('compare_claude'))
        candidate_count = reserved['candidate_count']
        count_per_provider = candidate_count // (2 if compare_claude else 1)
        seed = reserved.get('seed', 42)
        refinement = {key: reserved[key] for key in ('inline_room_id', 'prompt_slot_id',
                      'replaces_round_id', 'refinement_run_id', 'selective_refinement') if key in reserved}

        if not prompt.strip() or type(duration) is not int or not 5 <= duration <= 240:
            raise ValueError('指示と5〜240秒の長さを指定してください')
        if not 1 <= count_per_provider <= sound_first.MAX_COUNT:
            raise ValueError('生成数は1〜5案です')
        if provider not in ('openai', 'claude') or (compare_claude and provider != 'openai'):
            raise ValueError('生成元の指定が不正です')
        claude_model = structured_api.claude_model() if compare_claude or provider == 'claude' else None
        runner.mp3_encoder()
        load_dataset(self.store.root)
        generation_mode = os.getenv('SOUNDLAB_SOUND_GENERATION_MODE', 'direct').strip().lower()
        if generation_mode not in ('direct', 'director'):
            raise ValueError('SOUNDLAB_SOUND_GENERATION_MODE は direct または director です')
        version = sound_first.VERSION
        contract = sound_first.DIRECT if generation_mode == 'direct' else sound_first.DIRECTOR
        context = context_for(self.store, pid, parent_id, prompt, 'headphones')
        render_context = {'playback_mode': 'headphones', 'render_target': 'headphones_binaural',
                          'binaural': True, 'duration_seconds': duration, 'sample_rate': 48000, 'channels': 2}
        context.update(render_context=render_context, candidate_count=candidate_count, prompt_version=version,
                       sound_generation_mode=generation_mode, seed=seed, contract=contract)
        if claude_model:
            context.update(provider=provider, compare_claude=compare_claude, claude_model=claude_model)
        if refinement:
            context.update(refinement)
        if parent_id:
            parent_folder = self.store.candidate_dir(parent_id)
            plan_path = parent_folder/'plan.json'
            saved = json.loads(plan_path.read_text()) if plan_path.is_file() else {}
            parent_design = saved.get('candidate_plan') or {}
            context['parent_design'] = parent_design
            parent_runs = self.store.rows(
                "SELECT id,result FROM runs WHERE candidate_id=? AND status='succeeded' ORDER BY created DESC,id DESC",
                (parent_id,))
            selected_run = context.get('refinement_run_id')
            parent_run = next((row for row in parent_runs if not selected_run or row['id'] == selected_run), None)
            if parent_run and not context.get('selective_refinement'):
                layers = json.loads(parent_run['result'] or '{}').get('layers', {})
                if sound := layers.get('sound'):
                    executed_source = Path(sound)/'execution'/'source.py'
                    if executed_source.is_file():
                        runner.safe_artifact(executed_source, parent_folder/'layers'/'sound')
                        context['parent_code'] = executed_source.read_text()
                arrangement = layers.get('arrangement')
                if arrangement:
                    saved_program = Path(arrangement) / 'program.json'
                    runner.safe_artifact(saved_program, parent_folder / 'layers' / 'arrangement')
                    program = json.loads(saved_program.read_text())
                    context['parent_spatial_scene'] = program.get('scene') or program['request']['events']
                else:
                    placements = [{key: event[key] for key in ('id', 'trajectory', 'reverb')}
                                  for event in parent_design.get('events', []) if 'trajectory' in event]
                    if placements:
                        context['parent_spatial_scene'] = {'placements': placements}
        self.store.execute("UPDATE rounds SET status='planning',context=?,error=NULL WHERE id=?",
                           (json.dumps(context, ensure_ascii=False), rid))
        folder = self.store.root / 'rounds' / rid
        saved = self.write_generation_input(rid, prompt, mode, context)
        started_at, start_time = now(), time.perf_counter()
        attempts = success = 0
        metrics_lock = Lock()
        first_render_start = None
        first_audio_wall = first_audio_processing = None
        status = 'failed'

        def save_candidate(position, proposal, direction):
            nonlocal attempts, success, first_render_start, first_audio_wall, first_audio_processing
            cid = self._save_candidate(rid, position, proposal, direction, saved)
            if deletion_requested(self.store.root, context.get('inline_room_id', rid)):
                return
            with metrics_lock:
                attempts += 1
                if first_render_start is None:
                    first_render_start = (time.perf_counter(), now())
            if self._render_candidate(cid, self._candidate_approval(cid), 120, client=client):
                with metrics_lock:
                    success += 1
                    if first_audio_wall is None:
                        first_audio_wall = (datetime.fromisoformat(now()) - datetime.fromisoformat(created_at)).total_seconds()
                        first_audio_processing = time.perf_counter() - start_time

        try:
            check()
            batch, metadata = sound_first.generate(prompt, context, folder, client, save_candidate=save_candidate,
                                                    positions=None if position is None else {position})
            check()
            write_json(folder / 'proposal.json', {'batch': batch.model_dump(), 'metadata': metadata})
            if parent_id is None and not refinement and batch.project_title:
                self.store.rename('project', pid, batch.project_title[:120])
            status = self._render_status(success, candidate_count)
            self.store.execute('UPDATE rounds SET status=? WHERE id=?', (status, rid))
        except GenerationCancelled:
            status = 'cancelled'
            self.store.execute('UPDATE rounds SET status=?,error=NULL WHERE id=?', (status, rid))
        except Exception as exc:
            error = structured_api.redact(str(exc))
            status = 'partial' if success else 'failed'
            self.store.execute('UPDATE rounds SET status=?,error=? WHERE id=?', (status, error, rid))
        finally:
            elapsed = time.perf_counter() - start_time
            planning_elapsed = elapsed if first_render_start is None else first_render_start[0] - start_time
            write_json(folder / 'metrics.json', {'planning_started_at': started_at,
                'planning_finished_at': first_render_start[1] if first_render_start else now(),
                'planning_elapsed_seconds': planning_elapsed})
            if attempts:
                self._write_render_metrics(rid, started_at, elapsed - planning_elapsed, elapsed, first_audio_wall,
                                           first_audio_processing, attempts, attempts-success, status, bool(success))
        return rid

    def _save_candidate(self, rid, position, proposal, direction, brief):
        cid = uid()
        path = self.store.root / 'candidates' / cid
        render_context = brief['render_context']
        request = {
            'duration_seconds': render_context['duration_seconds'], 'sample_rate': 48000, 'channels': 2,
            'seed': brief['seed'] + sound_first.position_within_provider(brief, position),
            'render_context': render_context, 'events': direction['sound_events'],
            'pipeline_version': sound_first.VERSION, 'parameters': {p.name: p.value for p in proposal.parameters},
        }
        with self.store.connect() as db:
            db.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?)',
                       (cid, rid, position, proposal.title, proposal.approach, json.dumps(proposal.used_review_ids)))
            path.mkdir(parents=True)
            (path / 'source.py').write_text(proposal.code)
            write_json(path / 'plan.json', {**proposal.model_dump(), 'candidate_plan': direction})
            write_json(path / 'request.json', request)
        return cid

    def _candidate_approval(self, cid):
        folder = self.store.candidate_dir(cid)
        return runner.fingerprint((folder/'source.py').read_bytes(),
                                  read_json(folder/'request.json'))

    def _render_candidate(self, cid, approval, timeout, retry=False, *, client=None):
        run_id = uid()
        self.store.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)', (run_id, cid, 'running', approval, None, now()))
        path = self.store.candidate_dir(cid)
        try:
            reuse = None
            if retry:
                for prior in self.store.rows("SELECT id,result FROM runs WHERE candidate_id=? AND status IN ('failed','cancelled') AND approval=? ORDER BY created DESC", (cid, approval)):
                    hashes = json.loads(prior['result'] or '{}').get('event_hashes')
                    if hashes:
                        reuse = (self.store.root/'runs'/prior['id'], hashes)
                        break
            request = json.loads((path/'request.json').read_text())
            run_dir = self.store.root/'runs'/run_id
            if request.get('pipeline_version'):
                candidate = self.store.one('SELECT round_id,position FROM candidates WHERE id=?', (cid,))
                round_folder = self.store.root/'rounds'/candidate['round_id']
                pipeline = production_layers.Pipeline(path, round_folder, candidate['position']+1, client)
                result = pipeline.render(run_dir, approval, timeout, reuse=reuse)
            else:
                result = runner.execute(path/'source.py', request, run_dir, approval, timeout, reuse=reuse)
        except GenerationCancelled as exc:
            result = {'status': 'cancelled', 'error': str(exc)}
        except Exception as exc:
            result = {'status': 'failed', 'error': str(exc)}
        self.store.execute('UPDATE runs SET status=?,result=? WHERE id=?',
                           (result['status'], json.dumps(result, ensure_ascii=False), run_id))
        check()
        return result['status'] == 'succeeded'

    @staticmethod
    def _render_status(success, expected_count):
        return 'completed' if success == expected_count else 'partial' if success else 'failed'

    def _write_render_metrics(self, rid, started_at, elapsed, processing_elapsed, first_audio_wall,
                              first_audio_processing, attempts, failures, status, has_audio):
        metrics_dir = self.store.root/'rounds'/rid/'batches'
        metrics_dir.mkdir(exist_ok=True)
        created = self.store.one('SELECT created FROM rounds WHERE id=?', (rid,))['created']
        ready_wall = (datetime.fromisoformat(now())-datetime.fromisoformat(created)).total_seconds()
        write_json(metrics_dir/(uid()+'.json'), {'started_at': started_at, 'finished_at': now(),
            'batch_elapsed_seconds': elapsed,
            'first_audio_ready_after_request_seconds_including_approval': first_audio_wall,
            'processing_to_first_audio_ready_seconds_excluding_approval': first_audio_processing,
            'players_ready_after_request_seconds_including_approval': ready_wall if has_audio else None,
            'processing_to_players_ready_seconds_excluding_approval': processing_elapsed if has_audio else None,
            'attempts': attempts, 'failures': failures, 'status': status})

    def render_saved_candidates(self, rid, context, *, retry, position):
        """Run legacy saved code and pending approvals through the same renderer."""
        with self.operation(rid):
            owner = context.get('inline_room_id', rid)
            candidates = self.store.rows('SELECT id,position FROM candidates WHERE round_id=? ORDER BY position', (rid,))
            if not candidates:
                raise ValueError('実行できる保存済みのコードがありません')
            ready = self.ready_positions(rid) if retry else set()
            expected_count = len(sound_first.candidate_positions(context, (c['position'] for c in candidates)))
            expected = ('completed', 'partial', 'failed', 'interrupted', 'cancelled') if retry else ('awaiting_approval',)
            row = self.store.one('SELECT status,created FROM rounds WHERE id=?', (rid,))
            check()
            if row['status'] not in expected:
                raise ValueError('このラウンドは実行待ちではありません')
            approvals = {candidate['id']: self._candidate_approval(candidate['id']) for candidate in candidates
                         if (position is None or candidate['position'] == position) and candidate['position'] not in ready}
            planning_path = self.store.root/'rounds'/rid/'metrics.json'
            planning_time = read_json(planning_path).get('planning_elapsed_seconds', 0) if planning_path.is_file() else 0
            with self.store.connect() as db:
                db.execute("UPDATE rounds SET status='rendering' WHERE id=?", (rid,))
            success, attempts, failures = len(ready), 0, 0
            batch_started, batch_time = now(), time.perf_counter()
            first_audio_wall = first_audio_processing = None
            cancelled = False
            try:
                for cid, approval in approvals.items():
                    check()
                    if deletion_requested(self.store.root, rid) or deletion_requested(self.store.root, owner):
                        break
                    attempts += 1
                    completed = self._render_candidate(cid, approval, 120, retry=retry)
                    success += completed
                    failures += not completed
                    if completed and first_audio_wall is None:
                        first_audio_wall = (datetime.fromisoformat(now())-datetime.fromisoformat(row['created'])).total_seconds()
                        first_audio_processing = planning_time + time.perf_counter() - batch_time
            except GenerationCancelled:
                cancelled = True
            finally:
                status = 'cancelled' if cancelled else self._render_status(success, expected_count)
                self.store.execute('UPDATE rounds SET status=? WHERE id=?', (status, rid))
                batch_elapsed = time.perf_counter()-batch_time
                self._write_render_metrics(rid, batch_started, batch_elapsed,
                    planning_time + batch_elapsed, first_audio_wall,
                    first_audio_processing, attempts, failures, status, bool(success))


    def recover(self):
        # Exclusive recovery cannot mark any active worker as interrupted.
        with self.operation(exclusive=True):
            self.store.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
            self.store.execute("UPDATE rounds SET status='interrupted' WHERE status IN ('queued','planning','rendering')")
