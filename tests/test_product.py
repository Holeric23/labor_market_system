import hashlib
import hmac
import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlencode

import psycopg
import pytest
from fastapi.testclient import TestClient
from bot.config import Settings
from bot.miniapp.app import create_app
from database.connection import connect
from database.migrate import migrate
from database.repository import Repository
from parsers.storage import store_batch


def test_migration_is_repeatable_and_owner_gate_is_fail_closed():
    migrate()
    repository = Repository()
    repository.bootstrap_owners({42})
    user = 700001
    identity = uuid.uuid4()
    repository.issue(42,user,30,identity)
    assert repository.authorize(user,{42}) is None
    with pytest.raises(PermissionError):
        repository.issue(999,user,30,uuid.uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: repository.activate(42,user,identity), range(2)))
    assert sorted(results) == [False,True]
    assert len([e for e in repository.client(42,user)['history'] if e['event_type']=='activated']) == 1
    assert repository.authorize(user,{42}) is None  # Paid checks remain a later stage.


def test_manual_grants_extension_settings_and_audit():
    repo = Repository()
    repo.bootstrap_owners({42})
    user = 700002
    first, second = uuid.uuid4(), uuid.uuid4()
    repo.issue(42,user,30,first)
    repo.activate(42,user,first)
    initial = repo.client(42,user)['access_until']
    repo.issue(42,user,7,second)
    repo.activate(42,user,second)
    after = repo.client(42,user)
    assert (after['access_until']-initial).days == 7
    repo.revert(42,user,second,after['revision'])
    with pytest.raises(ValueError):
        repo.revert(42,user,second,after['revision'])
    after = repo.client(42,user)
    assert after['access_until'] == initial
    repo.revoke(42,user,after['revision'])
    values = repo.public_settings()
    repo.save_settings(42,'https://t.me/manager_name',45,values['revision'])
    with pytest.raises(ValueError):
        repo.save_settings(42,'https://example.com',45,values['revision'])
    assert repo.public_settings()['default_access_days'] == 45


def test_parallel_different_grants_and_future_unbound_issue():
    repo = Repository()
    repo.bootstrap_owners({42})
    user = 700003
    first, second = uuid.uuid4(), uuid.uuid4()
    repo.issue(42,user,7,first)
    repo.issue(42,user,10,second)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda identity:repo.activate(42,user,identity), (first,second)))
    assert results == [True,True]
    client = repo.client(42,user)
    grants = sorted(client['grants'],key=lambda item:item['starts_at'])
    assert grants[1]['starts_at'] == grants[0]['ends_at']
    assert (grants[1]['ends_at']-grants[0]['starts_at']).days == 17
    assert client['access_until'] == grants[1]['ends_at']
    unbound = uuid.uuid4()
    with connect() as con:
        con.execute("INSERT INTO access_grants(id,issued_by,duration_days,status) VALUES (%s,42,30,'pending')",(unbound,))
        assert con.execute('SELECT recipient_id FROM access_grants WHERE id=%s',(unbound,)).fetchone()['recipient_id'] is None


def test_parser_configuration_does_not_start_jobs():
    repo = Repository()
    repo.bootstrap_owners({42})
    address = 'https://t.me/settings_'+uuid.uuid4().hex[:12]
    identity = repo.save_source(42,'telegram','Fixture',address,600,True,True,{'batch_size':2,'max_items':10})
    with connect() as con:
        row = con.execute('SELECT * FROM sources WHERE id=%s',(identity,)).fetchone()
        assert row['parameters']['batch_size'] == 2 and row['schedule_enabled']
        assert row['next_run_at'] is not None
        assert con.execute('SELECT count(*) AS n FROM parser_runs WHERE source_id=%s',(identity,)).fetchone()['n'] == 0
    with pytest.raises(ValueError):
        repo.save_source(42,'telegram','Fixture','https://example.com/other',600,True)
    with pytest.raises(ValueError):
        repo.save_source(42,'telegram','Fixture',address,600,True,True,{'batch_size':0})


class FakeInference:
    version = 'autonomous-fixture-model'
    manifest = {'fixture':True}
    pipeline = SimpleNamespace(extractor=SimpleNamespace(skills={'python':{'name':'Python'}}),
                               occupations=SimpleNamespace(entries={}))

    def analyze(self, text):
        return {'text':text,'label':'vacancy','salary_from':None,'salary_to':None,
                'currency':None,'salary_period':None,'work_format':None,'schedule':None,
                'employment_type':None,'skill_lexicon_sha256':'test','skill_lexicon_version':'v1',
                'occupation_lexicon_sha256':'test','occupation_lexicon_version':'v1',
                'occupation_ids':[],'required_skills':['python']}


def test_deduplication_versions_and_original_text_once():
    repo = Repository()
    repo.bootstrap_owners({42})
    address = 'https://t.me/fixture_'+uuid.uuid4().hex[:12]
    source_id = repo.save_source(42,'telegram','Fixture',address,3600,True)
    run_id = uuid.uuid4()
    with connect() as con:
        source = con.execute('SELECT * FROM sources WHERE id=%s',(source_id,)).fetchone()
        con.execute("INSERT INTO parser_runs(id,source_id,state) VALUES (%s,%s,'running')",(run_id,source_id))
    def post(url,text):
        return SimpleNamespace(url=url,text=text,published_at=datetime.now(timezone.utc))
    batch = SimpleNamespace(publications=[post(address+'/1','fixture vacancy one'),post(address+'/2','fixture vacancy one')],cursor={'source':{'high_water':2}})
    assert store_batch(source,run_id,batch,FakeInference()) == 1
    with connect() as con:
        old = con.execute('SELECT result_set_id FROM active_result_set').fetchone()['result_set_id']
        assert con.execute("SELECT count(*) AS n FROM contents WHERE text='fixture vacancy one'").fetchone()['n'] == 1
        assert con.execute('SELECT count(*) AS n FROM result_publications WHERE result_set_id=%s',(old,)).fetchone()['n'] == 2
    changed = SimpleNamespace(publications=[post(address+'/1','fixture vacancy revised')],cursor={'source':{'high_water':3}})
    assert store_batch(source,run_id,changed,FakeInference()) == 1
    with connect() as con:
        current = con.execute('SELECT result_set_id FROM active_result_set').fetchone()['result_set_id']
        assert current != old
        assert con.execute('SELECT count(*) AS n FROM publication_revisions WHERE publication_id=(SELECT id FROM publications WHERE external_key=%s)',(address+'/1',)).fetchone()['n'] == 2
        assert con.execute('SELECT count(*) AS n FROM result_publications WHERE result_set_id=%s',(old,)).fetchone()['n'] == 2
        with pytest.raises(psycopg.Error):
            con.execute('DELETE FROM document_results WHERE result_set_id=%s',(old,))
    assert any('fixture vacancy revised' in item['text'] for item in repo.search_vacancies())
    assert repo.activate_result_set(42,old)
    assert not repo.activate_result_set(42,old)
    assert repo.activate_result_set(42,current)
    from database.search import page
    with connect() as con:
        skill_id = con.execute("SELECT id FROM classes WHERE namespace='skill:test' AND class_key='python'").fetchone()['id']
    items, more = page(42,{'skill_ids':[skill_id],'skills_mode':'ALL'},page_size=1)
    assert len(items) == 1 and more
    next_items, more = page(42,next_page=True,page_size=1)
    assert len(next_items) == 1 and not more
    with pytest.raises(ValueError):
        page(42,next_page=True)
    with pytest.raises(ValueError):
        page(42,{'post_type':'resume'})
    with pytest.raises(ValueError):
        page(42,{'skill_ids':[999999999]})


def _csrf(page):
    return re.search(r'name="csrf" value="([^"]+)"',page.text)[1]


def _init_data(token, identity):
    values = {'auth_date':str(int(time.time())),'user':json.dumps({'id':identity}),'query_id':uuid.uuid4().hex}
    payload = '\n'.join(f'{k}={v}' for k,v in sorted(values.items()))
    secret = hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
    values['hash'] = hmac.new(secret,payload.encode(),hashlib.sha256).hexdigest()
    return urlencode(values)


def test_miniapp_signature_csrf_admin_routes_and_replay():
    token = 'offline-fixture-token'
    Repository().bootstrap_owners({42})
    app = create_app(Settings(token,frozenset({42}),None,('testserver',)))
    with TestClient(app,base_url='https://testserver') as client:
        assert client.get('/clients').status_code == 401
        page = client.get('/miniapp')
        raw = _init_data(token,42)
        assert client.post('/miniapp',data={'csrf':'wrong','init_data':raw}).status_code == 403
        assert client.post('/miniapp',data={'csrf':_csrf(page),'init_data':raw},follow_redirects=False).status_code == 303
        page = client.get('/clients')
        assert page.status_code == 200
        assert client.post('/clients/issue',data={'csrf':'wrong'}).status_code == 403
        assert client.get('/sources').status_code == 200
        assert client.get('/settings').status_code == 200
        assert client.get('/catalog').status_code == 404
        page = client.get('/miniapp')
        assert client.post('/miniapp',data={'csrf':_csrf(page),'init_data':raw}).status_code == 403
