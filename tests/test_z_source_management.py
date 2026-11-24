"""Source management with isolated PostgreSQL and a synthetic Telegram session."""
import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bot.config import Settings
from bot.miniapp.app import create_app
from database.connection import connect
from database.repository import Repository
from parsers.__main__ import run_source
from parsers.storage import store_batch
from test_product import FakeInference, _csrf, _init_data


def new_address():
    return 'https://t.me/test_'+uuid.uuid4().hex[:14]


def test_source_create_edit_delete_restore_and_preserve_history():
    repo = Repository(); repo.bootstrap_owners({42})
    address = new_address()
    identity = repo.create_source(42,'telegram','Канал',address,3600,True,True,{'batch_size':3})
    with connect() as con:
        source = con.execute('SELECT * FROM sources WHERE id=%s',(identity,)).fetchone()
        run = uuid.uuid4()
        con.execute("INSERT INTO parser_runs(id,source_id,state) VALUES (%s,%s,'running')",(run,identity))
    post = SimpleNamespace(url=address+'/1',text='Тестовая публикация '+address,published_at=datetime.now(timezone.utc))
    batch = SimpleNamespace(publications=[post],cursor={'source':{'high_water':1}})
    assert store_batch(source,run,batch,FakeInference()) == 1
    with pytest.raises(ValueError):
        repo.create_source(42,'telegram','Повтор',address.upper().replace('HTTPS://T.ME/','https://t.me/'),3600,True)
    with pytest.raises(PermissionError):
        repo.archive_source(999,identity,source['revision'])
    assert repo.edit_source(42,identity,source['revision'],'telegram','Новое имя',address,7200,True,False,{'batch_size':7}) == identity
    with pytest.raises(ValueError):
        repo.archive_source(42,identity,source['revision'])
    source = next(s for s in repo.list_sources(42) if s['id'] == identity)
    assert source['name'] == 'Новое имя' and source['parameters']['batch_size'] == 7
    repo.archive_source(42,identity,source['revision'])
    assert not any(s['id'] == identity for s in repo.list_sources(42))
    removed = next(s for s in repo.list_sources(42,archived=True) if s['id'] == identity)
    assert not removed['enabled'] and not removed['schedule_enabled'] and removed['next_run_at'] is None
    assert asyncio.run(run_source(source,'history',FakeInference())) is False
    with pytest.raises(ValueError):
        store_batch(source,run,batch,FakeInference())
    with connect() as con:
        assert con.execute('SELECT count(*) AS n FROM publications WHERE source_id=%s',(identity,)).fetchone()['n'] == 1
        assert con.execute('SELECT count(*) AS n FROM parser_runs WHERE source_id=%s',(identity,)).fetchone()['n'] == 1
    with pytest.raises(ValueError):
        repo.create_source(42,'telegram','Повтор',address,3600,True)
    repo.restore_source(42,identity,removed['revision'])
    restored = next(s for s in repo.list_sources(42) if s['id'] == identity)
    assert not restored['enabled'] and not restored['schedule_enabled']
    with connect() as con:
        events = con.execute('SELECT event_type FROM source_events WHERE source_id=%s ORDER BY id',(identity,)).fetchall()
    assert [e['event_type'] for e in events] == ['created','updated','deleted','restored']


def test_change_address_creates_new_source_without_reassigning_history():
    repo = Repository(); repo.bootstrap_owners({42})
    old_address,new = new_address(),new_address()
    identity = repo.create_source(42,'telegram','Старый',old_address,3600,True)
    old = next(s for s in repo.list_sources(42) if s['id'] == identity)
    occupied = repo.create_source(42,'telegram','Занят','https://t.me/test_'+uuid.uuid4().hex[:14],3600,True)
    occupied_row = next(s for s in repo.list_sources(42) if s['id'] == occupied)
    with pytest.raises(ValueError):
        repo.edit_source(42,identity,old['revision'],'telegram','Новый',occupied_row['address'],1800,False)
    assert any(s['id'] == identity for s in repo.list_sources(42))
    replacement = repo.edit_source(42,identity,old['revision'],'telegram','Новый',new,1800,False)
    assert replacement != identity
    assert any(s['id'] == identity and s['address'] == old_address for s in repo.list_sources(42,archived=True))
    assert any(s['id'] == replacement and s['address'] == new for s in repo.list_sources(42))
    with connect() as con:
        events = con.execute('SELECT event_type,details FROM source_events WHERE source_id=%s ORDER BY id',(identity,)).fetchall()
        assert events[-1]['event_type'] == 'replaced' and events[-1]['details']['replacement_source_id'] == replacement


def test_sources_panel_create_edit_delete_restore_and_security():
    repo = Repository(); repo.bootstrap_owners({42})
    settings = Settings('offline-fixture-token',frozenset({42}),None,('testserver',))
    app = create_app(settings)
    with TestClient(app,base_url='https://testserver') as client:
        assert client.get('/sources').status_code == 401
        assert client.post('/sources/1/delete',data={'csrf':'bad','revision':'1'}).status_code == 401
        page = client.get('/miniapp')
        assert client.post('/miniapp',data={'csrf':_csrf(page),'init_data':_init_data(settings.bot_token,42)},follow_redirects=False).status_code == 303
        page = client.get('/sources'); csrf = _csrf(page)
        assert page.status_code == 200 and 'Добавить источник' in page.text
        address = new_address()
        form = {'csrf':csrf,'kind':'telegram','name':'Канал <script>','address':address,'interval_seconds':'3600','enabled':'1','batch_size':'5'}
        assert client.post('/sources',data={**form,'csrf':'bad'}).status_code == 403
        assert client.post('/sources',data=form,follow_redirects=False).status_code == 303
        row = next(s for s in repo.list_sources(42) if s['address'] == address)
        page = client.get('/sources')
        assert 'Канал &lt;script&gt;' in page.text and 'Канал <script>' not in page.text
        form.update(name='Изменён',interval_seconds='600',revision=str(row['revision']))
        assert client.post(f"/sources/{row['id']}/edit",data=form,follow_redirects=False).status_code == 303
        changed = next(s for s in repo.list_sources(42) if s['id'] == row['id'])
        assert changed['name'] == 'Изменён' and changed['interval_seconds'] == 600
        assert client.post(f"/sources/{row['id']}/delete",data={'csrf':csrf,'revision':str(row['revision'])}).status_code == 400
        assert client.post(f"/sources/{row['id']}/delete",data={'csrf':'bad','revision':str(changed['revision'])}).status_code == 403
        assert client.post(f"/sources/{row['id']}/delete",data={'csrf':csrf,'revision':str(changed['revision'])},follow_redirects=False).status_code == 303
        removed = next(s for s in repo.list_sources(42,archived=True) if s['id'] == row['id'])
        assert 'Удалённые источники' in client.get('/sources').text
        assert client.post(f"/sources/{row['id']}/restore",data={'csrf':csrf,'revision':str(removed['revision'])},follow_redirects=False).status_code == 303
        restored = next(s for s in repo.list_sources(42) if s['id'] == row['id'])
        assert not restored['enabled']


def test_parser_telegram_login_panel_owner_csrf_and_secret_handling(monkeypatch):
    from bot.miniapp import parser_auth
    repo=Repository(); repo.bootstrap_owners({42})
    settings=Settings('offline-fixture-token',frozenset({42}),'https://testserver/miniapp',('testserver',))
    state={'stage':'idle'}; calls=[]
    def fake_request(action,actor,value=''):
        calls.append((action,actor,value))
        if action=='start': state['stage']='code'
        elif action=='code': state['stage']='password'
        elif action=='password': state['stage']='session_present'
        elif action=='cancel': state['stage']='idle'
        return {'ok':True,'stage':state['stage']}
    monkeypatch.setattr(parser_auth,'request',fake_request)
    app=create_app(settings)
    with TestClient(app,base_url='https://testserver') as client:
        assert client.get('/parser/telegram').status_code==401
        assert client.post('/parser/telegram/start',data={'csrf':'bad','value':'+79991234567'}).status_code==401
        login=client.get('/miniapp')
        assert client.post('/miniapp',data={'csrf':_csrf(login),'init_data':_init_data(settings.bot_token,42)},follow_redirects=False).status_code==303
        page=client.get('/parser/telegram'); csrf=_csrf(page)
        assert page.status_code==200 and 'Номер телефона' in page.text
        assert client.post('/parser/telegram/start',data={'csrf':'bad','value':'+79991234567'}).status_code==403
        result=client.post('/parser/telegram/start',data={'csrf':csrf,'value':'+79991234567'},follow_redirects=False)
        assert result.status_code==303 and '+7999' not in result.headers['location']
        page=client.get('/parser/telegram'); assert 'Код Telegram' in page.text and '+7999' not in page.text
        result=client.post('/parser/telegram/code',data={'csrf':csrf,'value':'12345'},follow_redirects=False)
        assert result.status_code==303 and '12345' not in result.headers['location']
        page=client.get('/parser/telegram'); assert 'Пароль двухэтапной защиты' in page.text
        result=client.post('/parser/telegram/password',data={'csrf':csrf,'value':'secret-password'},follow_redirects=False)
        assert result.status_code==303 and 'secret-password' not in result.headers['location']
        page=client.get('/parser/telegram'); assert 'сессии парсера сохранён' in page.text and 'secret-password' not in page.text
        assert all(actor==42 for _,actor,_ in calls)


def test_parser_login_refuses_unconfigured_origin(monkeypatch):
    from bot.miniapp import parser_auth
    repo=Repository(); repo.bootstrap_owners({42})
    monkeypatch.setattr(parser_auth,'request',lambda *args: {'ok':True,'stage':'idle'})
    settings=Settings('offline-fixture-token',frozenset({42}),None,('testserver',))
    with TestClient(create_app(settings),base_url='https://testserver') as client:
        login=client.get('/miniapp')
        assert client.post('/miniapp',data={'csrf':_csrf(login),'init_data':_init_data(settings.bot_token,42)},follow_redirects=False).status_code==303
        page=client.get('/parser/telegram'); csrf=_csrf(page)
        assert 'собственный HTTPS-адрес' in page.text and 'Номер телефона в международном формате' not in page.text
        assert client.post('/parser/telegram/start',data={'csrf':csrf,'value':'+79991234567'}).status_code==403
