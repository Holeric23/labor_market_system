"""Explicit interactive creation of a new parser-only Telegram session."""
import asyncio
import os
from market_system.paths import STATE_DIR


async def main():
    from telethon import TelegramClient
    api_id = os.environ.get('TELEGRAM_API_ID','')
    api_hash = os.environ.get('TELEGRAM_API_HASH','')
    if not api_id or not api_hash:
        raise ValueError('Set TELEGRAM_API_ID and TELEGRAM_API_HASH')
    folder = STATE_DIR/'collection'
    folder.mkdir(parents=True,exist_ok=True)
    async with TelegramClient(str(folder/'telegram.session'),int(api_id),api_hash) as client:
        if not await client.is_user_authorized():
            raise RuntimeError('Telegram login was not completed')
    print('Parser session configured.')


if __name__ == '__main__':
    asyncio.run(main())
