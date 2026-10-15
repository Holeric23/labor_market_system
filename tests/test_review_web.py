import json
from test_web import client, login, csrf
from test_search_service import catalog
from market_system.review.store import Review, Actor
from market_system.search import Catalog
from market_system.search.store import connect


def payload(token,revision='0',active='v1',status='draft'):
    return {'csrf':token,'revision':revision,'active':active,'status':status,'post_type':'resume',
            'skill_id':['1'],'skill_role':['possessed'],'occupations':['3'],
            'salary_from':['250'],'salary_to':['350'],'currency':['RUB'],'salary_period':['month'],'tax_mode':['net'],
            'condition_work_format':'hybrid','comment':'owner review'}


def test_owner_edit_add_values_confirm_and_revision_conflict(client):
    token=csrf(login(client))
    response=client.get('/review/1')
    assert response.status_code==200
    assert 'Проверка документа' in response.text
    assert client.post('/review/1',data=payload(token)).status_code==200
    with connect(client.app.state.catalog.path) as con:
        assert con.execute('SELECT result_set_id FROM active_result_set').fetchone()[0]=='v1'
    response=client.post('/review/1',data=payload(token,revision='1',status='confirmed'))
    assert response.status_code==200
    assert 'Ревизия сохранена' in response.text
    with connect(client.app.state.catalog.path) as con:
        active=con.execute('SELECT result_set_id FROM active_result_set').fetchone()[0]
    assert active!='v1'
    card=Catalog(client.app.state.catalog.path).get(1,active)
    assert card['post_type']=='resume' and card['salary_from']==250 and card['work_format']=='hybrid'
    assert [c['role'] for c in card['classes'] if c['class_id']==1]==['possessed']
    assert client.post('/review/1',data=payload(token,revision='1',active=active,status='confirmed')).status_code==409
    assert client.get(f'/cards/1?result_set_id={active}').status_code==200


def test_all_review_actions_owner_only_and_csrf(client):
    page=login(client,'reader');token=csrf(page)
    assert 'href="/review"' not in page.text
    assert client.get('/review').status_code==403
    assert client.get('/review/1').status_code==403
    routes=['/review/audit','/review/1','/review/1/terms','/review/terms/missing/approve']
    for route in routes:
        assert client.post(route,data={'csrf':token}).status_code==403
    client.post('/logout',data={'csrf':token})
    login(client)
    for route in routes:
        assert client.post(route,data={'csrf':'bad'}).status_code==403


def test_dictionary_proposal_approval_audit_and_escaping(client):
    token=csrf(login(client))
    assert client.post('/review/audit',data={'csrf':token}).status_code==200
    for reason in ('audit','uncertain','ambiguous','terms','multiple'):
        assert client.get('/review?reason='+reason).status_code==200
    response=client.post('/review/1/terms',data={'csrf':token,'kind':'skill','code':'unique',
        'name':'<script>unsafe</script>','definition':'A distinct meaning'})
    assert response.status_code==200
    assert '<script>unsafe</script>' not in response.text
    assert '&lt;script&gt;unsafe' in response.text
    review=Review(client.app.state.catalog.path)
    term=review.get(1,Actor('test','ADMIN'))['terms'][0]
    assert term['class_id'] is None
    response=client.post('/review/terms/'+term['term_id']+'/approve',data={'csrf':token})
    assert response.status_code==200
    assert review.get(1,Actor('test','ADMIN'))['terms'][0]['class_id'] is not None
