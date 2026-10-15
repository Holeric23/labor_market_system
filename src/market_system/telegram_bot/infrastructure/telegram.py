import asyncio
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
    TelegramNetworkError, TelegramRetryAfter, TelegramServerError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters

from market_system.telegram_bot.domain.ports import ForwardUnavailable, TelegramError


def markup(rows):
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, **({"url": target} if target.startswith(("https://", "http://"))
                                           else {"callback_data": target})) for text, target in row]
        for row in rows
    ])


class AiogramTelegramClient:
    def __init__(self, bot: Bot, max_retries: int = 4, sleep: Callable[[float], Awaitable] = asyncio.sleep):
        self.bot, self.max_retries, self.sleep = bot, max_retries, sleep

    async def _request(self, call):
        for attempt in range(self.max_retries + 1):
            try:
                return await call()
            except TelegramRetryAfter as error:
                if attempt == self.max_retries:
                    raise TelegramError("Telegram flood control retries exhausted") from error
                await self.sleep(error.retry_after + 0.1)
            except (TelegramNetworkError, TelegramServerError) as error:
                # An ambiguous timeout could mean the send succeeded. Do not resend blindly.
                raise TelegramError("Telegram request failed; delivery status may be unknown") from error

    async def send_message(self, chat_id, text, keyboard=None, reply_to=None):
        try:
            message = await self._request(lambda: self.bot.send_message(
                chat_id=chat_id, text=text, parse_mode=None, reply_markup=markup(keyboard),
                reply_parameters=ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
                if reply_to is not None else None,
            ))
            return message.message_id
        except TelegramAPIError as error:
            raise TelegramError("Cannot send message") from error

    async def forward_message(self, chat_id, from_chat_id, message_id):
        try:
            message = await self._request(lambda: self.bot.forward_message(
                chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id,
            ))
            return message.message_id
        except TelegramBadRequest as error:
            raise ForwardUnavailable("Original post cannot be forwarded") from error
        except (TelegramForbiddenError, TelegramAPIError) as error:
            # Forbidden can refer to the original channel, so allow a text fallback.
            raise ForwardUnavailable("Forwarding is unavailable") from error

    async def delete_message(self, chat_id, message_id):
        try:
            await self._request(lambda: self.bot.delete_message(chat_id=chat_id, message_id=message_id))
        except TelegramAPIError as error:
            raise TelegramError("Cannot delete message") from error
