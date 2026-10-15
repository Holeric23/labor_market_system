"""Stage 10: preserve working results; import report data and evaluate six real texts."""
import hashlib
import json
from pathlib import Path

from market_system.runtime import enable_guard
from market_system.paths import DATABASE_PATH, local_path
from market_system.search.store import connect
from market_system.review.store import Actor
from market_system.model_management.store import Models
from market_system.model_management.experiments import import_history, run


def state():
    with connect(DATABASE_PATH) as con:
        con.execute('BEGIN')
        evidence = hashlib.sha256()
        for row in con.execute('SELECT * FROM result_evidence ORDER BY result_set_id,content_id'):
            evidence.update(json.dumps(tuple(row),ensure_ascii=False).encode())
        slices = hashlib.sha256()
        for row in con.execute('SELECT * FROM scientific_slices ORDER BY slice_id'):
            slices.update(json.dumps(tuple(row),ensure_ascii=False).encode())
        return {'active':con.execute('SELECT result_set_id FROM active_result_set').fetchone()[0],
                'counts':{t:con.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in
                          ('contents','publications','result_sets','document_processing','document_classes','result_evidence',
                           'result_publications','review_revisions','telegram_outbox','telegram_deliveries','scientific_slices')},
                'machine_evidence_sha256':evidence.hexdigest(),'scientific_slices_sha256':slices.hexdigest()}


def main():
    enable_guard()
    before = state()
    owner = Actor('stage10-verification','OWNER')
    models = Models(DATABASE_PATH)
    models.initialize(); models.initialize()
    version = models.register(owner)
    historical = import_history(models,Path('C:/Study/Анализ данных/Итоговый проект/Python_research_project/research/results'),owner)
    with connect(DATABASE_PATH) as con:
        candidates = con.execute('''SELECT m.content_id,min(l.label) label FROM review_memberships m
          JOIN review_type_labels l ON l.content_id=m.content_id WHERE m.cohort='development'
          GROUP BY m.content_id HAVING count(DISTINCT l.label)=1 ORDER BY m.content_id''').fetchall()
        ids = []
        for label in ('vacancy','resume','trash'):
            ids.extend([r['content_id'] for r in candidates if r['label']==label][:2])
    assert 1<=len(ids)<=6
    experiment = run(models,version,ids,6,120,owner,'development','diagnostic')
    record = models.experiments(owner,experiment)
    assert record['state']=='succeeded', record['error']
    after = state()
    assert before==after
    with connect(DATABASE_PATH) as con:
        errors = con.execute('PRAGMA foreign_key_check').fetchall()
        migrations = [r[0] for r in con.execute('SELECT version FROM schema_migrations ORDER BY version')]
    assert not errors
    report = {'before':before,'after':after,'migrations':migrations,'foreign_key_errors':0,
              'model':version,'registry_status':models.get(version,owner)['status'],'historical_experiments':historical,
              'real_experiment':experiment,'document_ids':ids,'budget':{'max_documents':6,'seconds':120},
              'metrics':json.loads(record['metrics_json']),'provenance':json.loads(record['provenance_json']),
              'mass_training_started':False,'full_benchmark_started':False,'full_rebuild_started':False,
              'live_collection_started':False,'real_sends':0}
    local_path('.artifacts/stage10-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='metrics'},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
