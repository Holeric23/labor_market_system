"""Six-document oracle: seven publications, repeated skill, three salary units.

Before correction: Python 2, SQL 1, Developer 3; salary measured 3,
unknown units 1, missing 1, ambiguous 1. Source two contains only a repost.
"""
import hashlib
import io
import json
import subprocess
import sys
import zipfile
from unittest.mock import patch

import pytest
from market_system.analytics.store import Analytics, filters
from market_system.analytics.engine_v1 import calculate, canonical
from market_system.review.store import Actor, Review
from market_system.search.store import connect, activate, utc
from test_web import client, login, csrf
from test_search_service import catalog

OWNER=Actor('test-owner','ADMIN')
USER=Actor('test-reader','USER')


@pytest.fixture
def analytics(tmp_path):
    service=Analytics(tmp_path/'analytics.db')
    service.initialize()
    with connect(service.path) as con:
        con.execute("INSERT INTO source_types VALUES(1,'telegram')")
        con.execute("INSERT INTO sources VALUES(1,1,'first'),(2,1,'repost')")
        con.executemany('INSERT INTO classes VALUES(?,?,?,?,?)',[
            (1,'skill:x','python','Python','1'),(2,'skill:x','sql','SQL','1'),
            (3,'occupation:x','dev','Developer','1'),(4,'occupation:x','design','Designer','1')])
        con.execute("INSERT INTO result_sets VALUES('oracle','model1','skills1','profiles1','{}','{}','{}','building','now',NULL,NULL)")
        for cid in range(1,7):
            con.execute('INSERT INTO contents VALUES(?,?,?)',(cid,f'Original {cid}: Python Python' if cid==1 else f'Original {cid}',str(cid)))
            con.execute("INSERT INTO document_processing VALUES('oracle',?,'processed')",(cid,))
            stamp=utc('2026-01-01T00:00:00Z') if cid!=5 else None
            low={1:100,2:10,3:500,4:1000}.get(cid)
            high={1:200,2:20}.get(cid)
            currency='USD' if cid==2 else 'RUB' if cid<5 else None
            period='hour' if cid==3 else 'month' if cid<3 else None
            con.execute('''INSERT INTO search_catalog(result_set_id,content_id,post_type,published_at_utc,publication_count,
                salary_from,salary_to,currency,salary_period,tax_mode,work_format,employment_type,review_status,salary_scope)
                VALUES('oracle',?,'vacancy',?,?,?,?,?,?,?,?,?,'unreviewed',?)''',
                (cid,stamp,2 if cid==1 else 1,low,high,currency,period,'gross' if cid==2 else 'net' if cid<5 else None,
                 'remote' if cid<4 else None,'full_time' if cid<4 else None,'unattributed' if cid==6 else 'document'))
            profile={'multiple_offers':cid==6}
            mentions=[{'skill_id':'python','role':'required'}]*3 if cid==1 else []
            con.execute("INSERT INTO result_evidence VALUES('oracle',?,'{}',?,?)",(cid,json.dumps(profile),json.dumps(mentions)))
            con.execute('INSERT INTO publications VALUES(?,?,1,?,?)',(cid,cid,f'https://example.org/{cid}',stamp))
            con.execute("INSERT INTO result_publications VALUES('oracle',?,?,?)",(cid,cid,stamp))
        con.execute("INSERT INTO publications VALUES(7,1,2,'https://example.org/repost','2026-01-02T00:00:00Z')")
        con.execute("INSERT INTO result_publications VALUES('oracle',1,7,?)",(utc('2026-01-02T00:00:00Z'),))
        con.executemany("INSERT INTO document_classes VALUES('oracle',?,?,?,NULL)",[
            (1,1,'required'),(1,1,'preferred'),(2,2,'required'),(1,3,'required'),(1,6,'required'),
            (3,1,'none'),(3,2,'none'),(3,3,'none'),(3,6,'none'),(4,6,'none')])
        con.execute("UPDATE document_processing SET status='no_classes' WHERE content_id IN (4,5)")
        con.execute('''UPDATE result_sets SET coverage_json='{"expected":6}',state='ready' WHERE result_set_id='oracle' ''')
        con.commit()
    activate(service.path,'oracle')
    return service


def report(service,**kwargs):
    return service.report(filters(**kwargs),OWNER)


def rows(result,name,**dimensions):
    return [r for r in result['reports'][name]['rows'] if all(r['dimensions'].get(k)==v for k,v in dimensions.items())]


def test_hand_calculated_oracle(analytics):
    result=report(analytics)
    assert result['manifest']['sample_size']==6
    assert result['manifest']['publication_sample_size']==7
    assert sum(r['publications'] for r in rows(result,'volume'))==7
    assert rows(result,'volume',source_id=2)[0]['document_ids']==[1]
    assert rows(result,'frequency',class_id=1)[0]['document_ids']==[1,3]
    assert rows(result,'frequency',class_id=2)[0]['count']==1
    assert rows(result,'frequency',class_id=3)[0]['count']==3
    assert rows(result,'occupation_skills',occupation_id=3,skill_id=1)[0]['count']==2
    assert rows(result,'conditions',field='work_format',value='remote')[0]['count']==3
    assert rows(result,'conditions',field='work_format',value=None)[0]['count']==2
    salary=result['reports']['salary']
    assert salary['measured_documents']==3 and salary['unknown_units']==1 and salary['excluded_ambiguous']==1
    assert len(salary['rows'])==5
    assert rows(result,'salary',currency='USD',period='month',tax_mode='gross',bound='salary_from')[0]['dimensions']['amount']==10
    assert rows(result,'salary',currency='RUB',period='hour')[0]['dimensions']['amount']==500
    assert not rows(result,'salary',amount=1000)
    assert rows(result,'quality',field='salary',status='missing')[0]['document_ids']==[5]
    assert rows(result,'quality',field='salary',status='ambiguous')[0]['share']==1/6
    assert rows(result,'quality',field='salary_period',status='missing')[0]['document_ids']==[4,5]
    for key in ('frequency','occupation_skills','conditions','salary'):
        assert result['reports'][key]['excluded_document_ids']==[6]


def test_publication_period_sources_repost_and_empty(analytics):
    result=report(analytics,start='2026-01-02',until='2026-01-03',sources=[2])
    assert result['manifest']['sample_size']==result['manifest']['publication_sample_size']==1
    assert rows(result,'frequency',class_id=1)[0]['count']==1
    assert result['inputs']['documents'][0]['publications'][0]['publication_id']==7
    assert report(analytics,start='2026-01-01',until='2026-01-02')['manifest']['sample_size']==5
    assert report(analytics,start='2027-01-01')['manifest']['sample_size']==0
    assert report(analytics,post_type='resume')['reports']['salary']['measured_documents']==0
    with pytest.raises(ValueError): report(analytics,sources=[99])
    with pytest.raises(ValueError): filters(start='2026-01-02',until='2026-01-01')


def test_quality_denominators_unknown_tax_and_field_ambiguity(analytics):
    result=report(analytics)
    for field in {r['dimensions']['field'] for r in rows(result,'quality')}:
        states=rows(result,'quality',field=field)
        assert sum(r['count'] for r in states if r['dimensions']['status']!='verified')==6
        assert sum(r['share'] for r in states if r['dimensions']['status']!='verified')==pytest.approx(1)
    empty=report(analytics,start='2027-01-01')
    assert all(r['share'] is None for r in rows(empty,'quality'))
    inputs=json.loads(canonical(result['inputs']))
    inputs['documents'][2]['catalog']['tax_mode']=None
    inputs['documents'][3]['evidence']['profile']['work_conditions']={'work_format':{'status':'multiple'}}
    calculated={'reports':calculate(inputs)}
    assert rows(calculated,'salary',currency='RUB',period='hour',tax_mode=None)[0]['count']==1
    assert calculated['reports']['conditions']['excluded_document_ids']==[4,6]
    assert rows(calculated,'conditions',field='work_format',value=None)[0]['document_ids']==[5]
    assert rows(calculated,'quality',field='work_format',status='ambiguous')[0]['document_ids']==[4,6]


def test_correction_active_change_upgrade_and_archive_replay(analytics,tmp_path):
    first=analytics.save(filters(),OWNER,'oracle')
    frozen=analytics.load(first,OWNER)
    review=Review(analytics.path)
    annotation=review.get(2,OWNER)['annotation']
    annotation['salary']=[{'salary_from':50,'salary_to':60,'currency':'RUB','salary_period':'year','tax_mode':'gross'}]
    annotation['skills']=[{'class_id':1,'role':'required'}]
    changed=review.save(2,0,'confirmed',annotation,OWNER,'oracle')['result_set_id']
    current=report(analytics)
    assert current['inputs']['result_set']['result_set_id']==changed
    assert rows(current,'frequency',class_id=1)[0]['count']==3
    assert rows(current,'quality',field='salary',status='verified')[0]['document_ids']==[2]
    assert rows(current,'salary',period='year',bound='salary_from')[0]['dimensions']['amount']==50
    second=analytics.save(filters(),OWNER,changed)
    annotation['salary'][0]['salary_from']=55
    review.save(2,1,'confirmed',annotation,OWNER,changed)
    assert analytics.document(analytics.load(second,OWNER),2,OWNER)['correction']['revision']==1
    assert analytics.load(first,OWNER)['reports']==frozen['reports']
    # Reads of saved results never invoke the current evaluator after an upgrade.
    with patch('market_system.analytics.engine_v1.calculate',side_effect=RuntimeError('new algorithm')):
        assert analytics.load(first,OWNER)['reports']==frozen['reports']
    archive=zipfile.ZipFile(io.BytesIO(analytics.export(analytics.load(second,OWNER),OWNER)))
    manifest=json.loads(archive.read('manifest.json'))
    for name,checksum in manifest['files_sha256'].items():
        assert hashlib.sha256(archive.read(name)).hexdigest()==checksum
    replay_dir=tmp_path/'standalone'
    replay_dir.mkdir()
    archive.extractall(replay_dir)
    # The exported standard-library calculator has no database/application imports.
    subprocess.run([sys.executable,str(replay_dir/'engine.py'),str(replay_dir/'inputs.json'),str(replay_dir/'replayed.json')],check=True,cwd=replay_dir)
    assert (replay_dir/'replayed.json').read_bytes()==archive.read('reports.json')
    portable=json.loads(archive.read('inputs.json'))
    for d in portable['documents']:
        assert hashlib.sha256(d['text'].encode()).hexdigest()==d['text_sha256']


def test_immutable_inputs_filters_definitions_and_csv(analytics):
    identity=analytics.save(filters(sources=[1]),OWNER,'oracle')
    result=analytics.load(identity,OWNER)
    with connect(analytics.path) as con:
        for sql in ("UPDATE contents SET text='changed' WHERE id=1",
                    "DELETE FROM contents WHERE id=1",
                    "UPDATE scientific_slices SET reports_json='{}'",
                    "DELETE FROM scientific_slices",
                    "DELETE FROM scientific_documents"):
            with pytest.raises(Exception,match='immutable'):
                con.execute(sql)
            con.rollback()
        con.execute("UPDATE sources SET name='Changed name' WHERE id=1")
        con.commit()
    assert analytics.load(identity,OWNER)==result
    assert analytics.document(result,1,OWNER)['publications'][0]['source_name']=='first'
    for key in result['reports']:
        archive=zipfile.ZipFile(io.BytesIO(analytics.export(result,OWNER,key)))
        assert set(archive.namelist())=={key+'.csv','manifest.json'}
        manifest=json.loads(archive.read('manifest.json'))
        assert manifest['filters']['sources']==[1] and manifest['sample_size']==6
        assert key in manifest['reports']
    with pytest.raises(PermissionError): analytics.load(identity,USER)
    with pytest.raises(PermissionError): analytics.save(filters(),USER,'oracle')
    with pytest.raises(PermissionError): analytics.report(filters(),USER)
    with pytest.raises(ValueError): analytics.save(filters(),OWNER,None)


def test_web_owner_drilldown_csv_saved_card_and_csrf(client):
    page=login(client)
    response=client.get('/analytics?start=2026-01-01&until=2026-02-01&sources=1')
    assert response.status_code==200 and 'Частота профессий и навыков' in response.text
    assert 'Неизвестные валюта или период' in response.text
    assert client.get('/analytics/documents/frequency/0?result_set_id=v1').status_code==200
    for key in ('volume','frequency','occupation_skills','conditions','salary','quality'):
        response=client.get('/analytics/export?report='+key)
        assert response.status_code==200 and response.headers['content-type']=='application/zip'
        assert 'manifest.json' in zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert client.post('/analytics/slices',data={'csrf':'bad'}).status_code==403
    response=client.post('/analytics/slices',data={'csrf':csrf(page),'result_set_id':'v1','sources':'1'})
    assert response.status_code==200 and 'Неизменяемый научный срез' in response.text
    path=response.url.path
    assert client.get(path+'/documents/frequency/0').status_code==200
    assert client.get(path+'/document/1').status_code==200
    assert client.get(path+'/export').status_code==200
    assert client.get('/analytics?start=broken').status_code==400
    assert client.get('/analytics/documents/frequency/-1').status_code==400
    client.post('/logout',data={'csrf':csrf(response)})
    login(client,'reader')
    for route in ('/analytics','/analytics/export',path,path+'/export',path+'/document/1',path+'/documents/frequency/0'):
        assert client.get(route).status_code==403
    assert client.post('/analytics/slices',data={'csrf':'bad'}).status_code==403
