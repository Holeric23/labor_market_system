import asyncio
import json
import pytest
from unittest.mock import patch
from starlette.datastructures import QueryParams
from market_system.search import SearchFilter
from market_system.search.store import connect, activate
from market_system.telegram_bot.store import BotStore
from market_system.telegram_bot.catalog import CatalogSearch, parse_filter
from market_system.telegram_bot.delivery import DeliveryWorker, TemporaryDeliveryError, PermanentDeliveryError
from market_system.telegram_bot.outbox import produce
from market_system.telegram_bot.unified import Controller, Access
from market_system.web.app import filter_input
from test_jobs import jobs, submit, worker, SPEC, analyze
from test_search_service import catalog


@pytest.fixture
def bot(jobs):
    store=BotStore(jobs.path)
    store.initialize()
    store.bootstrap_owners({10})
    store.grant(10,20)
    identity=submit(jobs)
    worker(jobs).run(once=True)
    activate(jobs.path,jobs.get(identity)['result_set_id'])
    return store


def rows(bot,table):
    with connect(bot.path) as con:
        return [dict(r) for r in con.execute('SELECT * FROM '+table)]


def add(jobs,identity=4,text='brand new vacancy',publication=True):
    with connect(jobs.path) as con:
        con.execute('INSERT INTO contents VALUES(?,?,?)',(identity,text,'hash-'+str(identity)))
        if publication:
            con.execute('INSERT INTO publications VALUES(?,?,1,?,?)',(identity,identity,'https://example.org/'+str(identity),'2026-10-09T00:00:00Z'))
        con.commit()


def ready(jobs):
    identity=submit(jobs,'incremental')
    worker(jobs).run(once=True)
    assert jobs.get(identity)['state']=='succeeded',jobs.get(identity)['error']
    return jobs.get(identity)['result_set_id']


@pytest.mark.parametrize('params',[
    'post_type=vacancy', 'post_type=&skill_ids=1&skill_ids=2&skills_mode=ANY',
    'post_type=vacancy&skill_ids=1&skill_roles=preferred',
    'post_type=&unknown_work_format=only',
    'post_type=vacancy&salary_min=100&currency=RUB&salary_period=month',
    'post_type=&published_from=2026-01-02&published_until=2026-01-04',
    'post_type=&occupation_ids=3&unknown_post_type=include'])
def test_web_bot_exact_semantics(catalog,params):
    f,_,_=filter_input(QueryParams(params),{'post_type':'vacancy','page_size':20})
    web=catalog.search(f,page_size=20)
    bot=asyncio.run(CatalogSearch(catalog.path).search(f.normalized(),page_size=20))
    assert bot==web


def test_access_ownership_persistence_and_no_cursor(bot):
    with pytest.raises(PermissionError):bot.subscribe(99,{'post_type':'vacancy'})
    with pytest.raises(PermissionError):bot.grant(20,30)
    identity=bot.subscribe(20,{'post_type':'vacancy'})
    assert not bot.delete(10,identity)
    assert bot.subscriptions(10)==[]
    assert BotStore(bot.path).subscriptions(20)[0]['id']==identity
    assert identity==bot.subscribe(20,{'post_type':'vacancy'})
    for key in ('cursor','offset','limit'):
        with pytest.raises(ValueError):bot.subscribe(20,{'post_type':'vacancy',key:1})
    bot.grant(10,20,False)
    assert not asyncio.run(Access(bot).check_access(20))
    with pytest.raises(PermissionError):bot.subscriptions(20)


def test_ready_event_dedup_and_no_rebuild_flood(bot,jobs):
    bot.subscribe(20,{'post_type':'vacancy'})
    assert rows(bot,'telegram_outbox')==[]
    add(jobs)
    rid=ready(jobs)
    assert len(rows(bot,'telegram_outbox'))==1
    assert len(rows(bot,'telegram_deliveries'))==1
    with bot.transaction() as con:produce(con,rid)
    activate(jobs.path,rid)
    with connect(jobs.path) as con:
        con.execute("INSERT INTO publications VALUES(5,4,1,'https://example.org/repost','2026-10-09T00:00:00Z')");con.commit()
    ready(jobs)
    identity=submit(jobs)
    worker(jobs).run(once=True)
    activate(jobs.path,jobs.get(identity)['result_set_id'])
    assert len(rows(bot,'telegram_outbox'))==1


def test_old_url_edit_is_not_new_notification(bot,jobs):
    bot.subscribe(20,{'post_type':'vacancy'})
    add(jobs,publication=False)
    with connect(jobs.path) as con:
        con.execute("INSERT INTO publication_revisions(publication_id,revision,content_id,published_at,observed_at) VALUES(1,1,1,'2026-01-01T00:00:00Z','2026-10-09T00:00:00Z')")
        con.execute("INSERT INTO publication_revisions(publication_id,revision,content_id,published_at,observed_at) VALUES(1,2,4,'2026-01-01T00:00:00Z','2026-10-09T00:00:00Z')")
        con.commit()
    ready(jobs)
    assert rows(bot,'telegram_outbox')==[]


def test_atomic_ready_and_outbox_write_failure(bot,jobs):
    bot.subscribe(20,{'post_type':'vacancy'});add(jobs)
    identity=submit(jobs,'incremental')
    with bot.transaction() as con:
        con.execute("CREATE TRIGGER fail_event BEFORE INSERT ON telegram_outbox BEGIN SELECT RAISE(ABORT,'injected write failure'); END")
    worker(jobs).run(once=True)
    assert jobs.get(identity)['state']=='failed'
    assert rows(bot,'telegram_outbox')==[]
    assert all(r['content_id']!=4 for r in rows(bot,'telegram_seen'))
    with bot.transaction() as con:
        assert con.execute('SELECT state FROM result_sets WHERE result_set_id=?',(jobs.get(identity)['result_set_id'],)).fetchone()[0]=='building'
        con.execute('DROP TRIGGER fail_event')
    jobs.retry(identity);worker(jobs).run(once=True)
    assert jobs.get(identity)['state']=='succeeded'
    assert len(rows(bot,'telegram_outbox'))==1


def queued(bot,jobs):
    bot.subscribe(20,{'post_type':'vacancy'});add(jobs);ready(jobs)


def due(bot):
    with bot.transaction() as con:con.execute("UPDATE telegram_deliveries SET next_attempt_at='2000-01-01T00:00:00+00:00'")


def test_delivery_retry_restart_receipt_and_secret_redaction(bot,jobs):
    queued(bot,jobs)
    def fail(*_):raise RuntimeError('TOKEN-secret must never be stored')
    d=DeliveryWorker(bot,'one',fail)
    assert d.run_one()
    assert rows(bot,'telegram_deliveries')[0]['state']=='queued'
    assert 'TOKEN' not in json.dumps(rows(bot,'telegram_attempts'))
    due(bot);sent=[]
    d=DeliveryWorker(BotStore(bot.path),'two',lambda user,text:sent.append((user,text)) or 101)
    d.recover();assert d.run_one();assert not d.run_one()
    row=rows(bot,'telegram_deliveries')[0]
    assert row['message_id']==101 and row['attempts']==2 and row['state']=='sent'
    assert len(sent)==1 and row['event_id'] in sent[0][1]


def test_crash_after_send_has_stable_id_and_documented_duplicate(bot,jobs):
    queued(bot,jobs);sent=[]
    def crash():raise SystemExit('crash after send')
    d=DeliveryWorker(bot,'one',lambda u,t:sent.append(t) or 1,after_send=crash)
    with pytest.raises(SystemExit):d.run_one()
    assert rows(bot,'telegram_deliveries')[0]['state']=='running'
    d=DeliveryWorker(bot,'two',lambda u,t:sent.append(t) or 2)
    d.recover();d.run_one()
    assert len(sent)==2 and sent[0]==sent[1]
    assert rows(bot,'telegram_attempts')[0]['outcome']=='interrupted'


def test_deleted_or_revoked_subscription_cancels_pending_delivery(bot,jobs):
    queued(bot,jobs)
    bot.delete(20,bot.subscriptions(20)[0]['id'])
    def forbidden(*_):pytest.fail('must not send')
    DeliveryWorker(bot,'one',forbidden).run_one()
    assert rows(bot,'telegram_deliveries')[0]['state']=='cancelled'


def test_sample_does_not_publish(bot,jobs):
    bot.subscribe(20,{'post_type':'vacancy'});add(jobs)
    identity=submit(jobs,'sample',ids=[4]);worker(jobs).run(once=True)
    assert jobs.get(identity)['state']=='succeeded'
    assert rows(bot,'telegram_outbox')==[]
    ready(jobs)
    assert len(rows(bot,'telegram_outbox'))==1


def test_model_switch_requires_confirmed_mapping_not_name(bot,jobs):
    with connect(bot.path) as con:
        skill=con.execute("SELECT class_id FROM classes WHERE namespace LIKE 'skill:%' LIMIT 1").fetchone()[0]
    bot.subscribe(20,{'post_type':'vacancy','skill_ids':[skill]})
    spec={**SPEC,'model_version':'new-model','fingerprint':'new-fingerprint'}
    identity=jobs.submit('full',spec=spec,corrections={})
    from market_system.jobs.worker import Worker
    Worker(jobs,analyzer_factory=lambda _:lambda texts:[{**r,'model_version':'new-model','skill_lexicon_sha256':'new-dictionary'} for r in analyze(texts)]).run(once=True)
    activate(jobs.path,jobs.get(identity)['result_set_id'])
    assert bot.subscriptions(20)[0]['status']=='needs_update'
    assert rows(bot,'telegram_outbox')==[]
    with connect(bot.path) as con:
        replacement=con.execute("SELECT class_id FROM classes WHERE namespace='skill:new-dictionary'").fetchone()[0]
        assert con.execute('SELECT name FROM classes WHERE class_id=?',(skill,)).fetchone()[0]==con.execute('SELECT name FROM classes WHERE class_id=?',(replacement,)).fetchone()[0]
    assert replacement!=skill
    with pytest.raises(PermissionError):bot.confirm_mapping(20,skill,skill,'new-model')
    bot.confirm_mapping(10,skill,replacement,'new-model')
    assert bot.subscriptions(20)[0]['status']=='active'
    assert json.loads(bot.subscriptions(20)[0]['filter_json'])['skill_ids']==[replacement]


def test_controller_uses_persistent_search_and_own_subscriptions(bot):
    from market_system.telegram_bot.infrastructure.sqlite_state import StateDatabase,SQLiteUserRepository
    class Telegram:
        def __init__(self):self.sent=[]
        async def send_message(self,user,text,*args,**kwargs):self.sent.append((user,text));return len(self.sent)
        async def delete_message(self,*args):pass
    tg=Telegram();users=SQLiteUserRepository(StateDatabase(bot.path.parent/'bot-state.db'),{10})
    controller=Controller(bot,users,tg)
    asyncio.run(controller.command(20,'/search {"post_type":"vacancy"}'))
    asyncio.run(Controller(BotStore(bot.path),users,tg).command(20,'/subscribe'))
    assert len(bot.subscriptions(20))==1
    asyncio.run(controller.command(20,'/allow 99'))
    assert tg.sent[-1][1]=='Доступ ограничен.'


def test_duplicate_text_separate_content_and_recipient_dedup(bot,jobs):
    bot.subscribe(20,{'post_type':'vacancy'})
    bot.subscribe(20,{'post_type':'vacancy','skills_mode':'ANY'})
    add(jobs,4,'same new text');add(jobs,5,'same new text')
    ready(jobs)
    assert len(rows(bot,'telegram_outbox'))==1
    assert len(rows(bot,'telegram_deliveries'))==1
    assert len(rows(bot,'telegram_delivery_subscriptions'))==2


def test_revoke_and_permanent_failure_do_not_send_or_retry(bot,jobs):
    queued(bot,jobs)
    bot.grant(10,20,False)
    def forbidden(*_):pytest.fail('revoked user must not receive')
    DeliveryWorker(bot,'one',forbidden).run_one()
    assert rows(bot,'telegram_deliveries')[0]['state']=='cancelled'


def test_permanent_and_rate_limit_failures(bot,jobs):
    queued(bot,jobs)
    def limited(*_):raise TemporaryDeliveryError(120)
    d=DeliveryWorker(bot,'one',limited)
    d.run_one()
    assert not d.run_one()
    due(bot)
    def blocked(*_):raise PermanentDeliveryError()
    DeliveryWorker(bot,'one',blocked).run_one()
    assert rows(bot,'telegram_deliveries')[0]['state']=='failed'
    assert not d.run_one()


def test_legacy_state_preserved_without_name_mapping(bot):
    from market_system.telegram_bot.infrastructure.sqlite_state import StateDatabase,SQLiteSubscriptionRepository
    from market_system.telegram_bot.domain.models import SearchFilter as LegacyFilter
    old=StateDatabase(bot.path.parent/'old-state.db')
    repo=SQLiteSubscriptionRepository(old)
    sub=asyncio.run(repo.create_subscription(20,LegacyFilter(post_type='VACANCY',skills=['python'])))
    before=old.path.read_bytes()
    assert bot.import_legacy(old.path)==1
    assert bot.import_legacy(old.path)==0
    assert old.path.read_bytes()==before
    imported=bot.subscriptions(20)[0]
    assert imported['id']==str(sub.id) and imported['status']=='needs_update'
    assert json.loads(imported['filter_json'])['skills']==['python']
    # Ready activation must not assume that legacy filters contain new ID fields.
    with connect(bot.path) as con:rid=con.execute('SELECT result_set_id FROM active_result_set').fetchone()[0]
    activate(bot.path,rid)
    assert bot.subscriptions(20)[0]['status']=='needs_update'


def test_telegram_sender_is_mocked_and_secrets_are_hidden():
    import io
    import logging
    from unittest.mock import AsyncMock
    from market_system.telegram_bot.config import Settings
    from market_system.telegram_bot.delivery import TelegramSender
    settings=Settings(token='123456:SECRET',admins=frozenset({10}))
    output=io.StringIO();handler=logging.StreamHandler(output)
    logging.getLogger().addHandler(handler)
    try:
        sender=TelegramSender(settings)
        logging.warning('URL %s',settings.token)
        assert settings.token not in output.getvalue() and settings.token not in repr(settings)
        fake=AsyncMock();fake.__aenter__.return_value=fake
        fake.send_message.return_value.message_id=123
        with patch('aiogram.Bot',return_value=fake),patch('aiogram.client.session.aiohttp.AiohttpSession'):
            assert sender(20,'fixture event')==123
            fake.send_message.assert_awaited_once_with(20,'fixture event',parse_mode=None)
    finally:
        logging.getLogger().removeHandler(handler)


def test_worker_drains_outbox_through_single_executor(bot,jobs):
    queued(bot,jobs);calls=[]
    from market_system.jobs.worker import Worker,worker_lock
    worker(jobs,telegram_sender=lambda user,text:calls.append(user) or 55).run(once=True)
    assert calls==[20]
    assert rows(bot,'telegram_deliveries')[0]['message_id']==55
    with worker_lock(jobs.path):
        with pytest.raises(RuntimeError):
            worker(jobs,telegram_sender=lambda *_:pytest.fail('second worker')).run(once=True)
