"""One bot container hosts the Telegram adapter and the admin Mini App."""
import asyncio
import ipaddress
import logging
import os
from aiogram import Bot, Dispatcher
import uvicorn
from bot.config import Settings
from bot.miniapp.app import create_app
from bot.telegram import make_router
from database.migrate import migrate
from database.repository import Repository


async def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings.load()
    await asyncio.to_thread(migrate)
    if settings.owner_ids:
        await asyncio.to_thread(Repository().bootstrap_owners,settings.owner_ids)
    proxy = os.environ.get('TRUSTED_PROXY_IP','127.0.0.1')
    try:
        if ipaddress.ip_address(proxy).is_unspecified:
            raise ValueError()
    except ValueError:
        raise ValueError('TRUSTED_PROXY_IP must be a single trusted IP address') from None
    server = uvicorn.Server(uvicorn.Config(create_app(settings),host='0.0.0.0',port=8000,proxy_headers=True,forwarded_allow_ips=proxy,access_log=False))
    if not settings.bot_token:
        await server.serve()
        return
    async with Bot(settings.bot_token) as bot:
        dispatcher = Dispatcher()
        dispatcher.include_router(make_router(settings))
        server_task = asyncio.create_task(server.serve())
        polling_task = asyncio.create_task(dispatcher.start_polling(bot,allowed_updates=['message','callback_query','inline_query'],handle_signals=False))
        done, pending = await asyncio.wait((server_task,polling_task),return_when=asyncio.FIRST_COMPLETED)
        server.should_exit = True
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)
        for task in done:
            task.result()


if __name__ == '__main__':
    asyncio.run(main())
