"""Bounded explicit experiments and read-only import of historical report data."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

from market_system.jobs.store import text_digest
from market_system.jobs.worker import worker_lock
from market_system.paths import local_path
from market_system.review.store import owner
from market_system.search.store import connect, encode, now, digest
from .evaluation import FIELDS, LIMITATION


def snapshot(con, ids, cohort, purpose):
    if cohort not in ('development','challenge','test') or purpose not in ('diagnostic','selection'):
        raise ValueError('Укажите development/challenge/test и diagnostic/selection.')
    if purpose == 'selection' and cohort != 'development':
        raise ValueError('Test и challenge не используются для подбора параметров; selection допускает только development.')
    items = []
    for cid in ids:
        row = con.execute('SELECT text,text_hash FROM contents WHERE id=?',(cid,)).fetchone()
        memberships = [dict(r) for r in con.execute('SELECT * FROM review_memberships WHERE content_id=?',(cid,))]
        if not row or not any(m['cohort']==cohort for m in memberships):
            raise ValueError('Документ не принадлежит объявленной выборке: '+str(cid))
        if purpose == 'selection':
            if any(m['cohort'] in ('train','test','challenge') for m in memberships):
                raise ValueError('Пересечение development с защищённой/обучающей выборкой: '+str(cid))
            for m in memberships:
                if m['group_key'] and con.execute('''SELECT 1 FROM review_memberships WHERE dataset_id=? AND group_key=?
                    AND cohort IN ('train','test','challenge')''',(m['dataset_id'],m['group_key'])).fetchone():
                    raise ValueError('Группа документа пересекается с защищённой выборкой.')
        labels = [dict(r) for r in con.execute('''SELECT l.*,d.origin,d.manifest_json FROM review_type_labels l
            JOIN review_datasets d USING(dataset_id) WHERE content_id=?''',(cid,))]
        revision = con.execute("""SELECT * FROM review_revisions WHERE content_id=? AND status='confirmed'
                                  ORDER BY revision DESC LIMIT 1""",(cid,)).fetchone()
        values = {x['label'] for x in labels}
        fields = None
        if revision:
            annotation = json.loads(revision['annotation_json'])
            values = {annotation['post_type']}
            if not annotation['multiple_offers']:
                fields = {}
                if len(annotation['salary']) <= 1:
                    fields.update(annotation['salary'][0] if annotation['salary'] else dict.fromkeys(FIELDS[:5]))
                fields.update({k:v[0] if v else None for k,v in annotation['conditions'].items() if len(v)<=1})
                fields.update(skills=[],occupations=[])
                for key, entries in [('occupations',annotation['occupations']),('skills',annotation['skills'])]:
                    for entry in entries:
                        if key=='skills' and entry['role'] not in (('required','preferred') if annotation['post_type']=='vacancy' else ('possessed',) if annotation['post_type']=='resume' else ()):
                            continue
                        cl = con.execute('SELECT namespace,class_key FROM classes WHERE class_id=?',
                                         (entry if key=='occupations' else entry['class_id'],)).fetchone()
                        if annotation['post_type'] in ('vacancy','resume'):
                            fields[key].append([*cl,*([entry['role']] if key=='skills' else [])])
        if len(values)!=1 or next(iter(values)) not in ('vacancy','resume','trash'):
            raise ValueError('Нет однозначной человеческой метки типа: '+str(cid))
        publications = [dict(r) for r in con.execute('SELECT id,source_id,url,published_at FROM publications WHERE content_id=?',(cid,))]
        items.append({'content_id':cid,'text_sha256':text_digest(row['text']),'label':next(iter(values)),
                      'fields':fields,'memberships':memberships,'label_origins':labels,
                      'confirmed_revision':dict(revision) if revision else None,'publications':publications})
    return items


def run(models, version, ids, max_documents, seconds, actor, cohort='development', purpose='diagnostic'):
    owner(actor)
    if type(max_documents) is not int or not 1<=max_documents<=100 or type(seconds) is not int or not 1<=seconds<=300:
        raise ValueError('Явный бюджет: 1..100 документов и 1..300 секунд.')
    if not ids or len(ids)!=len(set(ids)) or len(ids)>max_documents or any(type(i) is not int or i<=0 for i in ids):
        raise ValueError('Нужны уникальные ID в пределах заданного бюджета.')
    record = models.get(version,actor)
    spec = json.loads(record['spec_json'])
    if record['status']=='retired':
        raise ValueError('Сначала явно одобрите retired-модель.')
    # The same executor lock prevents experiments and catalog processing overlapping.
    with worker_lock(models.path):
        with models.jobs.transaction() as con:
            inputs = snapshot(con,ids,cohort,purpose)
            identity = uuid.uuid4().hex
            parameters = {'max_documents':max_documents,'seconds':seconds,'purpose':purpose,'cohort':cohort,
                          'model_spec_sha256':digest(spec),'training':False}
            provenance = {'scope':'retrospective_reused_historical_test' if cohort=='test' else 'purposefully_sampled_'+cohort,
                          'independent_audit':False,'test_used_for_selection':False,'limitations':LIMITATION}
            provenance['observed_cohorts'] = sorted({m['cohort'] for i in inputs for m in i['memberships']})
            provenance['historical_test_overlap'] = 'test' in provenance['observed_cohorts']
            con.execute('INSERT INTO model_experiments VALUES(?,?,?,?,?,?,?,NULL,NULL,?,NULL,?)',
                        (identity,version,'bounded_evaluation',encode(parameters),encode(inputs),encode(provenance),'running',now(),actor.identity))
        folder = local_path('.artifacts/experiments/'+identity)
        folder.mkdir(parents=True)
        request = folder/'request.json'
        output = folder/'metrics.json'
        request.write_text(encode({'database':str(models.path),'spec':spec,'inputs':inputs}),encoding='utf-8')
        start = time.perf_counter()
        try:
            with (folder/'worker.log').open('wb') as log:
                result = subprocess.run([sys.executable,'-m','market_system.model_management.experiment_worker',
                                         str(request),str(output)],stdout=log,stderr=log,timeout=seconds)
            if result.returncode:
                raise ValueError('Ошибка загрузки/оценки; подробности в '+str(folder/'worker.log'))
            metrics = json.loads(output.read_text(encoding='utf-8'))
            metrics['wall_seconds'] = time.perf_counter()-start
            with models.jobs.transaction() as con:
                con.execute("UPDATE model_experiments SET state='succeeded',metrics_json=?,finished_at=? WHERE experiment_id=?",
                            (encode(metrics),now(),identity))
        except Exception as exc:
            error = 'Исчерпан бюджет времени; процесс остановлен.' if isinstance(exc,subprocess.TimeoutExpired) else str(exc)
            with models.jobs.transaction() as con:
                con.execute("UPDATE model_experiments SET state='failed',error=?,finished_at=? WHERE experiment_id=?",
                            (error[:4000],now(),identity))
        return identity


REPORTS = {'manifest.json','protocol.json','selection.json','warnings.json','failures.json','data_audit.json',
           'test_results.json','test_result.json','extraction_protocol.json','exploratory_favorites.json',
           'incremental_protocol.json','cascade_selection.json','temporal.json'}


def import_history(models, root, actor):
    """Read report bytes only; no imports, execution or writable SQLite in source."""
    owner(actor)
    root = Path(root).resolve()
    imported = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.is_symlink():
            continue
        files = {}
        for path in sorted(folder.iterdir()):
            if path.is_symlink() or not path.is_file() or not (path.name in REPORTS or path.suffix=='.csv' and
                any(key in path.stem for key in ('selection','validation','test','metrics','comparison','finalists')) and 'predictions' not in path.stem):
                continue
            # Bounded data-only copy. Large tables retain hashes, not an invented summary.
            checksum = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda:stream.read(1024*1024),b''):
                    checksum.update(block)
            data = path.read_bytes() if path.stat().st_size<=2_000_000 else None
            if data is not None and hashlib.sha256(data).hexdigest()!=checksum.hexdigest():
                raise ValueError('Historical report changed during read: '+str(path))
            files[path.name] = {'sha256':checksum.hexdigest(),'size':path.stat().st_size,
                                'data':json.loads(data.decode('utf-8-sig')) if data is not None and path.suffix=='.json' else data.decode('utf-8-sig') if data is not None else None}
        if not files:
            continue
        identity = 'history_'+digest({'path':str(folder),'files':files})
        with models.jobs.transaction() as con:
            con.execute('INSERT OR IGNORE INTO model_experiments VALUES(?,NULL,?,?,?,?,?,?,NULL,?,?,?)',
                (identity,'historical_reports',encode({'report_files':list(files),'training':False}),encode({}),
                 encode({'origin':str(folder),'files_sha256':{k:v['sha256'] for k,v in files.items()},
                         'scope':'historical_reused_corpus','independent_audit':False,'limitations':LIMITATION}),
                 'succeeded',encode({'reports':files}),now(),now(),actor.identity))
        imported.append(identity)
    return imported
