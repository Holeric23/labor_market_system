"""Collection and retention manager shared by the Mini App and parser worker."""
import uuid
from psycopg.types.json import Jsonb
from database.connection import connect
from database.repository import _owner


def _enqueue(con, source, mode, actor):
    job_id = uuid.uuid4()
    row = con.execute('''INSERT INTO collection_jobs(id,source_id,source_revision,mode,status,requested_by)
      VALUES (%s,%s,%s,%s,'queued',%s) ON CONFLICT DO NOTHING RETURNING id''',
      (job_id,source['id'],source['revision'],mode,actor)).fetchone()
    return row['id'] if row else None


def enqueue(actor, source_id, mode):
    if type(source_id) is not int or not 0 < source_id < 2**63 or mode not in ('new','history'):
        raise ValueError('Некорректное задание сбора.')
    with connect() as con:
        _owner(con,actor)
        source = con.execute('SELECT * FROM sources WHERE id=%s AND enabled AND deleted_at IS NULL FOR UPDATE', (source_id,)).fetchone()
        if not source:
            raise ValueError('Источник не найден или выключен.')
        if source['kind']=='telegram' and mode=='new' and 'high_water' not in source['cursor']:
            raise ValueError('Сначала запустите первичный сбор истории для этого Telegram-канала.')
        job = _enqueue(con,source,mode,actor)
        if not job:
            raise ValueError('Для этого источника сбор уже выполняется или ожидает запуска.')
        return job


def enqueue_all(actor, mode):
    if mode not in ('new','history'):
        raise ValueError('Некорректный режим сбора.')
    with connect() as con:
        _owner(con,actor)
        sources = con.execute('SELECT * FROM sources WHERE enabled AND deleted_at IS NULL ORDER BY id FOR UPDATE').fetchall()
        created = 0
        for source in sources:
            if source['kind']=='telegram' and mode=='new' and 'high_water' not in source['cursor']:
                continue
            if _enqueue(con,source,mode,actor):
                created += 1
        return created


def enqueue_due():
    with connect() as con:
        sources = con.execute('''SELECT * FROM sources WHERE enabled AND deleted_at IS NULL
          AND schedule_enabled AND next_run_at<=now()
          AND (kind<>'telegram' OR cursor ? 'high_water') ORDER BY next_run_at,id FOR UPDATE SKIP LOCKED''').fetchall()
        created = 0
        for source in sources:
            if _enqueue(con,source,'new',None):
                con.execute("UPDATE sources SET next_run_at=now()+(interval_seconds * interval '1 second') WHERE id=%s", (source['id'],))
                created += 1
        return created


def list_jobs(actor, limit=40):
    with connect() as con:
        _owner(con,actor)
        jobs = con.execute('''SELECT j.*,s.name,s.address,r.inserted_count FROM collection_jobs j
          JOIN sources s ON s.id=j.source_id LEFT JOIN parser_runs r ON r.id=j.parser_run_id
          ORDER BY j.requested_at DESC LIMIT %s''', (limit,)).fetchall()
        online = con.execute("SELECT last_seen_at > now()-interval '60 seconds' AS online FROM collection_worker_status WHERE singleton=true").fetchone()
        return jobs, bool(online and online['online'])


def cancel(actor, job_id):
    try:
        job_id = uuid.UUID(str(job_id))
    except (ValueError,TypeError):
        raise ValueError('Некорректное задание.') from None
    with connect() as con:
        _owner(con,actor)
        row = con.execute("""UPDATE collection_jobs SET cancel_requested=true,
          status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,
          finished_at=CASE WHEN status='queued' THEN now() ELSE finished_at END
          WHERE id=%s AND status IN ('queued','running') RETURNING id""", (job_id,)).fetchone()
        if not row:
            raise ValueError('Задание уже завершено или отменено.')


def retention_settings(actor):
    with connect() as con:
        _owner(con,actor)
        settings = con.execute('SELECT retention_enabled,retention_days,revision FROM app_settings WHERE singleton=true').fetchone()
        status = con.execute('SELECT last_finished_at,last_error FROM retention_status WHERE singleton=true').fetchone()
        return {**settings,**status}


def save_retention(actor, enabled, days, revision):
    if type(enabled) is not bool or type(days) is not int or not 1 <= days <= 3650 or type(revision) is not int:
        raise ValueError('Укажите срок от 1 до 3650 дней.')
    with connect() as con:
        _owner(con,actor)
        old = con.execute('SELECT retention_enabled,retention_days,revision FROM app_settings WHERE singleton=true FOR UPDATE').fetchone()
        if old['revision'] != revision:
            raise ValueError('Настройки изменились; обновите страницу.')
        con.execute('''UPDATE app_settings SET retention_enabled=%s,retention_days=%s,
          revision=revision+1,updated_at=now(),updated_by=%s WHERE singleton=true''', (enabled,days,actor))
        con.execute('INSERT INTO settings_history(actor_id,previous_values,new_values) VALUES (%s,%s,%s)',
          (actor,Jsonb(dict(old)),Jsonb({'retention_enabled':enabled,'retention_days':days})))


def cleanup():
    """Remove expired publications and orphan contents; keep saved/corrected content.

    Old result snapshots are pruned as part of retention.  An unconfirmed or
    missing publication date is never used as a reason to delete an entry.
    """
    with connect() as con:
        con.execute('SELECT pg_advisory_xact_lock(%s)', (174925603,))
        settings = con.execute('SELECT retention_enabled,retention_days FROM app_settings WHERE singleton=true').fetchone()
        if not settings['retention_enabled']:
            return None
        days = settings['retention_days']
        con.execute('''CREATE TEMP TABLE expired_publications(id bigint PRIMARY KEY) ON COMMIT DROP''')
        con.execute('''INSERT INTO expired_publications(id)
          SELECT p.id FROM publications p WHERE p.published_at IS NOT NULL
          AND p.published_at < now()-(%s * interval '1 day')
          AND NOT EXISTS (
            SELECT 1 FROM (
              SELECT p.current_content_id AS content_id
              UNION SELECT pr.content_id FROM publication_revisions pr WHERE pr.publication_id=p.id
              UNION SELECT rp.content_id FROM result_publications rp WHERE rp.publication_id=p.id
            ) versions WHERE EXISTS (SELECT 1 FROM client_vacancies cv WHERE cv.content_id=versions.content_id AND cv.saved)
               OR EXISTS (SELECT 1 FROM corrections c WHERE c.content_id=versions.content_id)
          )''', (days,))
        con.execute('CREATE TEMP TABLE affected_contents(id bigint PRIMARY KEY) ON COMMIT DROP')
        con.execute('''INSERT INTO affected_contents(id)
          SELECT current_content_id FROM publications WHERE id IN (SELECT id FROM expired_publications)
          UNION SELECT content_id FROM publication_revisions WHERE publication_id IN (SELECT id FROM expired_publications)
          UNION SELECT content_id FROM result_publications WHERE publication_id IN (SELECT id FROM expired_publications)''')
        con.execute("SELECT set_config('proshe.retention','on',true)")
        con.execute('DELETE FROM result_publications WHERE publication_id IN (SELECT id FROM expired_publications)')
        con.execute('DELETE FROM publication_revisions WHERE publication_id IN (SELECT id FROM expired_publications)')
        removed_publications = con.execute('DELETE FROM publications WHERE id IN (SELECT id FROM expired_publications)').rowcount
        con.execute('''DELETE FROM affected_contents a WHERE
          EXISTS (SELECT 1 FROM publications p WHERE p.current_content_id=a.id) OR
          EXISTS (SELECT 1 FROM publication_revisions r WHERE r.content_id=a.id) OR
          EXISTS (SELECT 1 FROM client_vacancies c WHERE c.content_id=a.id AND c.saved) OR
          EXISTS (SELECT 1 FROM corrections c WHERE c.content_id=a.id)''')
        con.execute('DELETE FROM selection_items WHERE content_id IN (SELECT id FROM affected_contents)')
        con.execute('DELETE FROM client_vacancies WHERE content_id IN (SELECT id FROM affected_contents) AND NOT saved')
        con.execute('DELETE FROM document_classes WHERE content_id IN (SELECT id FROM affected_contents)')
        con.execute('DELETE FROM result_publications WHERE content_id IN (SELECT id FROM affected_contents)')
        con.execute('DELETE FROM document_results WHERE content_id IN (SELECT id FROM affected_contents)')
        removed_contents = con.execute('DELETE FROM contents WHERE id IN (SELECT id FROM affected_contents)').rowcount
        con.execute("SELECT set_config('proshe.retention','off',true)")
        con.execute('INSERT INTO retention_runs(retention_days,removed_publications,removed_contents) VALUES (%s,%s,%s)',
                    (days,removed_publications,removed_contents))
        con.execute('UPDATE retention_status SET last_finished_at=now(),last_error=NULL WHERE singleton=true')
        return {'publications':removed_publications,'contents':removed_contents}


def retention_failed(error):
    with connect() as con:
        con.execute('UPDATE retention_status SET last_finished_at=now(),last_error=%s WHERE singleton=true', (error,))


def claim_job():
    """Claim the oldest queued job; an old source revision cannot start."""
    with connect() as con:
        job = con.execute("SELECT * FROM collection_jobs WHERE status='queued' ORDER BY requested_at,id FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
        if not job:
            return None
        source = con.execute('SELECT * FROM sources WHERE id=%s FOR UPDATE', (job['source_id'],)).fetchone()
        if not source or source['deleted_at'] is not None or not source['enabled'] or source['revision'] != job['source_revision']:
            con.execute("UPDATE collection_jobs SET status='failed',finished_at=now(),error='Источник изменён или выключен' WHERE id=%s", (job['id'],))
            return {'skipped':True}
        con.execute("UPDATE collection_jobs SET status='running',started_at=now() WHERE id=%s", (job['id'],))
        return {'id':job['id'],'mode':job['mode'],'source':source}


def finish_job(job_id, success, error=None):
    with connect() as con:
        con.execute('''UPDATE collection_jobs SET status=CASE WHEN cancel_requested THEN 'cancelled' ELSE %s END,
          finished_at=now(),error=CASE WHEN cancel_requested THEN NULL ELSE %s END
          WHERE id=%s AND status='running' ''', ('succeeded' if success else 'failed',error,job_id))


def is_cancelled(job_id):
    if job_id is None:
        return False
    with connect() as con:
        row = con.execute('SELECT cancel_requested FROM collection_jobs WHERE id=%s', (job_id,)).fetchone()
        return bool(row and row['cancel_requested'])


def heartbeat():
    with connect() as con:
        con.execute('''INSERT INTO collection_worker_status(singleton,last_seen_at) VALUES (true,now())
          ON CONFLICT(singleton) DO UPDATE SET last_seen_at=excluded.last_seen_at''')


def recover_interrupted():
    with connect() as con:
        con.execute("UPDATE parser_runs SET state='failed',finished_at=now(),error='Interrupted' WHERE state='running'")
        con.execute("""UPDATE collection_jobs j SET
          status=CASE WHEN j.cancel_requested THEN 'cancelled'
                      WHEN (SELECT state FROM parser_runs WHERE id=j.parser_run_id)='succeeded' THEN 'succeeded'
                      ELSE 'failed' END,
          finished_at=now(),
          error=CASE WHEN j.cancel_requested OR (SELECT state FROM parser_runs WHERE id=j.parser_run_id)='succeeded'
                     THEN NULL ELSE 'Исполнитель был остановлен' END
          WHERE j.status='running'""")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Manage scheduled collection jobs')
    parser.add_argument('--enqueue-due',action='store_true',required=True)
    parser.parse_args()
    print('Запланировано заданий:',enqueue_due())
