"""Internal parser login endpoint; only the bot container knows the shared proof."""
import asyncio
import hmac
import json
import os
import threading
from concurrent.futures import TimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from parsers.telegram_auth import TelegramLogin, LoginError


from database.parser_auth import shared_proof


class LoginServer:
    def __init__(self, workflow, proof, address=('0.0.0.0',8091)):
        self.workflow, self.proof = workflow, proof
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever,daemon=True,name='telegram-login-loop')
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass  # Request bodies and credentials must never enter logs.

            def reply(self, status, result):
                body = json.dumps(result,ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                try: self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError): pass

            def do_POST(self):
                if self.path != '/internal/parser-login':
                    return self.reply(404,{'ok':False})
                supplied = self.headers.get('X-Parser-Auth','')
                if len(supplied) != 64 or not supplied.isascii() or not hmac.compare_digest(supplied,outer.proof):
                    return self.reply(403,{'ok':False})
                try:
                    size = int(self.headers.get('Content-Length','0'))
                    if not 1 <= size <= 4096: raise ValueError()
                    value = json.loads(self.rfile.read(size).decode('utf-8'))
                    if not isinstance(value,dict) or set(value)-{'action','actor','value'}: raise ValueError()
                    action,actor = value.get('action'),value.get('actor')
                    if action not in ('status','start','code','password','cancel'): raise ValueError()
                    if type(actor) is not int or not 0 < actor < 2**63: raise ValueError()
                    method = getattr(outer.workflow,action)
                    coroutine = method(actor,value.get('value','')) if action in ('start','code','password') else method(actor)
                except (ValueError,UnicodeError,TypeError,json.JSONDecodeError):
                    return self.reply(400,{'ok':False,'error':'Некорректный запрос.'})
                future = asyncio.run_coroutine_threadsafe(coroutine,outer.loop)
                try:
                    result = future.result(timeout=45)
                    return self.reply(200,{'ok':True,**result})
                except LoginError as exc:
                    return self.reply(200,{'ok':False,'error':str(exc)})
                except TimeoutError:
                    future.cancel()
                    return self.reply(200,{'ok':False,'error':'Telegram не ответил вовремя. Обновите состояние входа.'})
                except Exception:
                    return self.reply(200,{'ok':False,'error':'Не удалось выполнить вход. Попробуйте позже.'})

        self.http = ThreadingHTTPServer(address,Handler)
        self.http.daemon_threads = True
        self.server_thread = threading.Thread(target=self.http.serve_forever,daemon=True,name='telegram-login-api')

    def start(self):
        self.loop_thread.start()
        self.server_thread.start()

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        try: asyncio.run_coroutine_threadsafe(self.workflow.shutdown(),self.loop).result(timeout=10)
        except Exception: pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(timeout=10)
        self.loop.close()


def start_login_server():
    root = Path(os.environ.get('APP_HOME',Path(__file__).resolve().parents[1]))
    folder = root/'state'/'collection'
    # These are temporary files from interrupted login attempts, never active sessions.
    if folder.is_dir():
        import re
        for path in folder.iterdir():
            if re.fullmatch(r'\.login-[0-9a-f]{32}\.session(?:-journal|-wal|-shm)?',path.name):
                try: path.unlink()
                except OSError: pass
    workflow = TelegramLogin(folder/'telegram.session',os.environ.get('TELEGRAM_API_ID',''),os.environ.get('TELEGRAM_API_HASH',''))
    server = LoginServer(workflow,shared_proof(os.environ.get('DATABASE_URL','')))
    server.start()
    return server
