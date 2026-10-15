from test_web import client, login, csrf
from test_search_service import catalog
from market_system.jobs.store import specification as real_spec
from market_system.jobs import Jobs


def test_owner_jobs_create_view_cancel_retry_without_model(client, monkeypatch):
    from market_system.jobs import store
    monkeypatch.setattr(store,'specification', lambda: {'model_version':'fixture','model_manifest':{},'fingerprint':'fixture'})
    monkeypatch.setattr(store,'confirmed_corrections',lambda spec:{})
    page=login(client)
    token=csrf(page)
    response=client.post('/jobs',data={'csrf':token,'kind':'sample','ids':'1','batch_size':'2'})
    assert response.status_code==200
    identity=response.url.path.split('/')[-1]
    assert 'queued' in response.text
    jobs=Jobs(client.app.state.catalog.path)
    assert jobs.get(identity)['state']=='queued'
    assert client.post(f'/jobs/{identity}/cancel',data={'csrf':token}).status_code==200
    assert jobs.get(identity)['state']=='cancelled'
    assert client.post(f'/jobs/{identity}/retry',data={'csrf':token}).status_code==200
    assert jobs.get(identity)['state']=='queued'
    assert client.get('/jobs').status_code==200
    for route in ['/jobs',f'/jobs/{identity}/cancel',f'/jobs/{identity}/retry']:
        assert client.post(route,data={'csrf':'invalid'}).status_code==403


def test_user_denied_jobs_actions(client):
    page=login(client,'reader')
    token=csrf(page)
    assert 'href="/jobs"' not in page.text
    for url in ['/jobs','/jobs/unknown']:
        assert client.get(url).status_code==403
    for url in ['/jobs','/jobs/unknown/retry','/jobs/unknown/cancel']:
        assert client.post(url,data={'csrf':token}).status_code==403
