import json
import pytest
from conftest import generate_work
from soundlab.store import Store, uid
from soundlab.service import Studio


@pytest.fixture
def rendered(tmp_path, engine):
    store=Store(tmp_path)
    studio=Studio(store)
    rid=generate_work(studio, store.project('音声レビュー'), '音を比べる', 5)
    records=store.rows('SELECT runs.*,c.position FROM runs JOIN candidates c ON c.id=runs.candidate_id WHERE c.round_id=? ORDER BY c.position',(rid,))
    return store,studio,rid,records


def test_comments_bind_exact_audio_and_improvement_explicitly_picks_source(rendered):
    store,studio,rid,runs=rendered
    first,second=runs[:2]
    a=store.review(rid,first['candidate_id'],'この音は低音がよい',uid(),run_id=first['id'])
    b=store.review(rid,second['candidate_id'],'この音は高音が強すぎる',uid(),run_id=second['id'])
    assert store.one('SELECT run_id FROM reviews WHERE id=?',(a,))['run_id']==first['id']
    assert store.one('SELECT run_id FROM reviews WHERE id=?',(b,))['run_id']==second['id']
    store.review(rid,second['candidate_id'],'別の感想を追加',uid(),run_id=second['id'])
    restored=Store(store.root)
    assert len(restored.rows('SELECT * FROM reviews'))==3
    assert not restored.rows('SELECT * FROM audio_selections')
    next_round = uid()
    studio.reserve_improvement(rid, rid, first['id'], '低音を残して改善', next_round,
                               duration=5, candidate_count=1, remake=True)
    studio.generate_reserved_room(next_round)
    child = store.one('SELECT parent_id,context FROM rounds WHERE id=?', (next_round,))
    assert child['parent_id'] == first['candidate_id']
    ctx = json.loads(child['context'])
    assert ctx['refinement_run_id'] == first['id']
    assert any(r['run_id']==first['id'] for r in ctx['reviews'])


def test_wrong_sound_failed_run_and_cross_round_are_rejected(rendered):
    store,studio,rid,runs=rendered
    first,second,third=runs
    with pytest.raises(ValueError):
        store.review(rid,first['candidate_id'],'別音への誤登録',uid(),run_id=second['id'])
    store.execute("UPDATE runs SET status='failed' WHERE id=?",(third['id'],))
    with pytest.raises(ValueError):
        store.review(rid,third['candidate_id'],'失敗音',uid(),run_id=third['id'])
    other=generate_work(studio, store.one('SELECT id FROM projects')['id'],'別の音',5)
    with pytest.raises(ValueError):
        store.review(other,first['candidate_id'],'別の部屋への誤登録',uid(),run_id=first['id'])
    assert not store.rows('SELECT * FROM reviews')


def test_token_cannot_move_review_between_audio_files(rendered):
    store,_,rid,runs=rendered
    first,second=runs[:2]; token=uid()
    old=store.review(rid,first['candidate_id'],'原文',token,run_id=first['id'])
    assert store.review(rid,first['candidate_id'],'原文',token,run_id=first['id'])==old
    with pytest.raises(ValueError,match='内容が一致しません'):
        store.review(rid,first['candidate_id'],'修正版',token,run_id=first['id'])
    with pytest.raises(ValueError):
        store.review(rid,second['candidate_id'],'誤った対象',token,run_id=second['id'])
    assert store.one('SELECT * FROM reviews')['text']=='原文'


def test_review_interval_uses_rendered_audio_not_changed_request(rendered):
    store,_,rid,runs=rendered
    run=runs[0]; path=store.candidate_dir(run['candidate_id'])/'request.json'
    request=json.loads(path.read_text()); request['duration_seconds']=20
    path.write_text(json.dumps(request))
    with pytest.raises(ValueError,match='範囲'):
        store.review(rid,run['candidate_id'],'長さを間違えた',uid(),run_id=run['id'],
            context={'time_start':6,'time_end':9})
    assert not store.rows('SELECT * FROM reviews')
