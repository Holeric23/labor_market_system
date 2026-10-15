"""Read-only web projections. Filtering stays in Catalog.search."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
from market_system.sqlite import connect
from market_system.paths import local_path


class CatalogViews:
    def __init__(self, path):
        self.path = local_path(path)

    @contextmanager
    def reader(self):
        con = connect(self.path.as_uri() + '?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA query_only=ON')
        con.execute('BEGIN')
        try:
            yield con
        finally:
            con.close()

    def overview(self, freshness_days):
        with self.reader() as con:
            active = con.execute('''SELECT r.* FROM result_sets r JOIN active_result_set a
                ON a.result_set_id=r.result_set_id WHERE a.singleton=1''').fetchone()
            total = con.execute('SELECT count(*) FROM contents').fetchone()[0]
            result = {'active': dict(active) if active else None, 'total': total, 'latest': None,
                      'unknown_dates': 0, 'unprocessed': total, 'statuses': {}, 'processing': {}, 'types': {}}
            if active:
                args = (active['result_set_id'],)
                for column, key, table in [('review_status','statuses','search_catalog'),
                                           ('status','processing','document_processing'),
                                           ('post_type','types','search_catalog')]:
                    result[key] = {row[0]: row[1] for row in con.execute(
                        f'SELECT {column},count(*) FROM {table} WHERE result_set_id=? GROUP BY {column}', args)}
                result['unprocessed'] = total - sum(result['processing'].values())
                row = con.execute('SELECT max(published_at_utc),sum(published_at_utc IS NULL) FROM search_catalog WHERE result_set_id=?', args).fetchone()
                result['latest'], result['unknown_dates'] = row[0], row[1] or 0
                result['active']['versions'] = json.loads(active['versions_json'])
        age = None
        if result['latest']:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(result['latest'].replace('Z','+00:00'))).days
        result['age_days'] = age
        result['fresh'] = age is not None and 0 <= age <= freshness_days
        return result

    def choices(self):
        with self.reader() as con:
            row = con.execute('SELECT result_set_id FROM active_result_set WHERE singleton=1').fetchone()
            if not row:
                return {'classes': [], 'fields': {}}
            identity = row[0]
            classes = [dict(r) for r in con.execute('''SELECT k.* FROM classes k WHERE EXISTS(
                SELECT 1 FROM document_classes d WHERE d.result_set_id=? AND d.class_id=k.class_id)
                ORDER BY k.name,k.class_id''', (identity,))]
            fields = {}
            for field in ('work_format','schedule','employment_type','currency','salary_period'):
                fields[field] = [r[0] for r in con.execute(f'''SELECT DISTINCT {field} FROM search_catalog
                    WHERE result_set_id=? AND {field} IS NOT NULL ORDER BY {field}''', (identity,))]
            return {'classes': classes, 'fields': fields}
