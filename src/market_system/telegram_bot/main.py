import argparse
import asyncio
import json
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand
from aiohttp import web

from market_system.telegram_bot.bootstrap import build_application
from market_system.telegram_bot.config import Settings
from market_system.telegram_bot.domain.models import Post
from market_system.telegram_bot.infrastructure.memory import MemoryPostRepository, MemorySubscriptionRepository, MemoryUserRepository
from market_system.telegram_bot.infrastructure.telegram import AiogramTelegramClient
from market_system.telegram_bot.infrastructure.sqlite_state import (
    StateDatabase, SQLiteUserRepository,
)
from market_system.telegram_bot.presentation.router import AuthMiddleware, build_router


async def run(settings: Settings):
    if settings.events_enabled:
        raise ValueError('HTTP event receiver retired; the processing worker writes the SQLite outbox directly')
    if settings.repository_backend=='sqlite':
        await run_catalog(settings)
        return
    # Legacy memory backend remains an explicitly selected demonstration only.
    posts = []
    if settings.demo_posts_file:
        posts = [Post.model_validate(value) for value in json.loads(settings.demo_posts_file.read_text(encoding="utf-8"))]
    logging.warning("MEMORY BACKEND: state is lost on restart; loaded %s posts", len(posts))
    posts_repository = MemoryPostRepository(posts)
    users = MemoryUserRepository(settings.admins)
    subscriptions = MemorySubscriptionRepository()
    session = AiohttpSession(proxy=settings.telegram_proxy_url)
    async with Bot(token=settings.token, session=session) as bot:
        telegram = AiogramTelegramClient(bot)
        application = build_application(
            users, posts_repository, subscriptions, telegram,
        )
        dispatcher = Dispatcher()
        dispatcher.update.outer_middleware(AuthMiddleware(application.auth))
        dispatcher.include_router(build_router(application.controller))
        try:
            await bot.set_my_commands([
                BotCommand(command="start", description="Открыть меню"),
                BotCommand(command="subscriptions", description="Мои подписки"),
                BotCommand(command="cancel", description="Отменить ввод"),
            ])
            # Telegram update polling is independent of ML events. No DB polling.
            await dispatcher.start_polling(bot, allowed_updates=["message", "callback_query"], close_bot_session=False)
        finally:
            await dispatcher.storage.close()


def main():
    parser = argparse.ArgumentParser(description="Labor market Telegram bot")
    parser.add_argument("--env-file", type=Path, default=Path.cwd() / ".env")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.load(args.env_file.resolve())
    except (ValueError, OSError) as error:
        parser.error(str(error))
    try:
        asyncio.run(run(settings))
    except Exception as error:
        parser.exit(1,'Bot stopped: '+type(error).__name__+'\n')


async def run_catalog(settings):
    from .store import BotStore
    from .unified import Controller, Access, router
    from .security import install_redaction
    install_redaction(settings)
    store=BotStore(settings.research_db_path)
    await asyncio.to_thread(store.initialize)
    await asyncio.to_thread(store.bootstrap_owners,settings.admins)
    # Keep original persistent UI state and preserve old subscription ownership.
    state=await asyncio.to_thread(StateDatabase,settings.bot_db_path)
    await asyncio.to_thread(store.import_legacy,settings.bot_db_path)
    users=SQLiteUserRepository(state,settings.admins)
    async with Bot(settings.token,session=AiohttpSession(proxy=settings.telegram_proxy_url)) as bot:
        controller=Controller(store,users,AiogramTelegramClient(bot))
        dispatcher=Dispatcher()
        dispatcher.update.outer_middleware(AuthMiddleware(Access(store)))
        dispatcher.include_router(router(controller))
        try:
            await bot.set_my_commands([BotCommand(command='search',description='Поиск по общему каталогу'),
                                      BotCommand(command='subscriptions',description='Мои подписки'),
                                      BotCommand(command='help',description='Фильтры и команды')])
            await dispatcher.start_polling(bot,allowed_updates=['message','callback_query'],close_bot_session=False)
        finally:
            await dispatcher.storage.close()


if __name__ == "__main__":
    from market_system.runtime import enable_guard
    enable_guard()
    main()

