"""Shared, read-only catalog search for future web and Telegram adapters."""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import sqlite3
from typing import Any

from market_system.paths import DATABASE_PATH, local_path
from market_system.sqlite import connect
from .store import utc


class InvalidCursor(ValueError):
    """The client must discard its cursor and start a new search."""

    def __init__(self):
        super().__init__('Курсор несовместим или повреждён. Начните поиск заново.')


@dataclass(frozen=True)
class SearchFilter:
    post_type: str | None = None
    occupation_ids: tuple[int, ...] = ()
    skill_ids: tuple[int, ...] = ()
    skills_mode: str = 'ALL'
    skill_roles: tuple[str, ...] = ('required', 'preferred', 'possessed')
    published_from: str | None = None
    published_until: str | None = None
    work_format: str | None = None
    schedule: str | None = None
    employment_type: str | None = None
    salary_min: float | None = None
    currency: str | None = None
    salary_period: str | None = None
    tax_mode: str | None = None
    # Per-field policy: exclude (default), include, only. Applies to active
    # scalar predicates; "only" also works without a comparison value.
    unknown: dict[str, str] = field(default_factory=dict)

    def normalized(self) -> dict[str, Any]:
        value = asdict(self)
        if self.post_type not in (None, 'vacancy', 'resume'):
            raise ValueError('Invalid post_type')
        if self.skills_mode not in ('ALL', 'ANY'):
            raise ValueError('skills_mode must be ALL or ANY')
        for name in ('occupation_ids', 'skill_ids'):
            ids = value[name]
            if len(ids) > 100 or any(type(i) is not int or not 0 < i <= 2**63 - 1 for i in ids):
                raise ValueError('Class IDs must be positive integers; at most 100 per kind')
            value[name] = sorted(set(ids))
        roles = self.skill_roles
        if not roles or any(r not in ('required', 'preferred', 'possessed') for r in roles):
            raise ValueError('Only requirement/preference/possession roles are searchable')
        value['skill_roles'] = sorted(set(roles))
        for name in ('published_from', 'published_until'):
            if value[name] is not None:
                if not isinstance(value[name], str) or utc(value[name]) is None:
                    raise ValueError('Dates require an explicit timezone')
                value[name] = utc(value[name])
        if value['published_from'] and value['published_until'] and value['published_from'] >= value['published_until']:
            raise ValueError('Date interval must be nonempty')
        for name in ('work_format', 'schedule', 'employment_type', 'currency', 'salary_period', 'tax_mode'):
            if value[name] is not None and (not isinstance(value[name], str) or not value[name].strip()):
                raise ValueError(f'{name} must be a nonempty string')
        if self.tax_mode not in (None, 'gross', 'net'):
            raise ValueError('tax_mode must be gross or net')
        if self.salary_min is not None:
            if type(self.salary_min) not in (int, float) or not math.isfinite(self.salary_min) or self.salary_min < 0:
                raise ValueError('salary_min must be finite and nonnegative')
            if self.currency is None or self.salary_period is None:
                raise ValueError('salary_min requires currency and salary_period')
            value['salary_min'] = float(self.salary_min)
        elif self.currency is not None or self.salary_period is not None:
            raise ValueError('Currency and period require salary_min')
        allowed = {'post_type', 'published_at_utc', 'work_format', 'schedule', 'employment_type', 'tax_mode'}
        if any(k not in allowed or v not in ('exclude', 'include', 'only') for k, v in self.unknown.items()):
            raise ValueError('Invalid unknown-value policy; salary and class matches are always strict')
        value['unknown'] = {k: v for k, v in sorted(self.unknown.items()) if v != 'exclude'}
        return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _marks(values):
    return ','.join('?' for _ in values)


def compile_page(f, result_set_id, limit, after=None, *, strategy='indexed'):
    """Compile only compact page keys. Baseline exists solely for benchmarks."""
    predicates = ['c.result_set_id=?']
    params: list[Any] = [result_set_id]
    for column in ('post_type', 'work_format', 'schedule', 'employment_type', 'tax_mode', 'published_at_utc'):
        policy = f['unknown'].get(column, 'exclude')
        terms, values = [], []
        if column == 'published_at_utc':
            for key, op in (('published_from', '>='), ('published_until', '<')):
                if f[key] is not None:
                    terms.append(f'c.{column}{op}?')
                    values.append(f[key])
        elif f[column] is not None:
            terms.append(f'c.{column}=?')
            values.append(f[column])
        if policy == 'only':
            predicates.append(f'c.{column} IS NULL')
        elif terms:
            term = '(' + ' AND '.join(terms) + ')'
            if policy == 'include':
                term = f'({term} OR c.{column} IS NULL)'
            predicates.append(term)
            params.extend(values)
    if f['salary_min'] is not None:
        predicates.extend(['c.salary_from>=?', 'c.currency=?', 'c.salary_period=?'])
        params.extend([f['salary_min'], f['currency'], f['salary_period']])
    for kind, ids, roles, mode in (
        ('occupation', f['occupation_ids'], ['none'], 'ANY'),
        ('skill', f['skill_ids'], f['skill_roles'], f['skills_mode']),
    ):
        if not ids:
            continue
        # Role membership uses constants selected by validated enums, never names.
        role_sql = f'd.role IN ({_marks(roles)})'
        if kind == 'skill':
            role_sql += " AND ((s.post_type='vacancy' AND d.role IN ('required','preferred')) OR (s.post_type='resume' AND d.role='possessed'))"
        join = (' JOIN search_catalog s ON s.result_set_id=d.result_set_id AND s.content_id=d.content_id'
                if kind == 'skill' else '')
        if strategy == 'indexed' and f['salary_min'] is not None:
            # A salary range usually yields few candidates. Avoid materializing
            # every member of common classes before testing these candidates.
            groups = [[identity] for identity in ids] if mode == 'ALL' else [ids]
            for group in groups:
                predicates.append(
                    f'EXISTS(SELECT 1 FROM document_classes d{join} '
                    f'WHERE d.result_set_id=c.result_set_id AND d.content_id=c.content_id '
                    f'AND d.class_id IN ({_marks(group)}) AND {role_sql})')
                params.extend([*group, *roles])
            continue
        if strategy == 'baseline':
            term = (f'SELECT count(DISTINCT d.class_id) FROM document_classes d{join} '
                    f'WHERE d.result_set_id=c.result_set_id AND d.content_id=c.content_id '
                    f'AND d.class_id IN ({_marks(ids)}) AND {role_sql}')
            predicates.append(f'({term})' + ('=?' if mode == 'ALL' else '>0'))
            params.extend([*ids, *roles])
        elif strategy == 'indexed':
            term = (f'SELECT d.content_id FROM document_classes d{join} '
                    f'WHERE d.result_set_id=? AND d.class_id IN ({_marks(ids)}) AND {role_sql}')
            params.extend([result_set_id, *ids, *roles])
            if mode == 'ALL':
                term += ' GROUP BY d.content_id HAVING count(DISTINCT d.class_id)=?'
            predicates.append(f'c.content_id IN ({term})')
        else:
            raise ValueError('Unknown query strategy')
        if mode == 'ALL':
            params.append(len(ids))
    if after is not None:
        date, identity = after
        if date is None:
            predicates.append('c.published_at_utc IS NULL AND c.content_id<?')
            params.append(identity)
        else:
            predicates.append('(c.published_at_utc<? OR (c.published_at_utc=? AND c.content_id<?) OR c.published_at_utc IS NULL)')
            params.extend([date, date, identity])
    params.append(limit)
    source = 'search_catalog c'
    if strategy == 'indexed' and f['salary_min'] is not None:
        source += ' INDEXED BY catalog_salary'
    return ('SELECT c.content_id,c.published_at_utc FROM ' + source + ' WHERE '
            + ' AND '.join(predicates)
            + ' ORDER BY c.published_at_utc DESC,c.content_id DESC LIMIT ?'), params


@dataclass(frozen=True)
class SearchPage:
    cards: list[dict[str, Any]]
    next_cursor: str | None
    result_set_id: str
    revision: str


class Catalog:
    """No network, model loading, full-text scan, or writes during searches."""

    def __init__(self, path=DATABASE_PATH):
        self.path = local_path(path)

    def search(self, filters: SearchFilter, cursor: str | None = None, *,
               page_size: int = 20, result_set_id: str | None = None) -> SearchPage:
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError('page_size must be 1..100')
        f = filters.normalized()
        fingerprint = _digest(f)
        con = connect(self.path.as_uri() + '?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        try:
            con.execute('PRAGMA query_only=ON')
            con.execute('BEGIN')
            active = result_set_id is None
            if active:
                row = con.execute('SELECT result_set_id FROM active_result_set WHERE singleton=1').fetchone()
                if row is None:
                    raise ValueError('No active result set')
                result_set_id = row[0]
            row = con.execute("SELECT ready_at,versions_json FROM result_sets WHERE result_set_id=? AND state='ready'", (result_set_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown or unready result set')
            # Result sets are immutable. Active-generation also detects switching
            # away and back between requests; an explicitly pinned set remains valid.
            generation = con.execute('SELECT coalesce(max(id),0) FROM result_set_switches').fetchone()[0] if active else None
            revision = _digest([result_set_id, row['ready_at'], row['versions_json'], generation, active])
            after = self._cursor(cursor, fingerprint, revision) if cursor is not None else None
            self._validate_classes(con, f)
            sql, args = compile_page(f, result_set_id, page_size + 1, after)
            keys = con.execute(sql, args).fetchall()
            selected = keys[:page_size]
            cards = self._hydrate(con, result_set_id, selected)
            next_cursor = None
            if len(keys) > page_size:
                last = selected[-1]
                payload = [1, fingerprint, revision, last['published_at_utc'], last['content_id']]
                next_cursor = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode()
            return SearchPage(cards, next_cursor, result_set_id, revision)
        finally:
            con.close()

    def get(self, content_id: int, result_set_id: str) -> dict[str, Any] | None:
        """Load one card from an explicitly pinned ready result set."""
        if type(content_id) is not int or not 0 < content_id <= 2**63 - 1:
            raise ValueError('Invalid content ID')
        con = connect(self.path.as_uri() + '?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        try:
            con.execute('PRAGMA query_only=ON')
            con.execute('BEGIN')
            version = con.execute("SELECT * FROM result_sets WHERE result_set_id=? AND state='ready'", (result_set_id,)).fetchone()
            if version is None:
                return None
            keys = con.execute('SELECT content_id FROM search_catalog WHERE result_set_id=? AND content_id=?', (result_set_id, content_id)).fetchall()
            cards = self._hydrate(con, result_set_id, keys)
            if not cards:
                return None
            card = cards[0]
            card['version'] = dict(version)
            card['version']['versions'] = json.loads(version['versions_json'])
            return card
        finally:
            con.close()

    @staticmethod
    def _cursor(cursor, fingerprint, revision):
        try:
            if not isinstance(cursor, str) or len(cursor) > 2048:
                raise ValueError()
            data = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))
            if not isinstance(data, list) or len(data) != 5 or data[:3] != [1, fingerprint, revision]:
                raise ValueError()
            date, identity = data[3:]
            if type(identity) is not int or not 0 < identity <= 2**63 - 1 or (date is not None and (not isinstance(date, str) or utc(date) != date)):
                raise ValueError()
            return date, identity
        except (ValueError, TypeError, UnicodeError, OverflowError):
            raise InvalidCursor() from None

    @staticmethod
    def _validate_classes(con, f):
        for kind, key in (('occupation', 'occupation_ids'), ('skill', 'skill_ids')):
            ids = f[key]
            if not ids:
                continue
            rows = con.execute(f'SELECT class_id,namespace FROM classes WHERE class_id IN ({_marks(ids)})', ids).fetchall()
            if len(rows) != len(ids) or any(not r['namespace'].startswith(kind + ':') for r in rows):
                raise ValueError(f'Unknown or wrong-kind {kind} class ID')

    @staticmethod
    def _hydrate(con, result_set_id, keys):
        if not keys:
            return []
        ids = [r['content_id'] for r in keys]
        marks = _marks(ids)
        args = [result_set_id, *ids]
        rows = con.execute(f'''SELECT c.*,t.text,e.classification_json,e.profile_json,e.skills_json
            FROM search_catalog c JOIN contents t ON t.id=c.content_id
            JOIN result_evidence e ON e.result_set_id=c.result_set_id AND e.content_id=c.content_id
            WHERE c.result_set_id=? AND c.content_id IN ({marks})''', args)
        cards = {}
        for row in rows:
            card = dict(row)
            card['evidence'] = {key: json.loads(card.pop(key + '_json')) if card[key + '_json'] is not None else card.pop(key + '_json')
                                for key in ('classification', 'profile', 'skills')}
            card['publications'] = []
            card['classes'] = []
            cards[card['content_id']] = card
        rows = con.execute(f'''SELECT rp.content_id,rp.publication_id,rp.published_at_utc,
            p.url,p.source_id,s.name AS source_name,st.name AS source_type
            FROM result_publications rp JOIN publications p ON p.id=rp.publication_id
            JOIN sources s ON s.id=p.source_id JOIN source_types st ON st.id=s.source_type_id
            WHERE rp.result_set_id=? AND rp.content_id IN ({marks})
            ORDER BY rp.published_at_utc DESC,rp.publication_id DESC''', args)
        for row in rows:
            publication = dict(row)
            cards[publication.pop('content_id')]['publications'].append(publication)
        rows = con.execute(f'''SELECT d.content_id,d.class_id,d.role,d.score,
            k.namespace,k.class_key,k.name,k.definition_version
            FROM document_classes d JOIN classes k ON k.class_id=d.class_id
            WHERE d.result_set_id=? AND d.content_id IN ({marks})
            ORDER BY d.content_id,d.class_id,d.role''', args)
        for row in rows:
            definition = dict(row)
            cards[definition.pop('content_id')]['classes'].append(definition)
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='result_review_applications'").fetchone():
            for row in con.execute(f'''SELECT a.content_id,a.revision,a.multiple_offers,r.author,r.created_at,r.annotation_json
              FROM result_review_applications a JOIN review_revisions r ON r.content_id=a.content_id AND r.revision=a.revision
              WHERE a.result_set_id=? AND a.content_id IN ({marks})''',args):
                cards[row['content_id']]['human_review'] = dict(row)
        return [cards[i] for i in ids]
