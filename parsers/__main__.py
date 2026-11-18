"""Run selected parser jobs once; scheduling is external to the short-lived container."""
import argparse
import asyncio
import logging
import sys
import uuid
from psycopg.types.json import Jsonb
from database.connection import connect
from database.migrate import migrate
from parsers.adapters import batches
from parsers.storage import store_batch


async def run_source(source, mode, inference):
    run_id = uuid.uuid4()
    with connect() as con:
        configuration = {key:source[key] for key in ('kind','name','address','parameters','revision')}
        configuration['mode'] = mode
        con.execute("INSERT INTO parser_runs(id,source_id,state,configuration) VALUES (%s,%s,'running',%s)", (run_id,source['id'],Jsonb(configuration)))
    total = 0
    try:
        async for batch in batches(source,mode):
            total += store_batch(source,run_id,batch,inference)
        with connect() as con:
            con.execute("UPDATE parser_runs SET state='succeeded',finished_at=now(),inserted_count=%s WHERE id=%s", (total,run_id))
            con.execute("UPDATE sources SET last_success_at=now(),next_run_at=now()+(interval_seconds * interval '1 second') WHERE id=%s AND revision=%s", (source['id'],source['revision']))
        return True
    except Exception as exc:
        logging.error('Parser failed: %s',type(exc).__name__)
        with connect() as con:
            con.execute("UPDATE parser_runs SET state='failed',finished_at=now(),inserted_count=%s,error=%s WHERE id=%s", (total,type(exc).__name__,run_id))
            con.execute("UPDATE sources SET next_run_at=now()+(interval_seconds * interval '1 second') WHERE id=%s AND revision=%s", (source['id'],source['revision']))
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run supported parsers once')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--due',action='store_true')
    group.add_argument('--source',type=int)
    parser.add_argument('--mode',choices=('new','history'),default='new')
    args = parser.parse_args(argv)
    if args.due and args.mode != 'new':
        parser.error('History collection requires an explicit source')
    migrate()
    with connect() as lock:
        acquired = lock.execute('SELECT pg_try_advisory_lock(%s) AS locked', (174925602,)).fetchone()['locked']
        lock.commit()
        if not acquired:
            logging.error('Another parser executor is already running')
            return 1
        try:
            with connect() as con:
                con.execute("UPDATE parser_runs SET state='failed',finished_at=now(),error='Interrupted' WHERE state='running'")
                if args.due:
                    sources = con.execute('SELECT * FROM sources WHERE enabled AND schedule_enabled AND next_run_at<=now() ORDER BY next_run_at,id').fetchall()
                else:
                    sources = con.execute('SELECT * FROM sources WHERE enabled AND id=%s', (args.source,)).fetchall()
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
