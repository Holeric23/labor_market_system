"""Migrate unified review and verify preservation of the working machine snapshot."""
import hashlib
import json
from market_system.paths import DATABASE_PATH, local_path
from market_system.runtime import enable_guard
from market_system.search.store import connect
from market_system.review.store import Review, Actor
from market_system.review.import_legacy import import_legacy

enable_guard()

def snapshot():
    with connect(DATABASE_PATH) as con:
        active = con.execute('SELECT result_set_id FROM active_result_set').fetchone()[0]
        counts = {table: con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                  for table in ('contents', 'publications', 'result_sets', 'document_processing',
                                'document_classes', 'search_catalog', 'result_evidence', 'result_publications')}
        digest = hashlib.sha256()
        for row in con.execute('SELECT * FROM result_evidence ORDER BY result_set_id,content_id'):
            digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        return {'active': active, 'counts': counts, 'machine_evidence_sha256': digest.hexdigest()}

before = snapshot()
review = Review(DATABASE_PATH)
review.initialize()
imported = import_legacy(review, Actor('local-owner', 'OWNER'))
after = snapshot()
assert before == after, 'Migration changed the existing machine snapshot'
assert imported == import_legacy(review, Actor('local-owner', 'OWNER')), 'Import is not idempotent'
with connect(DATABASE_PATH) as con:
    counts = {table: con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
              for table in ('review_revisions', 'review_datasets', 'review_memberships',
                            'review_type_labels', 'result_review_applications', 'review_training_candidates')}
    cohorts = [tuple(row) for row in con.execute('SELECT cohort,count(*),sum(content_id IS NULL) FROM review_memberships GROUP BY cohort')]
    statuses = [tuple(row) for row in con.execute('SELECT status,count(*) FROM review_revisions GROUP BY status')]
    migrations = [row[0] for row in con.execute('SELECT version FROM schema_migrations ORDER BY version')]
    errors = con.execute('PRAGMA foreign_key_check').fetchall()
assert not errors
report = {'before': before, 'after': after, 'review_counts': counts, 'cohorts_count_unmatched': cohorts,
          'revision_statuses': statuses, 'migrations': migrations, 'foreign_key_errors': len(errors),
          'legacy_import': imported, 'idempotent_import': True, 'training_started': False}
local_path('.artifacts/stage7-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False, indent=2))
