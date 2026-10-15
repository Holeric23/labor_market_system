import asyncio
import logging
from collections import defaultdict

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, Update

from market_system.telegram_bot.application.services import AuthService
from market_system.telegram_bot.domain.ports import TelegramError

logger = logging.getLogger(__name__)


class AuthMiddleware(BaseMiddleware):
    """Installed at Dispatcher.update: every update passes through access control."""

    def __init__(self, auth: AuthService):
        self.auth = auth
        self._locks = defaultdict(asyncio.Lock)

    async def __call__(self, handler, event: Update, data):
        user = data.get("event_from_user")
        # Ignore service/channel updates without a real user.
        if user is None:
            return None
        if not await self.auth.check_access(user.id):
            if event.callback_query:
                await event.callback_query.answer("Доступ ограничен.", show_alert=True)
            elif event.message and event.message.chat.type == "private":
                await event.message.answer("Доступ ограничен. Обратитесь к администратору.")
            return None
        message = event.message or (event.callback_query.message if event.callback_query else None)
        if message is None or message.chat.type != "private" or message.chat.id != user.id:
            if event.callback_query:
                await event.callback_query.answer("Откройте личный чат с ботом.", show_alert=True)
            return None
        # Serialize per-user mutations and pagination; different users stay concurrent.
        async with self._locks[user.id]:
            return await handler(event, data)


def build_router(controller):
    router = Router(name="labor_market")

    @router.message(Command("start", "menu", "cancel"))
    async def start(message: Message):
        await controller.home(message.from_user.id)
        await controller.ui.remove_temporary(message.chat.id, message.message_id)

    @router.message(Command("subscriptions"))
    async def subscriptions(message: Message):
        await controller.show_subscriptions(message.from_user.id)
        await controller.ui.remove_temporary(message.chat.id, message.message_id)

    @router.callback_query()
    async def callback(query: CallbackQuery):
        user = await controller.users.get_user(query.from_user.id)
        if (not query.message or not user or not controller.sessions.contains(query.from_user.id)
                or query.message.message_id != user.last_menu_message_id):
            await query.answer("Это меню устарело. Используйте /menu.")
            return
        await query.answer()
        await controller.callback(query.from_user.id, query.data or "")

    @router.message(F.text)
    async def text(message: Message):
        await controller.text(message.from_user.id, message.text)
        await controller.ui.remove_temporary(message.chat.id, message.message_id)

    @router.message()
    async def unsupported(message: Message):
        session = controller.sessions.get(message.from_user.id)
        if session.editing:
            from .views import editor
            prompt, buttons = editor(session.editing)
            await controller.ui.show_temporary_menu(message.from_user.id, "Нужен текст.\n" + prompt, buttons)
        else:
            await controller.home(message.from_user.id)

    @router.errors()
    async def error_handler(event):
        logger.error("Bot update failed: %s", type(event.exception).__name__)
        update = event.update
        message = update.message or (update.callback_query.message if update.callback_query else None)
        if message:
            try:
                await controller.ui.show_temporary_menu(message.chat.id,
                    "Не удалось выполнить действие. Попробуйте снова или откройте меню.",
                    [[("Главное меню", "home")]])
            except TelegramError:
                pass
        return True

    return router
