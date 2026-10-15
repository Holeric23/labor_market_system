"""Aiogram controller using the shared catalog and durable structured filters."""
import asyncio
import json
import logging
from aiogram import Router, F
from market_system.search.service import InvalidCursor
from market_system.search.store import connect
from .catalog import CatalogSearch, parse_filter, card_text
from .application.services import UIMessageManager, text_chunks

HELP='''Поиск: /search {"post_type":"vacancy"}
Далее: /next. Подписаться на текущий фильтр: /subscribe
Или /subscribe {"post_type":"vacancy","skill_ids":[123]}
Мои подписки: /subscriptions; удалить: /unsubscribe ID
Классы: /classes skill Python или /classes occupation дизайнер
Фильтры общего каталога: occupation_ids, skill_ids, skills_mode ALL/ANY, skill_roles required/preferred/possessed, published_from/published_until (ISO с часовым поясом), work_format, schedule, employment_type, salary_min + currency + salary_period, tax_mode, unknown.
Пример: /search {"post_type":"vacancy","work_format":"remote","salary_min":100000,"currency":"RUB","salary_period":"month"}
Неизвестные значения: unknown={"work_format":"only"}; include включает неизвестные, exclude исключает. Классы и зарплата всегда строгие. Даты — фиксированный интервал, не скользящий.
Владелец: /allow TELEGRAM_ID; /deny TELEGRAM_ID; /map OLD_ID NEW_ID MODEL_VERSION (явное подтверждение соответствия).
Уведомления: только новые уникальные вакансии; изменения старых карточек не рассылаются.'''


class Access:
    def __init__(self,store):
        self.store=store

    async def check_access(self,user):
        try:
            await asyncio.to_thread(self.store.role,user)
            return True
        except PermissionError:
            return False


class Controller:
    def __init__(self,store,users,telegram):
        self.store,self.telegram=store,telegram
        self.search=CatalogSearch(store.path)
        self.ui=UIMessageManager(telegram,users)

    def session(self,user,value=None,cursor=None):
        with self.store.transaction() as con:
            self.store.access(con,user)
            if value is not None:
                con.execute('INSERT INTO telegram_search_sessions VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET filter_json=excluded.filter_json,cursor=excluded.cursor',(user,json.dumps(value),cursor))
            row=con.execute('SELECT * FROM telegram_search_sessions WHERE user_id=?',(user,)).fetchone()
            return dict(row) if row else None

    async def respond(self,user,text):
        for chunk in text_chunks(text):
            await self.telegram.send_message(user,chunk)

    async def command(self,user,text):
        await asyncio.to_thread(self.store.role,user)
        command,_,argument=text.strip().partition(' ')
        command=command.split('@')[0]
        try:
            if command in ('/start','/menu','/help','/cancel'):
                await self.ui.show_temporary_menu(user,HELP,[])
            elif command in ('/search','/next'):
                cursor=None
                if command=='/next':
                    session=await asyncio.to_thread(self.session,user)
                    if not session or not session['cursor']:
                        raise ValueError('Нет следующей страницы. Начните /search.')
                    value=json.loads(session['filter_json']);cursor=session['cursor']
                else:
                    value=parse_filter(json.loads(argument or '{"post_type":"vacancy"}'))
                page=await self.search.search(value,cursor)
                for card in page.cards:
                    await self.respond(user,card_text(card))
                await asyncio.to_thread(self.session,user,value,page.next_cursor)
                await self.respond(user,f'Найдено на странице: {len(page.cards)}. '+('Далее /next. ' if page.next_cursor else 'Конец выдачи. ')+'Подписаться: /subscribe')
            elif command=='/subscribe':
                if argument:
                    value=json.loads(argument)
                else:
                    session=await asyncio.to_thread(self.session,user)
                    if not session:
                        raise ValueError('Сначала выполните /search или укажите JSON-фильтр.')
                    value=json.loads(session['filter_json'])
                identity=await asyncio.to_thread(self.store.subscribe,user,value)
                await self.respond(user,'Подписка сохранена: '+identity)
            elif command=='/subscriptions':
                rows=await asyncio.to_thread(self.store.subscriptions,user)
                for row in rows:
                    await self.respond(user,row['id']+' · '+('Требует обновления: '+str(row['reason']) if row['status']=='needs_update' else 'Активна')+'\n'+row['filter_json']+'\nУдалить: /unsubscribe '+row['id'])
                if not rows:
                    await self.respond(user,'Подписок нет.')
            elif command=='/unsubscribe':
                deleted=await asyncio.to_thread(self.store.delete,user,argument.strip())
                await self.respond(user,'Подписка удалена.' if deleted else 'Собственная подписка не найдена.')
            elif command in ('/allow','/deny'):
                await asyncio.to_thread(self.store.grant,user,int(argument),command=='/allow')
                await self.respond(user,'Доступ обновлён.')
            elif command=='/map':
                old,new,model=argument.split()
                await asyncio.to_thread(self.store.confirm_mapping,user,int(old),int(new),model)
                await self.respond(user,'Соответствие подтверждено. Проверьте /subscriptions.')
            elif command=='/classes':
                kind,_,query=argument.partition(' ')
                if kind not in ('skill','occupation'):
                    raise ValueError('/classes skill НАЗВАНИЕ или /classes occupation НАЗВАНИЕ')
                def lookup():
                    with connect(self.store.path) as con:
                        return [dict(r) for r in con.execute('SELECT class_id,name,namespace,definition_version FROM classes WHERE namespace LIKE ? AND instr(lower(name),lower(?))>0 ORDER BY class_id LIMIT 30',(kind+':%',query))]
                rows=await asyncio.to_thread(lookup)
                await self.respond(user,'\n'.join(f"{r['class_id']}: {r['name']} [{r['namespace']}; {r['definition_version']}]" for r in rows) or 'Классы не найдены.')
            else:
                await self.respond(user,'Используйте /help.')
        except InvalidCursor:
            await self.respond(user,'Каталог изменился. Начните /search заново; фильтр подписки не содержит курсор.')
        except (ValueError,PermissionError) as exc:
            # JSON errors and access/filter validation are safe; no network detail.
            await self.respond(user,str(exc)[:1000])


def router(controller):
    result=Router(name='shared_catalog')
    @result.message(F.text)
    async def text(message):
        await controller.command(message.from_user.id,message.text)
    @result.errors()
    async def failed(event):
        logging.getLogger(__name__).error('Bot update failed (%s)',type(event.exception).__name__)
        return True
    return result
