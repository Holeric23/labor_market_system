"""Offline coverage of Telegram collection boundaries and checkpoints."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from parsers.telegram_window import telegram_batches

NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)


class FakeTelegram:
    def __init__(self, messages):
        self.messages = messages
        self.visited = []
        self.calls = []

    async def get_entity(self, address):
        return address

    async def get_messages(self, entity, limit):
        return sorted(self.messages, key=lambda m: m.id, reverse=True)[:limit]

    async def iter_messages(self, entity, *, limit, offset_id, max_id):
        self.calls.append((limit, offset_id, max_id))
        candidates = sorted(self.messages, key=lambda m: m.id, reverse=True)
        candidates = [m for m in candidates if (not offset_id or m.id < offset_id) and m.id < max_id]
        for message in candidates[:limit]:
            self.visited.append(message.id)
            yield message


def message(identity, age):
    return SimpleNamespace(id=identity, date=NOW-timedelta(days=age), text=f'post {identity}')


def run(client, mode, source=None, days=30, max_items=100, overlap_days=3):
    cursor = {'source': source or {}, 'seen': 0,
              'freshness_cutoff': (NOW-timedelta(days=days)).isoformat(),
              'cutoff': (NOW-timedelta(days=overlap_days)).isoformat()}
    parameters = {'batch_size': 2, 'max_items': max_items, 'overlap_limit': 20}
    async def collect():
        return [batch async for batch in telegram_batches(
            client, 'fixture', 'https://t.me/fixture', mode, cursor, parameters,
            lambda url, identity: f'{url}/{identity}')]
    return asyncio.run(collect())


def identities(batches):
    return [int(item.url.rsplit('/', 1)[-1]) for batch in batches for item in batch.publications]


def test_history_newest_first_stops_at_freshness_boundary():
    client = FakeTelegram([message(10, 0), message(9, 5), message(8, 30),
                           message(7, 30.1), message(6, 60)])
    result = run(client, 'history')
    assert identities(result) == [10, 9, 8]
    assert client.visited == [10, 9, 8, 7]
    assert result[-1].cursor['source']['history_offset'] == 8
    assert result[-1].cursor['source']['history_done']
    assert result[-1].cursor['source']['high_water'] == 10


def test_history_limit_resumes_and_extension_revisits_older_window():
    client = FakeTelegram([message(10, 0), message(9, 10), message(8, 30), message(7, 40), message(6, 70)])
    first = run(client, 'history', max_items=2)
    state = first[-1].cursor['source']
    assert identities(first) == [10, 9] and not state.get('history_done')
    second = run(client, 'history', state)
    assert identities(second) == [8]
    state = second[-1].cursor['source']
    assert state['history_done'] and state['history_offset'] == 8
    count = len(client.visited)
    assert identities(run(client, 'history', state)) == []
    assert len(client.visited) == count
    extended = run(client, 'history', state, days=90)
    assert identities(extended) == [7, 6]
    assert extended[-1].cursor['source']['history_exhausted']


def test_new_backlog_keeps_snapshot_and_does_not_skip_unseen_posts():
    client = FakeTelegram([message(i, 0) for i in range(99, 111)])
    state = {'high_water': 100}
    all_new = []
    for iteration in range(10):
        result = run(client, 'new', state, max_items=3)
        all_new += [i for i in identities(result) if i > 100]
        state = result[-1].cursor['source']
        if iteration == 0:
            assert state['high_water'] == 100
            client.messages += [message(111, 0), message(112, 0)]
        if 'new_progress' not in state:
            break
    assert all_new == list(range(110, 100, -1))
    assert state['high_water'] == 110
    later = run(client, 'new', state)
    assert identities(later)[:2] == [112, 111]
    assert later[-1].cursor['source']['high_water'] == 112


def test_new_scan_stops_on_old_post_and_advances_safely():
    client = FakeTelegram([message(5, 1), message(4, 31), message(3, 50)])
    result = run(client, 'new', {'high_water': 1})
    assert identities(result) == [5]
    assert client.visited == [5, 4]
    assert result[-1].cursor['source']['high_water'] == 5


def test_missing_telegram_date_does_not_make_post_fresh():
    client = FakeTelegram([SimpleNamespace(id=1, date=None, text='post')])
    with pytest.raises(ValueError, match='дата'):
        run(client, 'history')
