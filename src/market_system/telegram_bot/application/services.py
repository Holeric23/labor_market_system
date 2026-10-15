import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from market_system.telegram_bot.domain.models import Post, SearchFilter, User
from market_system.telegram_bot.domain.ports import (
    ForwardUnavailable, IPostRepository, ISubscriptionRepository,
    IUserRepository, TelegramClient, TelegramError, INotificationReceipts,
)

logger = logging.getLogger(__name__)
Keyboard = list[list[tuple[str, str]]]
FORMATS = {"REMOTE": "Удалённо", "OFFICE": "Офис", "HYBRID": "Гибрид", "ANY": "Не указан"}


class AuthService:
    def __init__(self, users: IUserRepository):
        self.users = users

    async def check_access(self, user_id: int) -> bool:
        return await self.users.is_admin(user_id)


def summary(post: Post) -> str:
    def amount(value):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if post.salary_min is not None and post.salary_max is not None:
        salary = f"{amount(post.salary_min)}–{amount(post.salary_max)}"
    elif post.salary_min is not None:
        salary = f"от {amount(post.salary_min)}"
    elif post.salary_max is not None:
        salary = f"до {amount(post.salary_max)}"
    else:
        salary = "Не указана"
    if post.salary_currency:
        salary += f" {post.salary_currency}"
    if post.salary_period:
        salary += " / " + {"month": "месяц", "year": "год", "hour": "час", "day": "день",
                           "shift": "смена", "project": "проект"}.get(post.salary_period, post.salary_period)
    if post.tax_mode:
        salary += " · " + {"gross": "до налогов", "net": "на руки"}.get(post.tax_mode, post.tax_mode)
    relocation = {True: "Да", False: "Нет", None: "Не указан"}[post.is_relocation_possible]
    skills = ", ".join(post.skills) or "Не указаны"
    conditions = []
    labels = {"full_time": "Полная занятость", "part_time": "Частичная занятость", "project": "Проектная работа",
              "freelance": "Фриланс", "internship": "Стажировка", "temporary": "Временная работа",
              "flexible": "Гибкий график", "shift": "Сменный график", "night": "Ночные смены",
              "weekdays": "По будням", "weekends": "По выходным", "rotational": "Вахта"}
    for value in (post.employment_type, post.schedule, post.working_hours):
        if value:
            conditions.append(labels.get(value, value))
    if post.hours_per_week is not None:
        conditions.append(f"{post.hours_per_week} ч/неделю")
    conditions_text = "\n🕒 " + " · ".join(conditions) if conditions else ""
    return (f"{'Вакансия' if post.post_type == 'VACANCY' else 'Резюме'}: {post.specialty[:500]}\n"
            f"💰 {salary}\n📍 Формат: {FORMATS[post.work_format]} | Переезд: {relocation}\n"
            f"🛠 Навыки: {skills[:1200]}{conditions_text}\n📅 {post.parsed_date.strftime('%d.%m.%Y') if post.parsed_date else 'Дата неизвестна'}")


def text_chunks(text: str, max_units: int = 3900):
    """Telegram limits UTF-16 units. Never split a surrogate pair / Python character."""
    buffer, units = [], 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > max_units:
            yield "".join(buffer)
            buffer, units = [], 0
        buffer.append(char)
        units += size
    if buffer:
        yield "".join(buffer)


class DeliveryService:
    def __init__(self, telegram: TelegramClient):
        self.telegram = telegram

    async def send_post_to_user(self, user_id: int, post: Post) -> None:
        keyboard = [[("Смотреть оригинал", post.tg_post_link)]]
        forwarded = None
        source = post.tg_channel_id or post.tg_channel_username
        if source is not None and post.tg_message_id is not None:
            try:
                forwarded = await self.telegram.forward_message(user_id, source, post.tg_message_id)
            except ForwardUnavailable:
                pass
        # Failures sending a summary must not repeat an already successful forward.
        text = summary(post)
        if forwarded is None:
            text += "\n\n--- ОРИГИНАЛ ---\n\n" + post.raw_text
        chunks = list(text_chunks(text))
        for index, chunk in enumerate(chunks):
            await self.telegram.send_message(
                user_id, chunk, keyboard if index == len(chunks) - 1 else None,
                reply_to=forwarded if index == 0 else None,
            )


class UIMessageManager:
    def __init__(self, telegram: TelegramClient, users: IUserRepository):
        self.telegram, self.users = telegram, users

    async def show_temporary_menu(self, user_id: int, text: str, keyboard: Keyboard) -> int:
        user = await self.users.get_user(user_id) or User(user_id=user_id)
        visible_text = next(text_chunks(text), "Меню")
        if len(visible_text) < len(text):
            visible_text += "…"
        # Send first so a failed send leaves the previous working menu available.
        new_id = await self.telegram.send_message(user_id, visible_text, keyboard)
        old_id = user.last_menu_message_id
        user.last_menu_message_id = new_id
        await self.users.update(user)
        if old_id is not None:
            await self.remove_temporary(user_id, old_id)
        return new_id

    async def remove_temporary(self, user_id: int, message_id: int):
        try:
            await self.telegram.delete_message(user_id, message_id)
        except TelegramError:
            logger.debug("Cannot delete temporary message for user %s", user_id)


@dataclass(frozen=True)
class SearchPage:
    total: int
    posts: list[Post]
    offset: int


class SearchService:
    def __init__(self, posts: IPostRepository):
        self.posts = posts

    async def search(self, filter: SearchFilter) -> SearchPage:
        return SearchPage(
            await self.posts.count_posts(filter),
            await self.posts.search_posts(filter, filter.limit, filter.offset), filter.offset,
        )


class SubscriptionService:
    def __init__(self, subscriptions: ISubscriptionRepository):
        self.subscriptions = subscriptions

    async def subscribe(self, user_id: int, filter: SearchFilter):
        return await self.subscriptions.create_subscription(user_id, filter)

    async def list(self, user_id: int):
        return await self.subscriptions.get_user_subscriptions(user_id)

    async def delete(self, user_id: int, subscription_id: UUID):
        return await self.subscriptions.delete_subscription(subscription_id, user_id)


class NotifierWorker:
    """Event-driven notification service; never polls the research database.

    SQLite mode injects durable receipts; memory mode keeps them in RAM.
    Telegram has no idempotency keys:
    a crash between sending and recording can still cause duplicate delivery.
    """

    def __init__(self, subscriptions: ISubscriptionRepository, delivery: DeliveryService, auth: AuthService,
                 receipts: INotificationReceipts | None = None):
        self.subscriptions, self.delivery, self.auth = subscriptions, delivery, auth
        self.receipts = receipts
        self._lock = asyncio.Lock()
        self._events: dict[UUID, tuple[str, list[int]]] = {}
        self._delivered: set[tuple[UUID, int]] = set()

    async def on_new_post_created(self, event_id: UUID, post: Post) -> bool:
        # Serialize events to avoid duplicates from simultaneous webhook retries.
        async with self._lock:
            payload = post.model_dump_json()
            if self.receipts is not None:
                users = await self.receipts.register(event_id, payload,
                    await self.subscriptions.find_matching_subscriptions(post))
            elif event_id not in self._events:
                self._events[event_id] = (payload, await self.subscriptions.find_matching_subscriptions(post))
            if self.receipts is None:
                previous, users = self._events[event_id]
                if previous != payload:
                    raise ValueError("event_id already belongs to a different payload")
            success = True
            for user_id in users:
                key = (post.id, user_id)
                delivered = (await self.receipts.was_delivered(*key)) if self.receipts else key in self._delivered
                if delivered or not await self.auth.check_access(user_id):
                    continue
                try:
                    await self.delivery.send_post_to_user(user_id, post)
                except TelegramError:
                    success = False
                    logger.warning("Notification failed for user %s, post %s", user_id, post.id)
                else:
                    if self.receipts:
                        await self.receipts.mark_delivered(*key)
                    else:
                        self._delivered.add(key)
            return success
