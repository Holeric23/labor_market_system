"""Read-only working-state evidence; no migrations, collection or ML."""
from contextlib import closing
import hashlib
import json

from market_system.operations.config import Operations
from market_system.sqlite import connect


def main():
    config = Operations.load()
    config.prepare()
    with closing(connect(config.paths['database'].as_uri()+'?mode=ro',uri=True)) as con:
        con.execute('BEGIN')
        evidence = hashlib.sha256()
        for row in con.execute('SELECT * FROM result_evidence ORDER BY result_set_id,content_id'):
            evidence.update(json.dumps(tuple(row),ensure_ascii=False).encode())
        result = {'active':con.execute('SELECT result_set_id FROM active_result_set').fetchone()[0],
                  'migrations':[r[0] for r in con.execute('SELECT version FROM schema_migrations ORDER BY version')],
                  'counts':{t:con.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in (
                      'contents','publications','result_sets','result_evidence','document_classes','jobs','model_registry',
                      'model_experiments','review_revisions','scientific_slices','telegram_subscriptions','telegram_outbox','telegram_deliveries')},
                  'machine_evidence_sha256':evidence.hexdigest()}
    (config.home/'.artifacts/stage11-working-state.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
