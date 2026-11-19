"""Telegram interface for customer search and owner administration."""
import asyncio
import uuid

from aiogram import F, Router, BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo,
                           InlineQueryResultArticle, InputTextMessageContent, Message, CallbackQuery, InlineQuery)
from database.client import ClientRepository, DOCUMENT_LABELS, REQUIRED
from database.repository import Repository
from database.search import normalize
from bot.ui_messages import UIMessageManager


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(**item) for item in row] for row in rows])


def button(text, data):
    return {'text':text, 'callback_data':data}


def link(text, url):
    return {'text':text, 'url':url}


def home_keyboard():
    return keyboard([[button('Найти вакансии','search')],
                     [button('Мои фильтры','filters'),button('Сохранённое','saved')],
                     [button('Мой доступ','access')],
                     [button('Документы и помощь','documents')]])


UNAVAILABLE = 'Извините, бот сейчас недоступен. Пожалуйста, попробуйте позже.'


class AvailabilityMiddleware(BaseMiddleware):
    def __init__(self, repository, owner_ids, ui):
        self.repository = repository
        self.owner_ids = owner_ids
        self.ui = ui

    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.from_user and event.chat.type == 'private':
            command = (event.text or '').split(maxsplit=1)
            if event.chat.id == event.from_user.id and event.from_user.id in self.owner_ids and command and command[0].split('@')[0] == '/admin':
                return await handler(event, data)
        try:
            available = await asyncio.to_thread(self.repository.bot_available)
        except Exception:
            available = False
        if available:
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer(UNAVAILABLE, show_alert=True)
        elif isinstance(event, InlineQuery):
            await event.answer([], cache_time=0, is_personal=True)
        elif isinstance(event, Message) and event.from_user and event.chat.type == 'private' and event.chat.id == event.from_user.id:
            await self.ui.menu(event,UNAVAILABLE)


def make_router(settings):
    router = Router(name='product')
    repository, clients = Repository(), ClientRepository()
    ui = UIMessageManager()
    availability = AvailabilityMiddleware(repository, settings.owner_ids, ui)
    router.message.outer_middleware(ui)
    router.callback_query.outer_middleware(ui)
    router.message.outer_middleware(availability)
    router.callback_query.outer_middleware(availability)
    router.inline_query.outer_middleware(availability)

    async def run(method, *args, **kwargs):
        return await asyncio.to_thread(method, *args, **kwargs)

    def private(message, user):
        return bool(message and user and message.chat.type == 'private' and message.chat.id == user.id)

    async def contact_keyboard():
        config = await run(clients.settings)
        rows = [[link('Связаться с менеджером',config['manager_url'])]] if config['manager_url'] else []
        rows.append([button('Главное меню','home')])
        return keyboard(rows)

    async def home(message):
        await ui.menu(message,'Привет, это ПРОЩЕ. Помогу найти вакансии по твоему направлению. Первые три подборки бесплатные.', reply_markup=home_keyboard())

    async def documents(message, user):
        docs = {item['kind']:item for item in await run(clients.documents,user)}
        rows = [[link(DOCUMENT_LABELS[kind],docs[kind]['url'])] for kind in ('terms','privacy','consent','offer') if kind in docs]
        missing = [DOCUMENT_LABELS[k] for k in REQUIRED if k not in docs]
        if missing:
            text = 'Документы ещё готовятся. Поиск будет доступен после их публикации.\n\nНе опубликованы: ' + ', '.join(missing) + '.'
        elif not docs['terms']['accepted']:
            text = 'Перед началом ознакомьтесь с условиями использования.'
            rows.append([button('Принимаю условия',f"accept:{docs['terms']['id']}")])
        elif not docs['consent']['accepted']:
            text = 'Ознакомьтесь с согласием на обработку персональных данных и политикой.'
            rows.append([button('Даю согласие',f"accept:{docs['consent']['id']}")])
        else:
            text = 'Документы и помощь. Вы можете открыть актуальные редакции в любое время.'
            state = await run(clients.state,user)
            if state['pending_token_hash']:
                rows.append([button('Активировать доступ','activate')])
            rows.append([button('Продолжить','begin')])
        config = await run(clients.settings)
        if config['manager_url']:
            rows.append([link('Связаться с поддержкой',config['manager_url'])])
        rows.append([button('Главное меню','home')])
        await ui.menu(message,text,reply_markup=keyboard(rows))

    async def require_ready(message, user):
        try:
            await run(clients.ready,user)
            return True
        except PermissionError as exc:
            if 'ограничен' in str(exc):
                await ui.menu(message,str(exc),reply_markup=await contact_keyboard())
            else:
                await documents(message,user)
        except ValueError:
            await documents(message,user)
        return False

    async def filter_menu(message, user):
        state = await run(clients.state,user)
        f = normalize(state['draft_filter'])
        classes = await run(clients.class_names,f['occupation_ids']+f['skill_ids'])
        names = {item['id']:item['name'] for item in classes}
        occupation = ', '.join(names.get(i,str(i)) for i in f['occupation_ids']) or 'Не важно'
        skills = ', '.join(names.get(i,str(i)) for i in f['skill_ids']) or 'Не важно'
        formats = {'remote':'Удалённо','onsite':'Офис','hybrid':'Гибрид',None:'Не важно'}
        salary = f"от {f['salary_min']:g} {f['currency']} / {f['salary_period']}" if f['salary_min'] is not None else 'Не важно'
        text = (f"Настройки поиска: {state['draft_name']}\n\nСпециальность: {occupation}\n"
                f"Формат: {formats.get(f['work_format'],f['work_format'])}\nЗарплата: {salary}\nНавыки: {skills}")
        rows = [[button('Специальность','edit:occupation'),button('Формат','edit:format')],
                [button('Зарплата','salary:settings'),button('Навыки','edit:skill')],
                [button('Название фильтра','edit:name')],
                [button('Сохранить как новый','filter:save')]]
        if state['editing_filter_id']:
            rows.append([button('Обновить выбранный фильтр','filter:update')])
            rows.append([button('Удалить этот фильтр',f"filter:delete:{state['editing_filter_id'].hex}")])
        rows.extend([[button('Мои фильтры','filters')],[button('Главное меню','home')]])
        await ui.menu(message,text,reply_markup=keyboard(rows))

    async def filter_list(message, user):
        rows = await run(clients.filters,user)
        buttons = [[button(row['name'],f"filter:select:{row['id'].hex}")] for row in rows]
        buttons.extend([[button('Создать фильтр','filter:new')],[button('Изменить текущий','filter:edit')],[button('Главное меню','home')]])
        await ui.menu(message,'Мои фильтры. Выберите сохранённый фильтр или создайте новый.' if rows else 'Сохранённых фильтров пока нет.',reply_markup=keyboard(buttons))

    async def wizard(message, user, step):
        await run(clients.wizard_step,user,step)
        if step == 4:
            await filter_menu(message,user)
            return
        if step == 0:
            text = '1 из 4. Какую специальность ищете?'
            rows = [[button('Указать специальность','edit:occupation')],[button('Пропустить','clear:occupation')]]
        elif step == 1:
            text = '2 из 4. Какой формат работы подходит?'
            rows = [[button('Удалённо','format:remote'),button('Офис','format:onsite')],
                    [button('Гибрид','format:hybrid'),button('Не важно','format:any')]]
        elif step == 2:
            text = '3 из 4. Укажите минимальную оплату или пропустите этот шаг.'
            rows = [[button('Указать зарплату','edit:salary')],[button('Пропустить','clear:salary')]]
        else:
            text = '4 из 4. Хотите уточнить навыки?'
            rows = [[button('Добавить навык','edit:skill')],[button('Пропустить','clear:skill')]]
        if step > 0:
            rows.append([button('Назад',f'wizard:{step-1}')])
        rows.append([button('Отмена','wizard:cancel')])
        await ui.menu(message,text,reply_markup=keyboard(rows))

    async def access(message, user):
        value = await run(clients.status,user,user in settings.owner_ids)
        config = await run(clients.settings)
        if value['paid']:
            description = 'Доступ активен до ' + value['until'].strftime('%d.%m.%Y %H:%M UTC') + '.'
        elif value['manual']:
            description = 'Доступ открыт администратором.'
        else:
            description = f"Осталось бесплатных подборок: {value['remaining']} из 3."
        description += '\n\n' + (config['pricing_text'] or 'Стоимость доступа уточняйте у менеджера.')
        rows = []
        state = await run(clients.state,user)
        if state['pending_token_hash']:
            rows.append([button('Активировать доступ','activate')])
        if config['manager_url']:
            rows.append([link('Связаться с менеджером',config['manager_url'])])
        rows.extend([[button('Документы','documents')],[button('Главное меню','home')]])
        await ui.menu(message,description,reply_markup=keyboard(rows))

    async def card(message, user, identity, ordinal):
        item = await run(clients.item,user,identity,ordinal)
        if not item:
            raise ValueError('Вакансия недоступна.')
        text = item['text'][:3000]
        if len(item['text']) > 3000:
            text += '…'
        rows = []
        if item['source_url'] and item['source_url'].startswith('https://'):
            rows.append([link('Оригинал',item['source_url'])])
        key = identity.hex
        rows.append([button('Убрать из сохранённого' if item['saved'] else 'Сохранить',f"mark:{'unsave' if item['saved'] else 'save'}:{key}:{ordinal}")])
        rows.append([button('Не подходит',f'reason:{key}:{ordinal}'),button('Уже откликнулся',f'mark:applied:{key}:{ordinal}')])
        rows.append([button('К подборке',f'selection:{key}'),button('Сохранённое','saved')])
        rows.append([button('Главное меню','home')])
        await ui.delivery(message,text,reply_markup=keyboard(rows),parse_mode=None)

    async def search(message, user):
        try:
            selection = await run(clients.prepare_selection,user,user in settings.owner_ids,message.message_id)
        except PermissionError:
            await access(message,user)
            return
        if not selection:
            await ui.menu(message,'Новых предложений по этим условиям пока нет. Бесплатная попытка сохранилась.',
                                 reply_markup=keyboard([[button('Настроить поиск','filter:edit')],[button('Главное меню','home')]]))
            return
        if 'existing_id' in selection:
            await ui.menu(message,'Эта подборка уже выдана.',reply_markup=keyboard([[button('Открыть подборку',f"selection:{selection['existing_id'].hex}")],[button('Главное меню','home')]]))
            return
        label = f"Пробная подборка {selection['number']} из 3.\n" if selection['trial'] else ''
        lines = []
        for index,item in enumerate(selection['items'],1):
            first = next((line.strip() for line in item['text'].splitlines() if line.strip()),'Вакансия')
            lines.append(f'{index}. {first[:110]}')
        text = label + f"Нашлось {len(lines)} предложений:\n\n" + '\n'.join(lines)
        if selection['trial'] and selection['number'] == 2:
            text += '\n\nОсталась одна бесплатная подборка.'
        elif selection['trial'] and selection['number'] == 3:
            text += '\n\nЭто последняя бесплатная подборка. Условия доступа можно посмотреть в меню.'
        choices = [button(str(i),f"card:{selection['id'].hex}:{i}") for i in range(1,len(lines)+1)]
        rows = [choices[i:i+5] for i in range(0,len(choices),5)]
        rows.append([button('Найти ещё','search')])
        rows.append([button('Мой доступ','access'),button('Главное меню','home')])
        try:
            sent = await ui.delivery(message,text,reply_markup=keyboard(rows),parse_mode=None)
        except (TelegramBadRequest,TelegramForbiddenError):
            await run(clients.finish_selection,user,selection['id'],'failed')
            raise
        except Exception:
            await run(clients.finish_selection,user,selection['id'],'uncertain')
            raise
        await run(clients.finish_selection,user,selection['id'],'delivered',sent.message_id)

    @router.message(Command('start','help','menu'))
    async def start(message):
        if not private(message,message.from_user):
            return
        await run(clients.register,message.from_user.id)
        args = (message.text or '').split(maxsplit=1)
        if len(args) == 2 and args[1].startswith('grant_'):
            try:
                await run(clients.remember_activation,message.from_user.id,args[1][6:])
                if await require_ready(message,message.from_user.id):
                    await ui.menu(message,'Доступ готов к активации.',reply_markup=keyboard([[button('Активировать доступ','activate')],[button('Главное меню','home')]]))
            except ValueError as exc:
                await ui.menu(message,str(exc),reply_markup=await contact_keyboard())
            return
        await ui.menu(message,'Привет, это ПРОЩЕ. Помогу найти вакансии по твоему направлению. Первые три подборки бесплатные.',
            reply_markup=keyboard([[button('Начать','begin')],[button('Как это работает','how')],[button('Документы и помощь','documents')]]))

    @router.inline_query()
    async def issue_inline(query):
        actor = query.from_user.id
        if not await run(clients.can_issue,actor):
            await query.answer([],cache_time=0,is_personal=True)
            return
        raw = query.query.strip()
        try:
            days = int(raw) if raw else (await run(repository.public_settings))['default_access_days']
            if not 1 <= days <= 3650:
                raise ValueError()
            bot = await query.bot.get_me()
            if not bot.username:
                raise ValueError()
            token = await run(clients.create_activation,actor,days)
            result = InlineQueryResultArticle(
                id=uuid.uuid4().hex,title=f'Доступ на {days} дн.',
                description='Отправить клиенту кнопку одноразовой активации',
                input_message_content=InputTextMessageContent(message_text='Доступ готов к активации. Откройте бота, ознакомьтесь с условиями и активируйте доступ.'),
                reply_markup=keyboard([[link('Активировать доступ',f'https://t.me/{bot.username}?start=grant_{token}')]]))
            await query.answer([result],cache_time=0,is_personal=True)
        except ValueError:
            await query.answer([],cache_time=0,is_personal=True)

    @router.message(Command('admin'))
    async def admin(message):
        if not private(message,message.from_user) or message.from_user.id not in settings.owner_ids:
            return
        if not settings.miniapp_url:
            await ui.menu(message,'Админ-панель пока не настроена.')
            return
        await ui.menu(message,'Управление системой:',reply_markup=keyboard([[{'text':'Открыть админ-панель','web_app':WebAppInfo(url=settings.miniapp_url)}]]))

    @router.message(Command('allow','deny'))
    async def set_access(message):
        if not private(message,message.from_user) or message.from_user.id not in settings.owner_ids:
            return
        parts = (message.text or '').split()
        try:
            if len(parts) != 2:
                raise ValueError('Укажите Telegram ID.')
            await run(repository.allow,message.from_user.id,int(parts[1]),parts[0].split('@')[0] == '/allow')
            await ui.menu(message,'Доступ обновлён.')
        except ValueError as exc:
            await ui.menu(message,str(exc))

    @router.callback_query()
    async def callback(query):
        if not private(query.message,query.from_user):
            await query.answer('Откройте личный чат с ботом.',show_alert=True)
            return
        await query.answer()
        user, message, data = query.from_user.id, query.message, query.data or ''
        await run(clients.register,user)
        try:
            if data == 'home':
                await home(message); return
            if data == 'how':
                await ui.menu(message,'Настройте поиск и получите три бесплатные подборки. После этого можно подключить платный доступ через менеджера.',
                    reply_markup=keyboard([[button('Начать','begin')],[button('Документы и помощь','documents')]])); return
            if data == 'documents':
                await documents(message,user); return
            if data.startswith('accept:'):
                await run(clients.accept,user,int(data.split(':')[1]))
                await documents(message,user); return
            if data == 'services':
                await home(message); return
            if not await require_ready(message,user):
                return
            if data == 'begin':
                state = await run(clients.state,user)
                if state['wizard_step'] is not None:
                    await wizard(message,user,state['wizard_step'])
                elif state['active_filter_id']:
                    await filter_list(message,user)
                else:
                    await run(clients.new_draft,user); await wizard(message,user,0)
            elif data == 'activate':
                end = await run(clients.redeem_activation,user)
                await ui.menu(message,'Доступ подключён до '+end.strftime('%d.%m.%Y %H:%M UTC')+'.',reply_markup=home_keyboard())
            elif data == 'filters':
                await filter_list(message,user)
            elif data == 'filter:new':
                await run(clients.new_draft,user)
                await wizard(message,user,0)
            elif data == 'wizard:cancel':
                await run(clients.wizard_step,user,None); await filter_list(message,user)
            elif data.startswith('wizard:'):
                await wizard(message,user,int(data.split(':')[1]))
            elif data == 'filter:edit':
                state = await run(clients.state,user)
                await run(clients.draft,user,state['draft_filter'])
                await filter_menu(message,user)
            elif data == 'filter:save':
                await run(clients.save_filter,user)
                await ui.menu(message,'Фильтр сохранён и выбран для поиска.',reply_markup=keyboard([[button('Найти вакансии','search')],[button('Мои фильтры','filters')]]))
            elif data == 'filter:update':
                await run(clients.update_filter,user)
                await ui.menu(message,'Фильтр обновлён.',reply_markup=keyboard([[button('Найти вакансии','search')],[button('Мои фильтры','filters')]]))
            elif data.startswith('filter:select:'):
                await run(clients.select_filter,user,uuid.UUID(hex=data.split(':')[2]))
                await ui.menu(message,'Фильтр выбран.',reply_markup=keyboard([[button('Найти вакансии','search')],[button('Изменить фильтр','filter:edit')],[button('Мои фильтры','filters')]]))
            elif data.startswith('filter:delete:'):
                key = uuid.UUID(hex=data.split(':')[2]).hex
                await ui.menu(message,'Удалить этот сохранённый фильтр?',reply_markup=keyboard([[button('Да, удалить',f'filter:confirmdelete:{key}'),button('Назад','filter:edit')]]))
            elif data.startswith('filter:confirmdelete:'):
                await run(clients.delete_filter,user,uuid.UUID(hex=data.split(':')[2]))
                await ui.menu(message,'Фильтр удалён.',reply_markup=keyboard([[button('Мои фильтры','filters')],[button('Главное меню','home')]]))
            elif data == 'edit:format':
                await ui.menu(message,'Выберите формат работы:',reply_markup=keyboard([[button('Удалённо','format:remote'),button('Офис','format:onsite')],[button('Гибрид','format:hybrid'),button('Не важно','format:any')],[button('Назад','filter:edit')]]))
            elif data.startswith('format:'):
                value = data.split(':')[1]
                if value not in ('remote','onsite','hybrid','any'):
                    raise ValueError('Некорректный формат.')
                state = await run(clients.state,user); draft = normalize(state['draft_filter']); draft['work_format'] = None if value == 'any' else value
                await run(clients.draft,user,draft)
                if state['wizard_step'] == 1: await wizard(message,user,2)
                else: await filter_menu(message,user)
            elif data == 'salary:settings':
                state = await run(clients.state,user); draft = normalize(state['draft_filter'])
                missing = draft['unknown'].get('salary','exclude') == 'include'
                await ui.menu(message,'Настройте сумму, валюту и период. Вакансии без зарплаты: '+('показывать' if missing else 'скрывать')+'.',reply_markup=keyboard([
                    [button('Ввести сумму','edit:salary'),button('Не важно','clear:salary')],
                    [button('₽','currency:RUB'),button('$','currency:USD'),button('€','currency:EUR')],
                    [button('В месяц','period:month'),button('В час','period:hour'),button('В год','period:year')],
                    [button('Скрывать без зарплаты' if missing else 'Показывать без зарплаты','salary:toggle')],
                    [button('Назад','filter:edit')]]))
            elif data.startswith(('currency:','period:')) or data == 'salary:toggle':
                state = await run(clients.state,user); draft = normalize(state['draft_filter'])
                if data == 'salary:toggle':
                    draft['unknown']['salary'] = 'exclude' if draft['unknown'].get('salary') == 'include' else 'include'
                else:
                    key,value = data.split(':')
                    allowed = {'currency':('RUB','USD','EUR'),'period':('month','hour','year')}
                    if value not in allowed[key] or draft['salary_min'] is None:
                        raise ValueError('Сначала укажите сумму.')
                    draft['currency' if key == 'currency' else 'salary_period'] = value
                await run(clients.draft,user,draft)
                await ui.menu(message,'Настройки зарплаты обновлены.',reply_markup=keyboard([[button('Продолжить настройку зарплаты','salary:settings')],[button('К фильтру','filter:edit')]]))
            elif data.startswith('edit:'):
                kind = data.split(':')[1]
                if kind not in ('occupation','skill','salary','name'):
                    raise ValueError('Неизвестное поле.')
                state = await run(clients.state,user)
                await run(clients.draft,user,state['draft_filter'],awaiting=kind)
                prompt = {'occupation':'Введите название специальности.','skill':'Введите название навыка.','salary':'Введите минимальную зарплату целым числом. Затем можно выбрать валюту и период.','name':'Введите название фильтра.'}[kind]
                back = f"wizard:{state['wizard_step']}" if state['wizard_step'] is not None else 'filter:edit'
                await ui.menu(message,prompt,reply_markup=keyboard([[button('Не важно','clear:'+kind),button('Назад',back)]]))
            elif data.startswith('clear:'):
                kind = data.split(':')[1]; state = await run(clients.state,user); draft = normalize(state['draft_filter'])
                if kind == 'occupation': draft['occupation_ids'] = []
                elif kind == 'skill': draft['skill_ids'] = []
                elif kind == 'salary': draft.update(salary_min=None,currency=None,salary_period=None)
                elif kind != 'name': raise ValueError('Неизвестное поле.')
                await run(clients.draft,user,draft)
                if state['wizard_step'] is not None:
                    await wizard(message,user,min(4,state['wizard_step']+1))
                else: await filter_menu(message,user)
            elif data.startswith('choose:'):
                _,kind,raw = data.split(':'); identity = int(raw)
                chosen = await run(clients.class_by_id,kind,identity)
                state = await run(clients.state,user); draft = normalize(state['draft_filter'])
                if kind == 'occupation': draft['occupation_ids'] = [chosen['id']]
                else: draft['skill_ids'] = sorted(set(draft['skill_ids']+[chosen['id']]))
                await run(clients.draft,user,draft)
                if state['wizard_step'] is not None:
                    await wizard(message,user,min(4,state['wizard_step']+1))
                else: await filter_menu(message,user)
            elif data == 'search':
                await search(message,user)
            elif data == 'access':
                await access(message,user)
            elif data == 'saved':
                items = await run(clients.saved,user)
                rows = [[button(item['text'].splitlines()[0][:60] or 'Вакансия',f"card:{item['selection_id'].hex}:{item['ordinal']}")] for item in items]
                rows.append([button('Главное меню','home')])
                await ui.menu(message,'Сохранённые вакансии:' if items else 'Пока нет сохранённых вакансий.',reply_markup=keyboard(rows))
            elif data.startswith('card:'):
                _,key,ordinal = data.split(':'); await card(message,user,uuid.UUID(hex=key),int(ordinal))
            elif data.startswith('selection:'):
                key = uuid.UUID(hex=data.split(':')[1])
                items = await run(clients.selection,user,key)
                if not items:
                    raise ValueError('Подборка недоступна.')
                lines = [f"{item['ordinal']}. {next((line.strip() for line in item['text'].splitlines() if line.strip()),'Вакансия')[:110]}" for item in items]
                choices = [button(str(item['ordinal']),f"card:{key.hex}:{item['ordinal']}") for item in items]
                rows = [choices[i:i+5] for i in range(0,len(choices),5)]
                rows.append([button('Главное меню','home')])
                await ui.delivery(message,'Ваша подборка:\n\n'+'\n'.join(lines),reply_markup=keyboard(rows))
            elif data.startswith('reason:'):
                _,key,ordinal = data.split(':')
                await ui.menu(message,'Почему вакансия не подошла?',reply_markup=keyboard([[button('Специализация',f'hide:{key}:{ordinal}:specialty'),button('Оплата',f'hide:{key}:{ordinal}:salary')],[button('Опыт',f'hide:{key}:{ordinal}:experience'),button('Формат',f'hide:{key}:{ordinal}:format')],[button('Другое',f'hide:{key}:{ordinal}:other')],[button('Назад',f'card:{key}:{ordinal}')]]))
            elif data.startswith('hide:'):
                _,key,ordinal,reason = data.split(':'); await run(clients.mark,user,uuid.UUID(hex=key),int(ordinal),'hide',reason)
                await ui.menu(message,'Вакансия скрыта. Можно изменить фильтр для следующих подборок.',reply_markup=keyboard([[button('Изменить фильтр','filter:edit')],[button('Главное меню','home')]]))
            elif data.startswith('mark:'):
                _,action,key,ordinal = data.split(':'); identity=uuid.UUID(hex=key)
                await run(clients.mark,user,identity,int(ordinal),action)
                text = {'save':'Вакансия сохранена.','unsave':'Вакансия убрана из сохранённого.','applied':'Отметили, что вы откликнулись.'}[action]
                rows = [[button('К вакансии',f'card:{key}:{ordinal}')]]
                rows.append([button('Главное меню','home')])
                await ui.menu(message,text,reply_markup=keyboard(rows))
            else:
                await home(message)
        except (ValueError,PermissionError,KeyError,TypeError,OverflowError) as exc:
            await ui.menu(message,str(exc) or 'Не удалось выполнить действие.',reply_markup=keyboard([[button('Главное меню','home')]]))

    @router.message(F.text)
    async def text_input(message):
        if not private(message,message.from_user):
            return
        user = message.from_user.id
        await run(clients.register,user)
        if not await require_ready(message,user):
            return
        try:
            state = await run(clients.state,user); kind = state['awaiting']; value = (message.text or '').strip()
            draft = normalize(state['draft_filter'])
            if kind in ('occupation','skill'):
                if not value or len(value)>160: raise ValueError('Введите название длиной до 160 символов.')
                rows = await run(repository.find_classes,kind,value)
                choices = [[button(row['name'][:50],f"choose:{kind}:{row['id']}")] for row in rows[:10]]
                back = f"wizard:{state['wizard_step']}" if state['wizard_step'] is not None else 'filter:edit'
                choices.append([button('Назад',back)])
                await ui.menu(message,'Выберите подходящий вариант:' if rows else 'Совпадений нет. Попробуйте другое название.',reply_markup=keyboard(choices))
            elif kind == 'salary':
                number = int(value.replace(' ',''))
                if not 0 <= number <= 1000000000: raise ValueError('Введите сумму от 0 до 1 000 000 000.')
                draft.update(salary_min=number,currency='RUB',salary_period='month')
                await run(clients.draft,user,draft)
                if state['wizard_step'] == 2: await wizard(message,user,3)
                else: await filter_menu(message,user)
            elif kind == 'name':
                await run(clients.draft,user,draft,name=value); await filter_menu(message,user)
            else:
                await home(message)
        except (ValueError,PermissionError,TypeError) as exc:
            await ui.menu(message,str(exc) or 'Не удалось обработать ответ.',reply_markup=keyboard([[button('Назад','filter:edit')]]))

    return router
