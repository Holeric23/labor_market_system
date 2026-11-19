"""Bounded, checkpointed Telegram collection in newest-first order."""
from copy import deepcopy
from datetime import datetime, timezone

from parsers.market_system.collection.runner import Batch
from parsers.market_system.core.utils.dataclasses import RawPublication


def _instant(value, label):
    try:
        stamp = datetime.fromisoformat(value) if isinstance(value,str) else value
        if not isinstance(stamp,datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc)
    except (TypeError,ValueError,OverflowError):
        raise ValueError(f'Неизвестная или некорректная дата: {label}') from None


async def telegram_batches(client, source_name, channel_url, mode, cursor, parameters, url_builder):
    """Process newest first, retain only fresh posts and persist safe offsets.

    A new-message scan freezes its upper bound and only advances high_water
    after every eligible page has been processed. This avoids skipping older
    unseen pages when a run reaches max_items or is interrupted.
    """
    cursor = deepcopy(cursor)
    state = cursor['source']
    fresh_cutoff = _instant(cursor.get('freshness_cutoff'),'граница актуальности')
    overlap_cutoff = _instant(cursor.get('cutoff'),'граница проверки изменений')
    if mode not in ('history','new'):
        raise ValueError('Неизвестный режим сбора Telegram')
    if mode == 'new' and 'high_water' not in state:
        raise ValueError('Run history collection once before regular Telegram collection')

    if mode == 'history' and state.get('history_done'):
        previous = state.get('history_freshness_cutoff')
        if state.get('history_exhausted') or (previous and _instant(previous,'предыдущая граница') <= fresh_cutoff):
            yield Batch([],deepcopy(cursor),True)
            return
        state['history_done'] = False

    entity = await client.get_entity(channel_url)
    if mode == 'new' and state.get('new_progress'):
        progress = state['new_progress']
        if not isinstance(progress,dict) or any(type(progress.get(key)) is not int or progress[key] < 0 for key in ('base','upper','offset','overlap_seen')):
            raise ValueError('Некорректное сохранённое состояние сбора')
    else:
        latest = await client.get_messages(entity,limit=1)
        upper = latest[0].id if latest else 0
        if mode == 'new':
            progress = {'base':state['high_water'],'upper':upper,'offset':0,'overlap_seen':0}
            state['new_progress'] = progress
        else:
            cursor['upper_id'] = upper

    size = parameters['batch_size']
    while True:
        remaining = parameters['max_items']-cursor['seen']
        if remaining <= 0:
            yield Batch([],deepcopy(cursor),True)
            return
        limit = min(size,remaining)
        if mode == 'history':
            upper = cursor['upper_id']
            offset = state.get('history_offset',0)
        else:
            upper,offset = progress['upper'],progress['offset']
        seen_in_page = 0
        crossed = False
        fresh = []
        last_id = None
        # Telethon's default direction is newest to oldest. offset_id and max_id
        # are exclusive, so the last processed ID is safe to use on resume.
        async for message in client.iter_messages(entity,limit=limit,offset_id=offset,max_id=upper+1):
            seen_in_page += 1
            stamp = _instant(message.date,'публикация Telegram')
            if stamp < fresh_cutoff:
                crossed = True
                break
            if mode == 'new' and message.id <= progress['base']:
                if stamp < overlap_cutoff or progress['overlap_seen'] >= parameters['overlap_limit']:
                    crossed = True
                    break
                progress['overlap_seen'] += 1
            last_id = message.id
            fresh.append(message)
        cursor['seen'] += seen_in_page
        if mode == 'history':
            if fresh:
                state['high_water'] = max(state.get('high_water',0),max(m.id for m in fresh))
                state['history_offset'] = last_id
            else:
                state.setdefault('high_water',0)
        elif last_id is not None:
            progress['offset'] = last_id

        exhausted = seen_in_page < limit and not crossed
        finished_window = crossed or exhausted
        if mode == 'history' and finished_window:
            state['history_done'] = True
            state['history_exhausted'] = exhausted
            state['history_freshness_cutoff'] = fresh_cutoff.isoformat()
        elif mode == 'new' and finished_window:
            state['high_water'] = max(state.get('high_water',0),progress['upper'])
            state.pop('new_progress',None)
        publications = [RawPublication('telegram',source_name,m.text,url_builder(channel_url,m.id),m.date)
                        for m in fresh if m.text and m.text.strip()]
        done = finished_window or cursor['seen'] >= parameters['max_items']
        yield Batch(publications,deepcopy(cursor),done)
        if done:
            return
