"""Read production state and published artifacts without advancing work."""

from collections import defaultdict
from dataclasses import dataclass
import json

from . import sound_first
from .store import read_json


@dataclass
class RoundOutput:
    plans: list
    views: dict
    errors: dict
    checkpoints: dict


def read_round_output(store, round_id, *, context=None):
    folder = store.root / 'rounds' / round_id
    if context is None:
        rows = store.rows('SELECT context FROM rounds WHERE id=?', (round_id,))
        context = json.loads(rows[0]['context']) if rows else {}
    groups = tuple(sound_first.generation_groups(context)) if context else ()
    plans = []
    for provider, _, count in groups:
        validated_plan = folder / sound_first.plan_filename(provider, context)
        recorded_director = folder / (sound_first.director_stage(provider, context) + '_response.json')
        if validated_plan.is_file():
            group_plans = read_json(validated_plan)['candidate_directions']
        elif recorded_director.is_file() and any(folder.glob('code*_request.json')):
            group_plans = (read_json(recorded_director)['output'] or {}).get('candidate_directions', [])
        else:
            group_plans = []
        if context.get('compare_claude') or context.get('sound_generation_mode') == 'direct':
            group_plans = (group_plans + [None] * count)[:count]
        plans.extend(group_plans)

    with store.connect() as db:
        db.execute('BEGIN')
        candidates = [dict(row) for row in db.execute(
            'SELECT * FROM candidates WHERE round_id=? ORDER BY position', (round_id,))]
        runs = defaultdict(list)
        for run in db.execute("""SELECT u.* FROM runs u JOIN candidates c ON c.id=u.candidate_id
                                WHERE c.round_id=? ORDER BY u.created DESC,u.id DESC""", (round_id,)):
            runs[run['candidate_id']].append(dict(run))

    views = {}
    for candidate in candidates:
        position = candidate['position']
        if position >= len(plans):
            plans.extend([None] * (position + 1 - len(plans)))
        if plans[position] is None:
            candidate_plan = store.root/'candidates'/candidate['id']/'plan.json'
            if candidate_plan.is_file():
                plans[position] = read_json(candidate_plan).get('candidate_plan')
        attempts = runs[candidate['id']]
        audio_run = next((run for run in attempts if run['status'] == 'succeeded'), None)
        views[candidate['position']] = (candidate, attempts, audio_run)
    errors = {}
    for source in sound_first.candidate_attempts(folder):
        for position in range(len(plans)):
            path = source / f'candidate_{position+1}_error.json'
            if path.is_file():
                errors[position] = read_json(path)['error']
            elif (source / f'code_{position+1}_request.json').is_file():
                errors.pop(position, None)
    checkpoints = {position: '制作計画' for position, plan in enumerate(plans) if plan is not None}
    for position, (candidate, _, _) in views.items():
        layers = store.root / 'candidates' / candidate['id'] / 'layers'
        checkpoints[position] = '生成コード'
        for kind, file, label in (
            ('arrangement', 'program.json', 'アレンジコード'),
            ('spatial', 'scene.json', '空間設計'),
            ('sound', 'material.json', '原音'),
        ):
            if next((layers/kind).glob('*/' + file), None) is not None:
                checkpoints[position] = label
                break
    return RoundOutput(plans, views, errors, checkpoints)
