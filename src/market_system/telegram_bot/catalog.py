"""Shared search adapter and bounded, single-message presentation."""
import asyncio
import json
from market_system.search.service import Catalog, SearchFilter
from market_system.telegram_bot.application.services import text_chunks


def parse_filter(value):
    if not isinstance(value, dict):
        raise ValueError('Нужен JSON-объект фильтра.')
    # SearchFilter deliberately has no cursor/offset/limit fields.
    try:
        result = SearchFilter(**value)
        return result.normalized()
    except (TypeError, AttributeError, OverflowError) as exc:
        raise ValueError('Некорректная структура фильтра.') from exc


class CatalogSearch:
    def __init__(self, path):
        self.catalog = Catalog(path)

    async def search(self, value, cursor=None, page_size=3):
        filters = SearchFilter(**parse_filter(value))
        return await asyncio.to_thread(self.catalog.search, filters, cursor, page_size=page_size)


def card_text(card, event_id=None):
    lines = [('Вакансия' if card['post_type']=='vacancy' else 'Резюме' if card['post_type']=='resume' else 'Тип неизвестен')
             + f" · документ {card['content_id']}"]
    if event_id:
        lines.append('Событие: '+event_id)
    for key, label in [('work_format','Формат'),('schedule','График'),('employment_type','Занятость')]:
        if card.get(key):
            lines.append(label+': '+str(card[key]))
    if card.get('salary_from') is not None or card.get('salary_to') is not None:
        lines.append(f"Зарплата: {card.get('salary_from')}–{card.get('salary_to')} {card.get('currency') or '?'} / {card.get('salary_period') or '?'}")
    lines.extend(str(p['url']) for p in card.get('publications',[])[:2])
    lines.append(card['text'])
    value='\n'.join(lines)
    chunk=next(text_chunks(value,3800),'')
    return chunk + ('…' if len(chunk)<len(value) else '')
