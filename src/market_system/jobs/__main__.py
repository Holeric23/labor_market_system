"""Local owner CLI. Worker is always a separate explicitly launched process."""
import argparse
import json
from market_system.paths import DATABASE_PATH
from market_system.runtime import enable_guard
from .store import Jobs


def main(argv=None):
    enable_guard()
    parser = argparse.ArgumentParser(description='Persistent processing jobs')
    parser.add_argument('--db',default=str(DATABASE_PATH))
    sub = parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init')
    create = sub.add_parser('create')
    create.add_argument('--kind',choices=['full','incremental','sample'],default='incremental')
    create.add_argument('--ids',help='Comma-separated content IDs')
    create.add_argument('--batch-size',type=int,default=64)
    create.add_argument('--key',help='Idempotency key')
    show = sub.add_parser('show')
    show.add_argument('id',nargs='?')
    for name in ('retry','cancel'):
        sub.add_parser(name).add_argument('id')
    worker = sub.add_parser('worker')
    worker.add_argument('--once',action='store_true')
    worker.add_argument('--telegram-env',help='Explicit local bot env file; enables real outbox delivery')
    args = parser.parse_args(argv)
    jobs = Jobs(args.db)
    try:
        if args.command=='init':
            jobs.initialize()
        elif args.command=='create':
            print(jobs.submit(args.kind,[int(v) for v in args.ids.split(',')] if args.ids else None,
                              args.batch_size,idempotency_key=args.key))
        elif args.command=='show':
            print(json.dumps(jobs.get(args.id) if args.id else jobs.list(),ensure_ascii=False,indent=2))
        elif args.command in ('retry','cancel'):
            getattr(jobs,args.command)(args.id)
        else:
            from .worker import Worker
            sender=None
            if args.telegram_env:
                from market_system.telegram_bot.config import Settings
                from market_system.telegram_bot.delivery import TelegramSender
                settings=Settings.load(args.telegram_env)
                if settings.research_db_path!=jobs.path:
                    raise ValueError('Bot catalog and worker database must match')
                sender=TelegramSender(settings)
            Worker(jobs,telegram_sender=sender).run(once=args.once)
    except (ValueError,RuntimeError) as exc:
        parser.exit(1,str(exc)+'\n')


if __name__=='__main__':
    main()
