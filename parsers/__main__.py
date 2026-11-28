"""Execute collection jobs in the parser container, without sending bot messages."""
import argparse
import asyncio
import logging
import sys
import threading
import time
import uuid
from psycopg.types.json import Jsonb
from database.connection import connect
from database.migrate import migrate
from database import collection
from parsers.adapters import batches
from parsers.storage import store_batch, stage_batch, staged_batch, discard_staged


async def run_source(source, mode, inference, job_id=None):
    run_id = uuid.uuid4()
    with connect() as con:
        current = con.execute('SELECT enabled,deleted_at,revision FROM sources WHERE id=%s FOR UPDATE', (source['id'],)).fetchone()
        if not current or not current['enabled'] or current['deleted_at'] is not None or current['revision'] != source['revision']:
            logging.warning('Источник изменён или удалён до начала сбора')
            return False
        freshness = con.execute("""SELECT retention_days,
          now()-(retention_days * interval '1 day') AS cutoff FROM app_settings WHERE singleton=true""").fetchone()
        source = dict(source)
        source['freshness_cutoff'] = freshness['cutoff'].isoformat()
        configuration = {key:source[key] for key in ('kind','name','address','parameters','revision')}
        configuration.update(mode=mode,freshness_days=freshness['retention_days'],freshness_cutoff=source['freshness_cutoff'])
        con.execute("INSERT INTO parser_runs(id,source_id,state,configuration) VALUES (%s,%s,'running',%s)", (run_id,source['id'],Jsonb(configuration)))
        if job_id is not None:
            con.execute("UPDATE collection_jobs SET parser_run_id=%s WHERE id=%s AND status='running'", (run_id,job_id))
    total = 0
    cursor = {'source':dict(source['cursor'])}
    try:
        if collection.is_cancelled(job_id):
            raise ValueError('Сбор отменён')
        async for batch in batches(source,mode):
            if collection.is_cancelled(job_id):
                raise ValueError('Сбор отменён')
            stage_batch(source,run_id,batch)
            cursor = batch.cursor
        if collection.is_cancelled(job_id):
            raise ValueError('Сбор отменён')
        total = store_batch(source,run_id,staged_batch(run_id,cursor),inference,job_id)
        if job_id is None:
            with connect() as con:
                con.execute("UPDATE parser_runs SET state='succeeded',finished_at=now(),inserted_count=%s WHERE id=%s", (total,run_id))
                con.execute("UPDATE sources SET last_success_at=now(),next_run_at=now()+(interval_seconds * interval '1 second') WHERE id=%s AND revision=%s", (source['id'],source['revision']))
        try:
            collection.cleanup()
        except Exception as exc:
            logging.exception('Automatic retention failed')
            try:
                collection.retention_failed(type(exc).__name__)
            except Exception:
                logging.exception('Could not record retention failure')
        return True
    except Exception as exc:
        safe_errors = {'Не настроены TELEGRAM_API_ID и TELEGRAM_API_HASH',
                       'Не создана отдельная Telegram-сессия парсера',
                       'Telegram-сессия парсера не авторизована',
                       'Run history collection once before regular Telegram collection',
                       'Настройки источника изменились во время запуска', 'Сбор отменён'}
        explanations = {
            'AuthKeyUnregisteredError':'Сессия Telegram больше не действует. Подключите аккаунт заново.',
            'SessionRevokedError':'Сессия Telegram отозвана. Подключите аккаунт заново.',
            'SessionPasswordNeededError':'Для аккаунта требуется дополнительный пароль. Повторите вход в панели.',
            'ChannelPrivateError':'Нет доступа к этому Telegram-каналу с подключённого аккаунта.',
            'UsernameNotOccupiedError':'Telegram-канал не найден. Проверьте адрес источника.',
            'FloodWaitError':'Telegram временно ограничил запросы. Попробуйте позже.',
            'TimeoutError':'Telegram не ответил вовремя. Проверьте соединение.',
            'SessionBusyError':'Аккаунт Telegram сейчас занят другим действием. Попробуйте позже.',
        }
        error = str(exc) if isinstance(exc,ValueError) and str(exc) in safe_errors else explanations.get(type(exc).__name__,type(exc).__name__)
        logging.error('Parser failed: %s',error)
        with connect() as con:
            con.execute("UPDATE parser_runs SET state='failed',finished_at=now(),inserted_count=%s,error=%s WHERE id=%s", (total,error,run_id))
            con.execute("UPDATE sources SET next_run_at=now()+(interval_seconds * interval '1 second') WHERE id=%s AND revision=%s", (source['id'],source['revision']))
        return False
    finally:
        try:
            discard_staged(run_id)
        except Exception:
            logging.exception('Could not remove staged collection data')


def _job_error(job_id):
    with connect() as con:
        row = con.execute('''SELECT r.error FROM collection_jobs j LEFT JOIN parser_runs r ON r.id=j.parser_run_id
          WHERE j.id=%s''', (job_id,)).fetchone()
        return row['error'] if row and row['error'] else 'Источник изменён или сбор не выполнен'


def _pulse(stop):
    while not stop.is_set():
        try:
            collection.heartbeat()
        except Exception:
            logging.exception('Could not update worker status')
        stop.wait(15)


def worker():
    with connect() as lock:
        acquired = lock.execute('SELECT pg_try_advisory_lock(%s) AS locked', (174925602,)).fetchone()['locked']
        lock.commit()
        if not acquired:
            logging.error('Another parser executor is already running')
            return 1
        stop = threading.Event()
        pulse = threading.Thread(target=_pulse,args=(stop,),daemon=True)
        login_server = None
        try:
            from parsers.telegram_auth_service import start_login_server
            login_server = start_login_server()
            collection.recover_interrupted()
            pulse.start()
            needs_recovery = False
            while True:
                # A lost lock connection must stop this executor instead of
                # continuing without the single-executor guarantee.
                lock.execute('SELECT 1')
                lock.commit()
                try:
                    if needs_recovery:
                        collection.recover_interrupted()
                        needs_recovery = False
                    collection.enqueue_due()
                    job = collection.claim_job()
                    if not job:
                        time.sleep(5)
                        continue
                    if job.get('skipped'):
                        continue
                    try:
                        from parsers.inference import Inference
                        inference = Inference()
                        try:
                            ok = asyncio.run(run_source(job['source'],job['mode'],inference,job['id']))
                        finally:
                            del inference
                        collection.finish_job(job['id'],ok,None if ok else _job_error(job['id']))
                    except Exception as exc:
                        logging.exception('Collection job failed')
                        collection.finish_job(job['id'],False,type(exc).__name__)
                except Exception:
                    needs_recovery = True
                    logging.exception('Collection worker failed; retrying')
                    time.sleep(5)
        finally:
            stop.set()
            if login_server:
                login_server.close()
            lock.execute('SELECT pg_advisory_unlock(%s)', (174925602,))
            lock.commit()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run supported parsers')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--due',action='store_true')
    group.add_argument('--source',type=int)
    group.add_argument('--worker',action='store_true')
    parser.add_argument('--mode',choices=('new','history'),default='new')
    args = parser.parse_args(argv)
    if (args.due or args.worker) and args.mode != 'new':
        parser.error('History collection requires an explicit source')
    migrate()
    if args.worker:
        return worker()
    with connect() as lock:
        acquired = lock.execute('SELECT pg_try_advisory_lock(%s) AS locked', (174925602,)).fetchone()['locked']
        lock.commit()
        if not acquired:
            logging.error('Another parser executor is already running')
            return 1
        try:
            collection.recover_interrupted()
            with connect() as con:
                if args.due:
                    sources = con.execute('SELECT * FROM sources WHERE enabled AND deleted_at IS NULL AND schedule_enabled AND next_run_at<=now() ORDER BY next_run_at,id').fetchall()
                else:
                    sources = con.execute('SELECT * FROM sources WHERE enabled AND deleted_at IS NULL AND id=%s', (args.source,)).fetchall()
                    if not sources:
                        raise ValueError('Источник не найден или выключен')
            if not sources:
                return 0
            from parsers.inference import Inference
            inference = Inference()
            results = [asyncio.run(run_source(source,args.mode,inference)) for source in sources]
            return 0 if all(results) else 1
        finally:
            lock.execute('SELECT pg_advisory_unlock(%s)', (174925602,))
            lock.commit()


if __name__ == '__main__':
    sys.exit(main())
