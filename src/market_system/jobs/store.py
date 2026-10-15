"""Queue persistence and frozen inputs. No analyzer imports in the HTTP path."""
from contextlib import contextmanager, closing, nullcontext
import ast
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from market_system.paths import DATABASE_PATH, local_path
from market_system.sqlite import connect as sqlite_connect
from market_system.search.store import connect, migrate as migrate_catalog, encode, digest, now, utc
from market_system.runtime import load_config


def text_digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def specification(config=None):
    paths = load_config() if config is None else config
    model = local_path(paths['model'])
    metadata = json.loads((model/'metadata.json').read_text(encoding='utf-8'))
    files = {str(local_path(model/name)): value for name, value in metadata['artifacts_sha256'].items()}
    files[str(model/'metadata.json')] = hashlib.sha256((model/'metadata.json').read_bytes()).hexdigest()
    for key in ('skill_lexicon', 'occupation_lexicon'):
        path = local_path(paths[key])
        files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    # Freeze extractor implementation as well as declared versions and dictionaries.
    extraction = Path(__file__).parents[1]/'core/extraction'
    for path in [*extraction.glob('*.py'), *Path(__file__).parents[1].joinpath('model_management').glob('*.py'), Path(__file__).parents[1]/'labor_market/pipeline.py',
                 Path(__file__).parents[1]/'labor_market/model.py', Path(__file__).with_name('projection.py')]:
        files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    def constant(filename, name):
        tree = ast.parse((extraction/filename).read_text(encoding='utf-8-sig'))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id==name for t in node.targets):
                return ast.literal_eval(node.value)
        raise ValueError('Missing extractor version')
    skills = json.loads(local_path(paths['skill_lexicon']).read_text(encoding='utf-8'))
    occupations = json.loads(local_path(paths['occupation_lexicon']).read_text(encoding='utf-8'))
    legacy_versions = {'skills_extractor':constant('skills.py','VERSION'),
        'skills_dictionary':skills['version'], 'skills_dictionary_sha256':files[str(local_path(paths['skill_lexicon']))],
        'occupation_extractor':constant('occupations.py','VERSION'),
        'occupation_dictionary':occupations['version'], 'occupation_dictionary_sha256':digest(occupations),
        'salary_extractor':constant('salary.py','EXTRACTOR_VERSION'),
        'conditions_extractor':constant('work_conditions.py','VERSION')}
    return {'legacy_versions':legacy_versions, 'model_version': metadata['model_version'], 'model_manifest': metadata,
            'paths': {k:str(local_path(v)) for k,v in paths.items()}, 'files': files,
            'fingerprint': digest(files)}


def confirmed_corrections(spec):
    """Snapshot last confirmed revision; later drafts do not erase confirmations."""
    paths = spec.get('paths', {})
    if not paths.get('annotation_database'):
        return {}
    dataset = json.loads(local_path(paths['dataset']).read_text(encoding='utf-8'))
    with closing(sqlite_connect(local_path(paths['annotation_database']).as_uri()+'?mode=ro', uri=True)) as con:
        rows = con.execute('''SELECT id,revision,annotation,saved_at FROM revisions r
          WHERE dataset=? AND status='confirmed' AND revision=(SELECT MAX(revision) FROM revisions x
          WHERE x.id=r.id AND x.dataset=r.dataset AND x.status='confirmed')''', (dataset['dataset_id'],)).fetchall()
    known = {item['id'] for item in dataset['items']}
    return {key:{'revision':revision, 'annotation':json.loads(value), 'saved_at':saved,
                 'dataset':dataset['dataset_id'], 'skills':dataset['skills'], 'occupations':dataset['occupations']}
            for key,revision,value,saved in rows if key in known}


class Jobs:
    def __init__(self, path=DATABASE_PATH):
        self.path = local_path(path)

    def initialize(self):
        migrate_catalog(self.path)
        sql = Path(__file__).with_name('003_jobs.sql').read_text(encoding='utf-8-sig')
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT sha256 FROM schema_migrations WHERE version=3').fetchone()
            if row:
                if row[0] != checksum:
                    raise ValueError('Migration checksum changed')
                return
            statement = ''
            for line in sql.splitlines(True):
                statement += line
                if sqlite3.complete_statement(statement):
                    con.execute(statement)
                    statement = ''
            con.execute('INSERT INTO schema_migrations VALUES(3,?,?)', (checksum, now()))
            con.commit()

    @contextmanager
    def transaction(self):
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise

    def list(self):
        with connect(self.path) as con:
            return [dict(r) for r in con.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100')]

    def get(self, identity):
        with connect(self.path) as con:
            row = con.execute('SELECT * FROM jobs WHERE job_id=?', (identity,)).fetchone()
            if not row:
                raise ValueError('Unknown job')
            return dict(row)

    def submit(self, kind='incremental', ids=None, batch_size=64, *, actor='local-owner',
               idempotency_key=None, spec=None, corrections=None, _connection=None):
        if kind not in ('full','incremental','sample') or type(batch_size) is not int or not 1<=batch_size<=512:
            raise ValueError('Invalid kind or batch size (1..512)')
        if ids is not None:
            if not ids or any(type(i) is not int or i<=0 for i in ids):
                raise ValueError('Expected positive document IDs')
            ids = sorted(set(ids))
        if kind=='sample' and (ids is None or len(ids)>100):
            raise ValueError('Sample requires 1..100 explicit IDs')
        if kind=='full' and ids is not None:
            raise ValueError('Full processing does not accept a subset')
        if spec is None:
            from market_system.model_management.store import active_spec
            spec = active_spec(self.path) or specification()
        if corrections is None:
            with connect(self.path) as check:
                unified = check.execute("SELECT 1 FROM sqlite_master WHERE name='review_revisions'").fetchone()
            corrections = {} if unified else confirmed_corrections(spec)
        parameters = {'batch_size':batch_size, 'requested_ids':ids, 'spec':spec}
        with (self.transaction() if _connection is None else nullcontext(_connection)) as con:
            if idempotency_key:
                old = con.execute('SELECT * FROM jobs WHERE idempotency_key=?',(idempotency_key,)).fetchone()
                if old:
                    if old['kind']!=kind or json.loads(old['parameters_json'])['request']!=parameters or old['actor']!=actor:
                        raise ValueError('Idempotency key used for a different request')
                    return old['job_id']
            manifest = encode(spec['model_manifest'])
            previous = con.execute('SELECT manifest_json FROM job_models WHERE model_version=?',(spec['model_version'],)).fetchone()
            if previous and previous[0]!=manifest:
                raise ValueError('Model version already has a different manifest')
            con.execute('INSERT OR IGNORE INTO job_models VALUES(?,?)',(spec['model_version'],manifest))
            base = con.execute('SELECT r.* FROM active_result_set a JOIN result_sets r USING(result_set_id)').fetchone()
            if kind=='incremental':
                if not base or base['model_version']!=spec['model_version']:
                    raise ValueError('Incremental processing requires an active set of the same model; use full')
                versions = json.loads(base['versions_json'])
                if (versions.get('pipeline_fingerprint') != spec['fingerprint'] and
                        not ('pipeline_fingerprint' not in versions and versions==spec.get('legacy_versions'))):
                    # Existing stage-2 sets declare extractor and dictionary versions explicitly.
                    raise ValueError('Active pipeline fingerprint differs; create a full set first')
            else:
                base = None
            base_id = base['result_set_id'] if base else None
            query = 'SELECT id,text,text_hash FROM contents'
            values = []
            if kind=='sample':
                query += ' WHERE id IN ('+','.join('?' for _ in ids)+')'
                values = ids
            rows = [dict(id=r['id'],text_sha256=text_digest(r['text']),text_hash=r['text_hash'])
                    for r in con.execute(query+' ORDER BY id',values)]
            known = {r['id'] for r in rows}
            if ids and not set(ids)<=known:
                raise ValueError('Unknown document IDs')
            selected = set(ids or [])
            existing = {r[0] for r in con.execute('SELECT content_id FROM document_processing WHERE result_set_id=?',(base_id,))}
            if kind=='incremental' and ids is None:
                selected = known-existing
            if kind=='full':
                selected = known
            if kind=='sample':
                rows = [r for r in rows if r['id'] in selected]
            if kind=='incremental':
                # A requested subset must not silently omit new documents from the output snapshot.
                rows = [r for r in rows if r['id'] in existing or r['id'] in selected]
                if {r['id'] for r in rows} != known:
                    raise ValueError('Incremental subset omits new documents; include all new IDs')
            if base:
                selected.update(r['id'] for r in rows if r['text_hash'] in corrections)
            identity = uuid.uuid4().hex
            result_id = 'job_'+identity
            con.execute('''INSERT INTO result_sets(result_set_id,model_version,skills_run_id,profiles_run_id,
              versions_json,provenance_json,coverage_json,state,created_at) VALUES(?,?,?,?,?,?,?,'building',?)''',
              (result_id,spec['model_version'],identity,identity,encode({**spec.get('legacy_versions',{}),'pipeline_fingerprint':spec['fingerprint']}),
               encode({'job_id':identity,'base':base_id,'correction_policy':'last confirmed at submission',
                       **({'class_space':spec['class_space'],'adapter':spec['adapter']} if 'class_space' in spec else {})}),
               encode({'expected':len(rows),'scope':kind}),now()))
            con.execute('''INSERT INTO jobs(job_id,kind,parameters_json,state,created_at,updated_at,total,
              result_set_id,actor,idempotency_key) VALUES(?,?,?,'queued',?,?,?,?,?,?)''',
              (identity,kind,encode({'request':parameters,'base':base_id}),now(),now(),len(selected),result_id,actor,idempotency_key))
            con.executemany('INSERT INTO job_inputs VALUES(?,?,?,?,?)',
              [(identity,r['id'],r['text_sha256'],int(r['id'] in selected),
                encode(corrections[r['text_hash']]) if r['text_hash'] in corrections else None) for r in rows])
            publication_table = 'current_publications' if con.execute("SELECT 1 FROM sqlite_master WHERE name='current_publications'").fetchone() else 'publications'
            con.execute(f'''INSERT INTO job_publications SELECT ?,p.content_id,p.id,NULL FROM {publication_table} p
              JOIN job_inputs i ON i.content_id=p.content_id AND i.job_id=?''',(identity,identity))
            for pub in con.execute(f'''SELECT p.id,p.published_at FROM {publication_table} p
              JOIN job_publications j ON j.publication_id=p.id WHERE j.job_id=?''',(identity,)).fetchall():
                con.execute('UPDATE job_publications SET published_at_utc=? WHERE job_id=? AND publication_id=?',
                            (utc(pub['published_at']),identity,pub['id']))
            if base:
                for table in ('document_processing','document_classes','search_catalog','result_evidence'):
                    columns = [r[1] for r in con.execute(f'PRAGMA table_info({table})')]
                    rest = ','.join('t.'+c for c in columns if c!='result_set_id')
                    con.execute(f'''INSERT INTO {table} SELECT ?,{rest} FROM {table} t
                      JOIN job_inputs i ON i.content_id=t.content_id AND i.job_id=? AND i.needs_analysis=0
                      WHERE t.result_set_id=?''',(result_id,identity,base_id))
                con.execute('''INSERT INTO result_publications SELECT ?,j.content_id,j.publication_id,j.published_at_utc
                  FROM job_publications j JOIN job_inputs i ON i.job_id=j.job_id AND i.content_id=j.content_id
                  WHERE j.job_id=? AND i.needs_analysis=0''',(result_id,identity))
                con.execute('''UPDATE search_catalog SET
                  publication_count=(SELECT count(*) FROM result_publications p WHERE p.result_set_id=search_catalog.result_set_id AND p.content_id=search_catalog.content_id),
                  published_at_utc=(SELECT max(published_at_utc) FROM result_publications p WHERE p.result_set_id=search_catalog.result_set_id AND p.content_id=search_catalog.content_id)
                  WHERE result_set_id=?''',(result_id,))
            return identity

    def cancel(self, identity):
        with self.transaction() as con:
            row = con.execute('SELECT state FROM jobs WHERE job_id=?',(identity,)).fetchone()
            if not row:
                raise ValueError('Unknown job')
            if row[0] in ('succeeded','cancelled'):
                return
            con.execute('''UPDATE jobs SET cancel_requested=1,updated_at=?,
              state=CASE WHEN state='running' THEN state ELSE 'cancelled' END,
              finished_at=CASE WHEN state='running' THEN finished_at ELSE ? END WHERE job_id=?''',(now(),now(),identity))

    def retry(self, identity):
        with self.transaction() as con:
            row = con.execute('SELECT state FROM jobs WHERE job_id=?',(identity,)).fetchone()
            if not row or row[0] not in ('failed','cancelled'):
                raise ValueError('Only failed or cancelled jobs can be retried')
            con.execute("UPDATE jobs SET state='queued',cancel_requested=0,error=NULL,owner=NULL,finished_at=NULL,updated_at=? WHERE job_id=?",(now(),identity))
