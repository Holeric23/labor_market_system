import asyncio
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from types import SimpleNamespace

import pytest

from market_system.collection.store import Sources
from market_system.collection.runner import Collector, Batch
from market_system.core.utils.dataclasses import RawPublication
from market_system.jobs.worker import Worker
from market_system.search.store import connect, activate, validate
from test_jobs import SPEC, analyze


@pytest.fixture
def sources(tmp_path):
    result=Sources(tmp_path/'collection.db')
    result.initialize()
    return result


def add(sources,name='channel_one'):
    return sources.save('telegram',name,'https://t.me/'+name,{'batch_size':1,'max_items':100})


def publication(text='same text',number=1,name='channel_one'):
    return RawPublication('telegram',name,text,f'https://t.me/{name}/{number}',datetime(2026,1,1,tzinfo=timezone.utc))


def feed(items, fail_at=None):
    async def adapter(config,mode,cursor):
        cursor=deepcopy(cursor)
        offset=cursor.get('offset',0)
        for i in range(offset,len(items)):
            if i==fail_at:
                raise ValueError('simulated source failure')
            cursor['offset']=i+1
            cursor['source']['high_water']=i+1
            yield Batch([items[i]],deepcopy(cursor),i==len(items)-1)
        if offset==len(items):
            yield Batch([],cursor,True)
    return adapter


def run(sources,adapter,**kwargs):
    collector=Collector(sources,'test-owner',adapter,**kwargs)
    identity=collector.claim()
    if identity:
        collector.execute(identity)
    return identity


def count(sources,table):
    with connect(sources.path) as con:
        return con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]


def test_republication_url_dedup_edit_and_reversion(sources):
    source=add(sources)
    job=sources.enqueue(source,'history')
    run(sources,feed([publication(),publication(),publication(number=2),publication('edited'),publication()]))
    assert sources.job(job)['state']=='succeeded'
    assert count(sources,'contents')==2
    assert count(sources,'publications')==2
    assert count(sources,'publication_revisions')==4
    with connect(sources.path) as con:
        assert [r[0] for r in con.execute('SELECT c.text FROM publication_revisions r JOIN contents c ON c.id=r.content_id WHERE publication_id=1 ORDER BY revision')]==['same text','edited','same text']
    again=sources.enqueue(source,'new')
    run(sources,feed([publication(),publication(number=2)]))
    assert count(sources,'publication_revisions')==4
    assert sources.job(again)['state']=='succeeded'
    assert sources.get(source)['last_success_at']


def test_whitespace_edit_keeps_exact_text(sources):
    source=add(sources)
    sources.enqueue(source,'history')
    run(sources,feed([publication('a  b'),publication('a b'),publication('a  b')]))
    assert count(sources,'contents')==2
    assert count(sources,'publication_revisions')==3


def test_failure_before_commit_rolls_back_cursor_and_handoff(sources):
    source=add(sources)
    identity=sources.enqueue(source,'history')
    def fail(_):
        raise ValueError('injected before commit')
    run(sources,feed([publication()]),before_commit=fail)
    assert sources.job(identity)['state']=='failed'
    assert sources.get(source)['cursor_json']=='{}'
    assert sources.get(source)['last_success_at'] is None
    assert count(sources,'contents')==count(sources,'publications')==count(sources,'collection_handoffs')==0
    sources.retry(identity)
    run(sources,feed([publication()]))
    assert sources.job(identity)['state']=='succeeded'
    assert count(sources,'contents')==1


def test_crash_after_commit_recovers_and_source_failures_are_independent(sources):
    first=add(sources)
    second=add(sources,'channel_two')
    one=sources.enqueue(first,'history')
    two=sources.enqueue(second,'history')
    def crash(_):
        raise SystemExit(17)
    with pytest.raises(SystemExit):
        run(sources,feed([publication(number=1),publication(number=2)]),after_commit=crash)
    assert sources.job(one)['state']=='running'
    assert sources.job(one)['batches']==1
    Collector(sources,'next-owner').recover()
    run(sources,feed([publication(number=1),publication(number=2)],fail_at=1))
    assert sources.job(one)['state']=='failed'
    run(sources,feed([publication(name='channel_two')]))
    assert sources.job(two)['state']=='succeeded'
    assert count(sources,'publications')==2
    sources.retry(one)
    run(sources,feed([publication(number=1),publication(number=2)]))
    assert sources.job(one)['state']=='succeeded'
    assert count(sources,'publications')==3
    assert sources.job(one)['attempts']==3


def test_schedule_disabled_durable_and_no_overlap(sources):
    source=add(sources)
    assert sources.schedule()==[]
    row=sources.get(source)
    sources.save(row['kind'],row['name'],row['address'],json.loads(row['parameters_json']),True,True,60,source,row['revision'])
    first=sources.schedule()[0]
    assert sources.enqueue(source,'history')==first
    assert sources.schedule()==[]
    assert sources.schedule((datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat())==[first]
    assert count(sources,'collection_jobs')==1
    assert Sources(sources.path).get(source)['schedule_enabled']==1


def test_stale_retry_cannot_regress_cursor(sources):
    source=add(sources)
    first=sources.enqueue(source,'history')
    run(sources,feed([publication(),publication(number=2)],fail_at=1))
    second=sources.enqueue(source,'new')
    run(sources,feed([publication(number=3)]))
    with pytest.raises(ValueError,match='Source changed'):
        sources.retry(first)
    assert sources.job(second)['state']=='succeeded'


def test_disabled_source_never_calls_network(sources):
    source=add(sources)
    identity=sources.enqueue(source,'history')
    row=sources.get(source)
    sources.save(row['kind'],row['name'],row['address'],json.loads(row['parameters_json']),False,False,60,source,row['revision'])
    async def forbidden(*args):
        raise AssertionError('network must not run')
        yield
    run(sources,forbidden)
    assert sources.job(identity)['state']=='cancelled'


def initial_catalog(sources):
    with connect(sources.path) as con:
        con.execute("INSERT INTO contents(text,text_hash) VALUES('old vacancy','initial')")
        con.commit()
    identity=sources.jobs.submit('full',spec=SPEC,corrections={})
    Worker(sources.jobs,analyzer_factory=lambda spec:analyze).run(once=True)
    rid=sources.jobs.get(identity)['result_set_id']
    activate(sources.path,rid)
    return rid


def test_handoff_transaction_and_processing_and_active_survive_edits(sources):
    source=add(sources)
    sources.enqueue(source,'history')
    run(sources,feed([publication()]))
    # Explicit full baseline for this isolated test, never an automatic full job.
    old=initial_catalog(sources)
    first=sources.dispatch(spec=SPEC,corrections={},force=True)
    assert first
    assert sources.dispatch(spec=SPEC,corrections={}) is None
    Worker(sources.jobs,analyzer_factory=lambda spec:analyze).run(once=True)
    sources.enqueue(source,'new')
    run(sources,feed([publication('edited vacancy'),publication('new text',2)]))
    processing=sources.dispatch(spec=SPEC,corrections={})
    assert processing
    assert sources.jobs.get(processing)['state']=='queued'
    Worker(sources.jobs,analyzer_factory=lambda spec:analyze).run(once=True)
    assert sources.jobs.get(processing)['state']=='succeeded'
    with connect(sources.path) as con:
        assert con.execute('SELECT result_set_id FROM active_result_set').fetchone()[0]==old
        validate(con,old)
        rid=sources.jobs.get(processing)['result_set_id']
        validate(con,rid)
        assert con.execute('SELECT c.text FROM result_publications r JOIN contents c ON c.id=r.content_id WHERE r.result_set_id=? AND r.publication_id=1',(old,)).fetchone()[0]=='same text'
        assert con.execute('SELECT c.text FROM result_publications r JOIN contents c ON c.id=r.content_id WHERE r.result_set_id=? AND r.publication_id=1',(rid,)).fetchone()[0]=='edited vacancy'
        assert con.execute('SELECT count(*) FROM result_publications WHERE result_set_id=?',(rid,)).fetchone()[0]==2


def test_handoff_failure_is_persistent_and_retryable(sources):
    source=add(sources)
    identity=sources.enqueue(source,'history')
    run(sources,feed([publication()]))
    assert sources.dispatch(spec=SPEC,corrections={}) is None
    assert sources.handoffs()[0]['error']
    assert count(sources,'contents')==1
    initial_catalog(sources)
    with connect(sources.path) as con:
        con.execute("CREATE TRIGGER fail_handoff BEFORE UPDATE ON collection_handoffs BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        con.commit()
    before=count(sources,'jobs')
    # Persisting even the diagnostic is deliberately blocked by this injected DB fault.
    with pytest.raises(Exception,match='test rollback'):
        sources.dispatch(spec=SPEC,corrections={},force=True)
    assert count(sources,'jobs')==before
    with connect(sources.path) as con:
        con.execute('DROP TRIGGER fail_handoff')
        con.commit()
    assert sources.dispatch(spec=SPEC,corrections={},force=True)
    assert sources.handoffs()[0]['processing_job_id']
    assert sources.job(identity)['state']=='succeeded'


def test_existing_unversioned_publication_gets_history_without_mutation(sources):
    source=add(sources)
    with connect(sources.path) as con:
        cid=con.execute("INSERT INTO contents(text,text_hash) VALUES('legacy','legacy')").lastrowid
        con.execute('INSERT INTO publications(content_id,source_id,url,published_at) VALUES(?,?,?,NULL)',(cid,source,'https://t.me/channel_one/1'))
        con.commit()
    sources.enqueue(source,'history')
    run(sources,feed([publication('revised')]))
    with connect(sources.path) as con:
        assert con.execute('SELECT content_id FROM publications').fetchone()[0]==cid
        revisions=con.execute('SELECT content_id,observed_at FROM publication_revisions ORDER BY revision').fetchall()
        assert revisions[0][0]==cid and revisions[0][1] is None
        assert revisions[1][0]!=cid and revisions[1][1]


def test_frozen_processing_publications_remain_valid_when_source_edited(sources):
    source=add(sources)
    sources.enqueue(source,'history')
    run(sources,feed([publication()]))
    identity=sources.jobs.submit('full',spec=SPEC,corrections={})
    sources.enqueue(source,'new')
    run(sources,feed([publication('edited')]))
    Worker(sources.jobs,analyzer_factory=lambda spec:analyze).run(once=True)
    assert sources.jobs.get(identity)['state']=='succeeded'
    assert sources.jobs.get(identity)['total']==1


class FakeTelegram:
    def __init__(self,messages):
        self.messages=messages
        self.calls=[]
    async def get_entity(self,url):
        return url
    async def get_messages(self,entity,limit):
        return sorted(self.messages,key=lambda m:m.id,reverse=True)[:limit]
    async def iter_messages(self,entity,limit,offset_id=0,min_id=0,max_id=0,reverse=False):
        self.calls.append({'min_id':min_id,'offset_id':offset_id,'reverse':reverse})
        messages=[m for m in self.messages if m.id>min_id and (not max_id or m.id<max_id) and (not offset_id or m.id<offset_id)]
        for message in sorted(messages,key=lambda m:m.id,reverse=not reverse)[:limit]:
            yield message


def test_telegram_history_continuation_new_ids_and_bounded_overlap():
    from market_system.core.parsers.telegram.telegram_parser import TelegramParser
    stamp=datetime.now(timezone.utc)
    client=FakeTelegram([SimpleNamespace(id=i,text=f'text {i}',date=stamp-timedelta(days=5-i)) for i in range(1,6)])
    parser=TelegramParser({'channel_one':'https://t.me/channel_one'},use_proxy=False)
    params={'batch_size':2,'max_items':3,'overlap_limit':2,'overlap_days':2}
    async def collect(mode,cursor):
        return [b async for b in parser.managed_batches(client,'channel_one','https://t.me/channel_one',mode,cursor,params)]
    history=asyncio.run(collect('history',{'source':{},'phase':'history','seen':0}))
    assert [p.url.rsplit('/',1)[-1] for b in history for p in b.publications]==['5','4','3']
    state=history[-1].cursor['source']
    assert state['history_offset']==3 and state['high_water']==5
    rest=asyncio.run(collect('history',{'source':state,'phase':'history','seen':0}))
    assert [p.url.rsplit('/',1)[-1] for b in rest for p in b.publications]==['2','1']
    assert rest[-1].cursor['source']['history_done']
    client.messages[-1].text='edited recent message'
    client.messages.append(SimpleNamespace(id=6,text='new',date=stamp))
    regular=asyncio.run(collect('new',{'source':state,'phase':'overlap','seen':0,'cutoff':(stamp-timedelta(days=2)).isoformat()}))
    assert 'edited recent message' in [p.text for b in regular for p in b.publications]
    assert regular[-1].cursor['source']['high_water']==6
    assert 'text 1' not in [p.text for b in regular for p in b.publications]
    assert any(c['reverse'] and c['min_id']==5 for c in client.calls)


def test_telegram_empty_text_advances_only_checkpoint():
    from market_system.core.parsers.telegram.telegram_parser import TelegramParser
    client=FakeTelegram([SimpleNamespace(id=1,text=None,date=datetime.now(timezone.utc))])
    parser=TelegramParser({'channel_one':'https://t.me/channel_one'},use_proxy=False)
    async def collect():
        return [b async for b in parser.managed_batches(client,'channel_one','https://t.me/channel_one','history',
            {'source':{},'phase':'history','seen':0},{'batch_size':2,'max_items':3,'overlap_limit':2})]
    result=asyncio.run(collect())
    assert result[-1].done and result[-1].publications==[]
    assert result[-1].cursor['source']['high_water']==1


def test_credentials_not_in_source_settings_and_paths_cannot_escape(sources):
    with pytest.raises(ValueError,match='Unknown source parameter'):
        sources.save('telegram','name','https://t.me/channel_one',{'api_hash':'secret'})
    with pytest.raises(ValueError):
        sources.save('telegram','name','http://localhost:8000',{})
    with pytest.raises(ValueError,match='inside project'):
        sources.save('designhunters','name','https://designhunters.ru',{'csv_path':'C:/outside.csv'})


def test_designhunters_frozen_list_resume_and_regular_cycle(sources,tmp_path,monkeypatch):
    from unittest.mock import AsyncMock
    import market_system.core.parsers.designhunters.designhunters_parser as module
    path=tmp_path/'urls.csv'
    urls=['https://designhunters.ru/vacancy/1','https://designhunters.ru/vacancy/2','https://designhunters.ru/vacancy/3']
    path.write_text('\n'.join(urls),encoding='utf-8')
    source=sources.save('designhunters','designhunters.ru','https://designhunters.ru',
                        {'csv_path':str(path),'batch_size':1,'max_items':2,'delay_seconds':0})
    class FakePlaywright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        async def __aexit__(self,*args):
            pass
    page=SimpleNamespace(close=AsyncMock())
    context=SimpleNamespace(new_page=AsyncMock(return_value=page))
    browser=SimpleNamespace(new_context=AsyncMock(return_value=context),close=AsyncMock())
    monkeypatch.setattr(module,'async_playwright',FakePlaywright)
    seen=[]
    async def parse(self,page,url):
        seen.append(url)
        return RawPublication('designhunters','designhunters.ru','vacancy '+url,url)
    monkeypatch.setattr(module.DesignHuntersParser,'_parse_page',parse)
    identity=sources.enqueue(source,'history')
    path.write_text('https://evil.example/not-allowed',encoding='utf-8')
    collector=Collector(sources,'owner')
    collector.execute(collector.claim())
    assert sources.job(identity)['state']=='succeeded'
    assert seen==urls[:2]
    # Restore the same list; history resumes after its first committed portion.
    path.write_text('\n'.join(urls),encoding='utf-8')
    sources.enqueue(source,'history')
    collector.execute(collector.claim())
    assert seen==urls
    assert json.loads(sources.get(source)['cursor_json'])['history_done']
    sources.enqueue(source,'new')
    collector.execute(collector.claim())
    sources.enqueue(source,'new')
    collector.execute(collector.claim())
    assert seen==urls+urls
    assert count(sources,'publications')==3
    assert count(sources,'contents')==3
    assert browser.close.await_count==4


def test_designhunters_page_failure_does_not_advance_uncommitted_batch(sources,tmp_path,monkeypatch):
    from unittest.mock import AsyncMock
    import market_system.core.parsers.designhunters.designhunters_parser as module
    path=tmp_path/'urls.csv'
    urls=['https://designhunters.ru/vacancy/1','https://designhunters.ru/vacancy/2']
    path.write_text('\n'.join(urls),encoding='utf-8')
    source=sources.save('designhunters','designhunters.ru','https://designhunters.ru',
                        {'csv_path':str(path),'batch_size':1,'delay_seconds':0})
    page=SimpleNamespace(close=AsyncMock())
    context=SimpleNamespace(new_page=AsyncMock(return_value=page))
    browser=SimpleNamespace(new_context=AsyncMock(return_value=context),close=AsyncMock())
    class FakePlaywright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        async def __aexit__(self,*args):
            pass
    monkeypatch.setattr(module,'async_playwright',FakePlaywright)
    fail=True
    seen=[]
    async def parse(self,page,url):
        seen.append(url)
        if fail and url==urls[1]:
            raise ValueError('page unavailable')
        return RawPublication('designhunters','designhunters.ru',url,url)
    monkeypatch.setattr(module.DesignHuntersParser,'_parse_page',parse)
    identity=sources.enqueue(source,'history')
    collector=Collector(sources,'owner')
    collector.execute(collector.claim())
    assert sources.job(identity)['state']=='failed'
    assert json.loads(sources.job(identity)['cursor_json'])['index']==1
    assert count(sources,'publications')==1
    fail=False
    sources.retry(identity)
    collector.execute(collector.claim())
    assert sources.job(identity)['state']=='succeeded'
    assert seen==[urls[0],urls[1],urls[1]]
    assert count(sources,'publications')==2


def test_integrated_worker_collects_then_queues_processing(sources,monkeypatch):
    import market_system.collection.store as module
    initial_catalog(sources)
    monkeypatch.setattr(module,'specification',lambda:SPEC)
    monkeypatch.setattr(module,'confirmed_corrections',lambda spec:{})
    source=add(sources)
    identity=sources.enqueue(source,'history')
    worker=Worker(sources.jobs,analyzer_factory=lambda spec:analyze,collection_adapter=feed([publication('new vacancy')]))
    assert worker.run(once=True)==identity
    assert sources.job(identity)['state']=='succeeded'
    processing=sources.handoffs()[0]['processing_job_id']
    assert processing and sources.jobs.get(processing)['state']=='queued'
    assert worker.run(once=True)==processing
    assert sources.jobs.get(processing)['state']=='succeeded'


def test_missing_local_credentials_fails_without_starting_client(sources,monkeypatch):
    import market_system.collection.adapters as module
    def missing():
        raise ValueError('Telegram is not configured')
    monkeypatch.setattr(module,'telegram_settings',missing)
    source=add(sources)
    identity=sources.enqueue(source,'history')
    collector=Collector(sources,'owner')
    collector.execute(collector.claim())
    assert sources.job(identity)['state']=='failed'
    assert 'not configured' in sources.job(identity)['error']
    assert count(sources,'publications')==0


def test_connection_cleanup_failure_keeps_committed_data_and_other_source_runs(sources):
    first=add(sources)
    second=add(sources,'channel_two')
    one=sources.enqueue(first,'history')
    two=sources.enqueue(second,'history')
    async def adapter(config,mode,cursor):
        try:
            cursor['source']['high_water']=1
            yield Batch([publication()],cursor,True)
        finally:
            raise ValueError('connection close failed')
    run(sources,adapter)
    assert sources.job(one)['state']=='succeeded'
    assert 'Cleanup:' in sources.job(one)['error']
    run(sources,feed([publication(name='channel_two')]))
    assert sources.job(two)['state']=='succeeded'
    assert count(sources,'publications')==2
