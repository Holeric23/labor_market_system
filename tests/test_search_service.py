"""Contract tests use only isolated local SQLite databases."""
import base64
import json
import sqlite3
from unittest.mock import patch

import pytest

from market_system.search import Catalog, InvalidCursor, SearchFilter
from market_system.search.optimize import optimize
from market_system.search.service import compile_page
from market_system.search.store import activate, connect, migrate, utc


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / 'search.db'
    migrate(path)
    with connect(path) as con:
        con.execute("INSERT INTO result_sets VALUES('v1','m','s','p','{}','{}','{}','building','now',NULL,NULL)")
        con.execute("INSERT INTO source_types VALUES(1,'telegram')")
        con.execute("INSERT INTO sources VALUES(1,1,'one'),(2,1,'two')")
        con.executemany('INSERT INTO classes VALUES(?,?,?,?,?)', [
            (1, 'skill:x', 'python', 'Same', '1'), (2, 'skill:x', 'sql', 'Same', '1'),
            (3, 'occupation:x', 'dev', 'Developer', '1'), (4, 'skill:y', 'python', 'Same', '2'),
        ])
        # Latest dates tie; unknown dates must come last in descending ID order.
        dates = {1: '2026-01-01', 2: '2026-01-02', 3: '2026-01-02', 4: '2026-01-03'}
        for i in range(1, 10):
            con.execute('INSERT INTO contents VALUES(?,?,?)', (i, f'text {i}', f'h{i}'))
            con.execute("INSERT INTO document_processing VALUES('v1',?,'processed')", (i,))
            date = utc(dates[i] + 'T00:00:00Z') if i in dates else None
            currency = 'USD' if i == 2 else None if i == 5 else 'RUB'
            period = 'hour' if i == 3 else None if i == 6 else 'month'
            salary = None if i == 7 else 99 if i == 8 else 100
            kind = None if i == 9 else 'resume' if i == 8 else 'vacancy'
            con.execute('''INSERT INTO search_catalog(result_set_id,content_id,post_type,published_at_utc,
                publication_count,salary_from,salary_to,currency,salary_period,tax_mode,work_format,
                schedule,employment_type,review_status,salary_scope)
                VALUES('v1',?,?,?,?,?,?,?,?,?,?,?,?, 'unreviewed','document')''',
                (i, kind, date, 2 if i == 1 else 1, salary, 1000, currency, period,
                 'gross' if i == 2 else None if i == 3 else 'net',
                 'remote' if i <= 3 else None, 'full', 'full_time'))
            con.execute("INSERT INTO result_evidence VALUES('v1',?,'{}',NULL,'[]')", (i,))
            con.execute('INSERT INTO publications VALUES(?,?,1,?,?)', (i, i, f'https://test/{i}', date))
            con.execute("INSERT INTO result_publications VALUES('v1',?,?,?)", (i, i, date))
        con.execute("INSERT INTO publications VALUES(10,1,2,'https://test/other','2025-12-01T00:00:00Z')")
        con.execute("INSERT INTO result_publications VALUES('v1',1,10,?)", (utc('2025-12-01T00:00:00Z'),))
        links = [(1, 1, 'required'), (1, 1, 'preferred'), (2, 1, 'preferred'),
                 (1, 2, 'required'), (2, 3, 'required'), (1, 4, 'negated'),
                 (2, 4, 'mentioned'), (1, 5, 'possessed'), (1, 8, 'possessed'),
                 (3, 1, 'none'), (3, 2, 'none'), (4, 6, 'required')]
        con.executemany("INSERT INTO document_classes VALUES('v1',?,?,?,NULL)", links)
        con.execute("UPDATE document_processing SET status='no_classes' WHERE NOT EXISTS(SELECT 1 FROM document_classes d WHERE d.content_id=document_processing.content_id)")
        con.execute("UPDATE result_sets SET coverage_json='{" + '"expected":9' + "}',state='ready',ready_at='now' WHERE result_set_id='v1'")
        con.commit()
    activate(path, 'v1')
    optimize(path)
    return Catalog(path)


def ids(page):
    return [c['content_id'] for c in page.cards]


def test_all_any_roles_and_exact_ids(catalog):
    assert ids(catalog.search(SearchFilter(skill_ids=(1, 2)))) == [1]
    assert ids(catalog.search(SearchFilter(skill_ids=(1, 2), skills_mode='ANY'))) == [3, 2, 1, 8]
    assert ids(catalog.search(SearchFilter(skill_ids=(1, 1, 2)))) == [1]
    assert ids(catalog.search(SearchFilter(skill_ids=(1,), skill_roles=('required',)))) == [2, 1]
    assert ids(catalog.search(SearchFilter(skill_ids=(1,), skill_roles=('preferred',)))) == [1]
    assert ids(catalog.search(SearchFilter(skill_ids=(1,), skill_roles=('possessed',)))) == [8]
    assert ids(catalog.search(SearchFilter(skill_ids=(4,)))) == [6]
    assert ids(catalog.search(SearchFilter(occupation_ids=(3,)))) == [2, 1]
    with pytest.raises(ValueError):
        catalog.search(SearchFilter(skill_ids=(3,)))
    with pytest.raises(ValueError):
        catalog.search(SearchFilter(skill_ids=(999,)))


def test_salary_strict_lower_bound(catalog):
    f = dict(salary_min=100, currency='RUB', salary_period='month')
    assert ids(catalog.search(SearchFilter(**f))) == [4, 1, 9]
    assert ids(catalog.search(SearchFilter(**(f | {'salary_min': 101})))) == []
    assert ids(catalog.search(SearchFilter(**(f | {'currency': 'USD'})))) == [2]
    assert ids(catalog.search(SearchFilter(**(f | {'salary_period': 'hour'})))) == [3]
    assert ids(catalog.search(SearchFilter(**(f | {'currency': 'USD', 'tax_mode': 'net'})))) == []
    assert ids(catalog.search(SearchFilter(**(f | {'currency': 'USD', 'tax_mode': 'gross'})))) == [2]
    assert ids(catalog.search(SearchFilter(tax_mode='net', unknown={'tax_mode': 'include'}))) == [4, 3, 1, 9, 8, 7, 6, 5]


def test_unknown_and_conditions(catalog):
    assert len(catalog.search(SearchFilter()).cards) == 9
    assert ids(catalog.search(SearchFilter(post_type='vacancy'))) == [4, 3, 2, 1, 7, 6, 5]
    assert ids(catalog.search(SearchFilter(unknown={'post_type': 'only'}))) == [9]
    assert ids(catalog.search(SearchFilter(work_format='remote'))) == [3, 2, 1]
    assert len(catalog.search(SearchFilter(work_format='remote', unknown={'work_format': 'include'})).cards) == 9
    assert ids(catalog.search(SearchFilter(unknown={'work_format': 'only'}))) == [4, 9, 8, 7, 6, 5]
    assert len(catalog.search(SearchFilter(schedule='full', employment_type='full_time')).cards) == 9
    assert not catalog.search(SearchFilter(schedule="full' OR 1=1 --")).cards


def test_dates_boundaries_and_sources(catalog):
    f = SearchFilter(published_from='2026-01-02T03:00:00+03:00', published_until='2026-01-03T00:00:00Z')
    assert ids(catalog.search(f)) == [3, 2]
    assert len(catalog.search(SearchFilter(**(f.__dict__ | {'unknown': {'published_at_utc': 'include'}}))).cards) == 7
    card = next(c for c in catalog.search(SearchFilter()).cards if c['content_id'] == 1)
    assert card['publication_count'] == len(card['publications']) == 2
    assert {p['source_name'] for p in card['publications']} == {'one', 'two'}
    assert card['published_at_utc'] == card['publications'][0]['published_at_utc']
    assert card['text'] == 'text 1' and card['evidence']['skills'] == []
    assert {(c['class_id'], c['role']) for c in card['classes']} == {(1, 'required'), (1, 'preferred'), (2, 'preferred'), (3, 'none')}


@pytest.mark.parametrize('size', [1, 2, 3, 4, 9, 100])
def test_cursor_no_repeats_including_nulls(catalog, size):
    result, cursor = [], None
    while True:
        page = catalog.search(SearchFilter(), cursor, page_size=size)
        result.extend(ids(page))
        cursor = page.next_cursor
        if cursor is None:
            break
    assert result == [4, 3, 2, 1, 9, 8, 7, 6, 5]
    assert len(set(result)) == len(result)


def test_cursor_filter_revision_and_malformed(catalog):
    page = catalog.search(SearchFilter(), page_size=1)
    with pytest.raises(InvalidCursor, match='Начните поиск заново'):
        catalog.search(SearchFilter(post_type='vacancy'), page.next_cursor)
    for cursor in ('', 'garbage', 'x' * 3000, base64.urlsafe_b64encode(b'{}').decode()):
        with pytest.raises(InvalidCursor):
            catalog.search(SearchFilter(), cursor)
    damaged = json.loads(base64.urlsafe_b64decode(page.next_cursor))
    damaged[-1] = 2**100
    with pytest.raises(InvalidCursor):
        catalog.search(SearchFilter(), base64.urlsafe_b64encode(json.dumps(damaged).encode()).decode())
    with pytest.raises(ValueError):
        catalog.search(SearchFilter(skill_ids=(2**100,)))
    with pytest.raises(InvalidCursor):
        catalog.search(SearchFilter(), page.next_cursor, result_set_id='v1')
    pinned = catalog.search(SearchFilter(), page_size=1, result_set_id='v1')
    with connect(catalog.path) as con:
        con.execute("INSERT INTO result_sets SELECT 'v2',model_version,skills_run_id,profiles_run_id,versions_json,provenance_json,coverage_json,'building',created_at,NULL,NULL FROM result_sets WHERE result_set_id='v1'")
        for table in ('document_processing', 'document_classes', 'search_catalog', 'result_evidence', 'result_publications'):
            columns = [r[1] for r in con.execute('PRAGMA table_info(' + table + ')') if r[1] != 'result_set_id']
            fields = ','.join(columns)
            con.execute(f"INSERT INTO {table}(result_set_id,{fields}) SELECT 'v2',{fields} FROM {table} WHERE result_set_id='v1'")
        con.execute("UPDATE result_sets SET state='ready',ready_at='later' WHERE result_set_id='v2'")
        con.commit()
    activate(catalog.path, 'v2')
    with pytest.raises(InvalidCursor):
        catalog.search(SearchFilter(), page.next_cursor)
    activate(catalog.path, 'v1')
    with pytest.raises(InvalidCursor):
        catalog.search(SearchFilter(), page.next_cursor)
    assert ids(catalog.search(SearchFilter(), pinned.next_cursor, result_set_id='v1')) == [3, 2, 1, 9, 8, 7, 6, 5]


@pytest.mark.parametrize('kwargs', [
    {'salary_min': 100}, {'salary_min': float('nan'), 'currency': 'RUB', 'salary_period': 'month'},
    {'currency': 'RUB'}, {'published_from': '2026-01-01'},
    {'published_from': '2026-01-02T00:00:00Z', 'published_until': '2026-01-01T00:00:00Z'},
    {'skills_mode': 'OR'}, {'skill_roles': ('negated',)}, {'skill_ids': ("1 OR 1=1",)},
    {'unknown': {'currency': 'include'}}, {'tax_mode': 'unknown'},
])
def test_invalid_filters(catalog, kwargs):
    with pytest.raises(ValueError):
        catalog.search(SearchFilter(**kwargs))


def test_baseline_equivalent_and_late_hydration(catalog):
    filters = [SearchFilter(), SearchFilter(skill_ids=(1,)), SearchFilter(skill_ids=(1, 2)),
               SearchFilter(skill_ids=(1, 2), skills_mode='ANY'),
               SearchFilter(occupation_ids=(3,), salary_min=100, currency='RUB', salary_period='month')]
    with connect(catalog.path) as con:
        for f in filters:
            results = [list(map(tuple, con.execute(*compile_page(f.normalized(), 'v1', 100, strategy=s))))
                       for s in ('baseline', 'indexed')]
            assert results[0] == results[1]
    import market_system.search.service as service
    original = service.connect
    statements = []
    def traced(*args, **kwargs):
        con = original(*args, **kwargs)
        con.set_trace_callback(statements.append)
        return con
    with patch.object(service, 'connect', traced):
        assert len(catalog.search(SearchFilter(), page_size=1).cards) == 1
    page_index = next(i for i, s in enumerate(statements) if s.startswith('SELECT c.content_id,c.published_at_utc'))
    text_index = next(i for i, s in enumerate(statements) if 'JOIN contents t' in s)
    assert page_index < text_index
    assert 'IN (4)' in statements[text_index]
    assert not any('json_each' in s or 'LIKE' in s or 'OFFSET' in s for s in statements)


def test_index_migration_idempotent(catalog):
    optimize(catalog.path)
    with connect(catalog.path) as con:
        assert con.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == 2
        sql, args = compile_page(SearchFilter().normalized(), 'v1', 20)
        plan = ' '.join(str(tuple(r)) for r in con.execute('EXPLAIN QUERY PLAN ' + sql, args))
        assert 'catalog_all_dates' in plan


def test_salary_class_strategies_and_cursor_equivalence(catalog):
    with connect(catalog.path) as con:
        for mode in ('ALL', 'ANY'):
            for skills in ((1,), (1, 2), (4,)):
                for policy in ('exclude', 'include', 'only'):
                    f = SearchFilter(skill_ids=skills, skills_mode=mode, salary_min=0,
                                     currency='RUB', salary_period='month',
                                     tax_mode='net', unknown={'tax_mode': policy}).normalized()
                    for after in (None, (utc('2026-01-02T00:00:00Z'), 3), (None, 8)):
                        result = [list(map(tuple, con.execute(*compile_page(f, 'v1', 3, after, strategy=s))))
                                  for s in ('baseline', 'indexed')]
                        assert result[0] == result[1]


def test_no_active_and_unready(catalog):
    with pytest.raises(ValueError, match='Unknown or unready'):
        catalog.search(SearchFilter(), result_set_id='missing')
    with connect(catalog.path) as con:
        con.execute('DELETE FROM active_result_set')
        con.commit()
    with pytest.raises(ValueError, match='No active'):
        catalog.search(SearchFilter())
    assert len(catalog.search(SearchFilter(), result_set_id='v1').cards) == 9


def test_empty_results_do_not_hydrate(catalog):
    with patch.object(Catalog, '_hydrate', wraps=Catalog._hydrate) as hydrate:
        page = catalog.search(SearchFilter(salary_min=1e9, currency='RUB', salary_period='month'))
        assert not page.cards and page.next_cursor is None
        assert hydrate.call_args.args[2] == []


def test_migration_two_rolls_back(tmp_path):
    path = tmp_path / 'collision.db'
    migrate(path)
    with connect(path) as con:
        con.execute('CREATE TABLE catalog_all_dates(x)')
        con.commit()
    with pytest.raises(sqlite3.OperationalError):
        optimize(path)
    with connect(path) as con:
        assert con.execute('SELECT version FROM schema_migrations').fetchall()[0][0] == 1
        assert con.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == 1
