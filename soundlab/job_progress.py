"""Approximate generation progress from observable milestones."""

import json

from . import sound_first
from .jobs import Job, job_route
from .store import Store


def job_progress(store: Store, job: Job) -> tuple[int, str]:
    if not job.future.running():
        return 0, '順番待ち'

    kind, round_id, _ = job_route(job)
    if kind == 'preview':
        return 40, '空間設計の試聴音を作成中'
    if kind not in ('room', 'render', 'improve', 'redo') or not round_id:
        return 5, '生成を準備中'

    rows = store.rows('SELECT status,context FROM rounds WHERE id=?', (round_id,))
    if not rows:
        return 5, '生成を準備中'

    status = rows[0]['status']
    if status == 'queued':
        return 5, '生成を準備中'
    context = json.loads(rows[0]['context'])
    folder = store.root / 'rounds' / round_id
    if kind == 'render' or status != 'planning':
        counts = store.one("""
            SELECT COUNT(*) AS total,
                   COUNT(CASE WHEN EXISTS (
                       SELECT 1 FROM runs r WHERE r.candidate_id=c.id AND r.status='succeeded'
                   ) THEN 1 END) AS ready
            FROM candidates c WHERE c.round_id=?
        """, (round_id,))
        total, ready = counts['total'], counts['ready']
        expected = context.get('candidate_count', total)
        if kind == 'render' and status == 'rendering' and total < expected:
            return 25, '未完成の候補を再開中'
        if not total:
            return 40, '生成結果を確認中'
        if ready == total:
            return 95, '結果を保存中'
        return 65 + 30 * ready // total, f'音声を書き出し中（{total}案中{ready}案が完成）'

    count = context.get('candidate_count', 3)
    direct = context.get('sound_generation_mode') == 'direct'
    if direct or any((folder / sound_first.plan_filename(provider, context)).is_file()
                     for provider, _, _ in sound_first.generation_groups(context)):
        ready = {row['position'] + 1 for row in store.rows("""
            SELECT c.position FROM candidates c WHERE c.round_id=? AND EXISTS (
                SELECT 1 FROM runs r WHERE r.candidate_id=c.id AND r.status='succeeded'
            )
        """, (round_id,))}
        stages = ('sound', 'spatial', 'arrangement', 'final')
        steps = 0
        for index in range(1, count + 1):
            if index in ready:
                steps += 6
            elif (folder / f'code_{index}_request.json').is_file():
                steps += max((offset for offset, stage in enumerate(stages, 2)
                              if (folder / f'layer_{index}_{stage}.json').is_file()), default=1)
        if steps:
            label = f'並列生成中（{count}案中{len(ready)}案が完成）' if count > 1 else '生成中'
            return 20 + 75 * steps // (6 * count), '結果を保存中' if len(ready) == count else label
        return 20, '生成を準備中'
    return 10, '制作設計中'
