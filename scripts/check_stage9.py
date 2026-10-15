"""Migrate the local catalog without changing inputs/results or creating slices."""
import hashlib
import json
from market_system.runtime import enable_guard
from market_system.paths import DATABASE_PATH, local_path
from market_system.search.store import connect
from market_system.analytics.store import Analytics, filters
from market_system.review.store import Actor

enable_guard()


def state():
    with connect(DATABASE_PATH) as con:
        con.execute('BEGIN')
        digest=hashlib.sha256()
        for row in con.execute('SELECT * FROM result_evidence ORDER BY result_set_id,content_id'):
            digest.update(json.dumps(tuple(row),ensure_ascii=False).encode())
        return {'active':con.execute('SELECT result_set_id FROM active_result_set').fetchone()[0],
                'counts':{t:con.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in
                          ('contents','publications','result_sets','document_processing','document_classes','result_evidence','result_publications','review_revisions','telegram_outbox','telegram_deliveries')},
                'machine_evidence_sha256':digest.hexdigest()}


before=state()
analytics=Analytics(DATABASE_PATH)
analytics.initialize()
analytics.initialize()
after=state()
assert before==after
with connect(DATABASE_PATH) as con:
    errors=con.execute('PRAGMA foreign_key_check').fetchall()
    migrations=[r[0] for r in con.execute('SELECT version FROM schema_migrations ORDER BY version')]
    slices=con.execute('SELECT count(*) FROM scientific_slices').fetchone()[0]
    # Small real-data read, never a synthetic assertion about extractor quality.
    publication=con.execute('''SELECT p.source_id,rp.published_at_utc FROM result_publications rp
        JOIN publications p ON p.id=rp.publication_id WHERE rp.result_set_id=? AND rp.published_at_utc IS NOT NULL
        ORDER BY rp.published_at_utc LIMIT 1''',(before['active'],)).fetchone()
assert not errors
from datetime import date,timedelta
start=publication['published_at_utc'][:10]
until=(date.fromisoformat(start)+timedelta(days=1)).isoformat()
result=analytics.report(filters(start,until,[publication['source_id']]),Actor('stage9-verification','OWNER'),before['active'])
assert result['manifest']['sample_size']>0
report={'before':before,'after':after,'migrations':migrations,'foreign_key_errors':len(errors),
        'scientific_slices':slices,'real_subset':{k:result['manifest'][k] for k in ('filters','sample_size','publication_sample_size','checksums')},
        'ml_started':False,'live_collection_started':False,'real_sends':0}
local_path('.artifacts/stage9-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False,indent=2))
