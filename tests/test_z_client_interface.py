"""Autonomous checks with synthetic documents, vacancies and Telegram updates."""
import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, AnswerInlineQuery, GetMe, SendMessage
from aiogram.types import Chat, Message, Update, User
from fastapi.testclient import TestClient

from bot.config import Settings
from bot.miniapp.app import create_app
from bot.telegram import make_router
from database.client import ClientRepository
from database.connection import connect
from database.repository import Repository
from parsers.storage import store_batch


def configure_documents():
    repo = Repository(); repo.bootstrap_owners({42})
    client = ClientRepository()
    version = uuid.uuid4().hex
    for kind in ('terms','privacy','consent'):
        client.publish_document(42,kind,version,'https://example.test/'+kind,'Тестовый текст '+kind)
    return client


def onboarding(client, user):
    client.register(user)
    for doc in client.documents(user):
        if doc['kind'] in ('terms','consent'):
            client.accept(user,doc['id'])
    client.ready(user)


class FakeInference:
    version = 'autonomous-fixture-model'
    manifest = {'fixture':True}
    pipeline = SimpleNamespace(extractor=SimpleNamespace(skills={}),occupations=SimpleNamespace(entries={}))

    def analyze(self, text):
        return {'text':text,'label':'vacancy','salary_from':None,'salary_to':None,
                'currency':None,'salary_period':None,'work_format':'remote','schedule':None,
                'employment_type':None,'skill_lexicon_sha256':'test','skill_lexicon_version':'v1',
                'occupation_lexicon_sha256':'test','occupation_lexicon_version':'v1',
                'occupation_ids':[],'required_skills':[]}


def seed_vacancies(count=16):
    repo = Repository(); repo.bootstrap_owners({42})
    address = 'https://t.me/fixture_'+uuid.uuid4().hex[:12]
    source_id = repo.save_source(42,'telegram','Fixture',address,3600,True)
    run = uuid.uuid4()
    with connect() as con:
        source = con.execute('SELECT * FROM sources WHERE id=%s',(source_id,)).fetchone()
        con.execute("INSERT INTO parser_runs(id,source_id,state) VALUES (%s,%s,'running')", (run,source_id))
    now = datetime.now(timezone.utc)
    posts = [SimpleNamespace(url=address+'/'+str(i),text=f'Тестовая вакансия {i} {address}',published_at=now) for i in range(count)]
    store_batch(source,run,SimpleNamespace(publications=posts,cursor={'source':{'high_water':count}}),FakeInference())


def test_documents_versioning_and_readiness():
    client = configure_documents(); user = 810001
    client.register(user)
    with pytest.raises(PermissionError): client.ready(user)
    docs = {d['kind']:d for d in client.documents(user)}
    client.accept(user,docs['terms']['id'])
    with pytest.raises(PermissionError): client.ready(user)
    client.accept(user,docs['consent']['id']); client.ready(user)
    with pytest.raises(ValueError):
        client.publish_document(42,'terms',docs['terms']['version'],docs['terms']['url'],'Изменённый текст')
    client.publish_document(42,'terms',uuid.uuid4().hex,'https://example.test/new','Новая редакция')
    with pytest.raises(PermissionError): client.ready(user)
    with pytest.raises(ValueError): client.accept(user,docs['terms']['id'])


def test_trial_delivery_filters_and_saved_cards():
    client = configure_documents(); user = 810002
    onboarding(client,user); seed_vacancies()
    client.save_product_settings(42,5,'Стоимость согласовывается','Портфолио','https://example.test/portfolio')
    client.draft(user,{'work_format':'remote'},name='Удалённо'); first = client.save_filter(user)
    client.new_draft(user); client.draft(user,{},name='Все'); second = client.save_filter(user)
    client.select_filter(user,first)
    assert len(client.filters(user)) == 2 and client.state(user)['active_filter_id'] == first
    batch = client.prepare_selection(user)
    assert len(batch['items']) == 5 and batch['number'] == 1
    with pytest.raises(ValueError): client.prepare_selection(user)
    client.finish_selection(user,batch['id'],'failed')
    assert client.status(user)['remaining'] == 3
    batch = client.prepare_selection(user,request_message_id=777)
    client.finish_selection(user,batch['id'],'delivered',123)
    assert client.prepare_selection(user,request_message_id=777)['existing_id'] == batch['id']
    assert client.status(user)['remaining'] == 2
    client.mark(user,batch['id'],1,'save')
    client.mark(user,batch['id'],2,'hide','salary')
    assert len(client.saved(user)) == 1
    assert client.item(user,batch['id'],2)['hidden']
    other = 810003; onboarding(client,other)
    assert client.item(other,batch['id'],1) is None
    for expected in (2,3):
        batch = client.prepare_selection(user)
        assert batch['number'] == expected
        client.finish_selection(user,batch['id'],'delivered',expected+123)
    assert client.status(user)['remaining'] == 0
    with pytest.raises(PermissionError): client.prepare_selection(user)
    client.select_filter(user,second)
    assert client.status(user)['remaining'] == 0


def test_one_time_activation_and_concurrent_redemption():
    client = configure_documents(); first, second = 810004, 810005
    onboarding(client,first); onboarding(client,second)
    with pytest.raises(PermissionError): client.create_activation(99999,30)
    token = client.create_activation(42,30)
    client.remember_activation(first,token); client.remember_activation(second,token)
    def redeem(user):
        try:
            return client.redeem_activation(user)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(redeem,(first,second)))
    assert sum(item is not None for item in results) == 1
    winner = first if results[0] else second
    end = client.status(winner)['until']
    next_token = client.create_activation(42,7)
    client.remember_activation(winner,next_token)
    assert (client.redeem_activation(winner)-end).days == 7
    with pytest.raises(ValueError): client.remember_activation(first,token)
    Repository().set_manager(42,810007,True)
    revoked = client.create_activation(810007,30)
    client.remember_activation(first,revoked)
    Repository().set_manager(42,810007,False)
    with pytest.raises(ValueError): client.redeem_activation(first)
    with pytest.raises(PermissionError): Repository().set_manager(810007,810008,True)


class FakeTelegramSession(BaseSession):
    def __init__(self):
        super().__init__(); self.sent=[]; self.inline=[]
    async def close(self): pass
    async def stream_content(self,*args,**kwargs):
        if False: yield b''
    async def make_request(self,bot,method,timeout=None):
        if isinstance(method,AnswerCallbackQuery): return True
        if isinstance(method,AnswerInlineQuery):
            self.inline.append(method); return True
        if isinstance(method,GetMe):
            return User(id=123456,is_bot=True,first_name='Test',username='fixture_test_bot')
        if isinstance(method,SendMessage):
            self.sent.append(method)
            return Message(message_id=len(self.sent),date=datetime.now(timezone.utc),
                           chat=Chat(id=int(method.chat_id),type='private'),text=method.text).as_(bot)
        raise AssertionError(type(method).__name__)


def test_telegram_buttons_connected_to_database():
    client = configure_documents(); user = 810006
    seed_vacancies()
    settings = Settings('123456:fixture',frozenset({42}),None,False,('testserver',))
    session = FakeTelegramSession(); bot = Bot(settings.bot_token,session=session)
    dp = Dispatcher(); dp.include_router(make_router(settings))
    person = User(id=user,is_bot=False,first_name='Test')
    async def message(text, index):
        update = Update(update_id=index,message=Message(message_id=index,date=datetime.now(timezone.utc),
            chat=Chat(id=user,type='private'),from_user=person,text=text))
        await dp.feed_update(bot,update)
    async def click(data,index):
        from aiogram.types import CallbackQuery
        msg = Message(message_id=100,date=datetime.now(timezone.utc),chat=Chat(id=user,type='private'),text='Меню')
        update = Update(update_id=index,callback_query=CallbackQuery(id=str(index),from_user=person,chat_instance='test',message=msg,data=data))
        await dp.feed_update(bot,update)
    async def run():
        await message('/start',1)
        assert 'Первые три' in session.sent[-1].text
        await click('search',2)
        assert 'условиями' in session.sent[-1].text
        docs = {d['kind']:d for d in client.documents(user)}
        await click('accept:'+str(docs['terms']['id']),3)
        await click('accept:'+str(docs['consent']['id']),4)
        await click('filter:new',5)
        await click('clear:occupation',9)
        await click('format:remote',6)
        await click('clear:salary',10)
        await click('clear:skill',11)
        await click('filter:save',7)
        assert client.state(user)['active_filter_id'] is not None
        await click('search',8)
        assert 'подборка 1 из 3' in session.sent[-1].text
        await click('search',12)
        assert 'уже выдана' in session.sent[-1].text
        assert client.status(user)['remaining'] == 2
    asyncio.run(run())


def test_inline_manager_authorization():
    settings = Settings('123456:fixture',frozenset({42}),None,False,('testserver',))
    Repository().bootstrap_owners({42})
    session = FakeTelegramSession(); bot = Bot(settings.bot_token,session=session)
    dp = Dispatcher(); dp.include_router(make_router(settings))
    async def run():
        from aiogram.types import InlineQuery
        async def query(user,index):
            update = Update(update_id=index,inline_query=InlineQuery(id=str(index),from_user=User(id=user,is_bot=False,first_name='Test'),query='45',offset=''))
            await dp.feed_update(bot,update)
        await query(99999,1)
        assert session.inline[-1].results == []
        await query(42,2)
        result = session.inline[-1]
        assert result.is_personal and result.cache_time == 0 and len(result.results) == 1
        assert 'grant_' in result.results[0].reply_markup.inline_keyboard[0][0].url
    asyncio.run(run())


def test_owner_product_panel():
    from test_product import _csrf, _init_data
    settings = Settings('offline-fixture-token',frozenset({42}),None,False,('testserver',))
    Repository().bootstrap_owners({42})
    app = create_app(settings)
    with TestClient(app,base_url='https://testserver') as client:
        assert client.get('/product').status_code == 401
        assert client.get('/team').status_code == 401
        page = client.get('/miniapp')
        assert client.post('/miniapp',data={'csrf':_csrf(page),'init_data':_init_data(settings.bot_token,42)},follow_redirects=False).status_code == 303
        page = client.get('/product'); assert page.status_code == 200
        csrf = _csrf(page)
        result = client.post('/product',data={'csrf':csrf,'selection_limit':'5','pricing_text':'Тестовая цена','services_text':'Тест услуги','services_url':'https://example.test/service'},follow_redirects=False)
        assert result.status_code == 303
        result = client.post('/product/documents',data={'csrf':csrf,'kind':'offer','version':uuid.uuid4().hex,'url':'https://example.test/offer','body':'Тестовая оферта'},follow_redirects=False)
        assert result.status_code == 303 and 'Тестовая цена' in client.get('/product').text
        result = client.post('/team',data={'csrf':csrf,'telegram_id':'812345','action':'enable'},follow_redirects=False)
        assert result.status_code == 303 and ClientRepository().can_issue(812345)


def test_salary_unknown_policy():
    from database.search import page
    seed_vacancies(1)
    value = {'work_format':'remote','salary_min':100000,'currency':'RUB','salary_period':'month'}
    none, _ = page(819999,value,page_size=20)
    value['unknown'] = {'salary':'include'}
    included, _ = page(819999,value,page_size=20)
    assert not none and included
