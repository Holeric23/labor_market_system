"""Create a three-document real-model job without changing the active catalog."""
import hashlib
import json
from market_system.runtime import enable_guard, load_config
from market_system.jobs import Jobs
from market_system.search.store import connect
from market_system.paths import local_path

enable_guard()
paths=load_config()
jobs=Jobs(paths['database'])
jobs.initialize()
with connect(jobs.path) as con:
    if con.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running')").fetchone():
        raise RuntimeError('Existing runnable job; do not run an unrelated job')
    active=con.execute('SELECT result_set_id FROM active_result_set').fetchone()[0]
    ids=[r[0] for r in con.execute("SELECT content_id FROM search_catalog WHERE result_set_id=? AND post_type='vacancy' ORDER BY content_id LIMIT 3",(active,))]
    before={t:con.execute(f'SELECT count(*) FROM {t} WHERE result_set_id=?',(active,)).fetchone()[0]
            for t in ('document_processing','document_classes','search_catalog','result_evidence','result_publications')}
annotation_hash=hashlib.sha256(paths['annotation_database'].read_bytes()).hexdigest()
identity=jobs.submit('sample',ids,batch_size=2,actor='stage5-real-smoke')
local_path('.artifacts/stage5-real-job.json').write_text(json.dumps({'job_id':identity,'ids':ids,'active_before':active,
    'counts_before':before,'annotation_sha256_before':annotation_hash},indent=2),encoding='utf-8')
print(identity,ids)
