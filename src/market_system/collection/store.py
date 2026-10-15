"""Validated source settings, durable collection queue and atomic batch persistence."""
import csv
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from market_system.jobs.store import Jobs, specification, confirmed_corrections
from market_system.paths import DATABASE_PATH, local_path
from market_system.search.store import connect, encode, now


def later(seconds, stamp=None):
    return (datetime.fromisoformat(stamp or now())+timedelta(seconds=seconds)).isoformat()


def validate_url(kind, address, publication=False):
    parsed = urlsplit(address)
    host = 't.me' if kind=='telegram' else 'designhunters.ru'
    if (parsed.scheme!='https' or parsed.netloc!=host or parsed.query or parsed.fragment or
            any(c.isspace() for c in address)):
        raise ValueError('Only HTTPS URLs on '+host+' are supported')
    if kind=='telegram':
        pattern = r'/[A-Za-z][A-Za-z0-9_]{3,31}/[1-9][0-9]*' if publication else r'/[A-Za-z][A-Za-z0-9_]{3,31}'
        if not re.fullmatch(pattern,parsed.path.rstrip('/')):
            raise ValueError('Use a public Telegram channel URL')
    return address.rstrip('/')


def parameters(kind, value):
    if not isinstance(value,dict):
        raise ValueError('Parameters must be a JSON object')
    defaults = {'batch_size':50, 'max_items':1000}
    if kind=='telegram':
        defaults.update(overlap_days=3, overlap_limit=500)
    else:
        defaults.update(csv_path='', delay_seconds=2, timeout_ms=30000)
    if set(value)-defaults.keys():
        raise ValueError('Unknown source parameter; secrets belong in separate setup')
    result = defaults | value
    limits = {'batch_size':(1,200),'max_items':(1,100000),'overlap_days':(1,30),
              'overlap_limit':(1,5000),'delay_seconds':(0,60),'timeout_ms':(1000,120000)}
    for key,(low,high) in limits.items():
        if key in result and (type(result[key]) is not int or not low<=result[key]<=high):
            raise ValueError(f'{key} must be an integer in {low}..{high}')
    if kind=='designhunters':
        if not isinstance(result['csv_path'],str) or not result['csv_path']:
            raise ValueError('csv_path must be a non-empty project-local path')
        path = local_path(result['csv_path'])
        if path.suffix.lower()!='.csv' or not path.is_file():
            raise ValueError('Select an existing project-local CSV of DesignHunters URLs')
        result['csv_path'] = str(path)
    return result


class Sources:
    def __init__(self,path=DATABASE_PATH):
        self.jobs = Jobs(path)
        self.path = self.jobs.path

    def initialize(self):
        self.jobs.initialize()
        sql = Path(__file__).with_name('004_collection.sql').read_text(encoding='utf-8-sig')
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        with self.jobs.transaction() as con:
            row = con.execute('SELECT sha256 FROM schema_migrations WHERE version=4').fetchone()
            if row:
                if row[0]!=checksum:
                    raise ValueError('Migration checksum changed')
                return
            statement = ''
            for line in sql.splitlines(True):
                statement += line
                if sqlite3.complete_statement(statement):
                    con.execute(statement)
                    statement = ''
            con.execute('INSERT INTO schema_migrations VALUES(4,?,?)',(checksum,now()))

    def list(self):
        with connect(self.path) as con:
            return [dict(r) for r in con.execute('SELECT m.*,s.name FROM managed_sources m JOIN sources s ON s.id=m.source_id ORDER BY source_id')]

    def get(self, source_id):
        with connect(self.path) as con:
            row = con.execute('SELECT m.*,s.name FROM managed_sources m JOIN sources s ON s.id=m.source_id WHERE source_id=?',(source_id,)).fetchone()
            if not row:
                raise ValueError('Unknown managed source')
            return dict(row)

    def save(self, kind, name, address, settings, enabled=True, schedule_enabled=False,
             interval_seconds=3600, source_id=None, revision=None):
        if kind not in ('telegram','designhunters') or not isinstance(name,str) or not name.strip() or len(name)>128:
            raise ValueError('Invalid source type or name')
        if type(interval_seconds) is not int or not 60<=interval_seconds<=31536000:
            raise ValueError('Interval must be 60..31536000 seconds')
        address = validate_url(kind,address)
        settings = parameters(kind,settings)
        with self.jobs.transaction() as con:
            if source_id is None:
                con.execute('INSERT OR IGNORE INTO source_types(name) VALUES(?)',(kind,))
                type_id = con.execute('SELECT id FROM source_types WHERE name=?',(kind,)).fetchone()[0]
                con.execute('INSERT OR IGNORE INTO sources(source_type_id,name) VALUES(?,?)',(type_id,name.strip()))
                source_id = con.execute('SELECT id FROM sources WHERE source_type_id=? AND name=?',(type_id,name.strip())).fetchone()[0]
                if con.execute('SELECT 1 FROM managed_sources WHERE source_id=? OR (kind=? AND address=?)',(source_id,kind,address)).fetchone():
                    raise ValueError('Source already configured; edit its settings')
                con.execute('''INSERT INTO managed_sources(source_id,kind,address,enabled,parameters_json,schedule_enabled,interval_seconds,next_run_at)
                  VALUES(?,?,?,?,?,?,?,?)''',(source_id,kind,address,int(enabled),encode(settings),int(schedule_enabled),interval_seconds,
                                            now() if schedule_enabled and enabled else None))
            else:
                row = con.execute('SELECT * FROM managed_sources WHERE source_id=?',(source_id,)).fetchone()
                if not row or row['revision']!=revision:
                    raise ValueError('Source settings changed; reload the page')
                current_name = con.execute('SELECT name FROM sources WHERE id=?',(source_id,)).fetchone()[0]
                if current_name!=name.strip():
                    raise ValueError('Source name is immutable; create a new source')
                if row['kind']!=kind or row['address']!=address:
                    raise ValueError('Changing source identity requires a new source')
                if con.execute("SELECT 1 FROM collection_jobs WHERE source_id=? AND state IN ('queued','running')",(source_id,)).fetchone():
                    # Disabling is allowed; worker checks this between batches.
                    if enabled or encode(settings)!=row['parameters_json']:
                        raise ValueError('Wait for the active collection job before changing parameters')
                con.execute('''UPDATE managed_sources SET enabled=?,parameters_json=?,revision=revision+1,
                  schedule_enabled=?,interval_seconds=?,next_run_at=? WHERE source_id=?''',
                  (int(enabled),encode(settings),int(schedule_enabled),interval_seconds,now() if schedule_enabled and enabled else None,source_id))
            return source_id

    def _enqueue(self, con, source_id, mode, actor):
        if mode not in ('history','new'):
            raise ValueError('Mode must be history or new')
        row = con.execute('SELECT m.*,s.name FROM managed_sources m JOIN sources s ON s.id=m.source_id WHERE source_id=?',(source_id,)).fetchone()
        if not row or not row['enabled']:
            raise ValueError('Source is missing or disabled')
        active = con.execute("SELECT job_id FROM collection_jobs WHERE source_id=? AND state IN ('queued','running')",(source_id,)).fetchone()
        if active:
            return active[0]
        source_cursor = json.loads(row['cursor_json'])
        config = dict(row)
        config['parameters'] = json.loads(row['parameters_json'])
        if row['kind']=='designhunters':
            with local_path(config['parameters']['csv_path']).open(encoding='utf-8-sig',newline='') as file:
                urls = list(dict.fromkeys(validate_url('designhunters',r[0].strip(),True) for r in csv.reader(file) if r and r[0].strip()))
            if not urls:
                raise ValueError('CSV contains no URLs')
            config['urls'] = urls
        cursor = {'source':source_cursor,'seen':0}
        if row['kind']=='telegram':
            cursor.update(phase='history' if mode=='history' else 'overlap',
                          cutoff=(datetime.now(timezone.utc)-timedelta(days=config['parameters']['overlap_days'])).isoformat())
        else:
            cursor['index'] = source_cursor.get('history_index',0) if mode=='history' else source_cursor.get('regular_index',0)
            # A changed URL list starts a new history pass rather than skipping new entries.
            if source_cursor.get('urls_hash')!=hashlib.sha256(encode(config['urls']).encode()).hexdigest():
                cursor['index']=0
        identity = uuid.uuid4().hex
        con.execute('''INSERT INTO collection_jobs(job_id,source_id,mode,config_json,cursor_json,source_cursor_version,
          state,created_at,updated_at,actor) VALUES(?,?,?,?,?,?,'queued',?,?,?)''',
          (identity,source_id,mode,encode(config),encode(cursor),row['cursor_version'],now(),now(),actor))
        con.execute("UPDATE managed_sources SET state='queued',error=NULL WHERE source_id=?",(source_id,))
        return identity

    def enqueue(self, source_id, mode='new', actor='local-owner'):
        with self.jobs.transaction() as con:
            return self._enqueue(con,source_id,mode,actor)

    def schedule(self, stamp=None):
        stamp = stamp or now()
        created = []
        with self.jobs.transaction() as con:
            for source in con.execute('''SELECT * FROM managed_sources WHERE enabled=1 AND schedule_enabled=1
              AND (next_run_at IS NULL OR next_run_at<=?) ORDER BY source_id''',(stamp,)).fetchall():
                try:
                    identity = self._enqueue(con,source['source_id'],'new','schedule')
                    created.append(identity)
                except (ValueError,OSError) as exc:
                    con.execute("UPDATE managed_sources SET state='failed',error=? WHERE source_id=?",(str(exc)[:1000],source['source_id']))
                con.execute('UPDATE managed_sources SET next_run_at=? WHERE source_id=?',(later(source['interval_seconds'],stamp),source['source_id']))
        return created

    def job(self, identity):
        with connect(self.path) as con:
            row = con.execute('SELECT * FROM collection_jobs WHERE job_id=?',(identity,)).fetchone()
            if not row:
                raise ValueError('Unknown collection job')
            return dict(row)

    def history(self):
        with connect(self.path) as con:
            return [dict(r) for r in con.execute('SELECT * FROM collection_jobs ORDER BY created_at DESC LIMIT 100')]

    def handoffs(self):
        with connect(self.path) as con:
            return [dict(r) for r in con.execute('SELECT * FROM collection_handoffs ORDER BY rowid DESC LIMIT 100')]

    def retry(self, identity):
        with self.jobs.transaction() as con:
            row = con.execute('SELECT * FROM collection_jobs WHERE job_id=?',(identity,)).fetchone()
            if not row or row['state'] not in ('failed','cancelled'):
                raise ValueError('Only failed or cancelled collection jobs can be retried')
            source = con.execute('SELECT * FROM managed_sources WHERE source_id=?',(row['source_id'],)).fetchone()
            if not source['enabled'] or source['cursor_version']!=row['source_cursor_version'] or source['revision']!=json.loads(row['config_json'])['revision']:
                raise ValueError('Source changed since this job; create a new collection job')
            if con.execute("SELECT 1 FROM collection_jobs WHERE source_id=? AND state IN ('queued','running')",(row['source_id'],)).fetchone():
                raise ValueError('Collection is already queued or running')
            con.execute("UPDATE collection_jobs SET state='queued',owner=NULL,error=NULL,finished_at=NULL,updated_at=? WHERE job_id=?",(now(),identity))
            con.execute("UPDATE managed_sources SET state='queued',error=NULL WHERE source_id=?",(row['source_id'],))

    def dispatch(self, spec=None, corrections=None, force=False):
        """Create processing jobs and link durable handoffs in one SQLite transaction."""
        with connect(self.path) as con:
            rows = con.execute('''SELECT * FROM collection_handoffs WHERE processing_job_id IS NULL
              AND (? OR next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY rowid''',(int(force),now())).fetchall()
        if not rows:
            return None
        try:
            if spec is None:
                from market_system.model_management.store import active_spec
                spec = active_spec(self.path) or specification()
            if corrections is None:
                with connect(self.path) as check:
                    unified = check.execute("SELECT 1 FROM sqlite_master WHERE name='review_revisions'").fetchone()
                corrections = {} if unified else confirmed_corrections(spec)
            with self.jobs.transaction() as con:
                # The worker is single; the transaction also fences a concurrent explicit retry.
                pending = [r for r in rows if con.execute('SELECT processing_job_id FROM collection_handoffs WHERE collection_job_id=? AND batch=?',
                           (r['collection_job_id'],r['batch'])).fetchone()[0] is None]
                if not pending:
                    return None
                identity = self.jobs.submit('incremental',actor='collection',spec=spec,corrections=corrections,_connection=con)
                con.executemany('UPDATE collection_handoffs SET processing_job_id=?,error=NULL,next_attempt_at=NULL WHERE collection_job_id=? AND batch=?',
                                [(identity,r['collection_job_id'],r['batch']) for r in pending])
                return identity
        except Exception as exc:
            with self.jobs.transaction() as con:
                con.executemany('UPDATE collection_handoffs SET error=?,next_attempt_at=? WHERE collection_job_id=? AND batch=? AND processing_job_id IS NULL',
                                [(f'{type(exc).__name__}: {exc}'[:2000],later(60),r['collection_job_id'],r['batch']) for r in rows])
            return None
