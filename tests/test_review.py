from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from market_system.review.store import Review, Actor
from market_system.review.import_legacy import import_legacy
from market_system.jobs.worker import Worker
from market_system.search import Catalog, SearchFilter
from market_system.search.store import connect, activate
from test_jobs import jobs, SPEC, analyze

OWNER=Actor('owner:1','ADMIN')
USER=Actor('reader:2','USER')


@pytest.fixture
def review(jobs):
    service=Review(jobs.path)
    service.initialize()
    identity=jobs.submit('full',spec=SPEC,corrections={})
    Worker(jobs,analyzer_factory=lambda _:analyze).run(once=True)
    activate(jobs.path,jobs.get(identity)['result_set_id'])
    return service


def active(review):
    with connect(review.path) as con:
        return con.execute('SELECT result_set_id FROM active_result_set').fetchone()[0]


def content(review,cid=1):
    return Catalog(review.path).get(cid,active(review))


def value(review,cid=1):
    return deepcopy(review.get(cid,OWNER)['annotation'])


def test_draft_uncertain_and_competing_revisions(review):
    old=active(review)
    annotation=value(review);annotation['skills']=[]
    def save():
        try:
            return review.save(1,0,'draft',annotation,OWNER)['revision']
        except ValueError:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:save(),range(2)))
    assert sorted(map(str,results))==['1','conflict']
    assert active(review)==old
    assert any(c['role']=='required' for c in content(review)['classes'])
    review.save(1,1,'uncertain',annotation,OWNER)
    assert active(review)==old
    assert review.queue('uncertain',OWNER)


def test_confirmation_replaces_links_preserves_machine_history_and_old_snapshot(review):
    old=active(review)
    before=Catalog(review.path).get(1,old)
    annotation=value(review)
    annotation['post_type']='resume';annotation['skills']=[]
    result=review.save(1,0,'confirmed',annotation,OWNER,old)
    assert result['result_set_id']!=old
    after=content(review)
    assert after['post_type']=='resume'
    assert not [c for c in after['classes'] if c['namespace'].startswith('skill:')]
    assert after['human_review']['revision']==1
    assert after['evidence']==before['evidence']
    assert Catalog(review.path).get(1,old)==before
    assert Catalog(review.path).search(SearchFilter(skill_ids=(before['classes'][1]['class_id'],)),result_set_id=old)
    # Later drafts do not remove the confirmation.
    annotation['post_type']='vacancy'
    review.save(1,1,'draft',annotation,OWNER)
    assert content(review)['post_type']=='resume'


def test_term_confirmation_separate_and_no_name_merge(review):
    one=review.propose(1,'skill','new_skill','Python','A separately defined concept',OWNER)
    two=review.propose(1,'skill','new_skill','Python','Another separately defined concept',OWNER)
    annotation=value(review)
    annotation['pending_terms']=[{'term_id':one,'role':'preferred'}]
    review.save(1,0,'draft',annotation,OWNER)
    with pytest.raises(ValueError,match='отдельно'):
        review.save(1,1,'confirmed',annotation,OWNER)
    first=review.approve(one,OWNER);second=review.approve(two,OWNER)
    assert first!=second
    assert review.approve(one,OWNER)==first
    review.save(1,1,'confirmed',annotation,OWNER)
    assert any(c['class_id']==first and c['role']=='preferred' for c in content(review)['classes'])
    assert not any(c['class_id']==second for c in content(review)['classes'])
    assert len([c for c in review.get(1,OWNER)['classes'] if c['name']=='Python'])>=3


def test_added_occupation_salary_conditions_and_multiple_guard(review):
    term=review.propose(1,'occupation','designer','Designer','Confirmed profession definition',OWNER)
    occupation=review.approve(term,OWNER)
    annotation=value(review)
    annotation.update(occupations=[occupation],salary=[{'salary_from':100,'salary_to':200,'currency':'RUB','salary_period':'month','tax_mode':'net'}])
    annotation['conditions']['work_format']=['hybrid']
    review.save(1,0,'confirmed',annotation,OWNER)
    assert content(review)['salary_from']==100
    assert content(review)['work_format']=='hybrid'
    with connect(review.path) as con:
        assert con.execute('SELECT count(*) FROM salary_occupation_facts WHERE result_set_id=?',(active(review),)).fetchone()[0]==1
    annotation['multiple_offers']=True
    review.save(1,1,'confirmed',annotation,OWNER)
    card=content(review)
    assert card['salary_from'] is None and card['salary_scope']=='unattributed'
    assert card['human_review']['multiple_offers']==1
    with connect(review.path) as con:
        assert not con.execute('SELECT * FROM salary_occupation_facts WHERE result_set_id=?',(active(review),)).fetchall()
    assert review.queue('multiple',OWNER)[0]['id']==1
    assert review.queue('ambiguous',OWNER)[0]['id']==1


def test_processing_submitted_before_confirmation_and_model_change_reapply(review):
    queued=review.jobs.submit('full',spec=SPEC,corrections={})
    annotation=value(review);annotation['skills']=[];annotation['post_type']='resume'
    review.save(1,0,'confirmed',annotation,OWNER)
    Worker(review.jobs,analyzer_factory=lambda _:analyze).run(once=True)
    rid=review.jobs.get(queued)['result_set_id']
    assert Catalog(review.path).get(1,rid)['post_type']=='resume'
    new_spec={**SPEC,'model_version':'new-model','model_manifest':{'new':True},'fingerprint':'new'}
    new=review.jobs.submit('full',spec=new_spec,corrections={})
    def new_analyze(texts):
        return [{**r,'model_version':'new-model'} for r in analyze(texts)]
    Worker(review.jobs,analyzer_factory=lambda _:new_analyze).run(once=True)
    activate(review.path,review.jobs.get(new)['result_set_id'])
    card=content(review)
    assert card['post_type']=='resume' and card['version']['model_version']=='new-model'
    assert card['human_review']['revision']==1
    assert not [c for c in card['classes'] if c['namespace'].startswith('skill:')]
    assert card['evidence']['classification']['label']=='vacancy'


def test_activation_of_old_ready_set_reapplies_current_confirmation(review):
    old=active(review)
    annotation=value(review);annotation['post_type']='trash';annotation['skills']=[]
    review.save(1,0,'confirmed',annotation,OWNER)
    activate(review.path,old)
    assert active(review)!=old
    assert content(review)['post_type'] is None
    assert Catalog(review.path).get(1,old)['post_type']=='vacancy'


def test_confirmation_write_failure_rolls_back_revision_and_active_links(review):
    old=active(review)
    annotation=value(review);annotation['skills']=[]
    with connect(review.path) as con:
        con.execute("CREATE TRIGGER failure BEFORE INSERT ON result_review_applications BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        con.commit()
    with pytest.raises(sqlite3.IntegrityError,match='injected'):
        review.save(1,0,'confirmed',annotation,OWNER)
    assert review.get(1,OWNER)['revision']==0
    assert active(review)==old
    assert any(c['role']=='required' for c in content(review)['classes'])


def test_owner_only_audit_queue_and_unknown_ids(review):
    for action in (lambda:review.get(1,USER),lambda:review.queue('audit',USER),lambda:review.audit(USER),
                   lambda:review.save(1,0,'draft',value(review),USER),
                   lambda:review.propose(1,'skill','x','x','x',USER)):
        with pytest.raises(PermissionError):
            action()
    assert review.audit(OWNER,2)==2
    first=review.queue('audit',OWNER)
    assert len(first)==2
    assert review.queue('audit',OWNER)==first
    annotation=value(review);annotation['occupations']=[999999]
    with pytest.raises(ValueError,match='Неизвестный класс'):
        review.save(1,0,'confirmed',annotation,OWNER)


def legacy_fixture(tmp_path):
    dataset={'dataset_id':'research-fixture','items':[{'id':'h1','cohort':'development','group':'a'},
        {'id':'h2','cohort':'test','group':'b'},{'id':'h3','cohort':'challenge','group':'c'}],
        'skills':[{'id':'python','name':'Python'}],'occupations':[],'scope':'test fixture'}
    dp=tmp_path/'dataset.json';dp.write_text(json.dumps(dataset),encoding='utf-8')
    annotation={'post_type':'resume','skills':[],'occupations':[],'salary':[],
                'conditions':{key:[] for key in ('work_format','schedule','employment_type','working_hours','hours_per_week')},'comment':'human'}
    ap=tmp_path/'annotations.sqlite'
    with sqlite3.connect(ap) as con:
        con.execute('CREATE TABLE revisions(id,revision,dataset,status,annotation,saved_at)')
        con.executemany('INSERT INTO revisions VALUES(?,?,?,?,?,?)',[
            ('h1',1,'research-fixture','draft',json.dumps(annotation),'2026-01-01'),
            ('h2',1,'research-fixture','confirmed',json.dumps(annotation),'2026-01-02')])
    model=tmp_path/'model';model.mkdir()
    (model/'split.csv').write_text('text_hash,label,split\nh1,vacancy,train\nh2,resume,test\nh3,vacancy,train\n',encoding='utf-8')
    labels=tmp_path/'labels.csv';labels.write_text('text_hash,label\nh3,vacancy\n',encoding='utf-8')
    return {'dataset':dp,'annotation_database':ap,'model':model},[labels]


def test_import_keeps_test_provenance_and_type_labels_not_extraction_confirmation(review,tmp_path):
    paths,files=legacy_fixture(tmp_path)
    originals={p:p.read_bytes() for p in [paths['dataset'],paths['annotation_database'],paths['model']/'split.csv',*files]}
    result=import_legacy(review,OWNER,paths,files)
    assert result['source_revisions']==2
    assert review.get(1,OWNER)['history'][0]['status']=='draft'
    test_document=review.get(2,OWNER)
    assert test_document['history'][0]['status']=='confirmed'
    assert test_document['history'][0]['author'] is None
    assert {m['cohort'] for m in test_document['memberships']}=={'test'}
    assert review.get(3,OWNER)['history']==[]
    assert review.get(3,OWNER)['type_labels']
    assert not content(review,3).get('human_review')
    old=active(review)
    import_legacy(review,OWNER,paths,files)
    assert active(review)==old
    with connect(review.path) as con:
        assert not con.execute('SELECT * FROM review_training_candidates WHERE content_id=2').fetchall()
        assert not con.execute('SELECT * FROM review_training_candidates WHERE content_id=3').fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE review_memberships SET cohort='train' WHERE content_id=2")
    assert all(p.read_bytes()==old for p,old in originals.items())


def test_nan_and_stale_active_and_immutable_review_snapshots(review):
    annotation=value(review)
    annotation['salary']=[dict(salary_from=float('nan'),salary_to=None,currency=None,salary_period=None,tax_mode=None)]
    with pytest.raises(ValueError):
        review.save(1,0,'confirmed',annotation,OWNER)
    annotation=value(review)
    with pytest.raises(ValueError,match='каталог изменился'):
        review.save(1,0,'confirmed',annotation,OWNER,'stale')
    review.save(1,0,'confirmed',annotation,OWNER)
    with connect(review.path) as con:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute('DELETE FROM review_revisions')
        with pytest.raises(sqlite3.IntegrityError):
            con.execute('DELETE FROM result_review_applications')


def test_confirmed_trash_exits_uncertain_queue_and_coverage_is_current(review):
    annotation=value(review)
    annotation['post_type']='trash';annotation['skills']=[]
    review.save(1,0,'confirmed',annotation,OWNER)
    assert 1 not in [r['id'] for r in review.queue('uncertain',OWNER)]
    with connect(review.path) as con:
        coverage=json.loads(con.execute('SELECT coverage_json FROM result_sets WHERE result_set_id=?',(active(review),)).fetchone()[0])
        assert coverage['no_classes']==2
