"""Explicit local server startup and account provisioning."""
import argparse
import getpass
import os
from market_system.paths import local_path
from market_system.runtime import enable_guard


def main(argv=None):
    parser = argparse.ArgumentParser(description='Веб-интерфейс рынка труда')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('create-admin','create-user'):
        command = commands.add_parser(name)
        command.add_argument('username')
        command.add_argument('--state', default=os.environ.get('MARKET_WEB_STATE','state/web.db'))
        command.add_argument('--password-env', help='Имя переменной окружения с паролем; без параметра используется скрытый ввод')
    server = commands.add_parser('serve')
    server.add_argument('--host', default='127.0.0.1')
    server.add_argument('--port', type=int, default=8000)
    args = parser.parse_args(argv)
    enable_guard()
    if args.command == 'serve':
        import uvicorn
        from .app import create_app
        uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=False)
        return
    from .state import WebState
    if args.password_env:
        password = os.environ.get(args.password_env)
        if password is None:
            parser.error('Переменная окружения с паролем не задана.')
    else:
        password = getpass.getpass('Пароль (Enter — без пароля): ')
        if password != getpass.getpass('Повторите пароль: '):
            parser.error('Пароли не совпадают.')
    state = WebState(local_path(args.state))
    state.initialize()
    try:
        state.create_user(args.username, password, 'ADMIN' if args.command=='create-admin' else 'USER')
    except ValueError as exc:
        parser.error(str(exc))
    print('Учётная запись создана. Пароль не сохранён в открытом виде.')


if __name__ == '__main__':
    main()
