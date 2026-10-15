from uuid import UUID

from pydantic import ValidationError

from market_system.telegram_bot.application.services import DeliveryService, SearchService, SubscriptionService, UIMessageManager
from market_system.telegram_bot.application.session import SessionStore
from market_system.telegram_bot.domain.models import PostType, SearchFilter, State, User
from market_system.telegram_bot.domain.ports import IUserRepository, TelegramError, UnsupportedFilter
from . import views


class BotController:
    def __init__(self, users: IUserRepository, sessions: SessionStore, ui: UIMessageManager,
                 search: SearchService, subscriptions: SubscriptionService, delivery: DeliveryService):
        self.users, self.sessions, self.ui = users, sessions, ui
        self.search, self.subscriptions, self.delivery = search, subscriptions, delivery

    async def _state(self, user_id, state):
        self.sessions.get(user_id).state = state
        user = await self.users.get_user(user_id) or User(user_id=user_id)
        user.current_state = state
        await self.users.update(user)

    async def home(self, user_id):
        self.sessions.get(user_id).editing = None
        await self._state(user_id, State.IDLE)
        await self.ui.show_temporary_menu(user_id, *views.home())

    async def filter_menu(self, user_id):
        session = self.sessions.get(user_id)
        session.editing = None
        await self._state(user_id, State.SEARCH_SETUP)
        await self.ui.show_temporary_menu(user_id, *views.filter_menu(session.filter))

    def _change(self, user_id, field, value):
        session = self.sessions.get(user_id)
        data = session.filter.model_dump()
        data.update({field: value, "offset": 0})
        session.filter = SearchFilter.model_validate(data)

    async def show_results(self, user_id, restart=False):
        session = self.sessions.get(user_id)
        session.editing = None
        if restart:
            session.filter.offset = 0
        try:
            page = await self.search.search(session.filter)
        except UnsupportedFilter as error:
            await self._state(user_id, State.SEARCH_SETUP)
            await self.ui.show_temporary_menu(user_id, str(error), [[("Изменить фильтр", "filter"), ("Сбросить", "reset")]])
            return
        await self._state(user_id, State.VIEWING_RESULTS)
        sent = 0
        failed = False
        for post in page.posts:
            try:
                await self.delivery.send_post_to_user(user_id, post)
            except TelegramError:
                failed = True
                break
            sent += 1
            session.filter.offset += 1
        if page.total == 0:
            text = "Ничего не найдено. Можно изменить фильтр или подписаться на новые публикации."
        elif sent:
            text = f"Найдено: {page.total}. Показаны {page.offset + 1}–{session.filter.offset}."
        else:
            text = f"Найдено: {page.total}. Новых результатов на этой странице нет."
        if failed:
            text += "\nОтправка прервалась. Нажмите «Показать ещё», чтобы продолжить."
        buttons = []
        if session.filter.offset < page.total and (page.posts or failed):
            buttons.append([("Показать ещё", "more")])
        buttons += [[("🔔 Подписаться на фильтр", "subscribe")],
                    [("Изменить фильтр", "filter"), ("Главное меню", "home")]]
        # The control panel always goes BELOW the newly delivered posts.
        await self.ui.show_temporary_menu(user_id, text, buttons)

    async def show_subscriptions(self, user_id, page=0, notice=""):
        await self._state(user_id, State.IDLE)
        self.sessions.get(user_id).editing = None
        subscriptions = await self.subscriptions.list(user_id)
        page = max(0, min(page, max(0, (len(subscriptions) - 1) // 5)))
        shown = subscriptions[page * 5:(page + 1) * 5]
        text = (notice + "\n\n" if notice else "") + f"Мои подписки: {len(subscriptions)}"
        buttons = []
        for index, sub in enumerate(shown, start=page * 5 + 1):
            filter = SearchFilter.model_validate(sub.filter)
            # Bound text to keep even maximum-length filters inside Telegram limits.
            text += f"\n\n{index}. {views.describe_filter(filter)[:500]}"
            buttons.append([(f"Удалить подписку {index}", f"delete:{sub.id}")])
        if page:
            buttons.append([("Назад", f"subscriptions:{page - 1}")])
        if (page + 1) * 5 < len(subscriptions):
            buttons.append([("Далее", f"subscriptions:{page + 1}")])
        buttons.append([("Главное меню", "home")])
        await self.ui.show_temporary_menu(user_id, text, buttons)

    async def callback(self, user_id, data):
        session = self.sessions.get(user_id)
        if data == "home":
            await self.home(user_id)
        elif data.startswith("type:") and data[5:] in PostType._value2member_map_:
            session.filter = SearchFilter(post_type=data[5:])
            await self.filter_menu(user_id)
        elif data == "filter":
            await self.filter_menu(user_id)
        elif data == "reset":
            session.filter = SearchFilter(post_type=session.filter.post_type)
            await self.filter_menu(user_id)
        elif data.startswith("edit:"):
            field = data[5:]
            if field not in views.CHOICES and field not in views.PROMPTS:
                return
            await self._state(user_id, State.SEARCH_SETUP)
            session.editing = field
            await self.ui.show_temporary_menu(user_id, *views.editor(field))
        elif data.startswith("set:"):
            parts = data.split(":")
            if len(parts) != 3:
                return
            _, field, value = parts
            if field != session.editing or (field not in views.CHOICES):
                return
            if value not in {v for _, v in views.CHOICES[field]}:
                return
            if field == "relocation":
                value = {"yes": True, "no": False, "any": None}[value]
            elif field == "published_within_days":
                value = None if value == "any" else int(value)
            self._change(user_id, field, value)
            await self.filter_menu(user_id)
        elif data.startswith("clear:"):
            field = data[6:]
            if field not in views.PROMPTS or session.editing != field:
                return
            self._change(user_id, field, [] if field == "skills" else None)
            await self.filter_menu(user_id)
        elif data == "search":
            await self.show_results(user_id, restart=True)
        elif data == "more" and session.state == State.VIEWING_RESULTS:
            await self.show_results(user_id)
        elif data == "subscribe":
            await self.subscriptions.subscribe(user_id, session.filter)
            await self.show_subscriptions(user_id, notice="Подписка сохранена.")
        elif data == "subscriptions" or data.startswith("subscriptions:"):
            value = data.partition(":")[2]
            await self.show_subscriptions(user_id, int(value) if value.isdigit() else 0)
        elif data.startswith("delete:"):
            try:
                sub_id = UUID(data[7:])
            except ValueError:
                return
            deleted = await self.subscriptions.delete(user_id, sub_id)
            await self.show_subscriptions(user_id, notice="Подписка удалена." if deleted else "Подписка не найдена.")

    async def text(self, user_id, text):
        session = self.sessions.get(user_id)
        field = session.editing
        if field not in views.PROMPTS or session.state != State.SEARCH_SETUP:
            await self.home(user_id)
            return
        try:
            value = text.strip()
            if not value:
                raise ValueError("Пустое значение")
            if field == "salary_min":
                value = int("".join(value.split()))
            elif field == "skills":
                value = [item.strip() for item in value.split(",") if item.strip()]
                if not value:
                    raise ValueError("Нет навыков")
            self._change(user_id, field, value)
        except (ValueError, ValidationError):
            prompt, buttons = views.editor(field)
            await self.ui.show_temporary_menu(user_id, "Некорректное значение.\n\n" + prompt, buttons)
            return
        await self.filter_menu(user_id)
