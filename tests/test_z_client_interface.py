"""Autonomous checks with synthetic documents, vacancies and Telegram updates."""
import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, AnswerInlineQuery, DeleteMessage, GetMe, SendMessage
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
    with connect() as con:
        con.execute('UPDATE clients SET manual_access=true WHERE telegram_id=%s',(user,))
    with pytest.raises(PermissionError): client.prepare_selection(user)
    client.select_filter(user,second)
    assert client.status(user)['remaining'] == 0


def test_saved_vacancies_pagination():
    client=configure_documents(); user=810012
    onboarding(client,user); seed_vacancies(35)
    Repository().set_manager(42,user,True)
    client.save_product_settings(42,5,'Стоимость согласовывается')
    client.draft(user,{},name='Все вакансии'); client.save_filter(user)
    for _ in range(7):
        selection=client.prepare_selection(user)
        assert selection and len(selection['items']) == 5
        client.finish_selection(user,selection['id'],'delivered',123)
        for ordinal in range(1,6):
            client.mark(user,selection['id'],ordinal,'save')
    pages=[client.saved(user,page) for page in range(4)]
    assert [len(page) for page in pages] == [11,11,11,5]
    identities=[item['id'] for page in pages for item in page[:10]]
    assert len(identities)==35 and len(set(identities))==35
    with pytest.raises(ValueError): client.saved(user,-1)
    with connect() as con: con.execute('UPDATE app_settings SET customer_bot_enabled=true WHERE singleton=true')
    settings=Settings('123456:fixture',frozenset({42}),None,('testserver',))
    session=FakeTelegramSession(); bot=Bot(settings.bot_token,session=session)
    dp=Dispatcher(); dp.include_router(make_router(settings))
    async def check_navigation():
        from aiogram.types import CallbackQuery
        person=User(id=user,is_bot=False,first_name='Test')
        for index,data in enumerate(('saved','saved:1','saved:3'),1):
            msg=Message(message_id=500+index,date=datetime.now(timezone.utc),chat=Chat(id=user,type='private'),text='Меню')
            query=CallbackQuery(id=str(index),from_user=person,chat_instance='fixture',message=msg,data=data)
            await dp.feed_update(bot,Update(update_id=index,callback_query=query))
            assert f'страница {1 if index == 1 else 2 if index == 2 else 4}' in session.sent[-1].text
            actions=[button.callback_data for row in session.sent[-1].reply_markup.inline_keyboard for button in row]
            assert ('saved:4' not in actions) if index == 3 else ('saved:'+str(index) in actions)
    asyncio.run(check_navigation())


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


def test_staff_access_and_role_revocation():
    repo=Repository(); repo.bootstrap_owners({42,812400}); repo.bootstrap_owners({42})
    client=ClientRepository(); manager=812401; customer=812402
    assert repo.staff_role(812400,{42}) is None and not client.can_issue(812400)
    repo.set_manager(42,manager,True)
    for user in (42,manager,customer): client.register(user)
    seed_vacancies(2)
    for user in (42,manager):
        client.ready(user)
        assert client.status(user)['staff']
        client.draft(user,{},name='Тест'); client.save_filter(user)
        selection=client.prepare_selection(user)
        assert selection and not selection['trial']
        client.finish_selection(user,selection['id'],'delivered',123)
        with connect() as con:
            assert con.execute('SELECT count(*) AS n FROM product_document_acceptances WHERE telegram_id=%s',(user,)).fetchone()['n'] == 0
    with pytest.raises((ValueError,PermissionError)): client.ready(customer)
    with pytest.raises(PermissionError): repo.managers(manager)
    repo.set_manager(42,manager,False)
    assert not client.can_issue(manager)
    with pytest.raises((ValueError,PermissionError)): client.ready(manager)


class FakeTelegramSession(BaseSession):
    def __init__(self):
        super().__init__(); self.sent=[]; self.inline=[]; self.callbacks=[]; self.deleted=[]; self.fail_send=False; self.fail_delete=False
    async def close(self): pass
    async def stream_content(self,*args,**kwargs):
        if False: yield b''
    async def make_request(self,bot,method,timeout=None):
        if isinstance(method,AnswerCallbackQuery):
            self.callbacks.append(method); return True
        if isinstance(method,AnswerInlineQuery):
            self.inline.append(method); return True
        if isinstance(method,DeleteMessage):
            if self.fail_delete: raise TelegramBadRequest(method=method,message='fixture')
            self.deleted.append((int(method.chat_id),method.message_id)); return True
        if isinstance(method,GetMe):
            return User(id=123456,is_bot=True,first_name='Test',username='fixture_test_bot')
        if isinstance(method,SendMessage):
            if self.fail_send: raise TelegramBadRequest(method=method,message='fixture')
            self.sent.append(method)
            return Message(message_id=10000+len(self.sent),date=datetime.now(timezone.utc),
                           chat=Chat(id=int(method.chat_id),type='private'),text=method.text).as_(bot)
        raise AssertionError(type(method).__name__)


def test_telegram_buttons_connected_to_database():
    with connect() as con: con.execute('UPDATE app_settings SET customer_bot_enabled=true WHERE singleton=true')
    client = configure_documents(); user = 810006
    seed_vacancies()
    settings = Settings('123456:fixture',frozenset({42}),None,('testserver',))
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


def test_single_page_navigation_and_saved_vacancies():
    from aiogram.types import CallbackQuery
    client = configure_documents(); user=810010
    onboarding(client,user); seed_vacancies(2)
    client.draft(user,{},name='Все вакансии'); client.save_filter(user)
    with connect() as con: con.execute('UPDATE app_settings SET customer_bot_enabled=true WHERE singleton=true')
    settings=Settings('123456:fixture',frozenset({42}),None,('testserver',))
    session=FakeTelegramSession(); bot=Bot(settings.bot_token,session=session)
    dp=Dispatcher(); dp.include_router(make_router(settings))
    person=User(id=user,is_bot=False,first_name='Test')
    async def command(text,index,dispatcher=dp):
        msg=Message(message_id=index,date=datetime.now(timezone.utc),chat=Chat(id=user,type='private'),from_user=person,text=text)
        await dispatcher.feed_update(bot,Update(update_id=index,message=msg))
    async def click(data,index,message_id):
        message=Message(message_id=message_id,date=datetime.now(timezone.utc),chat=Chat(id=user,type='private'),text='Меню')
        query=CallbackQuery(id=str(index),from_user=person,chat_instance='fixture',message=message,data=data)
        await dp.feed_update(bot,Update(update_id=index,callback_query=query))
    async def exercise():
        await command('/start',101)
        first=10000+len(session.sent)
        assert (user,101) in session.deleted
        await click('how',102,first)
        second=10000+len(session.sent)
        assert (user,first) in session.deleted
        await click('search',103,second)
        selection=10000+len(session.sent)
        assert (user,second) in session.deleted and (user,selection) not in session.deleted
        card_data=session.sent[-1].reply_markup.inline_keyboard[0][0].callback_data
        await click(card_data,104,selection)
        card=10000+len(session.sent)
        assert (user,selection) in session.deleted
        save_data=next(button.callback_data for row in session.sent[-1].reply_markup.inline_keyboard for button in row if button.text == 'Сохранить вакансию')
        await click(save_data,105,card)
        saved_card=10000+len(session.sent)
        assert (user,card) in session.deleted and len(client.saved(user)) == 1
        assert any(button.text == 'Убрать из сохранённого' for row in session.sent[-1].reply_markup.inline_keyboard for button in row)
        await click('saved',106,saved_card)
        saved_page=10000+len(session.sent)
        assert (user,saved_card) in session.deleted and 'Сохранённые вакансии' in session.sent[-1].text
        assert any(button.callback_data == card_data for row in session.sent[-1].reply_markup.inline_keyboard for button in row)
        restarted=Dispatcher(); restarted.include_router(make_router(settings))
        await command('/start',112,restarted)
        assert len(client.saved(user)) == 1
        await click('saved',113,10000+len(session.sent))
        saved_page=10000+len(session.sent)
        await click(card_data,107,saved_page)
        reopened=10000+len(session.sent)
        unsave_data=next(button.callback_data for row in session.sent[-1].reply_markup.inline_keyboard for button in row if button.text == 'Убрать из сохранённого')
        await click(unsave_data,108,reopened)
        assert not client.saved(user)
        unsaved_card=10000+len(session.sent)
        await click('home',109,unsaved_card)
        final_menu=10000+len(session.sent)
        assert (user,unsaved_card) in session.deleted
        # A new dispatcher simulates a restart: the last page remains tracked.
        restarted=Dispatcher(); restarted.include_router(make_router(settings))
        await command('/start',110,restarted)
        assert (user,final_menu) in session.deleted and (user,110) in session.deleted
        # A click on an old untracked card is removed as well as the current page.
        current=10000+len(session.sent)
        await click('home',111,9999)
        assert (user,current) in session.deleted and (user,9999) in session.deleted
        from aiogram.types import PhotoSize
        photo=Message(message_id=114,date=datetime.now(timezone.utc),chat=Chat(id=user,type='private'),from_user=person,
                      photo=[PhotoSize(file_id='photo',file_unique_id='photo',width=10,height=10)])
        previous=10000+len(session.sent)
        await dp.feed_update(bot,Update(update_id=114,message=photo))
        assert (user,114) in session.deleted and (user,previous) in session.deleted
        with connect() as con:
            assert con.execute("SELECT count(*) AS n FROM selections WHERE telegram_id=%s AND status='delivered'",(user,)).fetchone()['n']==1
    asyncio.run(exercise())


def test_failed_send_keeps_previous_menu_and_user_input():
    user=810011; settings=Settings('123456:fixture',frozenset({42}),None,('testserver',))
    with connect() as con: con.execute('UPDATE app_settings SET customer_bot_enabled=true WHERE singleton=true')
    session=FakeTelegramSession(); bot=Bot(settings.bot_token,session=session); dp=Dispatcher(); dp.include_router(make_router(settings))
    person=User(id=user,is_bot=False,first_name='Test')
    async def message(index):
        msg=Message(message_id=index,date=datetime.now(timezone.utc),chat=Chat(id=user,type='private'),from_user=person,text='/start')
        await dp.feed_update(bot,Update(update_id=index,message=msg))
    async def exercise():
        await message(301); previous=10000+len(session.sent)
        session.fail_send=True
        with pytest.raises(TelegramBadRequest): await message(302)
        assert (user,previous) not in session.deleted and (user,302) not in session.deleted
        with connect() as con: assert con.execute('SELECT message_id FROM telegram_ui_messages WHERE chat_id=%s',(user,)).fetchone()['message_id']==previous
        session.fail_send=False; session.fail_delete=True
        await message(303)  # Telegram can reject deletion; navigation still succeeds.
        assert len(session.sent)==2
    asyncio.run(exercise())


def test_inline_manager_authorization():
    with connect() as con: con.execute('UPDATE app_settings SET customer_bot_enabled=true WHERE singleton=true')
    settings = Settings('123456:fixture',frozenset({42}),None,('testserver',))
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


def test_bot_availability_and_admin_access():
    from aiogram.types import CallbackQuery, InlineQuery
    from test_product import _csrf, _init_data
    from bot.telegram import UNAVAILABLE
    repo = Repository(); repo.bootstrap_owners({42}); repo.set_manager(42,812403,True)
    settings = Settings('123456:fixture',frozenset({42}),'https://example.test/miniapp',('testserver',))
    app = create_app(settings)
    session = FakeTelegramSession(); bot = Bot(settings.bot_token,session=session)
    dp = Dispatcher(); dp.include_router(make_router(settings))
    with connect() as con:
        con.execute('UPDATE app_settings SET customer_bot_enabled=false WHERE singleton=true')
    user = 81123456
    async def feed():
        person=User(id=user,is_bot=False,first_name='Test')
        admin=User(id=42,is_bot=False,first_name='Owner')
        manager=User(id=812403,is_bot=False,first_name='Manager')
        def msg(sender,text,index):
            return Update(update_id=index,message=Message(message_id=index,date=datetime.now(timezone.utc),chat=Chat(id=sender.id,type='private'),from_user=sender,text=text))
        await dp.feed_update(bot,msg(person,'/start',1))
        assert session.sent[-1].text == UNAVAILABLE
        with connect() as con:
            assert not con.execute('SELECT 1 FROM clients WHERE telegram_id=%s',(user,)).fetchone()
        message=Message(message_id=2,date=datetime.now(timezone.utc),chat=Chat(id=user,type='private'),text='Menu')
        await dp.feed_update(bot,Update(update_id=2,callback_query=CallbackQuery(id='cb',from_user=person,chat_instance='t',message=message,data='search')))
        assert session.callbacks[-1].show_alert and session.callbacks[-1].text == UNAVAILABLE
        await dp.feed_update(bot,Update(update_id=3,inline_query=InlineQuery(id='q',from_user=admin,query='30',offset='')))
        assert len(session.inline[-1].results) == 1
        await dp.feed_update(bot,msg(admin,'/admin',4))
        assert session.sent[-1].reply_markup.inline_keyboard[0][0].web_app.url == 'https://example.test/miniapp'
        await dp.feed_update(bot,msg(admin,'/start',5))
        assert 'Доступ для команды открыт' in session.sent[-1].text
        await dp.feed_update(bot,msg(manager,'/start',6))
        assert 'Доступ для команды открыт' in session.sent[-1].text
        before=len(session.sent)
        await dp.feed_update(bot,msg(manager,'/admin',7))
        assert len(session.sent)==before
        message=Message(message_id=8,date=datetime.now(timezone.utc),chat=Chat(id=manager.id,type='private'),text='Menu')
        await dp.feed_update(bot,Update(update_id=8,callback_query=CallbackQuery(id='mgr',from_user=manager,chat_instance='t',message=message,data='begin')))
        assert '1 из 4' in session.sent[-1].text
        await dp.feed_update(bot,Update(update_id=9,inline_query=InlineQuery(id='qm',from_user=manager,query='30',offset='')))
        assert len(session.inline[-1].results)==1
        repo.set_manager(42,manager.id,False)
        await dp.feed_update(bot,msg(manager,'/start',10))
        assert session.sent[-1].text==UNAVAILABLE
    with TestClient(app,base_url='https://testserver') as manager_panel:
        login=manager_panel.get('/miniapp')
        response=manager_panel.post('/miniapp',data={'csrf':_csrf(login),'init_data':_init_data(settings.bot_token,812403)})
        assert response.status_code==403 and manager_panel.get('/clients').status_code==401
    asyncio.run(feed())
    with TestClient(app,base_url='https://testserver') as client:
        assert client.post('/settings/bot',data={'action':'enable','revision':'0','csrf':'invalid'}).status_code==401
        login=client.get('/miniapp')
        assert client.post('/miniapp',data={'csrf':_csrf(login),'init_data':_init_data(settings.bot_token,42)},follow_redirects=False).status_code==303
        page=client.get('/settings'); csrf=_csrf(page)
        assert 'временно недоступен' in page.text
        assert '<title>Настройки</title>' in page.text
        assert page.text.count('action="/settings/bot"') == 1
        rev=repo.public_settings()['revision']
        assert client.post('/settings/bot',data={'action':'enable','revision':str(rev),'csrf':'invalid'}).status_code==403
        assert client.post('/settings/bot',data={'action':'enable','revision':str(rev),'csrf':csrf},follow_redirects=False).status_code==303
        assert repo.bot_available()
        assert client.post('/settings/bot',data={'action':'disable','revision':str(rev),'csrf':csrf}).status_code==400
        assert 'доступен клиентам' in client.get('/settings').text
    async def enabled():
        person=User(id=user,is_bot=False,first_name='Test')
        update=Update(update_id=10,message=Message(message_id=10,date=datetime.now(timezone.utc),chat=Chat(id=user,type='private'),from_user=person,text='/start'))
        await dp.feed_update(bot,update)
        assert 'Первые три' in session.sent[-1].text
    asyncio.run(enabled())
    with pytest.raises(PermissionError):
        repo.set_bot_available(99999,False,repo.public_settings()['revision'])


def test_owner_product_panel():
    from test_product import _csrf, _init_data
    settings = Settings('offline-fixture-token',frozenset({42}),None,('testserver',))
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
        assert 'Другие услуги ПРОЩЕ' not in client.get('/product').text
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
