from conftest import generate_work, insert_candidate
import numpy as np
import pytest
from soundlab import runner
from soundlab.store import Store
from soundlab.service import Studio

@pytest.fixture(autouse=True)
def model_config(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "test-structured-model")


@pytest.fixture
def studio(tmp_path):
    return Studio(Store(tmp_path/'library'))





@pytest.mark.parametrize('kind',['nan','infinity','loud','shape','dtype','symlink'])
def test_bad_audio_rejected(tmp_path,kind):
    a=np.zeros((240000,2),dtype=np.float32)
    if kind=='nan': a[0,0]=np.nan
    if kind=='infinity': a[0,0]=np.inf
    if kind=='loud': a[0,0]=1.1
    if kind=='shape': a=a[:,0]
    if kind=='dtype': a=a.astype(np.int16)
    p=tmp_path/'mix.npy'; np.save(p,a)
    if kind=='symlink':
        p.rename(tmp_path/'original.npy'); p.symlink_to(tmp_path/'original.npy')
    with pytest.raises(ValueError): runner.convert(p,tmp_path,{'duration_seconds':5,'sample_rate':48000})






def test_recovery_lock_and_scope(tmp_path, engine):
    studio = Studio(Store(tmp_path))
    s=studio.store; pid=s.project('a'); rid=insert_candidate(s, project=pid)
    with studio.operation():
        with pytest.raises(ValueError): studio.recover()
    s.execute("UPDATE rounds SET status='rendering' WHERE id=?",(rid,))
    studio.recover()
    assert s.one('SELECT status FROM rounds WHERE id=?',(rid,))['status']=='interrupted'
    cid=s.one('SELECT id FROM candidates WHERE round_id=?', (rid,))['id']
    with pytest.raises(ValueError): generate_work(studio, s.project('b'),'音',5,cid)






