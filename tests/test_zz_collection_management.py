"""Autonomous tests for collection jobs and retention."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from bot.config import Settings
from bot.miniapp.app import create_app
from database import collection
from database.connection import connect
from database.repository import Repository
from database.search import _query, normalize
from parsers.storage import store_batch
from test_product import FakeInference, _csrf, _init_data


def source():
    repo = Repository(); repo.bootstrap_owners({42})
    address = 'https://t.me/test_'+uuid.uuid4().hex[:14]
    identity = repo.create_source(42,'telegram','Тестовый канал',address,3600,True)
    return repo,identity,address


def test_collection_queue_and_scheduler():
    repo,identity,address = source()
    with pytest.raises(PermissionError): collection.enqueue(99999,identity,'history')
    job = collection.enqueue(42,identity,'new')
    with pytest.raises(ValueError): collection.enqueue(42,identity,'history')
    collection.cancel(42,job)
    with connect() as con:
        assert con.execute('SELECT status FROM collection_jobs WHERE id=%s',(job,)).fetchone()['status'] == 'cancelled'
        con.execute("UPDATE sources SET next_run_at=now()+interval '1 day' WHERE id<>%s AND schedule_enabled",(identity,))
        con.execute("UPDATE sources SET cursor=%s,schedule_enabled=true,next_run_at=now()-interval '1 minute' WHERE id=%s",(Jsonb({'high_water':10}),identity))
    assert collection.enqueue_due() >= 1
    with connect() as con:
        queued = con.execute("SELECT * FROM collection_jobs WHERE source_id=%s AND status='queued'",(identity,)).fetchone()
        assert queued and queued['mode'] == 'history'
        assert con.execute('SELECT next_run_at>now() AS future FROM sources WHERE id=%s',(identity,)).fetchone()['future']
    assert collection.enqueue_due() == 0
    collection.cancel(42,queued['id'])
    collection.heartbeat()
    assert collection.list_jobs(42)[1]


def test_deleted_or_changed_source_never_starts():
    repo,identity,address = source()
    job = collection.enqueue(42,identity,'history')
    original = next(s for s in repo.list_sources(42) if s['id']==identity)
    repo.archive_source(42,identity,original['revision'])
    assert collection.claim_job() == {'skipped':True}
    with connect() as con:
        row=con.execute('SELECT status FROM collection_jobs WHERE id=%s',(job,)).fetchone()
        assert row['status']=='failed'
        assert con.execute('SELECT count(*) AS n FROM parser_runs WHERE source_id=%s',(identity,)).fetchone()['n']==0


def test_cancel_running_collection_between_batches(monkeypatch):
    import parsers.__main__ as runner
    repo,identity,address = source()
    # Keep retention disabled in this test.
    settings = collection.retention_settings(42)
    collection.save_retention(42,False,30,settings['revision'])
    job = collection.enqueue(42,identity,'history')
    claimed=collection.claim_job()
    assert claimed['id']==job
    now=datetime.now(timezone.utc)
    def batch(i):
        return SimpleNamespace(publications=[SimpleNamespace(url=address+'/'+str(i),text=f'fixture {address} {i}',published_at=now)],cursor={'source':{'high_water':i}})
    async def fake_batches(source,mode):
        yield batch(1)
        collection.cancel(42,job)
        yield batch(2)
    monkeypatch.setattr(runner,'batches',fake_batches)
    success=asyncio.run(runner.run_source(claimed['source'],'history',FakeInference(),job))
    collection.finish_job(job,success,'test error')
    with connect() as con:
        row=con.execute('SELECT status,parser_run_id FROM collection_jobs WHERE id=%s',(job,)).fetchone()
        assert not success and row['status']=='cancelled'
        assert con.execute('SELECT count(*) AS n FROM publications WHERE source_id=%s',(identity,)).fetchone()['n']==0
        assert con.execute('SELECT count(*) AS n FROM collection_staged_publications WHERE run_id=%s',(row['parser_run_id'],)).fetchone()['n']==0


def test_collection_publishes_only_after_all_batches_succeed(monkeypatch):
    import parsers.__main__ as runner
    repo,identity,address=source()
    job=collection.enqueue(42,identity)
    claimed=collection.claim_job()
    assert claimed['id']==job
    now=datetime.now(timezone.utc)
    with connect() as con:
        original=con.execute('SELECT result_set_id FROM active_result_set WHERE singleton=true').fetchone()
    async def fake_batches(source,mode):
        for i in (1,2):
            yield SimpleNamespace(publications=[SimpleNamespace(url=address+'/'+str(i),text='same text '+address,published_at=now)],cursor={'source':{'high_water':i}})
            with connect() as con:
                assert con.execute('SELECT count(*) AS n FROM publications WHERE source_id=%s',(identity,)).fetchone()['n']==0
                assert con.execute('SELECT result_set_id FROM active_result_set WHERE singleton=true').fetchone()==original
    monkeypatch.setattr(runner,'batches',fake_batches)
    assert asyncio.run(runner.run_source(claimed['source'],'history',FakeInference(),job))
    with connect() as con:
        assert con.execute('SELECT count(*) AS n FROM publications WHERE source_id=%s',(identity,)).fetchone()['n']==2
        assert con.execute('SELECT count(DISTINCT current_content_id) AS n FROM publications WHERE source_id=%s',(identity,)).fetchone()['n']==1
        assert con.execute('SELECT status FROM collection_jobs WHERE id=%s',(job,)).fetchone()['status']=='succeeded'
        assert con.execute('SELECT count(*) AS n FROM collection_staged_publications').fetchone()['n']==0


def test_collection_error_does_not_publish_partial_data(monkeypatch):
    import parsers.__main__ as runner
    repo,identity,address=source()
    job=collection.enqueue(42,identity)
    claimed=collection.claim_job()
    assert claimed['id']==job
    with connect() as con:
        original=con.execute('SELECT result_set_id FROM active_result_set WHERE singleton=true').fetchone()
    async def fake_batches(source,mode):
        yield SimpleNamespace(publications=[SimpleNamespace(url=address+'/1',text='partial '+address,published_at=datetime.now(timezone.utc))],cursor={'source':{'high_water':1}})
        raise TimeoutError('fixture')
    monkeypatch.setattr(runner,'batches',fake_batches)
    assert not asyncio.run(runner.run_source(claimed['source'],'history',FakeInference(),job))
    collection.finish_job(job,False,'timeout')
    with connect() as con:
        assert con.execute('SELECT count(*) AS n FROM publications WHERE source_id=%s',(identity,)).fetchone()['n']==0
        assert con.execute('SELECT result_set_id FROM active_result_set WHERE singleton=true').fetchone()==original
        assert con.execute('SELECT count(*) AS n FROM collection_staged_publications').fetchone()['n']==0


def test_retention_preserves_saved_unknown_recent_and_shared_content():
    repo,identity,address=source()
    run=uuid.uuid4(); now=datetime.now(timezone.utc); old=now-timedelta(days=60)
    with connect() as con:
        source_row=con.execute('SELECT * FROM sources WHERE id=%s',(identity,)).fetchone()
        con.execute("INSERT INTO parser_runs(id,source_id,state) VALUES (%s,%s,'running')",(run,identity))
    suffix=uuid.uuid4().hex
    values=[('old',old,'old '+suffix),('saved',old,'saved '+suffix),('fresh',now,'fresh '+suffix),
            ('unknown',None,'unknown '+suffix),('shared_old',old,'shared '+suffix),('shared_fresh',now,'shared '+suffix),
            ('corrected',old,'corrected '+suffix)]
    publications=[SimpleNamespace(url=address+'/'+key,text=text,published_at=stamp) for key,stamp,text in values]
    store_batch(source_row,run,SimpleNamespace(publications=publications,cursor={'source':{'high_water':7}}),FakeInference())
    with connect() as con:
        ids={r['external_key'].rsplit('/',1)[-1]:r['current_content_id'] for r in con.execute('SELECT external_key,current_content_id FROM publications WHERE source_id=%s',(identity,)).fetchall()}
        result_id=con.execute('SELECT result_set_id FROM active_result_set').fetchone()['result_set_id']
        con.execute('INSERT INTO clients(telegram_id) VALUES (819000) ON CONFLICT DO NOTHING')
        con.execute('INSERT INTO client_vacancies(telegram_id,content_id,saved) VALUES (%s,%s,true)',(819000,ids['saved']))
        con.execute('INSERT INTO corrections(content_id,revision,author_id,annotation) VALUES (%s,1,42,%s)',(ids['corrected'],Jsonb({'fixture':True})))
        sel=uuid.uuid4()
        con.execute("INSERT INTO selections(id,telegram_id,result_set_id,filter_json,trial,status) VALUES (%s,%s,%s,%s,true,'delivered')",(sel,819000,result_id,Jsonb({})))
        for ordinal,key in enumerate(('old','saved'),1):
            con.execute('INSERT INTO selection_items(selection_id,ordinal,result_set_id,content_id) VALUES (%s,%s,%s,%s)',(sel,ordinal,result_id,ids[key]))
    assert collection.cleanup() is None
    settings=collection.retention_settings(42)
    collection.save_retention(42,True,30,settings['revision'])
    result=collection.cleanup()
    assert result['publications']>=2 and result['contents']>=1
    with connect() as con:
        remaining={r['external_key'].rsplit('/',1)[-1] for r in con.execute('SELECT external_key FROM publications WHERE source_id=%s',(identity,)).fetchall()}
        assert remaining=={'saved','fresh','unknown','shared_fresh','corrected'}
        assert con.execute('SELECT 1 FROM contents WHERE id=%s',(ids['old'],)).fetchone() is None
        assert con.execute('SELECT 1 FROM contents WHERE id=%s',(ids['saved'],)).fetchone()
        assert con.execute('SELECT 1 FROM contents WHERE id=%s',(ids['shared_fresh'],)).fetchone()
        assert con.execute('SELECT count(*) AS n FROM selection_items WHERE selection_id=%s',(sel,)).fetchone()['n']==1
        assert con.execute('SELECT count(*) AS n FROM selections WHERE id=%s AND trial',(sel,)).fetchone()['n']==1
        found,_=_query(con,result_id,normalize({}),0,100)
        search_ids={row['id'] for row in found}
        assert ids['saved'] not in search_ids and ids['unknown'] in search_ids and ids['fresh'] in search_ids
        assert con.execute('SELECT last_error FROM retention_status').fetchone()['last_error'] is None
    settings=collection.retention_settings(42)
    collection.save_retention(42,False,30,settings['revision'])


def test_admin_collection_and_retention_routes():
    repo,identity,address=source()
    app=create_app(Settings('offline-fixture-token',frozenset({42}),None,('testserver',)))
    with TestClient(app,base_url='https://testserver') as client:
        assert client.post(f'/sources/{identity}/collect',data={'csrf':'bad','mode':'history'}).status_code==401
        page=client.get('/miniapp')
        assert client.post('/miniapp',data={'csrf':_csrf(page),'init_data':_init_data('offline-fixture-token',42)},follow_redirects=False).status_code==303
        page=client.get('/sources'); csrf=_csrf(page)
        assert 'Управление сбором' in page.text and 'Удаление старых публикаций' in page.text
        assert client.post(f'/sources/{identity}/collect',data={'csrf':'bad','mode':'history'}).status_code==403
        assert client.post(f'/sources/{identity}/collect',data={'csrf':csrf,'mode':'history'},follow_redirects=False).status_code==303
        jobs,_=collection.list_jobs(42); job=next(j for j in jobs if j['source_id']==identity and j['status']=='queued')
        assert client.post(f"/collection/{job['id']}/cancel",data={'csrf':csrf},follow_redirects=False).status_code==303
        settings=collection.retention_settings(42)
        assert client.post('/retention',data={'csrf':csrf,'days':'60','revision':str(settings['revision'])},follow_redirects=False).status_code==303
        assert collection.retention_settings(42)['retention_days']==60


def test_bulk_collection_uses_same_queue_and_rejects_non_owner():
    repo,identity,address=source()
    with pytest.raises(PermissionError):
        collection.enqueue_all(99999,'history')
    created=collection.enqueue_all(42,'history')
    assert created >= 1
    with connect() as con:
        rows=con.execute("SELECT id,source_id FROM collection_jobs WHERE status='queued' AND requested_by=42").fetchall()
    assert any(row['source_id']==identity for row in rows)
    for row in rows:
        collection.cancel(42,row['id'])


def test_successful_collection_triggers_retention(monkeypatch):
    import parsers.__main__ as runner
    repo,identity,address=source()
    settings=collection.retention_settings(42)
    collection.save_retention(42,True,30,settings['revision'])
    job=collection.enqueue(42,identity,'history')
    claimed=collection.claim_job()
    assert claimed['id']==job
    now=datetime.now(timezone.utc)
    old=SimpleNamespace(url=address+'/old',text='old fixture '+address,published_at=now-timedelta(days=60))
    fresh=SimpleNamespace(url=address+'/new',text='new fixture '+address,published_at=now)
    async def fake_batches(source,mode):
        yield SimpleNamespace(publications=[old,fresh],cursor={'source':{'high_water':2}})
    monkeypatch.setattr(runner,'batches',fake_batches)
    success=asyncio.run(runner.run_source(claimed['source'],'history',FakeInference(),job))
    collection.finish_job(job,success)
    with connect() as con:
        row=con.execute('''SELECT j.status,r.inserted_count FROM collection_jobs j
          JOIN parser_runs r ON r.id=j.parser_run_id WHERE j.id=%s''',(job,)).fetchone()
        assert success and row['status']=='succeeded' and row['inserted_count']==2
        urls=[r['url'] for r in con.execute('SELECT url FROM publications WHERE source_id=%s',(identity,)).fetchall()]
        assert urls==[address+'/new']
    settings=collection.retention_settings(42)
    collection.save_retention(42,False,30,settings['revision'])


def test_advertising_is_hidden_without_erasing_existing_configuration():
    from bot.telegram import home_keyboard
    from database.client import ClientRepository
    Repository().bootstrap_owners({42})
    client=ClientRepository()
    client.save_product_settings(42,5,'Тариф','Сохранённый текст','https://example.test/service')
    client.save_product_settings(42,5,'Новый тариф')
    assert client.settings()['services_text']=='Сохранённый текст'
    assert all('услуги' not in button.text.lower() for row in home_keyboard().inline_keyboard for button in row)


def test_interrupted_worker_recovers_finished_and_unfinished_jobs():
    repo,first,_=source()
    _,second,_=source()
    finished=collection.enqueue(42,first,'history')
    unfinished=collection.enqueue(42,second,'history')
    assert collection.claim_job()['id']==finished
    assert collection.claim_job()['id']==unfinished
    with connect() as con:
        first_run,second_run=uuid.uuid4(),uuid.uuid4()
        con.execute("INSERT INTO parser_runs(id,source_id,state,finished_at) VALUES (%s,%s,'succeeded',now())",(first_run,first))
        con.execute("INSERT INTO parser_runs(id,source_id,state) VALUES (%s,%s,'running')",(second_run,second))
        con.execute('UPDATE collection_jobs SET parser_run_id=%s WHERE id=%s',(first_run,finished))
        con.execute('UPDATE collection_jobs SET parser_run_id=%s WHERE id=%s',(second_run,unfinished))
    collection.recover_interrupted()
    with connect() as con:
        statuses={row['id']:row['status'] for row in con.execute('SELECT id,status FROM collection_jobs WHERE id=ANY(%s)',([finished,unfinished],)).fetchall()}
        assert statuses[finished]=='succeeded' and statuses[unfinished]=='failed'


def test_freshness_setting_reaches_parser_when_cleanup_is_disabled(monkeypatch):
    import parsers.__main__ as runner
    repo, identity, address = source()
    settings = collection.retention_settings(42)
    original_enabled, original_days = settings['retention_enabled'], settings['retention_days']
    collection.save_retention(42, False, 10, settings['revision'])
    captured = {}
    async def fake_batches(source_row, mode):
        captured.update(source_row)
        yield SimpleNamespace(publications=[], cursor={'source': {'high_water': 0}})
    monkeypatch.setattr(runner, 'batches', fake_batches)
    try:
        with connect() as con:
            row = con.execute('SELECT * FROM sources WHERE id=%s', (identity,)).fetchone()
        before = datetime.now(timezone.utc)
        assert asyncio.run(runner.run_source(row, 'history', FakeInference()))
        cutoff = datetime.fromisoformat(captured['freshness_cutoff'])
        assert before-timedelta(days=10, seconds=5) <= cutoff <= datetime.now(timezone.utc)-timedelta(days=10)+timedelta(seconds=5)
        with connect() as con:
            config = con.execute('SELECT configuration FROM parser_runs WHERE source_id=%s ORDER BY started_at DESC LIMIT 1', (identity,)).fetchone()['configuration']
        assert config['freshness_days'] == 10
        assert config['freshness_cutoff'] == captured['freshness_cutoff']
    finally:
        current = collection.retention_settings(42)
        collection.save_retention(42, original_enabled, original_days, current['revision'])
