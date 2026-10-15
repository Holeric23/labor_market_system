"""Explicit common CLI; no network except requested web/worker/bot commands."""
import argparse
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time


def stop_condition():
    event = threading.Event()
    def request(*_):
        event.set()
    for signum in (signal.SIGINT,signal.SIGTERM):
        signal.signal(signum,request)
    path = os.environ.get('MARKET_STOP_FILE')
    return lambda:event.is_set() or bool(path and Path(path).exists())


def main(argv=None):
    parser = argparse.ArgumentParser(description='Локальная эксплуатация рынка труда')
    parser.add_argument('--config')
    sub = parser.add_subparsers(dest='command',required=True)
    for name in ('init','web','bot','launch'):
        sub.add_parser(name)
    sub.add_parser('worker').add_argument('--once',action='store_true')
    sub.add_parser('diagnose').add_argument('--full',action='store_true')
    sub.add_parser('backup').add_argument('--target')
    restore = sub.add_parser('restore')
    restore.add_argument('bundle')
    restore.add_argument('target')
    sub.add_parser('verify-backup').add_argument('bundle')
    for name in ('create-admin','create-user'):
        account = sub.add_parser(name)
        account.add_argument('username')
    args = parser.parse_args(argv)
    try:
        from .config import Operations
        config = Operations.load(args.config)
        config.prepare()
        from .logs import configure
        configure(config,args.command)
        gate = os.environ.get('MARKET_LAUNCH_GATE')
        # Assignment barrier for the launcher's process tree container.
        if gate:
            deadline = time.monotonic()+30
            while not Path(gate).exists():
                if time.monotonic()>deadline:
                    raise RuntimeError('Launcher assignment timed out')
                time.sleep(0.05)
        logging.info('starting')
        if args.command=='init':
            from market_system.model_management.store import Models
            from market_system.search.optimize import optimize
            from market_system.web.state import WebState
            Models(config.paths['database']).initialize()
            optimize(config.paths['database'])
            WebState(config.paths['web_state']).initialize()
            print('Миграции и веб-состояние готовы; аккаунты и секреты настройте отдельно.')
        elif args.command in ('create-admin','create-user'):
            from market_system.web.__main__ import main as accounts
            accounts([args.command,args.username,'--state',str(config.paths['web_state'])])
        elif args.command=='diagnose':
            from .diagnostics import inspect
            result = inspect(config,full=args.full)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0 if result['ok'] else 1
        elif args.command in ('backup','verify-backup','restore'):
            from .backup import create,verify_bundle,restore
            if args.command=='backup':
                print(create(config,args.target))
            elif args.command=='verify-backup':
                manifest = verify_bundle(args.bundle)
                print(json.dumps({'ok':True,'files':len(manifest['files']),'created_at':manifest['created_at']}))
            else:
                target = restore(args.bundle,args.target)
                # Compatibility verification runs in the restored tree against its own code and files.
                import subprocess
                environment = dict(os.environ,MARKET_HOME=str(target),PYTHONPATH=str(target/'src'),PYTHONDONTWRITEBYTECODE='1',PYTHONIOENCODING='utf-8')
                for key in ('MARKET_PATH_ALIASES','MARKET_RUNTIME_CONFIG','MARKET_OPERATIONS_CONFIG','MARKET_LAUNCH_GATE','MARKET_STOP_FILE'):
                    environment.pop(key,None)
                completed = subprocess.run([sys.executable,'-m','market_system.operations','diagnose','--full'],cwd=target,env=environment,
                                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',timeout=300)
                (target/'restore-verification.json').write_text(completed.stdout,encoding='utf-8')
                if completed.returncode:
                    raise ValueError('Restored compatibility check failed; see restore-verification.json. Working system untouched.')
                print(target)
        elif args.command=='launch':
            from .launcher import launch
            return launch(config)
        elif args.command=='web':
            import uvicorn
            from market_system.web.app import create_app
            server = uvicorn.Server(uvicorn.Config(create_app(),host=config.values['host'],port=config.values['port'],
                log_config=None,access_log=False,proxy_headers=config.values['mode']=='linux',forwarded_allow_ips='127.0.0.1'))
            stop = os.environ.get('MARKET_STOP_FILE')
            def watcher():
                while not server.should_exit:
                    if stop and Path(stop).exists():
                        server.should_exit = True
                        return
                    time.sleep(0.1)
            threading.Thread(target=watcher,daemon=True).start()
            server.run()
        elif args.command=='worker':
            from market_system.jobs import Jobs
            from market_system.jobs.worker import Worker
            from market_system.model_management.store import Models
            Models(config.paths['database']).initialize()
            sender = None
            if config.values['delivery_enabled']:
                from market_system.telegram_bot.delivery import TelegramSender
                sender = TelegramSender(config.telegram())
            Worker(Jobs(config.paths['database']),telegram_sender=sender,should_stop=stop_condition()).run(once=args.once)
        else:
            import asyncio
            from market_system.telegram_bot.main import run
            settings = config.telegram()
            should_stop = stop_condition()
            async def bot():
                task = asyncio.create_task(run(settings))
                try:
                    while not task.done() and not should_stop():
                        await asyncio.sleep(0.1)
                    if not task.done():
                        task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task,return_exceptions=True)
            asyncio.run(bot())
        logging.info('stopped')
        return 0
    except (ValueError,OSError,RuntimeError) as exc:
        # Secrets can occur in an exception message: report the class, keep no raw payload.
        logging.error('operation_failed type=%s',type(exc).__name__)
        print('Ошибка эксплуатации: '+type(exc).__name__+'. Проверьте конфигурацию, пути, manifest и доступ к файлам.',file=sys.stderr)
        return 1


if __name__=='__main__':
    sys.exit(main())
