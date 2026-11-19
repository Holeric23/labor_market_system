"""Adapt the existing supported parsers to a shared bounded batch interface."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


async def batches(source, mode):
    parameters = {'batch_size':50,'max_items':1000,'overlap_days':3,'overlap_limit':500,
                  'timeout_ms':30000,'delay_seconds':2}
    unknown = set(source['parameters']) - set(parameters)
    if unknown:
        raise ValueError('Неподдерживаемые параметры источника')
    parameters.update(source['parameters'])
    limits = {'batch_size':(1,200),'max_items':(1,100000),'overlap_days':(1,30),
              'overlap_limit':(1,5000),'timeout_ms':(1000,120000),'delay_seconds':(0,60)}
    for key,(low,high) in limits.items():
        if type(parameters[key]) is not int or not low <= parameters[key] <= high:
            raise ValueError('Некорректный параметр источника: '+key)
    state = dict(source['cursor'])
    cursor = {'source':state, 'seen':0}
    if source['kind'] == 'telegram':
        from telethon import TelegramClient
        from market_system.paths import STATE_DIR
        from market_system.core.parsers.telegram.telegram_parser import TelegramParser
        api_id, api_hash = os.environ.get('TELEGRAM_API_ID',''), os.environ.get('TELEGRAM_API_HASH','')
        if not api_id or not api_hash:
            raise ValueError('Не настроены TELEGRAM_API_ID и TELEGRAM_API_HASH')
        session = STATE_DIR/'collection/telegram.session'
        if not session.is_file():
            raise ValueError('Не создана отдельная Telegram-сессия парсера')
        cursor.update(phase='history' if mode == 'history' else 'new',
                      cutoff=(datetime.now(timezone.utc)-timedelta(days=parameters['overlap_days'])).isoformat(),
                      freshness_cutoff=source['freshness_cutoff'])
        parser = TelegramParser({source['name']:source['address']},use_proxy=False,session_name=str(session))
        client = TelegramClient(str(session),int(api_id),api_hash,timeout=30,connection_retries=2)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise ValueError('Telegram-сессия парсера не авторизована')
            async for batch in parser.managed_batches(client,source['name'],source['address'],mode,cursor,parameters):
                yield batch
        finally:
            await client.disconnect()
    elif source['kind'] == 'designhunters':
        from market_system.core.parsers.designhunters.designhunters_parser import DesignHuntersParser
        cursor['index'] = state.get('history_index',0) if mode == 'history' else state.get('regular_index',0)
        parser = DesignHuntersParser(urls=[source['address']],timeout=parameters['timeout_ms'],delay_between_requests=parameters['delay_seconds'])
        async for batch in parser.managed_batches(mode,cursor,parameters):
            yield batch
    else:
        raise ValueError('Неизвестный тип источника')
