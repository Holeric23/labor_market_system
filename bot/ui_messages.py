"""Keep one current interface message per private chat."""
import asyncio
import contextvars
import logging
import weakref

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery

from database.connection import connect

logger = logging.getLogger(__name__)


class UIMessageManager(BaseMiddleware):
    def __init__(self):
        self._locks = weakref.WeakValueDictionary()
        self._interaction = contextvars.ContextVar('telegram_ui_interaction', default=None)

    @staticmethod
    def _private(event):
        message = event if isinstance(event, Message) else event.message if isinstance(event, CallbackQuery) else None
        user = event.from_user if isinstance(event, (Message, CallbackQuery)) else None
        return bool(isinstance(message, Message) and user and message.chat.type == 'private' and message.chat.id == user.id)

    async def __call__(self, handler, event, data):
        if not self._private(event):
            return await handler(event, data)
        chat_id = event.from_user.id
        lock = self._locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[chat_id] = lock
        async with lock:
            interaction = {'sent': False, 'clicked': event.message.message_id if isinstance(event, CallbackQuery) else None}
            token = self._interaction.set(interaction)
            try:
                return await handler(event, data)
            finally:
                self._interaction.reset(token)
                if isinstance(event, Message) and interaction['sent']:
                    await self._delete(event.bot,chat_id,event.message_id)

    @staticmethod
    def _swap(chat_id, message_id):
        with connect() as con:
            con.execute('INSERT INTO telegram_ui_messages(chat_id) VALUES (%s) ON CONFLICT DO NOTHING',(chat_id,))
            previous = con.execute('SELECT message_id FROM telegram_ui_messages WHERE chat_id=%s FOR UPDATE',(chat_id,)).fetchone()
            con.execute('UPDATE telegram_ui_messages SET message_id=%s,updated_at=now() WHERE chat_id=%s',(message_id,chat_id))
            return previous['message_id']

    @staticmethod
    async def _delete(bot, chat_id, message_id):
        try:
            await bot.delete_message(chat_id=chat_id,message_id=message_id)
        except Exception as exc:
            # Deleted, too old, or inaccessible messages must not block navigation.
            logger.debug('Could not delete temporary Telegram message: %s',type(exc).__name__)

    async def _send(self, message, text, **kwargs):
        sent = await message.answer(text,**kwargs)
        interaction = self._interaction.get()
        if interaction is not None:
            interaction['sent'] = True
        if message.chat.type != 'private':
            return sent
        try:
            previous = await asyncio.to_thread(self._swap,message.chat.id,sent.message_id)
        except Exception as exc:
            # A bookkeeping failure must never turn a delivered selection into a failed one.
            logger.warning('Could not track Telegram interface message: %s',type(exc).__name__)
            return sent
        if previous and previous != sent.message_id:
            await self._delete(message.bot,message.chat.id,previous)
        clicked = interaction['clicked'] if interaction else None
        if clicked and clicked not in (previous, sent.message_id):
            await self._delete(message.bot,message.chat.id,clicked)
        return sent

    async def menu(self, message, text, **kwargs):
        return await self._send(message,text,**kwargs)

    async def delivery(self, message, text, **kwargs):
        return await self._send(message,text,**kwargs)
