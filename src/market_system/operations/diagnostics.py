"""Read-only operational evidence. Never migrates or loads serialized ML weights."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from market_system.sqlite import connect
from market_system.paths import local_path, PACKAGE_ROOT

MIGRATIONS = {1:'search/001_search.sql',2:'search/002_search.sql',3:'jobs/003_jobs.sql',
              4:'collection/004_collection.sql',5:'review/005_review.sql',6:'telegram_bot/006_delivery.sql',
              7:'analytics/007_analytics.sql',8:'model_management/008_models.sql'}


def migration_errors(con):
    rows = dict(con.execute('SELECT version,sha256 FROM schema_migrations'))
    expected = {n:hashlib.sha256((PACKAGE_ROOT/p).read_text(encoding='utf-8-sig').encode()).hexdigest()
                for n,p in MIGRATIONS.items()}
    return [f'migration {n}: missing or incompatible' for n,h in expected.items() if rows.get(n)!=h] + [
        f'unknown migration {n}' for n in rows if n not in expected]


def inspect(config=None, *, database=None, full=False, freshness_hours=72, lightweight=False):
    try:
        return _inspect(config, database=database, full=full,
                        freshness_hours=freshness_hours, lightweight=lightweight)
    except sqlite3.DatabaseError:
        if not lightweight:
            raise
        return {'ok': False, 'errors': ['Диагностика временно недоступна: база занята или проверка прервана. Повторите позже.'],
                'warnings': [], 'integrity': 'not_checked', 'foreign_key_errors': None}


def _inspect(config=None, *, database=None, full=False, freshness_hours=72, lightweight=False):
    path = local_path(database or config.paths['database'])
    errors, warnings = [], []
    result = {'database_exists':path.is_file(),'errors':errors,'warnings':warnings}
    if not path.is_file():
        errors.append('Database is missing; restore a verified backup')
        result['ok'] = False
        return result
    with closing(connect(path.as_uri()+'?mode=ro',uri=True, timeout=0.25 if lightweight else 15)) as con:
        con.row_factory = __import__('sqlite3').Row
        if lightweight:
            deadline = time.monotonic() + 2
            con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            result['integrity'] = 'not_checked; use diagnose'
            result['foreign_key_errors'] = None
            warnings.append('Проверка целостности и всех связей не выполняется в вебе. Используйте команду diagnose.')
        else:
            con.execute('BEGIN')
            result['integrity'] = [r[0] for r in con.execute('PRAGMA integrity_check' if full else 'PRAGMA quick_check')]
            if result['integrity'] != ['ok']:
                errors.append('SQLite integrity failed')
            result['foreign_key_errors'] = sum(1 for _ in con.execute('PRAGMA foreign_key_check'))
            if result['foreign_key_errors']:
                errors.append('Foreign key violations')
        try:
            result['migrations'] = [dict(r) for r in con.execute('SELECT * FROM schema_migrations ORDER BY version')]
            errors.extend(migration_errors(con))
            if errors:
                result['ok'] = False
                return result
            active = con.execute('SELECT r.* FROM active_result_set a JOIN result_sets r USING(result_set_id)').fetchone()
            result['active'] = {k:active[k] for k in ('result_set_id','model_version','state','ready_at','coverage_json')} if active else None
            if not active or active['state']!='ready':
                errors.append('No active ready result set')
            elif full:
                from market_system.search.store import validate
                try:
                    result['active_counts'] = validate(con, active['result_set_id'])
                except ValueError:
                    errors.append('Active result set coverage/links failed')
            model = con.execute("SELECT model_version,spec_json,status FROM model_registry WHERE status='active'").fetchone()
            result['model'] = {'version':model['model_version'],'status':model['status']} if model else None
            if not model or not active or model['model_version']!=active['model_version']:
                errors.append('Active model registry mismatch')
            elif full:
                from .compatibility import resolved_spec
                from market_system.model_management.adapters import verify
                try:
                    verify(resolved_spec(json.loads(model['spec_json'])))
                    result['model']['artifacts_and_dependencies'] = 'verified'
                except (ValueError,OSError) as exc:
                    errors.append('Model compatibility failed: '+type(exc).__name__)
                    result['model']['artifacts_and_dependencies'] = 'failed'
            else:
                result['model']['artifacts_and_dependencies'] = 'not_checked; use diagnose --full'
            for key,table in [('jobs','jobs'),('collection_jobs','collection_jobs'),('delivery','telegram_deliveries')]:
                result[key] = {r[0]:r[1] for r in con.execute(f'SELECT state,count(*) FROM {table} GROUP BY state')}
                if result[key].get('failed'):
                    warnings.append(key+': failed items need OWNER attention')
            result['queue_oldest'] = con.execute("SELECT min(created_at) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
            result['delivery_debt'] = con.execute("SELECT count(*),min(next_attempt_at) FROM telegram_deliveries WHERE state IN ('queued','running','failed')").fetchone()
            result['delivery_debt'] = dict(zip(('count','oldest_due_at'),result['delivery_debt']))
            result['handoff_debt'] = con.execute('SELECT count(*) FROM collection_handoffs WHERE processing_job_id IS NULL').fetchone()[0]
            result['unfinished_experiments'] = con.execute("SELECT count(*) FROM model_experiments WHERE state='running'").fetchone()[0]
            if result['unfinished_experiments']:
                warnings.append('Interrupted/running experiment requires manual investigation')
            result['subscription_counts'] = {r[0]:r[1] for r in con.execute('SELECT status,count(*) FROM telegram_subscriptions GROUP BY status')}
            # Aggregates only: private recipients and filters never appear in diagnostics.
            result['sources'] = []
            hours = config.values['source_freshness_hours'] if config else freshness_hours
            for row in con.execute('SELECT source_id,enabled,schedule_enabled,state,last_success_at FROM managed_sources'):
                item = dict(row)
                age = None
                if row['last_success_at']:
                    parsed = datetime.fromisoformat(row['last_success_at'])
                    if parsed.tzinfo:
                        age = (datetime.now(timezone.utc)-parsed).total_seconds()/3600
                item['age_hours'] = age
                item['freshness'] = 'unknown' if age is None else ('stale' if age>hours else 'fresh')
                result['sources'].append(item)
                if item['enabled'] and item['freshness']!='fresh':
                    warnings.append(f"source {item['source_id']}: {item['freshness']}")
            if not result['sources']:
                warnings.append('No managed sources configured; freshness is unknown')
        except __import__('sqlite3').DatabaseError:
            errors.append('Диагностика прервана: база занята, превышено время проверки или недоступна схема.' if lightweight else 'Schema diagnostics failed')
    if config:
        result['delivery_enabled'] = config.values['delivery_enabled']
        if result.get('delivery_debt',{}).get('count') and not config.values['delivery_enabled']:
            warnings.append('Delivery debt exists and transport is disabled')
        for name in ('web_state','bot_state','annotation_database'):
            db = config.paths[name]
            if db.exists():
                with closing(connect(db.as_uri()+'?mode=ro',uri=True)) as aux:
                    state = [r[0] for r in aux.execute('PRAGMA quick_check')]
                    if state!=['ok'] or aux.execute('PRAGMA foreign_key_check').fetchone():
                        errors.append(name+': integrity failed')
            elif name=='annotation_database':
                errors.append('annotation_database missing')
    result['ok'] = not errors
    return result
