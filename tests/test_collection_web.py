import json
from test_web import client, login, csrf
from test_search_service import catalog
from market_system.collection.store import Sources
from market_system.collection.runner import Collector
from test_collection import feed, publication


def test_owner_source_settings_launch_error_retry_and_schedule(client):
    token=csrf(login(client))
    values={'csrf':token,'kind':'telegram','name':'channel_one','address':'https://t.me/channel_one',
            'parameters':'{"batch_size":1}','enabled':'1','interval_seconds':'120'}
    response=client.post('/sources',data=values)
    assert response.status_code==200
    sources=Sources(client.app.state.catalog.path)
    source=sources.list()[0]
    assert source['schedule_enabled']==0
    source_id=source['source_id']
    assert client.post(f'/sources/{source_id}/collect',data={'csrf':token,'mode':'history'}).status_code==200
    identity=sources.history()[0]['job_id']
    assert client.post(f'/sources/{source_id}/collect',data={'csrf':token,'mode':'history'}).status_code==200
    assert len(sources.history())==1
    collector=Collector(sources,'test-owner',feed([publication()],fail_at=0))
    collector.execute(collector.claim())
    response=client.get('/sources')
    assert 'simulated source failure' in response.text
    assert client.post(f'/collection/{identity}/retry',data={'csrf':token}).status_code==200
    assert sources.job(identity)['state']=='queued'
    # Disabling cancels collection between batches and never runs a parser in HTTP.
    values.update(source_id=str(source_id),revision=str(source['revision']),enabled='0')
    assert client.post('/sources',data=values).status_code==200
    collector.execute(collector.claim())
    assert sources.job(identity)['state']=='cancelled'
    row=sources.get(source_id)
    values.update(revision=str(row['revision']),enabled='1',schedule_enabled='1')
    assert client.post('/sources',data=values).status_code==200
    assert sources.get(source_id)['schedule_enabled']==1
    assert sources.get(source_id)['interval_seconds']==120
    # Optimistic configuration writes reject a stale revision.
    assert client.post('/sources',data=values).status_code==400


def test_user_and_csrf_for_every_source_action(client):
    page=login(client,'reader')
    token=csrf(page)
    assert 'href="/sources"' not in page.text
    assert client.get('/sources').status_code==403
    routes=['/sources','/sources/1/collect','/collection/unknown/retry','/collection/dispatch']
    for route in routes:
        assert client.post(route,data={'csrf':token}).status_code==403
    client.post('/logout',data={'csrf':token})
    login(client)
    for route in routes:
        assert client.post(route,data={'csrf':'bad'}).status_code==403


def test_source_validation_and_error_escaping(client):
    token=csrf(login(client))
    source={'csrf':token,'kind':'telegram','name':'<script>alert(1)</script>',
            'address':'https://t.me/channel_one','parameters':'{}','enabled':'1','interval_seconds':'120'}
    response=client.post('/sources',data=source)
    assert response.status_code==200
    assert '<script>alert(1)</script>' not in response.text
    assert '&lt;script&gt;' in response.text
    source.update(name='different',parameters='{"api_hash":"secret"}')
    assert client.post('/sources',data=source).status_code==400
    source.update(parameters='{}',address='https://other.example/whatever')
    assert client.post('/sources',data=source).status_code==400
