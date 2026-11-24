import json
from pathlib import Path
from threading import Barrier, Event
import time

import numpy as np
import pytest
import subprocess
from starlette.testclient import TestClient

from conftest import generate_work, mock_client
from soundlab import production_layers, runner, sound_first, structured_api
from soundlab.api import create_app
from soundlab.read_model import read_round_output
from soundlab.rooms import RoomHistory
from soundlab.service import Studio
from soundlab.spatial import work
from soundlab.spatial_view import trajectory_data
from soundlab.store import Store, uid


def test_measured_work_keeps_provenance_and_exact_207_seconds(tmp_path, engine):
    studio = Studio(Store(tmp_path))
    rid = generate_work(studio, None, '二つの音が交差する作品', 207, candidate_count=1)
    output = read_round_output(studio.store, rid)
    candidate, _, run = output.views[0]
    assert run, studio.store.rows('SELECT result FROM runs')
    result = json.loads(run['result'])
    track = result['audio']
    probe = subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams',
                                     '-of', 'json', track['path']])
    stream = json.loads(probe)['streams'][0]
    assert (stream['codec_name'], stream['sample_rate'], stream['channels'], stream['bit_rate']) == ('mp3', '48000', 2, '320000')
    decoded = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', track['path'], '-f', 's16le', 'pipe:1'])
    stereo = np.frombuffer(decoded, dtype='<i2').reshape(-1, 2)
    assert stereo.shape == (207 * 48000, 2)
    assert stereo.any() and np.any(stereo[:, 0] != stereo[:, 1])
    assert list(result['event_hashes']) == ['first', 'second']
    assert result['dataset']['id'].startswith('FABIAN')
    path = Path(result['manifest'])
    manifest = json.loads(path.read_text())
    assert {'work.mp3', 'source.py', 'request.json', 'events/first.npy'} <= {
        entry['path'] for entry in manifest['artifacts']}
    assert list(path.parent.glob('*.mp3')) == [path.parent/'work.mp3']
    assert not list(path.parent.glob('*.wav'))
    assert json.loads((tmp_path/'rounds'/rid/'director_request.json').read_text())['version'] == sound_first.VERSION
    assert Path(studio.store.candidate_dir(candidate['id'])/'source.py').is_file()


def test_dry_candidates_may_share_form_timbre_and_synthesis(tmp_path, monkeypatch):
    client = mock_client([])
    parse = client.responses.parse

    def shared_design(**kwargs):
        response = parse(**kwargs)
        plan = response.output_parsed
        if isinstance(plan, sound_first.Plan):
            first = plan.candidate_directions[0]
            second = first.model_copy(deep=True)
            plan.candidate_directions = [first, second]
        return response

    client.responses.parse = shared_design
    monkeypatch.setenv('OPENAI_MODEL', 'mock')
    folder = tmp_path / 'rounds' / 'test-round'
    folder.mkdir(parents=True)
    context = {'render_context': {'duration_seconds': 5}, 'candidate_count': 2, 'reviews': []}
    saved = []

    def generate():
        return sound_first.generate('同じ音で動きだけ変える', context, folder, client,
                                    save_candidate=lambda index, *_: saved.append(index))

    generate()
    assert sorted(saved) == [0, 1]
    for stage, expected in [('director', sound_first.DIRECTOR), ('code_1', sound_first.CODE)]:
        request = json.loads((folder / f'{stage}_request.json').read_text())
        assert request['input'][0] == {'role': 'system', 'content': expected}
        assert request['version'] == sound_first.VERSION


def test_two_overlapping_events_keep_separate_directions(tmp_path, monkeypatch):
    clips = tmp_path / 'events'
    clips.mkdir()
    np.save(clips/'left.npy', np.full(8, .2, dtype=np.float32))
    np.save(clips/'right.npy', np.full(8, .4, dtype=np.float32))
    monkeypatch.setattr(work, 'load_dataset', lambda _: (
        np.array([[0, 1, 0], [0, -1, 0]]), np.array([[[1.], [0.]], [[0.], [1.]]]), {}))
    events = [dict(id=name, start_frame=10, frames=8, reverb=0, keys=[
        {'frame': frame, 'vector': [0, side, 0], 'distance': 1.7} for frame in (0, 7)])
        for name, side in [('left', 1), ('right', -1)]]
    monkeypatch.setattr(work, 'convert', lambda path, *_: np.load(path))
    pcm, _ = work.mix_work(tmp_path, clips, events, 48000*5, tmp_path)
    np.testing.assert_allclose(pcm[10:18], np.tile([.2, .4], (8, 1)))
    assert not pcm[:10].any() and not pcm[18:].any()


def test_missing_clip_does_not_block_other_candidates(tmp_path, engine):
    engine(lambda i, code: code + "\n(clips/'second.npy').unlink()" if i == 2 else code)
    studio = Studio(Store(tmp_path))
    rid = generate_work(studio, None, '素材を確認', 5)
    outs = read_round_output(studio.store, rid).views
    assert [bool(outs[i][2]) for i in range(3)] == [True, False, True]
    assert studio.store.one('SELECT status FROM rounds')['status'] == 'partial'


@pytest.mark.parametrize('repair_succeeds', [True, False])
def test_python_exit_repairs_only_failing_candidate(tmp_path, engine, monkeypatch, repair_succeeds):
    from types import SimpleNamespace

    calls = engine()
    client = mock_client(calls, before_code=lambda i, code: (
        code + '\nraise RuntimeError("test synthesis failure")' if i == 2 else code))
    parse = client.responses.parse
    repairs = []

    def reply(**kwargs):
        if kwargs['text_format'] is not production_layers.SoundRepair:
            return parse(**kwargs)
        payload = json.loads(kwargs['input'][1]['content'])
        repairs.append(payload)
        code = payload['code']
        if repair_succeeds:
            code = code.split('\nraise RuntimeError')[0]
        return SimpleNamespace(id='repair', model='mock', usage=None,
                               output_parsed=production_layers.SoundRepair(code=code))

    client.responses.parse = reply
    monkeypatch.setattr(structured_api, 'resolve_client', lambda _: ('mock', client))
    studio = Studio(Store(tmp_path))
    rid = generate_work(studio, None, '合成エラーから再開', 5, candidate_count=2)
    candidates = read_round_output(studio.store, rid).views
    assert len(repairs) == 1
    assert 'RuntimeError: test synthesis failure' in repairs[0]['execution_log']
    assert candidates[0][2] is not None
    candidate, _, run = candidates[1]
    original = studio.store.candidate_dir(candidate['id'])/'source.py'
    assert 'raise RuntimeError' in original.read_text()
    assert (run is not None) == repair_succeeds
    if repair_succeeds:
        sound = Path(json.loads(run['result'])['layers']['sound'])
        assert 'raise RuntimeError' not in (sound/'execution'/'source.py').read_text()
        assert any(payload['sound_code'] == (sound/'execution'/'source.py').read_text()
                   for schema, payload in calls if schema is production_layers.Scene)


def test_resume_repairs_failed_arrangement_using_saved_dry_audio_and_scene(tmp_path, engine, monkeypatch):
    from types import SimpleNamespace

    calls = engine()
    client = mock_client(calls)
    parse = client.responses.parse
    repairs = []

    def reply(**kwargs):
        if kwargs['text_format'] is production_layers.SoundRepair:
            payload = json.loads(kwargs['input'][1]['content'])
            repairs.append(payload)
            return SimpleNamespace(id='repair', model='mock', usage=None,
                                   output_parsed=production_layers.SoundRepair(code='# Use the provided clips.'))
        response = parse(**kwargs)
        if kwargs['text_format'] is production_layers.Arrangement:
            response.output_parsed.code = 'raise ValueError("dust_far: non-finite audio")'
        return response

    client.responses.parse = reply
    monkeypatch.setattr(structured_api, 'resolve_client', lambda _: ('mock', client))
    studio = Studio(Store(tmp_path))
    rid = generate_work(studio, None, 'アレンジだけ再開', 5, candidate_count=1)
    assert studio.store.one('SELECT status FROM rounds')['status'] == 'failed'
    candidate, _, _ = read_round_output(studio.store, rid).views[0]
    layers = studio.store.candidate_dir(candidate['id'])/'layers'
    previous = next((layers/'arrangement').glob('*/program.json'))
    before = json.loads(previous.read_text())
    call_count = len(calls)
    studio.render_saved(rid, retry=True, position=0)
    assert len(repairs) == 1 and len(calls) == call_count
    assert 'dust_far: non-finite audio' in repairs[0]['execution_log']
    _, _, run = read_round_output(studio.store, rid).views[0]
    arrangement = Path(json.loads(run['result'])['layers']['arrangement'])
    after = json.loads((arrangement/'program.json').read_text())
    assert after['inputs'] == before['inputs']
    assert after['request'] == before['request']
    assert after['scene'] == before['scene']
    assert 'raise ValueError' in (previous.parent/'arrangement.py').read_text()


@pytest.mark.parametrize('tamper', [False, True])
def test_render_retry_validates_saved_clips_without_running_python(tmp_path, engine, monkeypatch, tamper):
    original = work.mix_work
    def fail(*args):
        raise ValueError('レンダラーの一時エラー')
    monkeypatch.setattr(work, 'mix_work', fail)
    studio = Studio(Store(tmp_path))
    rid = generate_work(studio, None, '再レンダリング', 5, candidate_count=1)
    initial_run = studio.store.one('SELECT * FROM runs')
    if tamper:
        clip = tmp_path/'runs'/initial_run['id']/'events'/'first.npy'
        np.save(clip, np.load(clip) * .7)
    monkeypatch.setattr(work, 'mix_work', original)
    original_popen = runner.subprocess.Popen
    def unexpected(args, *rest, **kwargs):
        assert '-I' not in args, 'validated generated code must not run again'
        return original_popen(args, *rest, **kwargs)
    monkeypatch.setattr(runner.subprocess, 'Popen', unexpected)

    def unexpected_llm(*args):
        pytest.fail('最終レンダリングの再試行でLLMを呼び出した')

    monkeypatch.setattr(structured_api, 'resolve_client', unexpected_llm)
    studio.render_saved(rid, retry=True)
    result = json.loads(studio.store.rows('SELECT result FROM runs ORDER BY created DESC')[0]['result'])
    assert result['status'] == ('failed' if tamper else 'succeeded')
    if tamper:
        assert '変更' in result['error']


def test_improve_and_edit_the_same_slot(tmp_path, engine):
    calls = engine()
    studio = Studio(Store(tmp_path))
    root = generate_work(studio, None, '制作指示', 5, candidate_count=1)
    candidate, _, run = read_round_output(studio.store, root).views[0]
    review = studio.store.review(root, candidate['id'], '響きを残したい', uid(), run_id=run['id'])
    child = uid()
    studio.reserve_improvement(root, root, run['id'], '少し深く', child, duration=6, candidate_count=1, remake=True)
    studio.generate_reserved_room(child)
    director = next(payload for schema, payload in reversed(calls) if schema is sound_first.Plan)
    assert director['reviews'][0]['id'] == review
    assert director['parent_design']['events']
    assert all('trajectory' not in event for event in director['parent_design']['events'])
    latest_scene = next(payload for schema, payload in reversed(calls) if schema is production_layers.Scene)
    assert latest_scene['parent_spatial_scene']['placements']
    _, _, child_run = read_round_output(studio.store, child).views[0]
    assert child_run
    path = Path(json.loads(child_run['result'])['audio']['path'])
    replacement = uid()
    studio.reserve_redo(root, child, '深くしすぎない', replacement)
    studio.generate_reserved_room(replacement)
    assert [(row['id'], row['prompt']) for row in RoomHistory(studio.store).steps(root)] == [
        (root, '制作指示'), (replacement, '深くしすぎない')]
    studio.recover()
    assert path.exists()
    assert Path(json.loads(run['result'])['audio']['path']).exists()

    call_count = len(calls)
    with TestClient(create_app(tmp_path)) as http:
        response = http.post(f'/api/rooms/{root}/rounds/{replacement}/undo', json={})
        assert response.status_code == 200
        assert http.get('/api/audio/' + child_run['id'] + '/status').status_code == 200
    assert len(calls) == call_count
    assert [(row['id'], row['prompt']) for row in RoomHistory(studio.store).steps(root)] == [
        (root, '制作指示'), (child, '少し深く')]
    assert read_round_output(studio.store, child).views[0][2]['id'] == child_run['id']
    assert path.exists()


def wait_for_finished_candidate(root):
    store = Store(root)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if store.rows("SELECT id FROM runs WHERE status='succeeded'"):
            return
        time.sleep(.05)
    pytest.fail('他の候補の停止中に先行候補が完成しませんでした')



def test_api_exposes_candidates_as_they_finish_and_keeps_polling_read_only(tmp_path, engine):
    gate = Event()
    later = Event()

    def hold_second(index, code):
        if index != 1:
            later.set()
            assert gate.wait(20)
        return code

    engine(hold_second)
    with TestClient(create_app(tmp_path)) as http:
        try:
            response = http.post('/api/rooms', json={'prompt': 'はじめの空間作品', 'duration': 5, 'count': 3})
            assert response.status_code == 202
            rid = response.json()['roomId']
            assert http.get('/api/rooms').json()['rooms'][0]['id'] == rid
            assert later.wait(10)
            wait_for_finished_candidate(tmp_path)
            current = http.get('/api/rooms/' + rid).json()['rounds'][0]
            assert current['count'] == 3
            assert current['task'] is not None
            candidate = current['candidates'][0]
            assert candidate['plan']
            run_id = candidate['track']['id']
            assert [item['stage'] for item in candidate['previews']] == ['original', 'spatial']
            assert http.get('/api/audio/' + run_id, headers={'Range': 'bytes=0-15'}).status_code == 206
            assert http.post(f'/api/rooms/{rid}/rounds/{rid}/retry', json={}).status_code == 409
            assert http.post('/api/audio/' + run_id + '/improve',
                             json={'prompt': '完成した作品の改善', 'duration': 5, 'count': 1, 'remake': True}).status_code == 202
        finally:
            gate.set()
        for job in http.app.state.production.jobs.snapshot():
            job.future.result(timeout=25)
        root, improvement = http.get('/api/rooms/' + rid).json()['rounds']
        assert all(item['track'] for item in root['candidates'])
        assert improvement['candidates'][0]['track']
        preview = next(tmp_path.glob('candidates/' + candidate['id'] + '/layers/spatial/*/previews/*/work.mp3'))
        preview.unlink()
        assert [item['stage'] for item in http.get('/api/rooms/' + rid).json()['rounds'][0]['candidates'][0]['previews']] == ['original']
        assert not preview.exists()



def test_saved_work_reopens_from_api_in_new_session(tmp_path, engine):
    studio = Studio(Store(tmp_path))
    rid = generate_work(studio, None, '保存する作品', 5, candidate_count=1)
    candidate, _, run = read_round_output(studio.store, rid).views[0]
    run_id = run['id']
    with TestClient(create_app(tmp_path)) as http:
        assert http.post('/api/audio/' + run_id + '/save', json={}).status_code == 200
    with TestClient(create_app(tmp_path)) as http:
        saved, = http.get('/api/saved').json()['tracks']
        assert (saved['id'], saved['title'], saved['roomId']) == (run_id, candidate['title'], rid)
        assert http.get(saved['url'] + '/status').status_code == 200


def test_prompt_polish_updates_draft_without_starting_generation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from soundlab import ai

    requests = []

    def reply(**kwargs):
        assert kwargs['text_format'] is ai.PolishedPrompt
        requests.append(json.loads(kwargs['input'][1]['content']))
        return SimpleNamespace(id='polish', model='mock', usage=None,
                               output_parsed=ai.PolishedPrompt(prompt='低い響きが静かに広がる音を作って。'))

    client = SimpleNamespace(responses=SimpleNamespace(parse=reply))
    monkeypatch.setattr(structured_api, 'resolve_client', lambda _: ('mock', client))
    with TestClient(create_app(tmp_path)) as http:
        assert http.post('/api/prompt/polish', json={'prompt': ' '}).status_code == 422
        response = http.post('/api/prompt/polish', json={'prompt': '低く静かな音'})
        assert response.json()['prompt'] == '低い響きが静かに広がる音を作って。'
        assert requests == [{'draft': '低く静かな音', 'purpose': 'create_work'}]
        assert http.get('/api/rooms').json()['rooms'] == []
        assert not http.app.state.production.jobs.snapshot()


def test_improvement_suggestions_use_final_code_and_fill_editable_prompt(tmp_path, engine, monkeypatch):
    from types import SimpleNamespace
    from soundlab import ai

    calls = engine()
    client = mock_client(calls)
    parse = client.responses.parse
    suggestions = ['輪郭を保ったまま低域を深くする', '空間に余白を作る', '終盤の動きを強める']
    requests = []

    def reply(**kwargs):
        if kwargs['text_format'] is not ai.ImprovementSuggestions:
            return parse(**kwargs)
        requests.append(json.loads(kwargs['input'][1]['content']))
        return SimpleNamespace(id='ideas', model='mock', usage=None,
                               output_parsed=ai.ImprovementSuggestions(prompts=suggestions))

    client.responses.parse = reply
    monkeypatch.setattr(structured_api, 'resolve_client', lambda _: ('mock', client))
    monkeypatch.setenv('SOUNDLAB_DATA_DIR', str(tmp_path))
    studio = Studio(Store(tmp_path))
    rid = generate_work(studio, None, '元の作品', 5, candidate_count=1)
    run = read_round_output(studio.store, rid).views[0][2]
    arrangement = Path(json.loads(run['result'])['layers']['arrangement'])
    with TestClient(create_app(tmp_path)) as http:
        assert not requests
        assert http.post('/api/audio/' + run['id'] + '/suggestions', json={}).json()['prompts'] == suggestions
        assert requests == [{'prompt': '元の作品', 'final_code': (arrangement/'execution'/'source.py').read_text()}]
        prompt = suggestions[1] + '。余韻を残す。'
        assert http.post('/api/audio/' + run['id'] + '/improve',
                         json={'prompt': prompt, 'duration': 5, 'count': 1, 'remake': True}).status_code == 202
        http.app.state.production.jobs.snapshot()[0].future.result(timeout=20)
    assert RoomHistory(studio.store).steps(rid)[-1]['prompt'] == prompt



def test_api_allows_independent_rooms_even_with_queued_improvement(tmp_path, engine):
    studio = Studio(Store(tmp_path))
    root = generate_work(studio, None, '元の作品を2案', 5, candidate_count=2)
    runs = [entry[2]['id'] for entry in read_round_output(studio.store, root).views.values()]

    started = Event()
    release = Event()

    def hold_first(index, code):
        if not started.is_set():
            started.set()
            assert release.wait(15)
        return code

    engine(hold_first)
    with TestClient(create_app(tmp_path)) as http:
        try:
            body = {'prompt': '1案目の改善', 'duration': 5, 'count': 1, 'remake': True}
            path = '/api/audio/' + runs[0] + '/improve'
            accepted = http.post(path, json=body)
            assert accepted.status_code == 202
            assert started.wait(10)
            queue = http.app.state.production.jobs
            first = queue.snapshot()[0]
            duplicate = http.post(path, json=body)
            assert duplicate.status_code == 202 and duplicate.json() == accepted.json()
            assert queue.snapshot() == (first,)
            assert http.post('/api/audio/' + runs[1] + '/improve',
                             json={**body, 'prompt': '2案目の改善'}).status_code == 202
            queued = queue.snapshot()[0]
            assert not queued.future.running()
            assert http.post(f'/api/rooms/{root}/rounds/{root}/redo', json={'prompt': '変更'}).status_code == 409
            assert http.post('/api/rooms', json={'prompt': '別の部屋', 'duration': 5, 'count': 1}).status_code == 202
            queue.snapshot()[0].future.result(timeout=12)
            assert not first.future.done()
            assert not queued.future.running()
        finally:
            release.set()
        queued.future.result(timeout=20)
        assert [row['prompt'] for row in RoomHistory(studio.store).steps(root)] == ['元の作品を2案', '1案目の改善', '2案目の改善']


@pytest.mark.parametrize('mode', ['direct', 'director'])
def test_compare_openai_and_claude_in_one_room(tmp_path, engine, monkeypatch, mode):
    import httpx
    from soundlab import ai

    calls = engine()
    monkeypatch.setenv('SOUNDLAB_SOUND_GENERATION_MODE', mode)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-claude-key')
    monkeypatch.setenv('ANTHROPIC_MODEL', 'claude-sonnet-5-5')
    monkeypatch.setenv('SOUNDLAB_DATA_DIR', str(tmp_path))
    claude = mock_client([])
    schemas = {schema.__name__: schema for schema in (
        sound_first.DirectProposal, sound_first.Plan, ai.Proposal,
        production_layers.Scene, production_layers.Arrangement,
    )}
    sent = []
    truncate_spatial = mode == 'director'

    def claude_api(request):
        nonlocal truncate_spatial
        assert request.headers['x-api-key'] == 'test-claude-key'
        if request.method == 'GET':
            assert request.url == 'https://api.anthropic.com/v1/models/claude-sonnet-5-5'
            return httpx.Response(200, json={'max_tokens': 128000})
        assert request.url == 'https://api.anthropic.com/v1/messages'
        body = json.loads(request.content)
        assert body['model'] == 'claude-sonnet-5-5' and body['stream']
        assert body['output_config']['format']['type'] == 'json_schema'
        schema = schemas[body['output_config']['format']['schema']['title']]
        assert body['max_tokens'] == 128000
        assert body['output_config']['effort'] == 'medium'
        payload = json.loads(body['messages'][0]['content'])
        sent.append((schema.__name__, payload))
        reason = 'end_turn'
        if schema is production_layers.Scene and truncate_spatial:
            truncate_spatial = False
            reason = 'max_tokens'
        reply = claude.responses.parse(store=False, input=[
            {'role': 'system', 'content': body['system']}, *body['messages'],
        ], text_format=schema)
        output = reply.output_parsed.model_dump_json()
        half = len(output) // 2
        events = [
            {'type': 'message_start', 'message': {'id': 'claude-test', 'model': body['model'], 'usage': {}}},
            {'type': 'content_block_start', 'content_block': {'type': 'text', 'text': ''}},
            *({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': chunk}}
              for chunk in (output[:half], output[half:])),
            {'type': 'message_delta', 'delta': {'stop_reason': reason}, 'usage': {'output_tokens': 20}},
            {'type': 'message_stop'},
        ]
        return httpx.Response(200, text=''.join(f'data: {json.dumps(event)}\n\n' for event in events))

    async_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: async_client(
        transport=httpx.MockTransport(claude_api), **kw))
    with TestClient(create_app(tmp_path)) as app:
        response = app.post('/api/rooms', json={'prompt': '同じ指示で比較', 'duration': 5, 'count': 1, 'compare': True})
        assert response.status_code == 202
        rid = response.json()['roomId']
        queue = app.app.state.production.jobs
        queue.snapshot()[0].future.result(timeout=20)
        outputs = app.get('/api/rooms/' + rid).json()['rounds'][0]['candidates']
        assert [item['provider'] for item in outputs] == ['openai', 'claude']
        if mode == 'director':
            assert '出力上限' in outputs[1]['error']
            assert app.post(f'/api/rooms/{rid}/rounds/{rid}/retry', json={'position': 1}).status_code == 202
            queue.snapshot()[0].future.result(timeout=20)
        assert all(item['track'] and len(item['previews']) == 2
                   for item in app.get('/api/rooms/' + rid).json()['rounds'][0]['candidates'])
    studio = Studio(Store(tmp_path))
    assert len(RoomHistory(studio.store).steps(rid)) == 1
    expected = ['DirectProposal', 'Scene', 'Arrangement'] if mode == 'direct' else [
        'Plan', 'Proposal', 'Scene', 'Arrangement',
    ]
    expected_claude = ['Plan', 'Proposal', 'Scene', 'Scene', 'Arrangement'] if mode == 'director' else expected
    assert [name for name, _ in sent] == expected_claude
    assert all(payload['prompt'] == '同じ指示で比較' for _, payload in sent)
    assert [schema.__name__ for schema, _ in calls] == expected
    outputs = read_round_output(studio.store, rid)
    requests = [json.loads((studio.store.candidate_dir(c['id'])/'request.json').read_text())
                for c, _, _ in outputs.views.values()]
    assert [request['seed'] for request in requests] == [42, 42]
    assert all(run is not None for _, _, run in outputs.views.values())


def test_missing_dataset_fails_before_api(tmp_path):
    with pytest.raises(ValueError, match='FABIAN'):
        generate_work(Studio(Store(tmp_path)), None, '音を作る', 5)


def test_room_generation_runs_in_parallel_and_can_be_deleted(tmp_path, engine):
    from functools import partial
    from soundlab.jobs import Jobs
    from soundlab.room_delete import request_room_deletion, finish_room_deletion

    first_rendering = Event()
    release_first = Event()
    first_id = uid()

    def hold_first(i, code):
        # Only the first API transport blocks.
        first_rendering.set()
        assert release_first.wait(15)
        return code

    calls = engine()
    first_client = mock_client(calls, before_code=hold_first)
    studio = Studio(Store(tmp_path))
    jobs = Jobs()
    try:
        first = jobs.submit('最初の部屋', partial(generate_work, studio, None, '最初の作品', 5,
                            round_id=first_id, candidate_count=1, client=first_client), key=f'room:{first_id}')
        assert first_rendering.wait(15)
        second = jobs.submit('次の部屋', partial(generate_work, studio, None, '次の作品', 5,
                             candidate_count=1))
        second_job = next(job for job in jobs.snapshot() if job.id == second)
        second_id = second_job.future.result(timeout=15)
        assert read_round_output(studio.store, second_id).views[0][2]
        request_room_deletion(tmp_path, first_id)
        release_first.set()
        first_job = next(job for job in jobs.snapshot() if job.id == first)
        first_job.future.result(timeout=15)
        finish_room_deletion(studio, first_id)
        assert [row['id'] for row in RoomHistory(studio.store).list()] == [second_id]
        assert not (tmp_path/'rounds'/first_id).exists()
    finally:
        release_first.set()
        jobs.executor.shutdown()


def test_dense_plan_accepts_more_events_points_and_overlap(tmp_path, monkeypatch):
    client = mock_client([])
    parse = client.responses.parse

    def dense_plan(**kwargs):
        response = parse(**kwargs)
        plan = response.output_parsed
        if isinstance(plan, sound_first.Plan):
            direction = plan.candidate_directions[0]
            event = direction.events[0]
            event.end_seconds = 4
            direction.events = [event.model_copy(update={'id': f'event_{i}'}) for i in range(25)]
        return response

    client.responses.parse = dense_plan
    monkeypatch.setenv('OPENAI_MODEL', 'mock')
    folder = tmp_path / 'rounds' / 'dense'
    folder.mkdir(parents=True)
    context = {'render_context': {'duration_seconds': 5}, 'candidate_count': 1, 'reviews': []}
    saved = []
    sound_first.generate('重なり合う音', context, folder, client,
                         save_candidate=lambda _, __, plan: saved.append(plan['sound_events']))
    events, = saved
    assert len(events) == 25
    scene = production_layers.Scene(intent='複雑な軌道', placements=[
        dict(id=event['id'], reverb=.1, trajectory=[
            dict(progress=i/9, azimuth=i*4, elevation=0, distance=1.7)
            for i in range(10)
        ]) for event in events
    ])
    assert all(len(event['keys']) == 10 for event in production_layers.compile_scene(events, scene))
    assert sum(event['frames'] for event in events) > 4 * 5 * 48000


def test_layers_publish_only_the_arranged_materials(tmp_path, engine):
    calls = engine()
    client = mock_client(calls)
    parse = client.responses.parse

    def creative_arrangement(**kwargs):
        response = parse(**kwargs)
        if isinstance(response.output_parsed, production_layers.Arrangement):
            payload = json.loads(kwargs['input'][1]['content'])
            source = payload['dry_sources'][0]
            rate = payload['sample_rate']
            first = {key: source[key] for key in ('id', 'role', 'audible_change')}
            first.update(time_seconds=source['start_frame']/rate,
                         end_seconds=(source['start_frame'] + source['frames'])/rate)
            placement = payload['spatial_scene']['placements'][0]
            response.output_parsed = production_layers.Arrangement(
                code="""import json, sys
from pathlib import Path
import numpy as np

request = json.loads(Path(sys.argv[1]).read_text())
original = Path(request['input_dir']) / 'events' / 'first.npy'
output = Path(request['output_dir']) / 'events' / 'first.npy'
np.save(output, np.load(original) * .5)
np.save(output.with_name('added.npy'), np.load(original)[::-1] * .25)
""",
                events=[first, {**first, 'id': 'added', 'time_seconds': .75, 'end_seconds': 1.0}],
                scene={'intent': '出現位置を変更',
                       'placements': [placement, {**placement, 'id': 'added'}],
                       'formations': [{'id': 'paired', 'name': '二つの響き',
                                       'members': ['added', 'first'], 'closed': False}]},
                changes='原音の一つを弱め、別の音を追加する',
            )
        return response

    client.responses.parse = creative_arrangement
    studio = Studio(Store(tmp_path))
    generate_work(studio, None, '核になる音が動く', 5, candidate_count=1, client=client)
    assert [schema.__name__ for schema, _ in calls] == ['Plan', 'Proposal', 'Scene', 'Arrangement']
    sound_request, spatial_request, arrangement_request = [payload for _, payload in calls[1:]]
    assert not any('keys' in event for event in sound_request['events'])
    assert spatial_request['dry_sources'] == arrangement_request['dry_sources']
    for payload in (spatial_request, arrangement_request):
        assert 'sound_events' not in payload['sound_plan']
        assert 'events' not in payload['sound_plan']
        source = payload['dry_sources'][0]
        assert {'id', 'start_frame', 'frames', 'peak', 'rms', 'role', 'audible_change'} <= source.keys()
        assert source['id'] == 'first'
    assert spatial_request['renderer'] == production_layers.CAPABILITIES
    assert arrangement_request['renderer'] == production_layers.SCENE_CONTRACT
    assert 'sound_code' in spatial_request and 'dry_mix_analysis' in arrangement_request
    assert 'sound_code' not in arrangement_request
    assert 'spatial_renderer_code' not in spatial_request
    renderer_dir = Path(work.__file__).parent
    assert arrangement_request['spatial_renderer_code'] == {
        name: (renderer_dir/name).read_text(encoding='utf-8') for name in ('work.py', 'render.py')
    }
    assert arrangement_request['spatial_scene']['placements']
    run = json.loads(studio.store.one('SELECT result FROM runs')['result'])
    paths = {key: Path(value) for key, value in run['layers'].items()}
    dry = paths['sound'] / 'execution'
    dry_audio = np.load(dry/'events'/'first.npy')
    final_audio = np.load(Path(run['audio']['path']).parent/'events'/'first.npy')
    np.testing.assert_allclose(final_audio, dry_audio * .5)
    assert 'sha256' not in spatial_request['dry_sources'][0]
    assert 'sha256' not in arrangement_request['dry_mix_analysis']
    stored_material = json.loads((dry.parent/'material.json').read_text())
    assert runner.file_hash(dry/'events'/'first.npy') == stored_material['event_hashes']['first']
    assert (dry/'work.mp3').exists()
    assert (paths['spatial']/'scene.json').exists()
    executable_request = json.loads((Path(run['audio']['path']).parent/'request.json').read_text())
    assert [event['id'] for event in executable_request['events']] == ['first', 'added']
    assert executable_request['events'][1]['start_frame'] == 36000
    assert (paths['arrangement']/'program.json').exists()

    cid = studio.store.one('SELECT id FROM candidates')['id']
    rid = studio.store.one('SELECT id FROM rounds')['id']
    scene = production_layers.read_json(paths['spatial']/'scene.json')
    preview_request = production_layers.spatial_preview_request(studio.store.candidate_dir(cid), scene)
    assert [s['id'] for s in trajectory_data(preview_request, {})['sources']] == ['first', 'second']
    final_sources = trajectory_data(executable_request, {'first': '核'})['sources']
    assert [(s['id'], s['label'], s['start']) for s in final_sources] == [
        ('first', '核', 12000), ('added', 'added', 36000)]
    assert [(s['formation']['id'], s['formation']['order']) for s in final_sources] == [
        ('paired', 1), ('paired', 0)]
    pipeline = production_layers.Pipeline(studio.store.candidate_dir(cid), tmp_path/'rounds'/rid, 1)
    sound = pipeline.sound(120)
    spatial = pipeline.spatial(sound)
    new_attempt, _, _ = pipeline.arrangement(sound, spatial, 120, rerun_saved=True)
    assert new_attempt != paths['arrangement']
    assert len(calls) == 4


def test_failed_spatial_retries_without_resynthesizing_dry_audio(tmp_path, engine):
    calls = engine()
    client = mock_client(calls)
    parse = client.responses.parse

    def invalid_scene(**kwargs):
        response = parse(**kwargs)
        if isinstance(response.output_parsed, production_layers.Scene):
            response.output_parsed.placements[0].id = 'unknown'
        return response

    client.responses.parse = invalid_scene
    studio = Studio(Store(tmp_path))
    rid = generate_work(studio, None, '空間を動かす', 5, candidate_count=1, client=client)
    failed = json.loads(studio.store.one('SELECT result FROM runs')['result'])
    assert failed['error'].startswith('spatial：')
    assert not read_round_output(studio.store, rid).views[0][2]
    assert [schema.__name__ for schema, _ in calls] == ['Plan', 'Proposal', 'Scene']
    studio.render_saved(rid, retry=True)
    assert read_round_output(studio.store, rid).views[0][2]
    assert [schema.__name__ for schema, _ in calls[3:]] == ['Scene', 'Arrangement']
    assert len(list(tmp_path.glob('candidates/*/layers/sound/*/material.json'))) == 1


def test_saved_legacy_code_still_renders_without_layer_api(tmp_path, engine, monkeypatch):
    from conftest import SOURCE, insert_candidate
    from soundlab.store import write_json

    def unexpected(*args):
        pytest.fail('既存コード実行でLLMを呼び出した')
    monkeypatch.setattr(structured_api, 'resolve_client', unexpected)
    studio = Studio(Store(tmp_path))
    rid = insert_candidate(studio.store)
    cid = studio.store.one('SELECT id FROM candidates')['id']
    folder = studio.store.candidate_dir(cid)
    (folder/'source.py').write_text(SOURCE.replace('FREQUENCY', '220'))
    event = {'id': 'before', 'start_frame': 12000, 'frames': 12000, 'reverb': 0.,
             'keys': [{'frame': frame, 'vector': [1., 0., 0.], 'distance': 1.7}
                      for frame in (0, 11999)]}
    (tmp_path/'rounds'/rid).mkdir(parents=True)
    (folder/'request.json').unlink()
    write_json(folder/'request.json', {'duration_seconds': 5, 'sample_rate': 48000, 'events': [event]})
    studio.render_saved(rid)
    result = json.loads(studio.store.one('SELECT result FROM runs')['result'])
    assert result['status'] == 'succeeded' and Path(result['audio']['path']).is_file()



def test_api_can_queue_failed_candidate_resume_before_other_candidate_finishes(tmp_path, engine):
    waiting = Event()
    release = Event()
    retried = False

    def fail_first(index, code):
        nonlocal retried
        if index == 1 and not retried:
            retried = True
            raise ValueError('コード生成の一時エラー')
        if index == 2:
            waiting.set()
            assert release.wait(15)
        return code

    calls = engine(fail_first)
    studio = Studio(Store(tmp_path))
    with TestClient(create_app(tmp_path)) as http:
        try:
            response = http.post('/api/rooms', json={'prompt': '二つの候補', 'duration': 5, 'count': 2})
            rid = response.json()['roomId']
            queue = http.app.state.production.jobs
            original = queue.snapshot()[0]
            assert waiting.wait(10)
            deadline = time.monotonic() + 10
            while 0 not in read_round_output(studio.store, rid).errors:
                assert time.monotonic() < deadline
                time.sleep(.05)
            retry = f'/api/rooms/{rid}/rounds/{rid}/retry'
            assert http.post(retry, json={'position': 1}).status_code == 409
            assert http.post(retry, json={'position': 0}).status_code == 202
            assert http.post(retry, json={'position': 0}).status_code == 409
            resume = queue.snapshot()[0]
            assert not resume.future.running() and not resume.future.done()
            task = http.get('/api/rooms/' + rid).json()['rounds'][0]['task']
            assert task['id'] == original.id and task['progress'] > 0
        finally:
            release.set()
        resume.future.result(timeout=20)
    after = read_round_output(studio.store, rid)
    assert all(after.views[position][2] for position in (0, 1))
    assert len(after.views[1][1]) == 1
    assert [payload['candidate_number'] for schema, payload in calls if schema.__name__ == 'Proposal'].count(1) == 2
    assert studio.store.one('SELECT status FROM rounds')['status'] == 'completed'


def test_api_stops_inflight_api_and_resumes_only_missing_layer(tmp_path, engine, monkeypatch):
    import asyncio

    calls = engine()
    client = mock_client(calls)
    parse = client.responses.parse
    waiting = Event()
    cancelled = Event()
    async def controlled_parse(**kwargs):
        if kwargs['text_format'] is production_layers.Arrangement:
            payload = json.loads(kwargs['input'][1]['content'])
            if payload['sound_plan']['sonic_identity'] == '材質1' and not waiting.is_set():
                waiting.set()
                try:
                    await asyncio.sleep(60)
                finally:
                    cancelled.set()
        return parse(**kwargs)

    client.responses.parse = controlled_parse
    monkeypatch.setattr(structured_api, 'resolve_client', lambda _: ('mock', client))
    studio = Studio(Store(tmp_path))
    with TestClient(create_app(tmp_path)) as http:
        response = http.post('/api/rooms', json={'prompt': '中止ボタン', 'duration': 5, 'count': 2})
        rid = response.json()['roomId']
        queue = http.app.state.production.jobs
        first_job = queue.snapshot()[0]
        assert waiting.wait(15)
        wait_for_finished_candidate(tmp_path)
        before = http.get('/api/rooms/' + rid).json()['rounds'][0]['candidates']
        assert before[0]['track'] and len(before[0]['previews']) == 2
        assert not before[1]['track'] and len(before[1]['previews']) == 2
        first_audio = before[0]['track']['id']
        assert http.post(f'/api/rooms/{rid}/rounds/{rid}/cancel', json={}).status_code == 202
        assert cancelled.wait(5)
        first_job.future.result(timeout=10)
        assert studio.store.one('SELECT status FROM rounds')['status'] == 'cancelled'
        candidates = http.get('/api/rooms/' + rid).json()['rounds'][0]['candidates']
        assert [candidate['canRetry'] for candidate in candidates] == [False, True]
        assert len(list(tmp_path.glob('candidates/*/layers/spatial/*/scene.json'))) == 2
        calls_before = len(calls)
        assert http.post(f'/api/rooms/{rid}/rounds/{rid}/retry', json={'position': 1}).status_code == 202
        queue.snapshot()[0].future.result(timeout=15)
        assert [schema.__name__ for schema, _ in calls[calls_before:]] == ['Arrangement']
        assert studio.store.one('SELECT status FROM rounds')['status'] == 'completed'
        after = read_round_output(studio.store, rid)
        assert after.views[0][2]['id'] == first_audio
        assert after.views[1][2]


def test_cancel_stops_generated_python_process(tmp_path, engine):
    import os
    import time
    from functools import partial
    from soundlab.jobs import Jobs

    engine(lambda index, code: 'import time\ntime.sleep(60)\n' + code)
    studio = Studio(Store(tmp_path))
    queue = Jobs()
    rid = uid()
    try:
        queue.submit('Python停止',
                     partial(generate_work, studio, None, 'Python停止', 5, round_id=rid, candidate_count=1),
                     key=f'room:{rid}')
        deadline = time.monotonic() + 10
        marker = None
        while marker is None and time.monotonic() < deadline:
            marker = next(tmp_path.glob('candidates/*/layers/sound/*/execution/process.json'), None)
            if marker is None:
                time.sleep(.05)
        assert marker is not None
        pid = json.loads(marker.read_text())['pid']
        job = queue.snapshot()[0]
        queue.cancel(job)
        assert job.future.result(timeout=10) == rid
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert studio.store.one('SELECT status FROM rounds')['status'] == 'cancelled'
    finally:
        queue.executor.shutdown()


def test_truncated_arrangement_retries_and_spatial_preview_is_renderable(tmp_path, engine):

    client = mock_client([])
    parse = client.responses.parse
    budgets = []

    def truncate_once(**kwargs):
        if kwargs['text_format'] is production_layers.Arrangement:
            budgets.append(kwargs['max_output_tokens'])
            if len(budgets) == 1:
                production_layers.Arrangement.model_validate_json('{"code":"unfinished')
        return parse(**kwargs)

    client.responses.parse = truncate_once
    studio = Studio(Store(tmp_path))
    rid = generate_work(studio, None, '中断対策と段階試聴', 5, candidate_count=1, client=client)
    candidate, _, run = read_round_output(studio.store, rid).views[0]
    assert run is not None
    assert budgets == [24000, 40000]
    result = json.loads(run['result'])
    candidate_dir = studio.store.candidate_dir(candidate['id'])
    preview = production_layers.make_spatial_preview(candidate_dir, Path(result['layers']['spatial']), tmp_path)
    assert Path(preview).is_file()
    assert runner.file_hash(preview) != runner.file_hash(Path(result['layers']['sound'])/'execution'/'work.mp3')


@pytest.mark.parametrize('brief', ['弱い擦過音が途切れずに変形する', '鋭い粒が不規則に飛び出す'])
def test_direct_sound_uses_agent_defined_sources(tmp_path, engine, monkeypatch, brief):
    barrier = Barrier(2, timeout=10)

    def require_overlapping_code_requests(_, code):
        barrier.wait()
        return code

    calls = engine(require_overlapping_code_requests)
    monkeypatch.delenv('SOUNDLAB_SOUND_GENERATION_MODE', raising=False)
    studio = Studio(Store(tmp_path))
    rid = generate_work(studio, None, brief, 5, candidate_count=2)
    assert sorted(schema.__name__ for schema, _ in calls) == [
        'Arrangement', 'Arrangement', 'DirectProposal', 'DirectProposal', 'Scene', 'Scene',
    ]
    assert all(payload['prompt'] == brief for _, payload in calls)
    assert all('events' not in payload and 'candidate_plan' not in payload
               for schema, payload in calls if schema is sound_first.DirectProposal)
    for row in read_round_output(studio.store, rid).views.values():
        assert row[2] is not None
        result = json.loads(row[2]['result'])
        assert set(result['event_hashes']) == {'self_designed'}
        original = Path(result['layers']['sound'])/'execution'/'events'/'self_designed.npy'
        assert len(np.load(original, mmap_mode='r')) == (6000 if '鋭い粒' in brief else 240000)


@pytest.mark.parametrize('audio,motion,provider', [(True, False, 'openai'), (False, True, 'claude')])
def test_selective_improvement_retains_finished_materials(tmp_path, engine, monkeypatch, audio, motion, provider):
    from soundlab import ai, refinement

    engine()
    studio = Studio(Store(tmp_path))
    actual_request = structured_api.request
    calls = []
    scoped_code = """
import json
import sys
from pathlib import Path
import numpy as np
req = json.loads(Path(sys.argv[1]).read_text())
assert [event['id'] for event in req['events']] == ['first']
src = Path(req['input_dir'])/'events'
assert sorted(p.name for p in src.iterdir()) == ['first.npy']
np.save(Path(req['output_dir'])/'events'/'first.npy', np.load(src/'first.npy') * .5)
"""

    def reply(client, model, version, folder, stage, system, payload, schema, tokens, **kwargs):
        if schema is production_layers.Arrangement:
            return schema(code=scoped_code.replace("== ['first']", "== ['first', 'second']")
                          .replace("== ['first.npy']", "== ['first.npy', 'second.npy']")
                          + "\nnp.save(Path(req['output_dir'])/'events'/'second.npy', np.load(src/'second.npy') * .25)",
                          events=None, scene=None, changes='音量を調整'), {}
        if refinement.FORMAT not in version:
            return actual_request(client, model, version, folder, stage, system, payload, schema, tokens, **kwargs)
        calls.append((schema, kwargs['provider']))
        if schema is refinement.EditPlan:
            return schema(edits=[dict(id='first', audio=audio, motion=motion, instruction='核を変える')]), {}
        if schema is ai.Proposal:
            return schema(title='核を改善', approach='低音を調整', code=scoped_code, used_review_ids=[]), {}
        if schema is production_layers.SelectiveArrangement:
            return schema(code=scoped_code, changes='核を調整'), {}
        assert schema is production_layers.Scene
        if sum(s is schema for s, _ in calls) == 1:
            raise ValueError('一時的な空間設計エラー')
        return schema(intent='核を動かす', placements=[dict(id='first', reverb=.1, trajectory=[
            dict(progress=0, azimuth=0, elevation=0, distance=1.7),
            dict(progress=1, azimuth=90, elevation=0, distance=1.7)])]), {}

    monkeypatch.setattr(structured_api, 'request', reply)
    monkeypatch.setattr(structured_api, 'claude_model', lambda: 'claude-test')
    rid = generate_work(studio, None, '元の作品', 5, candidate_count=1)
    original = read_round_output(studio.store, rid).views[0][2]
    parent = json.loads(original['result'])
    dry = Path(parent['layers']['sound'])/'execution'/'events'/'second.npy'
    assert runner.file_hash(dry) != parent['event_hashes']['second']
    if provider == 'claude':
        studio.store.execute("UPDATE rounds SET context=json_set(context,'$.provider','claude') WHERE id=?", (rid,))
    child = uid()
    studio.reserve_improvement(rid, rid, original['id'], '核だけを変える', child, duration=5, candidate_count=1)
    studio.generate_reserved_room(child)
    if motion:
        assert not read_round_output(studio.store, child).views[0][2]
        studio.render_saved(child, retry=True)
        assert len(list(tmp_path.glob('candidates/*/layers/sound/*/material.json'))) == 2
    revised = read_round_output(studio.store, child).views[0][2]
    assert revised, studio.store.rows('SELECT result FROM runs')
    after = json.loads(revised['result'])
    assert after['event_hashes']['second'] == parent['event_hashes']['second']
    assert (after['event_hashes']['first'] != parent['event_hashes']['first']) == audio
    before_events, after_events = [
        json.loads((tmp_path/'runs'/run['id']/'request.json').read_text())['events']
        for run in (original, revised)]
    assert before_events[1] == after_events[1]
    assert (before_events[0] != after_events[0]) == motion
    scene = production_layers.read_json(Path(after['layers']['spatial'])/'scene.json')
    candidate = read_round_output(studio.store, child).views[0][0]
    preview_request = production_layers.spatial_preview_request(studio.store.candidate_dir(candidate['id']), scene)
    assert trajectory_data(preview_request, {}) == trajectory_data(
        {'sample_rate': 48000, 'events': after_events}, {})
    assert calls == [(schema, provider) for schema in (
        [refinement.EditPlan, ai.Proposal, production_layers.SelectiveArrangement] if audio else
        [refinement.EditPlan, production_layers.Scene, production_layers.Scene])]
    if audio:
        edited = uid()
        studio.reserve_redo(rid, child, '核の質感を変える', edited)
        studio.generate_reserved_room(edited)
        assert read_round_output(studio.store, edited).views[0][2]
        assert sum(schema is refinement.EditPlan for schema, _ in calls) == 2
        studio.undo_redo_prompt(rid, edited)
        assert RoomHistory(studio.store).steps(rid)[-1]['id'] == child
        assert read_round_output(studio.store, child).views[0][2]['id'] == revised['id']
    if motion:
        for layer in ('sound', 'arrangement'):
            assert not (Path(after['layers'][layer])/'execution'/'source.py').exists()


def test_selective_runner_rejects_output_for_protected_id(tmp_path):
    original = tmp_path/'original'
    clips = original/'events'
    clips.mkdir(parents=True)
    events = [{'id': name, 'start_frame': 0, 'frames': 8} for name in ('first', 'second')]
    for event in events:
        np.save(clips/f"{event['id']}.npy", np.full(8, .1, dtype=np.float32))
    digest = runner.file_hash(clips/'second.npy')
    code = tmp_path/'edit.py'
    code.write_text("""
import json
from pathlib import Path
import sys
import numpy as np
request = json.loads(Path(sys.argv[1]).read_text())
clips = Path(request['output_dir'])/'events'
clips.mkdir()
for name in ('first', 'second'):
    np.save(clips/(name + '.npy'), np.full(8, .5, dtype=np.float32))
""")
    request = {'events': events, 'duration_seconds': 5}
    result = runner.execute(code, request, tmp_path/'work',
                            runner.fingerprint(code.read_bytes(), request), stage='arrangement',
                            retained=(original, events[1:], {'second': digest}))
    assert result['status'] == 'failed'
    assert '変更対象外' in result['error']
    assert runner.file_hash(clips/'second.npy') == digest
