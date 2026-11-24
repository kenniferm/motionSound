import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from soundlab import production_layers, sound_first, structured_api
from soundlab.spatial import dataset
from soundlab.store import uid


SOURCE = """import json, os, sys
from pathlib import Path
import numpy as np
assert 'OPENAI_API_KEY' not in os.environ
settings = json.loads(Path(sys.argv[1]).read_text())
clips = Path(settings['output_dir']) / 'events'
clips.mkdir()
for event in settings['events']:
    t = np.arange(event['frames']) / settings['sample_rate']
    wave = .18 * np.sin(2*np.pi*FREQUENCY*t) * np.hanning(len(t))
    np.save(clips / (event['id'] + '.npy'), wave.astype(np.float32))
"""


def mock_client(calls, *, before_code=None):
    def parse(**kwargs):
        assert kwargs['store'] is False
        payload = json.loads(kwargs['input'][1]['content'])
        schema = kwargs['text_format']
        calls.append((schema, payload))
        from soundlab import refinement

        if schema is refinement.EditPlan:
            obj = schema(edits=[dict(id=payload['sources'][0]['id'], audio=True, motion=False,
                                     instruction='最初の素材を変える')])
        elif schema is production_layers.SelectiveArrangement:
            obj = schema(code='# Keep the provided editable clips.\n', changes='素材を活かす')
        elif schema is sound_first.Plan:
            directions = []
            for i in range(payload['candidate_count']):
                directions.append(dict(generation_principle=f'共鳴法{i}', state_evolution='減衰',
                    audible_mapping='交差', sonic_identity=f'材質{i}', time_structure=f'展開{i}',
                    component_roles=['核'], failure_and_check='動きを確認',
                    events=[dict(id=name, time_seconds=.25, end_seconds=.5, audible_change='音色変化', role='響きの核')
                            for name in ('first', 'second')]))
            obj = schema(perceptual_goal='交差する音', used_review_ids=[], candidate_directions=directions)
        elif schema is production_layers.Scene:
            obj = schema(intent='交差する音', placements=[
                dict(id=source['id'], reverb=.2, trajectory=[
                    dict(progress=0, azimuth=65, elevation=0, distance=1.7),
                    dict(progress=1, azimuth=-65, elevation=0, distance=2.4),
                ]) for source in payload['dry_sources']
            ])
        elif schema is production_layers.Arrangement:
            obj = schema(code='# Keep the provided clips.\n', events=None, scene=None,
                         changes='空間設計と素材の組み合わせを活かす')
        else:
            i = payload['candidate_number']
            code = SOURCE.replace('FREQUENCY', str(130 + i * 91))
            if 'edits' in payload:
                code = '''import json
import sys
from pathlib import Path
import numpy as np
req = json.loads(Path(sys.argv[1]).read_text())
for event in req['events']:
    name = event['id'] + '.npy'
    clip = np.load(Path(req['input_dir'])/'events'/name)
    np.save(Path(req['output_dir'])/'events'/name, clip * .5)
'''
            if before_code:
                code = before_code(i, code)
            fields = dict(title=f'空間作品{i}', approach='試験用', code=code, used_review_ids=[])
            if schema is sound_first.DirectProposal:
                fields['events'] = [
                    dict(id='self_designed', time_seconds=0,
                         end_seconds=.125 if '鋭い粒' in payload['prompt'] else payload['duration_seconds'],
                         audible_change='持続的に変化', role='主音源')]
            obj = schema(**fields)
        return SimpleNamespace(id='mock', model='mock', usage=None, output_parsed=obj)
    return SimpleNamespace(responses=SimpleNamespace(parse=parse))


@pytest.fixture
def fabian(tmp_path):
    source = Path('data/datasets').resolve()
    if not (source / dataset.DATASET / 'manifest.json').is_file():
        pytest.skip('実測FABIANデータセットが未導入です')
    (tmp_path / 'datasets').symlink_to(source, target_is_directory=True)
    return tmp_path


@pytest.fixture
def engine(fabian, monkeypatch):
    monkeypatch.setenv('SOUNDLAB_SOUND_GENERATION_MODE', 'director')
    calls = []

    def configure(before_code=None):
        client = mock_client(calls, before_code=before_code)
        monkeypatch.setattr(structured_api, 'resolve_client', lambda client_override: ('mock', client_override or client))
        return calls

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key-kept-out-of-subprocess')
    configure()
    return configure


def generate_work(studio, project, prompt, duration, parent_id=None, *, round_id=None,
                  client=None, candidate_count=sound_first.DEFAULT_COUNT, seed=42):
    rid = round_id or uid()
    studio.reserve_generation(project, prompt, duration, parent_id, round_id=rid,
                              candidate_count=candidate_count, seed=seed)
    return studio.generate_reserved_room(rid, client=client)


def insert_candidate(store, prompt='保存した指示', *, project=None, count=1):
    rid = uid()
    store.execute('INSERT INTO rounds VALUES(?,?,?,?,?,?,?,?,?)',
                  (rid, project or store.project('作品'), None, prompt, 'openai', 'awaiting_approval',
                   '{"render_context":{"duration_seconds":5}}', None, '2026-01-01T00:00:00Z'))
    for i in range(count):
        cid = uid()
        store.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?)', (cid, rid, i, '候補', '方法', '[]'))
        path = store.candidate_dir(cid)
        path.mkdir(parents=True)
        (path/'source.py').write_text('# mock')
        (path/'request.json').write_text('{"duration_seconds":5,"sample_rate":48000}')
    return rid
