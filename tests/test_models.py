"""Autonomous small analyzer; no artifact loading except the explicit compatibility tests."""
import hashlib
import json
from unittest.mock import patch

import pytest
from market_system.model_management.store import Models, active_spec
from market_system.model_management.adapters import IncompatibleArtifact, verify, class_space
from market_system.model_management.experiments import run, snapshot, import_history
from market_system.model_management.evaluation import evaluate
from market_system.review.store import Actor
from market_system.analytics.store import filters
from market_system.search.store import connect, encode, digest
from market_system.jobs.worker import Worker
from test_web import client, login, csrf
from test_search_service import catalog

OWNER = Actor('fixture-owner','ADMIN')
USER = Actor('fixture-user','USER')


def spec(version='m1', key='python', meaning='Programming'):
    checksum = digest({'key':key,'meaning':meaning})
    return {'model_version':version,'model_manifest':{'model_version':version,'packages':{},'evaluation_scope':'autonomous fixture'},
            'adapter':'ensemble-v1','files':{},'paths':{},'fingerprint':digest({}),
            'legacy_versions':{'occupation_dictionary_sha256':'occ'},
            'class_space':[{'namespace':'post_type:'+version,'key':k,'name':k,'version':version,'definition':{'meaning':k}} for k in ('vacancy','resume')]+[
                {'namespace':'skill:'+checksum,'key':key,'name':'Python','version':'1','definition':{'meaning':meaning}}]}


def register(models, value):
    with patch('market_system.model_management.store.specification',return_value=value), \
         patch('market_system.model_management.store.class_space',return_value=value['class_space']), \
         patch('market_system.model_management.store.verify'):
        return models.register(OWNER)


def designate(models, version):
    with patch('market_system.model_management.store.verify'):
        return models.designate(version,OWNER,batch_size=1)


def analyzer(value):
    skill = value['class_space'][-1]
    def predict(texts):
        return [{'model_version':value['model_version'],'label':None if text=='empty' else 'vacancy',
                 'skills':[skill['key']],'skill_names':['Python'],'required_skills':[skill['key']],
                 'preferred_skills':[],'possessed_skills':[],'occupation_ids':[],
                 'occupation_names':[],'skill_mentions':[],'skill_lexicon_sha256':skill['namespace'].split(':',1)[1],
                 'skill_lexicon_version':'1','occupation_lexicon_sha256':'occ','occupation_lexicon_version':'1'} for text in texts]
    return predict


def execute(models):
    Worker(models.jobs,analyzer_factory=analyzer).run(once=True)


def active(models):
    with connect(models.path) as con:
        row = con.execute('SELECT result_set_id FROM active_result_set').fetchone()
        return row[0] if row else None


@pytest.fixture
def models(tmp_path):
    models = Models(tmp_path/'models.db')
    models.initialize()
    with connect(models.path) as con:
        con.executemany('INSERT INTO contents VALUES(?,?,?)',[(1,'Python vacancy','h1'),(2,'empty','h2'),(3,'Resume','h3')])
        con.execute("INSERT INTO source_types VALUES(1,'telegram')")
        con.execute("INSERT INTO sources VALUES(1,1,'fixture')")
        con.execute("INSERT INTO publications VALUES(1,1,1,'https://example.org/1','2026-01-01T00:00:00Z')")
        con.execute("INSERT INTO review_datasets VALUES('labels','fixture-human','{}','now')")
        con.executemany("INSERT INTO review_memberships VALUES('labels',?,?,?,NULL)", [('h1',1,'development'),('h2',2,'test'),('h3',3,'development')])
        con.executemany("INSERT INTO review_type_labels VALUES('labels',?,?,?)", [('h1',1,'vacancy'),('h2',2,'trash'),('h3',3,'resume')])
        con.commit()
    return models


def test_designation_freezes_corpus_and_class_space_without_switch(models):
    register(models,spec())
    assert models.get('m1',OWNER)['status']=='candidate'
    job = designate(models,'m1')
    assert active(models) is None
    assert models.get('m1',OWNER)['status']=='approved'
    with connect(models.path) as con:
        con.execute("INSERT INTO contents VALUES(4,'added later','h4')")
        con.commit()
    execute(models)
    rid = models.jobs.get(job)['result_set_id']
    assert models.jobs.get(job)['state']=='succeeded'
    assert active(models) is None
    models.publish(rid,OWNER)
    assert active(models)==rid and models.get('m1',OWNER)['status']=='active'
    assert active_spec(models.path)['class_space']==spec()['class_space']
    with connect(models.path) as con:
        assert con.execute('SELECT count(*) FROM document_processing WHERE result_set_id=?',(rid,)).fetchone()[0]==3
        assert con.execute('SELECT status FROM document_processing WHERE result_set_id=? AND content_id=2',(rid,)).fetchone()[0]=='no_classes'
        frozen = json.loads(con.execute('SELECT provenance_json FROM result_sets WHERE result_set_id=?',(rid,)).fetchone()[0])
        assert frozen['class_space']==spec()['class_space']


@pytest.mark.parametrize('key,meaning',[('python2','Programming'),('python','Different meaning with same display name')])
def test_changed_list_or_same_name_new_meaning_switch_and_rollback(models,key,meaning):
    register(models,spec())
    first = designate(models,'m1'); execute(models)
    old = models.jobs.get(first)['result_set_id']; models.publish(old,OWNER)
    schema_before = None
    with connect(models.path) as con:
        schema_before = [tuple(r) for r in con.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name")]
        prior = con.execute("SELECT class_id FROM classes WHERE class_key='python'").fetchone()[0]
        con.execute("INSERT INTO telegram_access VALUES(7,'USER',1,7,'now')")
        from market_system.telegram_bot.catalog import parse_filter
        value = parse_filter({'skill_ids':[prior]})
        con.execute("INSERT INTO telegram_subscriptions(id,user_id,filter_json,model_version,status,created_at) VALUES('sub',7,?,'m1','active','now')",(encode(value),))
        con.commit()
    scientific = models.analytics.save(filters(),OWNER,old)
    preserved = models.analytics.load(scientific,OWNER)
    register(models,spec('m2',key,meaning)); second = designate(models,'m2'); execute(models)
    new = models.jobs.get(second)['result_set_id']
    assert active(models)==old
    models.publish(new,OWNER)
    assert models.get('m1',OWNER)['status']=='approved' and models.get('m2',OWNER)['status']=='active'
    with connect(models.path) as con:
        current = con.execute('SELECT class_id FROM classes WHERE namespace=? AND class_key=?',(spec('m2',key,meaning)['class_space'][-1]['namespace'],key)).fetchone()[0]
        assert current!=prior
        assert con.execute('SELECT status FROM telegram_subscriptions').fetchone()[0]=='needs_update'
        con.execute('INSERT INTO telegram_class_mappings VALUES(?,?,?,?,?)',(prior,'m2',current,7,'now'))
        con.commit()
    models.publish(new,OWNER) # idempotent activation still reconciles explicit mappings
    with connect(models.path) as con:
        assert con.execute('SELECT status FROM telegram_subscriptions').fetchone()[0]=='active'
    models.publish(old,OWNER)
    assert active(models)==old
    with connect(models.path) as con:
        assert con.execute('SELECT status FROM telegram_subscriptions').fetchone()[0]=='needs_update' # reverse mapping is explicit too
        assert schema_before == [tuple(r) for r in con.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name")]
        assert con.execute('SELECT count(*) FROM telegram_outbox').fetchone()[0]==0
    assert models.analytics.load(scientific,OWNER)==preserved


def test_failed_rebuild_preserves_active_and_frozen_unknown_class_rejected(models):
    register(models,spec()); first=designate(models,'m1'); execute(models)
    old=models.jobs.get(first)['result_set_id']; models.publish(old,OWNER)
    register(models,spec('m2')); second=designate(models,'m2')
    def factory(value):
        def wrong(texts):
            rows=analyzer(value)(texts)
            rows[0]['required_skills']=['not-registered']
            return rows
        return wrong
    Worker(models.jobs,analyzer_factory=factory).run(once=True)
    assert models.jobs.get(second)['state']=='failed'
    assert 'frozen class space' in models.jobs.get(second)['error']
    with pytest.raises(ValueError,match='ready'):
        models.publish(models.jobs.get(second)['result_set_id'],OWNER)
    assert active(models)==old


def test_confirmed_correction_survives_model_and_rollback(models):
    register(models,spec()); first=designate(models,'m1'); execute(models)
    old=models.jobs.get(first)['result_set_id']; models.publish(old,OWNER)
    review=models.analytics.bot.review
    value={
        'post_type':'resume','skills':[],'occupations':[],'salary':[],
        'conditions':{k:[] for k in ('work_format','schedule','employment_type','working_hours','hours_per_week')},
        'multiple_offers':False,'pending_terms':[],'comment':'human correction'}
    review.save(1,0,'confirmed',value,OWNER)
    register(models,spec('m2')); second=designate(models,'m2'); execute(models)
    new=models.jobs.get(second)['result_set_id']; models.publish(new,OWNER)
    with connect(models.path) as con:
        assert con.execute('SELECT post_type FROM search_catalog WHERE result_set_id=? AND content_id=1',(new,)).fetchone()[0]=='resume'
        assert con.execute('SELECT revision FROM result_review_applications WHERE result_set_id=? AND content_id=1',(new,)).fetchone()[0]==1
    rolled=models.publish(old,OWNER)
    assert rolled!=old # rollback retains newer human corrections in derivative snapshot
    with connect(models.path) as con:
        assert con.execute('SELECT post_type FROM search_catalog WHERE result_set_id=? AND content_id=1',(rolled,)).fetchone()[0]=='resume'
        assert json.loads(con.execute('SELECT classification_json FROM result_evidence WHERE result_set_id=? AND content_id=1',(old,)).fetchone()[0])['label']=='vacancy'


def test_roles_status_immutable_and_incompatible_adapter(models):
    with pytest.raises(PermissionError): models.register(USER)
    with pytest.raises(IncompatibleArtifact,match='Непроверенный'):
        models.register(OWNER,adapter='three_pairs')
    with pytest.raises(IncompatibleArtifact,match='адаптер'):
        verify({'adapter':'multiclass'})
    with pytest.raises(IncompatibleArtifact,match='метаданные'):
        verify({'model_manifest':{}})
    register(models,spec())
    with pytest.raises(PermissionError): models.designate('m1',USER)
    with pytest.raises(Exception,match='immutable'):
        with models.jobs.transaction() as con: con.execute("UPDATE model_registry SET evaluation_scope='new audit'")
    with pytest.raises(Exception,match='immutable'):
        with models.jobs.transaction() as con: con.execute("UPDATE model_class_definitions SET definition_json='{}'")
    models.status('m1','retired',OWNER)
    with pytest.raises(ValueError,match='retired'): designate(models,'m1')
    models.status('m1','approved',OWNER)
    first=designate(models,'m1'); execute(models); models.publish(models.jobs.get(first)['result_set_id'],OWNER)
    with pytest.raises(ValueError,match='переключите'): models.status('m1','retired',OWNER)


def test_budget_test_guard_and_timeout_are_persisted(models):
    register(models,spec())
    with pytest.raises(ValueError,match='бюджет'): run(models,'m1',[1],101,10,OWNER)
    with pytest.raises(ValueError,match='бюджета'): run(models,'m1',[1,3],1,10,OWNER)
    with pytest.raises(ValueError,match='Test'): run(models,'m1',[2],1,10,OWNER,'test','selection')
    with patch('market_system.model_management.experiments.subprocess.run',side_effect=__import__('subprocess').TimeoutExpired('fixture',1)):
        identity=run(models,'m1',[2],1,1,OWNER,'test')
    result=models.experiments(OWNER,identity)
    assert result['state']=='failed' and 'бюджет' in result['error']
    assert json.loads(result['provenance_json'])['independent_audit'] is False
    assert json.loads(result['inputs_json'])[0]['label_origins'][0]['origin']=='fixture-human'
    with pytest.raises(Exception,match='immutable'):
        with models.jobs.transaction() as con: con.execute("UPDATE model_experiments SET error='changed'")


def test_selection_group_overlap_rejected(models):
    with connect(models.path) as con:
        con.execute("INSERT INTO review_memberships VALUES('labels','extra1',1,'development','same')")
        con.execute("INSERT INTO review_memberships VALUES('labels','extra2',2,'test','same')")
        con.commit()
        with pytest.raises(ValueError,match='Группа'): snapshot(con,[1],'development','selection')


def test_field_metrics_unknown_truth_and_refusals():
    value=spec()
    predict=analyzer(value)
    class Predictor:
        def predict(self,texts): return predict(texts)
    inputs=[{'content_id':1,'text':'vacancy','label':'vacancy','fields':{'salary_from':100,'skills':[]}},
            {'content_id':2,'text':'empty','label':'resume','fields':{'salary_from':None}},
            {'content_id':3,'text':'vacancy','label':'vacancy','fields':None}]
    result=evaluate(value,inputs,Predictor())
    assert result['coverage']==2/3 and result['refusals']==1
    assert result['precision_recall']['vacancy']['precision']==1
    assert result['precision_recall']['resume']['recall']==0
    assert result['fields']['salary_from']['fn']==1 and result['fields']['salary_from']['true_absence']==1
    assert result['fields']['skills']['fp']==1 and result['fields']['skills']['evaluated']==1
    assert result['fields']['skills']['unknown_or_incompatible_truth']==2


def test_history_import_data_only_idempotent(models,tmp_path):
    folder=tmp_path/'history/run'; folder.mkdir(parents=True)
    (folder/'protocol.json').write_text('{"parameters":{"threshold":0.8},"scope":"old test"}',encoding='utf-8')
    (folder/'test_errors.csv').write_text('truth,prediction\nresume,vacancy\n',encoding='utf-8')
    (folder/'model.joblib').write_bytes(b'never loaded')
    before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
    ids=import_history(models,folder.parent,OWNER)
    assert import_history(models,folder.parent,OWNER)==ids
    reports=json.loads(models.experiments(OWNER,ids[0])['metrics_json'])['reports']
    assert 'protocol.json' in reports and 'test_errors.csv' in reports and 'model.joblib' not in reports
    assert before=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}


def test_model_web_owner_csrf_and_no_training_on_start(client):
    login(client,'reader')
    assert client.get('/models').status_code==403
    assert client.get('/experiments/unknown').status_code==403
    assert client.post('/models/no/designate',data={'csrf':'wrong'}).status_code==403
    login(client)
    page=client.get('/models'); assert page.status_code==200
    assert client.post('/models/no/designate',data={'csrf':'wrong'}).status_code==403
    assert client.post('/models/results/unknown/activate',data={'csrf':csrf(page)}).status_code==400
    assert client.post('/models/no/designate',data={'csrf':csrf(page)}).status_code==400
    assert client.get('/models/no').status_code==404


def test_budget_actually_stops_a_child_process(models):
    import subprocess
    import sys
    register(models,spec())
    real_run=subprocess.run
    def sleeping_worker(command,**kwargs):
        return real_run([sys.executable,'-c','import time; time.sleep(20)'],**kwargs)
    with patch('market_system.model_management.experiments.subprocess.run',side_effect=sleeping_worker):
        identity=run(models,'m1',[1],1,1,OWNER)
    assert models.experiments(OWNER,identity)['state']=='failed'
    assert 'бюджет' in models.experiments(OWNER,identity)['error']


def test_bounded_success_persists_metrics_and_does_not_create_catalog(models):
    from pathlib import Path
    import subprocess
    register(models,spec())
    def small_worker(command,**kwargs):
        request=json.loads(Path(command[-2]).read_text(encoding='utf-8'))
        with connect(models.path) as con:
            for item in request['inputs']:
                item['text']=con.execute('SELECT text FROM contents WHERE id=?',(item['content_id'],)).fetchone()[0]
        class Predictor:
            def predict(self,texts): return analyzer(request['spec'])(texts)
        Path(command[-1]).write_text(encode(evaluate(request['spec'],request['inputs'],Predictor())),encoding='utf-8')
        return subprocess.CompletedProcess(command,0)
    with patch('market_system.model_management.experiments.subprocess.run',side_effect=small_worker):
        identity=run(models,'m1',[1,3],2,5,OWNER)
    result=models.experiments(OWNER,identity)
    assert result['state']=='succeeded'
    metrics=json.loads(result['metrics_json'])
    assert metrics['precision_recall']['vacancy']['precision']==0.5
    assert metrics['coverage']==1 and len(metrics['errors'])==1
    assert active(models) is None and models.results(OWNER)==[]


def test_dictionary_list_split_merge_and_same_name_semantics_create_new_definitions(tmp_path):
    skill=tmp_path/'skills.json'; occupations=tmp_path/'occupations.json'
    occupations.write_text('{"version":"1","occupations":[]}',encoding='utf-8')
    value=spec(); value['paths']={'skill_lexicon':str(skill),'occupation_lexicon':str(occupations)}
    spaces=[]
    for entries in ([{'id':'one','name':'Same name','definition':'meaning one'}],
                    [{'id':'one','name':'Same name','definition':'meaning two'}],
                    [{'id':'part-a','name':'Same name','definition':'part one'}, {'id':'part-b','name':'Same name','definition':'part two'}],
                    [{'id':'merged','name':'Same name','definition':'union'}]):
        skill.write_text(encode({'version':'1','skills':entries}),encoding='utf-8')
        value['files'][str(skill)]=hashlib.sha256(skill.read_bytes()).hexdigest()
        spaces.append(class_space(value))
    assert len({s[-1]['namespace'] for s in spaces})==4
    assert len(spaces[2])==4 and spaces[0][-1]['name']==spaces[1][-1]['name']


def test_compatibility_checks_before_deserialization(tmp_path):
    import importlib.metadata
    from market_system.model_management.adapters import load
    folder=tmp_path/'bundle'; folder.mkdir()
    meta={'model_version':'local-test','ensemble':'arithmetic','weight_lr':0.2,'weight_cb':0.8,
          'vacancy_threshold':0.8,'resume_threshold':0.8,'max_other_probability':0.3,
          'decision_policy':'strict_target_threshold_and_low_other_else_null',
          'packages':{p:importlib.metadata.version(p) for p in ('numpy','pandas','joblib','scikit-learn','catboost')},
          'artifacts_sha256':{}}
    for name in ('linear.joblib','vacancy.cbm','resume.cbm'):
        (folder/name).write_bytes(b'incompatible serialized format')
        meta['artifacts_sha256'][name]=hashlib.sha256((folder/name).read_bytes()).hexdigest()
    (folder/'metadata.json').write_text(encode(meta),encoding='utf-8')
    value={'model_version':'local-test','model_manifest':meta,'paths':{'model':str(folder)},
           'files':{str(folder/k):v for k,v in meta['artifacts_sha256'].items()}}
    value['files'][str(folder/'metadata.json')]=hashlib.sha256((folder/'metadata.json').read_bytes()).hexdigest()
    value['fingerprint']=digest(value['files'])
    with patch('market_system.model_management.adapters.importlib.metadata.version',return_value='incompatible'):
        with pytest.raises(IncompatibleArtifact,match='зависимость'): verify(value)
    with pytest.raises(IncompatibleArtifact,match='загрузку'): load(value)
    (folder/'linear.joblib').write_bytes(b'changed')
    with patch('market_system.labor_market.model.Ensemble.load') as loader:
        with pytest.raises(IncompatibleArtifact,match='артефакт'): load(value)
        loader.assert_not_called()


def test_incremental_and_collection_use_active_registry_model(models):
    from test_collection import add, publication, feed, run as collect
    register(models,spec())
    job=designate(models,'m1'); execute(models)
    models.publish(models.jobs.get(job)['result_set_id'],OWNER)
    source_store=models.analytics.bot.review.sources
    source=add(source_store)
    source_store.enqueue(source,'history')
    collect(source_store,feed([publication('new real-shaped fixture vacancy')]))
    with patch('market_system.collection.store.specification',side_effect=AssertionError('old runtime used')):
        processing=source_store.dispatch(corrections={})
    assert processing
    request=json.loads(models.jobs.get(processing)['parameters_json'])['request']
    assert request['spec']['model_version']=='m1'
    execute(models)
    assert models.jobs.get(processing)['state']=='succeeded'
    with patch('market_system.jobs.store.specification',side_effect=AssertionError('old runtime used')):
        next_job=models.jobs.submit('full',corrections={})
    assert json.loads(models.jobs.get(next_job)['parameters_json'])['request']['spec']['model_version']=='m1'
