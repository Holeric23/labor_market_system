import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from market_system.jobs import Jobs
from market_system.jobs.worker import Worker, worker_lock
from market_system.jobs.store import text_digest
from market_system.search.store import connect, activate

SPEC = {'model_version':'fixture-model','model_manifest':{'fixture':True},'fingerprint':'fixture-v1'}


def analyze(texts):
    return [{'model_version':'fixture-model','label':None if text=='empty' else 'vacancy',
             'skills':['python'], 'skill_names':['Python'],'required_skills':['python'],
             'preferred_skills':[], 'possessed_skills':[], 'occupation_ids':[],
             'occupation_names':[], 'skill_mentions':[], 'skill_lexicon_sha256':'test',
             'skill_lexicon_version':'1','occupation_lexicon_sha256':'test','occupation_lexicon_version':'1'}
            for text in texts]


@pytest.fixture
def jobs(tmp_path):
    queue = Jobs(tmp_path/'jobs.db')
    queue.initialize()
    with connect(queue.path) as con:
        con.executemany('INSERT INTO contents VALUES(?,?,?)',[(1,'python vacancy','h1'),(2,'empty','h2'),(3,'third','h3')])
        con.execute("INSERT INTO source_types VALUES(1,'telegram')")
        con.execute("INSERT INTO sources VALUES(1,1,'fixture')")
        con.execute("INSERT INTO publications VALUES(1,1,1,'https://example.org/1','2026-01-01T00:00:00Z')")
        con.commit()
    return queue


def submit(jobs,kind='full',**kwargs):
    return jobs.submit(kind,batch_size=1,spec=SPEC,corrections={},**kwargs)


def worker(jobs,**kwargs):
    return Worker(jobs,analyzer_factory=lambda spec:analyze,**kwargs)


def counts(jobs,identity):
    rid=jobs.get(identity)['result_set_id']
    with connect(jobs.path) as con:
        return {table:con.execute(f'SELECT count(*) FROM {table} WHERE result_set_id=?',(rid,)).fetchone()[0]
                for table in ('document_processing','document_classes','search_catalog','result_evidence')}


def test_complete_zero_classes_idempotency_and_model_reuse(jobs):
    first=submit(jobs,idempotency_key='unique')
    assert submit(jobs,idempotency_key='unique')==first
    loads=[]
    w=Worker(jobs,analyzer_factory=lambda spec:loads.append(1) or analyze)
    w.run(once=True)
    assert jobs.get(first)['state']=='succeeded'
    second=submit(jobs)
    w.run(once=True)
    assert loads==[1]
    with connect(jobs.path) as con:
        assert con.execute('SELECT count(*) FROM job_models').fetchone()[0]==1
        assert con.execute("SELECT status FROM document_processing WHERE result_set_id=? AND content_id=2",(jobs.get(first)['result_set_id'],)).fetchone()[0]=='no_classes'
        assert con.execute('SELECT count(*) FROM active_result_set').fetchone()[0]==0
    assert counts(jobs,first)==counts(jobs,second)


def test_crash_between_batches_recovers_checkpoint(jobs):
    identity=submit(jobs)
    def crash(_):
        raise SystemExit('simulated abrupt stop')
    with pytest.raises(SystemExit):
        worker(jobs,after_batch=crash).run(once=True)
    assert jobs.get(identity)['progress']==1
    assert jobs.get(identity)['state']=='running'
    seen=[]
    w=Worker(jobs,analyzer_factory=lambda spec:lambda texts:seen.extend(texts) or analyze(texts))
    w.run(once=True)
    assert seen==['empty','third']
    assert jobs.get(identity)['attempts']==2
    assert jobs.get(identity)['state']=='succeeded'
    assert counts(jobs,identity)['document_classes']==4


def test_os_process_death_releases_lock_and_second_worker_refused(jobs):
    identity=submit(jobs)
    script='''import os,sys
from market_system.jobs import Jobs
from market_system.jobs.worker import Worker
from test_jobs import analyze
Worker(Jobs(sys.argv[1]),analyzer_factory=lambda spec:analyze,after_batch=lambda _:os._exit(17)).run(once=True)
'''
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).parent),PYTHONDONTWRITEBYTECODE='1')
    with worker_lock(jobs.path):
        with pytest.raises(RuntimeError,match='Another worker'):
            worker(jobs).run(once=True)
    result=subprocess.run([sys.executable,'-c',script,str(jobs.path)],env=env,capture_output=True)
    assert result.returncode==17, result.stderr.decode()
    worker(jobs).run(once=True)
    assert jobs.get(identity)['state']=='succeeded'
    assert jobs.get(identity)['attempts']==2


def test_cancel_between_batches_and_retry(jobs):
    identity=submit(jobs)
    worker(jobs,after_batch=lambda _:jobs.cancel(identity)).run(once=True)
    assert jobs.get(identity)['state']=='cancelled'
    assert jobs.get(identity)['progress']==1
    assert counts(jobs,identity)['document_processing']==1
    jobs.retry(identity)
    worker(jobs).run(once=True)
    assert jobs.get(identity)['state']=='succeeded'
    assert counts(jobs,identity)['document_processing']==3
    with pytest.raises(ValueError):
        jobs.retry(identity)


def test_cancel_during_analysis_discards_uncommitted_batch(jobs):
    identity=submit(jobs)
    def cancel(texts):
        jobs.cancel(identity)
        return analyze(texts)
    Worker(jobs,analyzer_factory=lambda spec:cancel).run(once=True)
    assert jobs.get(identity)['state']=='cancelled'
    assert jobs.get(identity)['progress']==0
    assert counts(jobs,identity)['document_processing']==0


def test_write_failure_rolls_back_all_tables_and_checkpoint(jobs):
    identity=submit(jobs)
    with connect(jobs.path) as con:
        con.execute("CREATE TRIGGER inject_failure BEFORE INSERT ON result_evidence WHEN NEW.content_id=2 BEGIN SELECT RAISE(ABORT,'injected write failure'); END")
        con.commit()
    worker(jobs).run(once=True)
    assert jobs.get(identity)['state']=='failed'
    assert jobs.get(identity)['checkpoint']==1
    assert counts(jobs,identity)['document_processing']==1
    with connect(jobs.path) as con:
        con.execute('DROP TRIGGER inject_failure')
        con.commit()
    jobs.retry(identity)
    worker(jobs).run(once=True)
    assert jobs.get(identity)['state']=='succeeded'
    assert counts(jobs,identity)['document_classes']==4


def test_partial_batch_fails_and_preserves_active(jobs):
    old=submit(jobs)
    worker(jobs).run(once=True)
    activate(jobs.path,jobs.get(old)['result_set_id'])
    identity=submit(jobs)
    Worker(jobs,analyzer_factory=lambda spec:lambda texts:[]).run(once=True)
    assert jobs.get(identity)['state']=='failed'
    assert 'partial batch' in jobs.get(identity)['error']
    with pytest.raises(ValueError):
        activate(jobs.path,jobs.get(identity)['result_set_id'])
    with connect(jobs.path) as con:
        assert con.execute('SELECT result_set_id FROM active_result_set').fetchone()[0]==jobs.get(old)['result_set_id']


def test_new_collection_does_not_change_frozen_corpus(jobs):
    identity=submit(jobs)
    with connect(jobs.path) as con:
        con.execute("INSERT INTO contents VALUES(4,'new','h4')")
        con.execute("INSERT INTO publications VALUES(2,1,1,'https://example.org/2','2026-02-01T00:00:00Z')")
        con.commit()
    worker(jobs).run(once=True)
    assert jobs.get(identity)['total']==3
    assert counts(jobs,identity)['document_processing']==3
    with connect(jobs.path) as con:
        assert con.execute('SELECT publication_count FROM search_catalog WHERE content_id=1').fetchone()[0]==1
    activate(jobs.path,jobs.get(identity)['result_set_id'])
    new=submit(jobs,'incremental')
    worker(jobs).run(once=True)
    assert jobs.get(new)['total']==1
    assert counts(jobs,new)['document_processing']==4


def test_incremental_replaces_removed_links_without_duplicates(jobs):
    old=submit(jobs)
    worker(jobs).run(once=True)
    activate(jobs.path,jobs.get(old)['result_set_id'])
    identity=submit(jobs,'incremental',ids=[1])
    def no_skills(texts):
        rows=analyze(texts)
        for row in rows:
            row['required_skills']=[]
        return rows
    Worker(jobs,analyzer_factory=lambda spec:no_skills).run(once=True)
    with connect(jobs.path) as con:
        rid=jobs.get(identity)['result_set_id']
        links=con.execute('SELECT role FROM document_classes WHERE result_set_id=? AND content_id=1',(rid,)).fetchall()
        assert [r[0] for r in links]==['none']
        assert con.execute('SELECT count(*) FROM document_classes WHERE result_set_id=? AND content_id=1',(jobs.get(old)['result_set_id'],)).fetchone()[0]==2


def test_confirmed_correction_preserves_machine_and_effective_results(jobs):
    correction={'revision':2,'annotation':{'post_type':'resume','skills':[], 'occupations':[], 'salary':[],
               'conditions':{k:[] for k in ('work_format','schedule','employment_type','working_hours','hours_per_week')}},
               'skills':[],'occupations':[]}
    identity=jobs.submit('full',batch_size=1,spec=SPEC,corrections={'h1':correction})
    worker(jobs,after_batch=lambda _:jobs.cancel(identity)).run(once=True)
    jobs.retry(identity)
    worker(jobs).run(once=True)
    with connect(jobs.path) as con:
        assert con.execute('SELECT post_type FROM search_catalog WHERE content_id=1').fetchone()[0]=='resume'
        evidence=json.loads(con.execute('SELECT profile_json FROM result_evidence WHERE content_id=1').fetchone()[0])
        assert evidence['machine']['label']=='vacancy'
        assert evidence['confirmed_correction']['revision']==2
        assert evidence['effective']['required_skills']==[]


def test_sample_ready_but_never_activatable(jobs):
    identity=submit(jobs,'sample',ids=[1])
    worker(jobs).run(once=True)
    assert jobs.get(identity)['state']=='succeeded'
    with pytest.raises(Exception,match='sample cannot be activated'):
        activate(jobs.path,jobs.get(identity)['result_set_id'])


def test_changed_frozen_text_fails(jobs):
    identity=submit(jobs)
    with connect(jobs.path) as con:
        con.execute("UPDATE contents SET text='changed' WHERE id=1")
        con.commit()
    worker(jobs).run(once=True)
    assert jobs.get(identity)['state']=='failed'
    assert 'Frozen input' in jobs.get(identity)['error']


def test_sql_ready_checks_exact_ids_not_counts(jobs):
    identity=submit(jobs,'sample',ids=[1])
    with connect(jobs.path) as con:
        job=jobs.get(identity)
        rid=job['result_set_id']
        con.execute("INSERT INTO document_processing VALUES(?,2,'no_classes')",(rid,))
        con.execute("INSERT INTO result_evidence VALUES(?,2,'{}',NULL,'[]')",(rid,))
        con.execute("INSERT INTO search_catalog(result_set_id,content_id,publication_count,review_status,salary_scope) VALUES(?,2,0,'unreviewed','document')",(rid,))
        with pytest.raises(Exception,match='input coverage mismatch'):
            con.execute("UPDATE result_sets SET state='ready' WHERE result_set_id=?",(rid,))


def test_queued_cancel_never_analyzes(jobs):
    identity=submit(jobs)
    jobs.cancel(identity)
    assert worker(jobs).run(once=True) is None
    assert jobs.get(identity)['attempts']==0


def test_atomic_claim_prevents_second_owner(jobs):
    first=submit(jobs)
    submit(jobs)
    one,two=worker(jobs),worker(jobs)
    assert one.claim()==first
    assert two.claim() is None
    with jobs.transaction() as con:
        with pytest.raises(RuntimeError,match='ownership'):
            two.owned(con,first)


def test_incomplete_record_is_not_no_classes(jobs):
    identity=submit(jobs)
    Worker(jobs,analyzer_factory=lambda spec:lambda texts:[{'model_version':SPEC['model_version']}]).run(once=True)
    assert jobs.get(identity)['state']=='failed'
    assert jobs.get(identity)['progress']==0


def test_cli_create_show_cancel_retry(jobs):
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
    def call(*args):
        result=subprocess.run([sys.executable,'-m','market_system.jobs','--db',str(jobs.path),*args],
                              env=env,capture_output=True,text=True,encoding='utf-8')
        assert result.returncode==0,result.stderr
        return result.stdout.strip()
    call('init')
    identity=call('create','--kind','sample','--ids','1','--batch-size','1')
    assert json.loads(call('show',identity))['state']=='queued'
    call('cancel',identity)
    assert json.loads(call('show',identity))['state']=='cancelled'
    call('retry',identity)
    assert json.loads(call('show',identity))['state']=='queued'


def test_imported_confirmations_survive_later_draft(jobs,tmp_path):
    from market_system.jobs.store import confirmed_corrections
    from market_system.sqlite import connect as sqlite_connect
    dataset=tmp_path/'dataset.json'
    dataset.write_text(json.dumps({'dataset_id':'d','items':[{'id':'h1'}],'skills':[],'occupations':[]}),encoding='utf-8')
    database=tmp_path/'annotations.db'
    con=sqlite_connect(database)
    con.execute('CREATE TABLE revisions(id,revision,dataset,status,annotation,saved_at)')
    con.executemany('INSERT INTO revisions VALUES(?,?,?,?,?,?)',[
        ('h1',1,'d','confirmed','{"post_type":"resume"}','now'),
        ('h1',2,'d','draft','{"post_type":"vacancy"}','later'),
        ('h1',3,'other','confirmed','{"post_type":"vacancy"}','later')])
    con.commit();con.close()
    before=database.read_bytes()
    result=confirmed_corrections({'paths':{'dataset':str(dataset),'annotation_database':str(database)}})
    assert result['h1']['revision']==1
    assert result['h1']['annotation']['post_type']=='resume'
    assert database.read_bytes()==before


def test_new_model_requires_full_and_preserves_active(jobs):
    identity=submit(jobs)
    worker(jobs).run(once=True)
    active=jobs.get(identity)['result_set_id']
    activate(jobs.path,active)
    different={**SPEC,'model_version':'new-model','model_manifest':{'fixture':'new'},'fingerprint':'new'}
    with pytest.raises(ValueError,match='same model'):
        jobs.submit('incremental',spec=different,corrections={})
    new=jobs.submit('full',spec=different,corrections={})
    def analyze_new(texts):
        rows=analyze(texts)
        for r in rows:
            r['model_version']='new-model'
        return rows
    Worker(jobs,analyzer_factory=lambda spec:analyze_new).run(once=True)
    assert jobs.get(new)['state']=='succeeded'
    with connect(jobs.path) as con:
        assert con.execute('SELECT result_set_id FROM active_result_set').fetchone()[0]==active


def test_idempotency_conflict_and_same_version_changed_manifest(jobs):
    submit(jobs,idempotency_key='same')
    with pytest.raises(ValueError,match='different request'):
        submit(jobs,'sample',ids=[1],idempotency_key='same')
    with pytest.raises(ValueError,match='different manifest'):
        jobs.submit('full',spec={**SPEC,'model_manifest':{'changed':True}},corrections={})
