"""Atomic Content/Publication persistence and continuation of collection jobs."""
import asyncio
from dataclasses import dataclass
import hashlib
import json
import re

from market_system.search.store import connect, encode, now
from .store import validate_url


@dataclass
class Batch:
    publications: list
    cursor: dict
    done: bool = False


class Collector:
    def __init__(self, sources, owner, adapter=None, before_commit=None, after_commit=None, should_stop=None):
        self.sources = sources
        self.owner = owner
        self.adapter = adapter
        self.before_commit = before_commit
        self.after_commit = after_commit
        self.should_stop = should_stop or (lambda:False)

    def recover(self):
        with self.sources.jobs.transaction() as con:
            for job in con.execute("SELECT * FROM collection_jobs WHERE state='running'").fetchall():
                con.execute("UPDATE collection_attempts SET finished_at=?,outcome='interrupted' WHERE job_id=? AND attempt=?",
                            (now(),job['job_id'],job['attempts']))
                con.execute("UPDATE collection_jobs SET state='queued',owner=NULL,updated_at=? WHERE job_id=?",(now(),job['job_id']))
                con.execute("UPDATE managed_sources SET state='queued' WHERE source_id=?",(job['source_id'],))

    def claim(self):
        with self.sources.jobs.transaction() as con:
            if con.execute("SELECT 1 FROM collection_jobs WHERE state='running'").fetchone():
                return None
            job = con.execute("SELECT * FROM collection_jobs WHERE state='queued' ORDER BY created_at,job_id LIMIT 1").fetchone()
            if not job:
                return None
            con.execute("UPDATE collection_jobs SET state='running',owner=?,attempts=attempts+1,updated_at=? WHERE job_id=?",
                        (self.owner,now(),job['job_id']))
            con.execute('INSERT INTO collection_attempts(job_id,attempt,owner,started_at) VALUES(?,?,?,?)',
                        (job['job_id'],job['attempts']+1,self.owner,now()))
            con.execute("UPDATE managed_sources SET state='running',error=NULL WHERE source_id=?",(job['source_id'],))
            return job['job_id']

    def owned(self,con,identity):
        job = con.execute('SELECT * FROM collection_jobs WHERE job_id=?',(identity,)).fetchone()
        if not job or job['owner']!=self.owner or job['state']!='running':
            raise RuntimeError('Collection job ownership lost')
        return job

    def finish(self,con,job,state,error=None):
        con.execute('UPDATE collection_jobs SET state=?,owner=NULL,error=?,finished_at=?,updated_at=? WHERE job_id=?',
                    (state,error,now(),now(),job['job_id']))
        con.execute('UPDATE collection_attempts SET outcome=?,error=?,finished_at=? WHERE job_id=? AND attempt=?',
                    (state,error,now(),job['job_id'],job['attempts']))
        con.execute('''UPDATE managed_sources SET state=?,error=?,
          last_success_at=CASE WHEN ?='succeeded' THEN ? ELSE last_success_at END WHERE source_id=?''',
          (state,error,state,now(),job['source_id']))

    def persist(self,con,job,publication):
        config = json.loads(job['config_json'])
        if publication.source_type_name!=config['kind']:
            raise ValueError('Publication source type mismatch')
        url = validate_url(config['kind'],publication.url,True)
        if config['kind']=='telegram' and url.rsplit('/',1)[0]!=config['address']:
            raise ValueError('Publication belongs to a different Telegram channel')
        if config['kind']=='designhunters' and url not in config['urls']:
            raise ValueError('Publication is outside the frozen URL list')
        # Reuse legacy normalized hash only when the stored text is exactly equal.
        # A whitespace edit must not destroy a previous textual revision.
        text = publication.text
        normalized = re.sub(r'\s+',' ',text.strip())
        legacy_hash = hashlib.sha256(normalized.encode()).hexdigest()
        row = con.execute('SELECT id,text FROM contents WHERE text_hash=?',(legacy_hash,)).fetchone()
        key = legacy_hash if row is None or row['text']==text else 'exact:'+hashlib.sha256(text.encode()).hexdigest()
        existing = con.execute('SELECT id,text FROM contents WHERE text_hash=?',(key,)).fetchone()
        if existing and existing['text']!=text:
            raise ValueError('Content hash collision')
        con.execute('INSERT OR IGNORE INTO contents(text,text_hash) VALUES(?,?)',(text,key))
        cid = con.execute('SELECT id FROM contents WHERE text_hash=?',(key,)).fetchone()[0]
        pub = con.execute('SELECT * FROM publications WHERE url=?',(url,)).fetchone()
        date = publication.published_at.isoformat() if publication.published_at else None
        external_id = url.rsplit('/',1)[-1] if config['kind']=='telegram' else url
        if pub and pub['source_id']!=job['source_id']:
            raise ValueError('URL is already owned by another source')
        if not pub:
            pid = con.execute('INSERT INTO publications(content_id,source_id,url,published_at) VALUES(?,?,?,?)',
                              (cid,job['source_id'],url,date)).lastrowid
            con.execute('INSERT INTO publication_revisions VALUES(?,1,?,?,?,?,?)',
                        (pid,cid,date,now(),external_id,job['job_id']))
            return cid
        revision = con.execute('SELECT * FROM publication_revisions WHERE publication_id=? ORDER BY revision DESC LIMIT 1',(pub['id'],)).fetchone()
        if not revision:
            # The original observation date is unknown; do not invent it.
            con.execute('INSERT INTO publication_revisions VALUES(?,1,?,?,NULL,?,NULL)',
                        (pub['id'],pub['content_id'],pub['published_at'],external_id))
            revision = {'revision':1,'content_id':pub['content_id'],'published_at':pub['published_at']}
        if revision['content_id']==cid and revision['published_at']==date:
            return None
        con.execute('INSERT INTO publication_revisions VALUES(?,?,?,?,?,?,?)',
                    (pub['id'],revision['revision']+1,cid,date,now(),external_id,job['job_id']))
        return cid

    def save_batch(self,identity,batch):
        with self.sources.jobs.transaction() as con:
            job = self.owned(con,identity)
            source = con.execute('SELECT * FROM managed_sources WHERE source_id=?',(job['source_id'],)).fetchone()
            if not source['enabled']:
                self.finish(con,job,'cancelled')
                return False
            if source['cursor_version']!=job['source_cursor_version']:
                raise RuntimeError('Source cursor changed outside this job')
            changed = set()
            for publication in batch.publications:
                cid = self.persist(con,job,publication)
                if cid is not None:
                    changed.add(cid)
            number = job['batches']+1
            if changed:
                con.execute('INSERT INTO collection_handoffs(collection_job_id,batch,content_ids_json) VALUES(?,?,?)',
                            (identity,number,encode(sorted(changed))))
            con.execute('UPDATE collection_jobs SET cursor_json=?,source_cursor_version=source_cursor_version+1,batches=?,progress=progress+?,updated_at=? WHERE job_id=?',
                        (encode(batch.cursor),number,len(batch.publications),now(),identity))
            con.execute('UPDATE managed_sources SET cursor_json=?,cursor_version=cursor_version+1 WHERE source_id=?',
                        (encode(batch.cursor['source']),job['source_id']))
            if batch.done:
                self.finish(con,job,'succeeded')
            if self.before_commit:
                self.before_commit(identity)
        if self.after_commit:
            self.after_commit(identity)
        return True

    async def _execute(self,identity):
        job = self.sources.job(identity)
        if not self.sources.get(job['source_id'])['enabled']:
            with self.sources.jobs.transaction() as con:
                self.finish(con,self.owned(con,identity),'cancelled')
            return
        config,cursor = json.loads(job['config_json']),json.loads(job['cursor_json'])
        if self.adapter is None:
            from .adapters import batches
            iterator = batches(config,job['mode'],cursor)
        else:
            iterator = self.adapter(config,job['mode'],cursor)
        try:
            async for batch in iterator:
                if self.should_stop():
                    return
                if not self.save_batch(identity,batch) or batch.done:
                    return
            raise ValueError('Source stopped without a completion checkpoint')
        finally:
            await iterator.aclose()

    def execute(self,identity):
        try:
            asyncio.run(self._execute(identity))
        except Exception as exc:
            # Network exceptions can contain credentials. Persist class and safe message only.
            message = str(exc) if isinstance(exc,(ValueError,RuntimeError,FileNotFoundError)) else 'Collection failed; retry after checking the source connection'
            error = f'{type(exc).__name__}: {message}'[:2000]
            with self.sources.jobs.transaction() as con:
                job = con.execute('SELECT * FROM collection_jobs WHERE job_id=?',(identity,)).fetchone()
                if job and job['state'] in ('succeeded','cancelled'):
                    # Closing a remote connection can fail after the final batch committed.
                    # Keep committed completion, expose the cleanup error, and serve other sources.
                    con.execute('UPDATE collection_jobs SET error=? WHERE job_id=?',('Cleanup: '+error,identity))
                    con.execute('UPDATE collection_attempts SET error=? WHERE job_id=? AND attempt=?',
                                ('Cleanup: '+error,identity,job['attempts']))
                    con.execute('UPDATE managed_sources SET error=? WHERE source_id=?',('Cleanup: '+error,job['source_id']))
                else:
                    self.finish(con,self.owned(con,identity),'failed',error)
