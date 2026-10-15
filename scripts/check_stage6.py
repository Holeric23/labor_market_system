"""Apply stage-6 schema and verify that migration performs no collection."""
import json
from market_system.collection.store import Sources
from market_system.paths import DATABASE_PATH, local_path
from market_system.runtime import enable_guard
from market_system.search.store import connect

enable_guard()
def snapshot():
    with connect(DATABASE_PATH) as con:
        active=con.execute('SELECT result_set_id FROM active_result_set').fetchone()[0]
        return {'active':active,
                'base_counts':{table:con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                               for table in ('contents','publications','sources','source_types')},
                'active_counts':{table:con.execute(f'SELECT count(*) FROM {table} WHERE result_set_id=?',(active,)).fetchone()[0]
                                 for table in ('document_processing','document_classes','search_catalog','result_evidence','result_publications')}}
before=snapshot()
sources=Sources(DATABASE_PATH)
sources.initialize()
after=snapshot()
assert before==after
with connect(DATABASE_PATH) as con:
    state={table:con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
           for table in ('managed_sources','collection_jobs','publication_revisions','collection_handoffs')}
    migrations=[r[0] for r in con.execute('SELECT version FROM schema_migrations ORDER BY version')]
    fk=con.execute('PRAGMA foreign_key_check').fetchall()
assert not fk
report={'before':before,'after':after,'collection_counts':state,'migrations':migrations,'foreign_key_errors':len(fk),
        'telegram_credentials_present':local_path('state/collection/telegram.json').is_file(),
        'telegram_session_present':local_path('state/collection/telegram.session').is_file(),
        'live_collection_tested':False,'parsers_started':False}
local_path('.artifacts/stage6-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False,indent=2))
