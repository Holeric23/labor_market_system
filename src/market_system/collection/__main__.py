"""Source configuration CLI; Telegram credentials/session are set up explicitly here."""
import argparse
import asyncio
from getpass import getpass
import json
from market_system.paths import DATABASE_PATH, local_path
from market_system.runtime import enable_guard
from .store import Sources


def main(argv=None):
    enable_guard()
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',default=str(DATABASE_PATH))
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('init')
    commands.add_parser('list')
    commands.add_parser('setup-telegram')
    commands.add_parser('login-telegram')
    commands.add_parser('dispatch')
    add=commands.add_parser('add')
    add.add_argument('kind',choices=['telegram','designhunters'])
    add.add_argument('name')
    add.add_argument('address')
    add.add_argument('--parameters',default='{}')
    run=commands.add_parser('collect')
    run.add_argument('source_id',type=int)
    run.add_argument('--mode',choices=['history','new'],default='new')
    retry=commands.add_parser('retry')
    retry.add_argument('job_id')
    args=parser.parse_args(argv)
    sources=Sources(args.db)
    if args.command=='setup-telegram':
        path=local_path('state/collection/telegram.json')
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():
            parser.error('Credentials already exist; edit the local file explicitly to change them')
        values={'api_id':int(getpass('Telegram API ID: ')), 'api_hash':getpass('Telegram API hash: ')}
        if values['api_id']<=0 or len(values['api_hash'])!=32 or any(c not in '0123456789abcdefABCDEF' for c in values['api_hash']):
            parser.error('Invalid Telegram API credentials format')
        with path.open('x',encoding='utf-8') as file:
            json.dump(values,file)
        print('Saved locally. Run login-telegram to create a new session.')
    elif args.command=='login-telegram':
        async def login():
            from telethon import TelegramClient
            path=local_path('state/collection/telegram.json')
            values=json.loads(path.read_text(encoding='utf-8'))
            client=TelegramClient(str(local_path('state/collection/telegram.session')),values['api_id'],values['api_hash'])
            try:
                await client.start(phone=lambda:getpass('Phone: '),password=lambda:getpass('2FA password: '),
                                   code_callback=lambda:getpass('Login code: '))
            finally:
                await client.disconnect()
        asyncio.run(login())
    elif args.command=='init':
        sources.initialize()
    elif args.command=='list':
        print(json.dumps(sources.list(),ensure_ascii=False,indent=2))
    elif args.command=='add':
        print(sources.save(args.kind,args.name,args.address,json.loads(args.parameters)))
    elif args.command=='collect':
        print(sources.enqueue(args.source_id,args.mode))
    elif args.command=='retry':
        sources.retry(args.job_id)
    else:
        print(sources.dispatch(force=True))


if __name__=='__main__':
    main()
