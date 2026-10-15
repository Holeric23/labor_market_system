"""One local worker, protected by an OS lock released on process death."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import time
import uuid
import logging

from market_system.paths import local_path
from market_system.search.store import connect, now, validate
from .store import Jobs, text_digest
from .projection import project


@contextmanager
def worker_lock(path):
    # Kernel lock, not PID/TTL guessing. A paused worker retains ownership.
    lock_path = local_path(str(path)+'.worker.lock')
    with lock_path.open('a+b') as handle:
        handle.seek(0,2)
        if handle.tell()==0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another worker owns this database') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name=='nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Worker:
    def __init__(self, jobs, analyzer_factory=None, after_batch=None, collection_adapter=None, telegram_sender=None, should_stop=None):
        self.jobs = jobs
        self.owner = f'{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}'
        self.analyzer_factory = analyzer_factory
        self.after_batch = after_batch
        self.analyzers = {}
        self.collection_adapter = collection_adapter
        self.telegram_sender = telegram_sender
        self.should_stop = should_stop or (lambda:False)

    def analyzer(self, spec):
        key = spec['fingerprint']
        if key not in self.analyzers:
            if self.analyzer_factory:
                self.analyzers[key] = self.analyzer_factory(spec)
            else:
                from market_system.model_management.adapters import load
                from market_system.operations.compatibility import resolved_spec
                self.analyzers[key] = load(resolved_spec(spec)).predict
        return self.analyzers[key]

    def recover(self):
        # Only called while holding the OS lock. No previous executor can still commit.
        with self.jobs.transaction() as con:
            for row in con.execute("SELECT * FROM jobs WHERE state='running'").fetchall():
                con.execute("UPDATE job_attempts SET finished_at=?,outcome='interrupted',error='Worker process stopped' WHERE job_id=? AND attempt=?",
                            (now(),row['job_id'],row['attempts']))
                con.execute("UPDATE jobs SET state=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'queued' END,owner=NULL,updated_at=?,finished_at=CASE WHEN cancel_requested=1 THEN ? ELSE NULL END WHERE job_id=?",
                            (now(),now(),row['job_id']))

    def claim(self):
        with self.jobs.transaction() as con:
            if con.execute("SELECT 1 FROM jobs WHERE state='running'").fetchone():
                return None
            row = con.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created_at,job_id LIMIT 1").fetchone()
            if not row:
                return None
            con.execute("UPDATE jobs SET state='running',owner=?,attempts=attempts+1,started_at=?,updated_at=? WHERE job_id=? AND state='queued'",
                        (self.owner,now(),now(),row['job_id']))
            con.execute('INSERT INTO job_attempts(job_id,attempt,owner,started_at) VALUES(?,?,?,?)',
                        (row['job_id'],row['attempts']+1,self.owner,now()))
            return row['job_id']

    def owned(self, con, identity):
        row = con.execute('SELECT j.*,r.model_version FROM jobs j JOIN result_sets r USING(result_set_id) WHERE job_id=?',(identity,)).fetchone()
        if not row or row['state']!='running' or row['owner']!=self.owner:
            raise RuntimeError('Job ownership lost')
        return row

    def finish(self, con, job, state, error=None):
        logging.info('processing_finished job=%s state=%s',job['job_id'],state)
        con.execute('UPDATE jobs SET state=?,error=?,updated_at=?,finished_at=?,owner=NULL WHERE job_id=?',
                    (state,error,now(),now(),job['job_id']))
        con.execute('UPDATE job_attempts SET outcome=?,error=?,finished_at=? WHERE job_id=? AND attempt=?',
                    (state,error,now(),job['job_id'],job['attempts']))

    def execute(self, identity):
        try:
            while True:
                if self.should_stop():
                    logging.info('processing_paused job=%s',identity)
                    return  # running checkpoint is recovered on next launch, not marked cancelled
                with self.jobs.transaction() as con:
                    job = self.owned(con,identity)
                    if job['cancel_requested']:
                        self.finish(con,job,'cancelled')
                        return
                    parameters = json.loads(job['parameters_json'])['request']
                    items = con.execute('''SELECT i.*,c.text FROM job_inputs i JOIN contents c ON c.id=i.content_id
                      WHERE job_id=? AND needs_analysis=1 AND content_id>? ORDER BY content_id LIMIT ?''',
                      (identity,job['checkpoint'],parameters['batch_size'])).fetchall()
                    if not items:
                        # Verify exact IDs, not merely counts; verify content is still the frozen input.
                        for row in con.execute('SELECT i.text_sha256,c.text FROM job_inputs i JOIN contents c ON c.id=i.content_id WHERE job_id=?',(identity,)):
                            if text_digest(row['text'])!=row['text_sha256']:
                                raise ValueError('Frozen input text changed')
                        from market_system.review.projection import apply_current
                        apply_current(con,job['result_set_id'])
                        validate(con,job['result_set_id'])
                        if job['progress']!=job['total']:
                            raise ValueError('Incomplete processing progress')
                        con.execute("UPDATE result_sets SET state='ready',ready_at=? WHERE result_set_id=?",(now(),job['result_set_id']))
                        from market_system.telegram_bot.outbox import produce
                        produce(con,job['result_set_id'])
                        self.finish(con,job,'succeeded')
                        return
                for item in items:
                    if text_digest(item['text'])!=item['text_sha256']:
                        raise ValueError('Frozen input text changed')
                results = self.analyzer(parameters['spec'])([item['text'] for item in items])
                if len(results)!=len(items):
                    raise ValueError('Analyzer returned partial batch')
                with self.jobs.transaction() as con:
                    job = self.owned(con,identity)
                    if job['cancel_requested']:
                        self.finish(con,job,'cancelled')
                        return
                    for item, result in zip(items,results):
                        current = con.execute('SELECT text FROM contents WHERE id=?',(item['content_id'],)).fetchone()
                        if not current or text_digest(current[0])!=item['text_sha256']:
                            raise ValueError('Frozen input text changed during analysis')
                        required = {'model_version','label','skills','skill_names','required_skills',
                                    'preferred_skills','possessed_skills','occupation_ids','occupation_names',
                                    'skill_mentions','skill_lexicon_sha256','skill_lexicon_version',
                                    'occupation_lexicon_sha256','occupation_lexicon_version'}
                        if not isinstance(result,dict) or not required <= result.keys():
                            raise ValueError('Analyzer returned incomplete result')
                        if result.get('model_version')!=job['model_version']:
                            raise ValueError('Analyzer model version mismatch')
                        project(con,job,item,result)
                    con.execute('UPDATE jobs SET checkpoint=?,progress=progress+?,updated_at=? WHERE job_id=?',
                                (items[-1]['content_id'],len(items),now(),identity))
                if self.after_batch:
                    self.after_batch(identity)
        except Exception as exc:
            with self.jobs.transaction() as con:
                job = self.owned(con,identity)
                self.finish(con,job,'failed',f'{type(exc).__name__}: {exc}'[:4000])

    def run(self, once=False, poll_seconds=2):
        with worker_lock(self.jobs.path):
            from market_system.collection.store import Sources
            from market_system.collection.runner import Collector
            sources = Sources(self.jobs.path)
            from market_system.telegram_bot.store import BotStore
            from market_system.telegram_bot.delivery import DeliveryWorker
            bot_store=BotStore(self.jobs.path)
            bot_store.initialize()
            delivery=DeliveryWorker(bot_store,self.owner,self.telegram_sender) if self.telegram_sender else None
            collector = Collector(sources,self.owner,adapter=self.collection_adapter,should_stop=self.should_stop)
            self.recover()
            collector.recover()
            if delivery:
                delivery.recover()
            while True:
                if self.should_stop():
                    return
                sources.schedule()
                sources.dispatch()
                with connect(self.jobs.path) as con:
                    next_job = con.execute("""SELECT 'processing' kind,created_at FROM jobs WHERE state='queued'
                      UNION ALL SELECT 'collection',created_at FROM collection_jobs WHERE state='queued'
                      ORDER BY created_at LIMIT 1""").fetchone()
                if next_job and next_job['kind']=='collection':
                    identity = collector.claim()
                    if identity:
                        logging.info('collection_claimed job=%s',identity)
                        collector.execute(identity)
                        sources.dispatch()
                else:
                    identity = self.claim()
                    if identity:
                        logging.info('processing_claimed job=%s',identity)
                        self.execute(identity)
                delivered=delivery.run_one() if delivery and not self.should_stop() else False
                if once:
                    return identity
                if not identity and not delivered:
                    until = time.monotonic()+poll_seconds
                    while time.monotonic()<until and not self.should_stop():
                        time.sleep(min(0.1,poll_seconds))
