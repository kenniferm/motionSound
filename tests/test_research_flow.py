from conftest import insert_candidate
import hashlib
import sqlite3

import pytest

from soundlab.memory import context_for
from soundlab.store import Store, uid, now


def test_legacy_database_migration_preserves_original_records(tmp_path):
    db = sqlite3.connect(tmp_path/'history.sqlite3')
    db.executescript('''
      CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,created TEXT NOT NULL);
      CREATE TABLE rounds(id TEXT PRIMARY KEY,project_id TEXT,parent_id TEXT,prompt TEXT,mode TEXT,status TEXT,context TEXT,error TEXT,created TEXT);
      CREATE TABLE candidates(id TEXT PRIMARY KEY,round_id TEXT,position INTEGER,title TEXT,approach TEXT,refs TEXT);
      CREATE TABLE runs(id TEXT PRIMARY KEY,candidate_id TEXT,status TEXT,approval TEXT,result TEXT,created TEXT);
      CREATE TABLE reviews(id TEXT PRIMARY KEY,round_id TEXT,candidate_id TEXT,text TEXT,token TEXT UNIQUE,created TEXT);
      INSERT INTO projects VALUES('p','以前の作品','2026-01-01');
      INSERT INTO rounds VALUES('r','p',NULL,'以前の指示','demo','completed','{}',NULL,'2026-01-01');
      INSERT INTO candidates VALUES('c','r',0,'以前の候補','以前の説明','[]');
      INSERT INTO reviews VALUES('v','r','c','原文は残す','token','2026-01-01');
    ''')
    db.commit(); db.close()
    artifact=tmp_path/'candidates'/'c'/'source.py'; artifact.parent.mkdir(parents=True)
    artifact.write_text('# old source\n')
    before=hashlib.sha256(artifact.read_bytes()).hexdigest()
    store=Store(tmp_path)
    assert store.one('SELECT * FROM reviews')['context']=='{}'
    assert store.one('SELECT * FROM reviews')['run_id'] is None
    assert store.one('SELECT * FROM reviews')['text']=='原文は残す'
    assert store.one('SELECT * FROM rounds')['context']=='{}'
    assert hashlib.sha256(artifact.read_bytes()).hexdigest()==before
    Store(tmp_path)  # migration is idempotent
    assert len(store.rows('SELECT * FROM reviews'))==1




def test_relevance_retrieval_keeps_current_and_older_relevant_project_review(tmp_path):
    store=Store(tmp_path); pid=store.project('空間')
    round1=insert_candidate(store, '音像の高さと前後', project=pid)
    cid=store.one('SELECT id FROM candidates WHERE round_id=?', (round1,))['id']
    # Trusted database fixtures avoid repeated audio work while exercising retrieval.
    for i in range(14):
        text='高さの変化は感じなかった' if i==0 else '柔らかい音色'
        store.execute('INSERT INTO reviews(id,round_id,candidate_id,text,token,created,context) VALUES(?,?,?,?,?,?,?)',
            (f'r{i}',round1,cid,text,uid(),f'2026-01-{i+1:02d}', '{}'))
    parent_round=insert_candidate(store, '低音を比較', project=pid)
    parent=store.one('SELECT id FROM candidates WHERE round_id=?', (parent_round,))['id']
    store.execute('INSERT INTO reviews(id,round_id,candidate_id,text,token,created,context) VALUES(?,?,?,?,?,?,?)',
                  ('current',parent_round,parent,'低音を維持',uid(),now(),'{}'))
    ctx=context_for(store,pid,parent,'高さを再検証','headphones')
    assert ctx['retrieved_review_ids'][0]=='current'
    assert 'r0' in ctx['retrieved_review_ids'] and len(ctx['reviews'])<=10
    record=next(r for r in ctx['reviews'] if r['id']=='r0')
    assert record['scope']=='project' and record['retrieval_reason']=='project_relevant'




@pytest.mark.parametrize('change',[
    {'playback_mode':'binaural'}, {'normalization':'guaranteed_safe'},
    {'time_start':1}, {'time_start':4,'time_end':9}, {'time_start':float('nan'),'time_end':4}])
def test_invalid_review_context_never_changes_raw_history(tmp_path,change):
    store=Store(tmp_path)
    rid=insert_candidate(store); cid=store.one('SELECT id FROM candidates WHERE round_id=?', (rid,))['id']
    store.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)',(uid(),cid,'succeeded','fixture','{}',now()))
    with pytest.raises(ValueError):
        store.review(rid,cid,'記録すべき原文',uid(),context=change)
    assert not store.rows('SELECT * FROM reviews')


