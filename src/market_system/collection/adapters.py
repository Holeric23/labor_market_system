"""Existing parser adapters. No client starts until the separate worker calls batches."""
import json
from market_system.paths import local_path


def telegram_settings():
    path = local_path('state/collection/telegram.json')
    session = local_path('state/collection/telegram.session')
    if not path.is_file() or not session.is_file():
        raise ValueError('Telegram is not configured: use collection setup-telegram and login-telegram')
    values = json.loads(path.read_text(encoding='utf-8'))
    if set(values)!={'api_id','api_hash'} or type(values['api_id']) is not int or not values['api_hash']:
        raise ValueError('Invalid local Telegram credentials configuration')
    return values,session


async def batches(config,mode,cursor):
    settings = config['parameters']
    if config['kind']=='telegram':
        from telethon import TelegramClient
        from market_system.core.parsers.telegram.telegram_parser import TelegramParser
        credentials,session = telegram_settings()
        parser = TelegramParser({config['name']:config['address']},use_proxy=False,session_name=str(session))
        client = TelegramClient(str(session),credentials['api_id'],credentials['api_hash'],timeout=30,connection_retries=2)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise ValueError('Telegram session is not authorized; run login-telegram separately')
            async for batch in parser.managed_batches(client,config['name'],config['address'],mode,cursor,settings):
                yield batch
        finally:
            await client.disconnect()
    else:
        from market_system.core.parsers.designhunters.designhunters_parser import DesignHuntersParser
        parser = DesignHuntersParser(urls=config['urls'],timeout=settings['timeout_ms'],
                                     delay_between_requests=settings['delay_seconds'])
        async for batch in parser.managed_batches(mode,cursor,settings):
            yield batch
