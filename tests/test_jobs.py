from conftest import insert_candidate
from concurrent.futures import Future
from threading import Event

import pytest
from starlette.testclient import TestClient

from soundlab.job_progress import job_progress
from soundlab.api import create_app
from soundlab.jobs import Job
from soundlab.service import Studio
from soundlab.store import Store, now, uid




def test_progress_counts_rendered_candidates_not_attempts(tmp_path):
    store = Store(tmp_path)
    rid = insert_candidate(store, count=3)
    candidate_ids = [r['id'] for r in store.rows('SELECT id FROM candidates WHERE round_id=?', (rid,))]
    future: Future[str] = Future()
    job = Job(uid(), '音', future, f'render:{rid}', Event())

    assert job_progress(store, job) == (0, '順番待ち')
    assert future.set_running_or_notify_cancel()
    assert job_progress(store, job) == (65, '音声を書き出し中（3案中0案が完成）')
    for cid, status in [(candidate_ids[0], 'succeeded'), (candidate_ids[0], 'succeeded'),
                        (candidate_ids[1], 'failed')]:
        store.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)', (uid(), cid, status, '', '{}', now()))
    assert job_progress(store, job) == (75, '音声を書き出し中（3案中1案が完成）')


def test_same_round_lock_cannot_be_acquired_twice(tmp_path):
    store = Store(tmp_path)
    studio = Studio(store)
    rid = insert_candidate(store)
    with studio.operation(rid):
        with pytest.raises(ValueError, match='すでに実行中'):
            with studio.operation(rid):
                pytest.fail('duplicate acquired')


def test_generation_status_opens_saved_round_without_memory_job(tmp_path):
    store = Store(tmp_path)
    rid = insert_candidate(store)
    with TestClient(create_app(tmp_path)) as http:
        assert http.get('/api/rooms').json()['rooms'][0]['id'] == rid
        saved, = http.get('/api/rooms/' + rid).json()['rounds']
        assert saved['status'] == 'awaiting_approval'
        assert saved['task'] is None
        assert not http.app.state.production.jobs.snapshot()
