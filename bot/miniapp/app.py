"""Mini App: owner-only administration, with no public vacancy catalogue."""
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool
from starlette.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from bot.miniapp import sessions, parser_auth
from bot.miniapp.telegram_auth import TelegramAdminAuth
from database.repository import Repository
from database import collection
from database.client import ClientRepository, DOCUMENT_LABELS

HERE = Path(__file__).parent
COOKIE = 'market_admin_session'


def create_app(settings):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    app.mount('/static', StaticFiles(directory=HERE/'static'), name='static')
    templates = Jinja2Templates(env=Environment(loader=FileSystemLoader(HERE/'templates'), autoescape=select_autoescape(['html'])))
    repository = Repository()
    client_repository = ClientRepository()
    verifier = TelegramAdminAuth(settings.bot_token, settings.owner_ids) if settings.bot_token and settings.owner_ids else None

    def render(request, name, **context):
        return templates.TemplateResponse(request=request, name=name, context={'session':getattr(request.state,'session',None), **context})

    def denied(request, status_code):
        return templates.TemplateResponse(request=request, name='access_denied.html', context={}, status_code=status_code)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        if exc.status_code in (401,403):
            return denied(request,exc.status_code)
        return await http_exception_handler(request,exc)

    @app.middleware('http')
    async def security(request, call_next):
        request.state.session = await run_in_threadpool(sessions.get, request.cookies.get(COOKIE))
        path, method = request.url.path, request.method
        public = ((path == '/miniapp' and method in ('GET','POST')) or
                  (path in ('/','/health') and method == 'GET') or
                  (path.startswith('/static/') and method in ('GET','HEAD')))
        if public:
            response = await call_next(request)
        else:
            try:
                owner(request)
            except HTTPException as exc:
                response = denied(request,exc.status_code)
            else:
                response = await call_next(request)
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors https://web.telegram.org"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        return response

    def owner(request):
        session = request.state.session
        if not session or not session['telegram_id']:
            raise HTTPException(401, 'Откройте панель через Telegram.')
        if session['telegram_id'] not in settings.owner_ids or not session['active'] or session['role'] != 'owner':
            raise HTTPException(403, 'Доступ ограничен.')
        return session

    async def form(request, max_bytes=16384):
        if request.headers.get('content-type','').split(';')[0] != 'application/x-www-form-urlencoded':
            raise HTTPException(415, 'Некорректная форма.')
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > max_bytes:
                raise HTTPException(413, 'Слишком большая форма.')
        try:
            values = parse_qs(body.decode('utf-8'), keep_blank_values=True, max_num_fields=25, strict_parsing=True)
            if any(len(value) != 1 for value in values.values()):
                raise ValueError()
            return {key:value[0] for key,value in values.items()}
        except (UnicodeError, ValueError):
            raise HTTPException(400, 'Некорректная форма.') from None

    def csrf(request, data):
        session = request.state.session
        origin = request.headers.get('origin')
        supplied = data.get('csrf','')
        if (not session or not secrets.compare_digest(supplied.encode(),session['csrf'].encode()) or
                (origin and origin != 'null' and origin.rstrip('/') != str(request.base_url).rstrip('/'))):
            raise HTTPException(403, 'Проверка безопасности не пройдена.')

    @app.get('/health')
    def health():
        from database.connection import connect
        with connect() as con:
            con.execute('SELECT 1').fetchone()
        return {'status':'ok'}

    @app.get('/miniapp')
    async def miniapp(request: Request):
        token, nonce = await run_in_threadpool(sessions.create_anonymous)
        request.state.session = {'csrf':nonce}
        response = render(request, 'miniapp.html')
        response.set_cookie(COOKIE,token,httponly=True,secure=True,samesite='none',max_age=600,path='/')
        return response

    @app.post('/miniapp')
    async def miniapp_login(request: Request):
        data = await form(request)
        csrf(request,data)
        if request.url.scheme != 'https' or not verifier:
            raise HTTPException(403, 'Для входа требуется настроенный Telegram и HTTPS.')
        try:
            identity, proof, expires = verifier.validate(data.get('init_data',''))
            token = await run_in_threadpool(sessions.login, request.cookies.get(COOKIE), identity, proof, expires)
        except (ValueError, PermissionError):
            raise HTTPException(403, 'Вход через Telegram не подтверждён.') from None
        response = RedirectResponse('/clients',status_code=303)
        response.set_cookie(COOKIE,token,httponly=True,secure=True,samesite='none',max_age=3600,path='/')
        return response

    @app.get('/')
    def home(request: Request):
        return RedirectResponse('/clients' if request.state.session and request.state.session['telegram_id'] else '/miniapp',status_code=303)

    @app.post('/logout')
    async def logout(request: Request):
        owner(request)
        data = await form(request)
        csrf(request,data)
        await run_in_threadpool(sessions.logout,request.cookies.get(COOKIE))
        response = RedirectResponse('/miniapp',status_code=303)
        response.delete_cookie(COOKIE,path='/',secure=True,httponly=True,samesite='none')
        return response

    @app.get('/clients')
    def clients(request: Request):
        session = owner(request)
        return render(request,'clients.html',clients=repository.list_clients(session['telegram_id']))

    @app.get('/clients/{user}')
    def client(request: Request, user: int):
        session = owner(request)
        try:
            item = repository.client(session['telegram_id'],user)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from None
        if not item:
            raise HTTPException(404,'Клиент не найден.')
        item['active'] = bool(item['access_until'] and item['access_until'] > datetime.now(timezone.utc))
        item['last_access_event'] = next((event for event in item['history'] if event['event_type'] in ('activated','revoked','reverted')),None)
        return render(request,'client.html',client=item)

    @app.post('/clients/{user}/{action}')
    async def client_action(request: Request, user: int, action: str):
        session = owner(request)
        data = await form(request)
        csrf(request,data)
        try:
            actor = session['telegram_id']
            if action == 'activate':
                await run_in_threadpool(repository.activate,actor,user,data.get('grant_id',''))
            elif action == 'cancel':
                await run_in_threadpool(repository.cancel,actor,user,data.get('grant_id',''))
            elif action == 'revoke':
                await run_in_threadpool(repository.revoke,actor,user,int(data.get('revision','')))
            elif action == 'revert':
                await run_in_threadpool(repository.revert,actor,user,data.get('grant_id',''),int(data.get('revision','')))
            else:
                raise HTTPException(404,'Действие не найдено.')
        except (ValueError, PermissionError, OverflowError) as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse(f'/clients/{user}',status_code=303)

    def parser_login_allowed(request):
        if not settings.miniapp_url:
            return False
        configured = urlsplit(settings.miniapp_url)
        return configured.scheme == 'https' and str(request.base_url).rstrip('/') == f'{configured.scheme}://{configured.netloc}'

    @app.get('/parser/telegram')
    async def telegram_login_page(request: Request):
        session = owner(request)
        result = await run_in_threadpool(parser_auth.request,'status',session['telegram_id'])
        notice = request.query_params.get('notice','')[:300]
        login_allowed = parser_login_allowed(request)
        return render(request,'parser_telegram.html',result=result,notice=notice,login_allowed=login_allowed)

    @app.post('/parser/telegram/{action}')
    async def telegram_login_action(request: Request, action: str):
        session = owner(request)
        if request.url.scheme != 'https':
            raise HTTPException(403,'Для входа в Telegram требуется HTTPS.')
        if action not in ('start','code','password','cancel'):
            raise HTTPException(404,'Действие не найдено.')
        if action != 'cancel' and not parser_login_allowed(request):
            raise HTTPException(403,'Вход доступен только через настроенный собственный HTTPS-адрес.')
        data = await form(request,max_bytes=4096)
        csrf(request,data)
        result = await run_in_threadpool(parser_auth.request,action,session['telegram_id'],data.get('value',''))
        # The service returns only fixed, non-secret error messages.
        notice = result.get('error','') if not result.get('ok') else ''
        location = '/parser/telegram' + ('?' + urlencode({'notice':notice}) if notice else '')
        return RedirectResponse(location,status_code=303)

    @app.get('/sources')
    def sources(request: Request):
        session = owner(request)
        jobs,worker_online = collection.list_jobs(session['telegram_id'])
        return render(request,'sources.html',sources=repository.list_sources(session['telegram_id']),
                      archived_sources=repository.list_sources(session['telegram_id'],archived=True),
                      jobs=jobs,worker_online=worker_online,retention=collection.retention_settings(session['telegram_id']))

    def source_fields(data):
        keys = ('batch_size','max_items','overlap_days','overlap_limit','timeout_ms','delay_seconds')
        parameters = {key:int(data[key]) for key in keys if data.get(key,'').strip()}
        return (data.get('kind',''),data.get('name',''),data.get('address',''),
                int(data.get('interval_seconds','')),data.get('enabled') == '1',
                data.get('schedule_enabled') == '1',parameters)

    @app.post('/sources')
    async def source_save(request: Request):
        session = owner(request)
        data = await form(request)
        csrf(request,data)
        try:
            await run_in_threadpool(repository.create_source,session['telegram_id'],*source_fields(data))
        except (ValueError, PermissionError, OverflowError) as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sources',status_code=303)

    @app.post('/sources/{source_id}/edit')
    async def source_edit(request: Request, source_id: int):
        session = owner(request)
        data = await form(request)
        csrf(request,data)
        try:
            await run_in_threadpool(repository.edit_source,session['telegram_id'],source_id,
                                    int(data.get('revision','')),*source_fields(data))
        except (ValueError, PermissionError, OverflowError) as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sources',status_code=303)

    @app.post('/sources/{source_id}/collect')
    async def source_collect(request: Request, source_id: int):
        session = owner(request)
        data = await form(request)
        csrf(request,data)
        try:
            await run_in_threadpool(collection.enqueue,session['telegram_id'],source_id,data.get('mode',''))
        except (ValueError, PermissionError, OverflowError) as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sources',status_code=303)

    @app.post('/collection/all')
    async def collection_all(request: Request):
        session = owner(request)
        data = await form(request)
        csrf(request,data)
        try:
            await run_in_threadpool(collection.enqueue_all,session['telegram_id'],data.get('mode',''))
        except (ValueError, PermissionError, OverflowError) as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sources',status_code=303)

    @app.post('/collection/{job_id}/cancel')
    async def collection_cancel(request: Request, job_id: str):
        session = owner(request)
        data = await form(request)
        csrf(request,data)
        try:
            await run_in_threadpool(collection.cancel,session['telegram_id'],job_id)
        except (ValueError, PermissionError, OverflowError) as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sources',status_code=303)

    @app.post('/retention')
    async def retention_save(request: Request):
        session = owner(request)
        data = await form(request)
        csrf(request,data)
        try:
            await run_in_threadpool(collection.save_retention,session['telegram_id'],
                data.get('enabled') == '1',int(data.get('days','')),int(data.get('revision','')))
        except (ValueError, PermissionError, OverflowError) as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sources',status_code=303)

    @app.post('/sources/{source_id}/{action}')
    async def source_state(request: Request, source_id: int, action: str):
        session = owner(request)
        data = await form(request)
        csrf(request,data)
        if action not in ('delete','restore'):
            raise HTTPException(404,'Действие не найдено.')
        try:
            operation = repository.archive_source if action == 'delete' else repository.restore_source
            await run_in_threadpool(operation,session['telegram_id'],source_id,int(data.get('revision','')))
        except (ValueError, PermissionError, OverflowError) as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sources',status_code=303)

    @app.get('/settings')
    def settings_page(request: Request):
        owner(request)
        return render(request,'settings.html',settings=repository.public_settings())

    @app.post('/settings')
    async def settings_save(request: Request):
        session = owner(request)
        data = await form(request)
        csrf(request,data)
        try:
            await run_in_threadpool(repository.save_settings,session['telegram_id'],data.get('manager_url',''),int(data.get('default_access_days','')),int(data.get('revision','')))
        except (ValueError, PermissionError, OverflowError) as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/settings',status_code=303)

    @app.post('/settings/bot')
    async def bot_availability_save(request: Request):
        session = owner(request)
        data = await form(request)
        csrf(request,data)
        try:
            action = data.get('action')
            if action not in ('enable','disable'):
                raise ValueError('Неизвестное действие.')
            await run_in_threadpool(repository.set_bot_available,session['telegram_id'],action == 'enable',int(data.get('revision','')))
        except (ValueError, PermissionError, OverflowError) as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/settings',status_code=303)

    @app.get('/product')
    def product_page(request: Request):
        session = owner(request)
        return render(request,'product.html',settings=client_repository.settings(),
                      documents=client_repository.documents(session['telegram_id']),labels=DOCUMENT_LABELS)

    @app.get('/team')
    def team_page(request: Request):
        session = owner(request)
        return render(request,'team.html',managers=repository.managers(session['telegram_id']))

    @app.post('/team')
    async def team_save(request: Request):
        session = owner(request)
        data = await form(request)
        csrf(request,data)
        try:
            if data.get('action') not in ('enable','disable'):
                raise ValueError('Неизвестное действие.')
            await run_in_threadpool(repository.set_manager,session['telegram_id'],int(data.get('telegram_id','')),data['action'] == 'enable')
        except (ValueError,PermissionError,OverflowError) as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/team',status_code=303)

    @app.post('/product')
    async def product_save(request: Request):
        session = owner(request)
        data = await form(request)
        csrf(request,data)
        try:
            await run_in_threadpool(client_repository.save_product_settings,session['telegram_id'],
                int(data.get('selection_limit','')),data.get('pricing_text',''))
        except (ValueError, PermissionError, OverflowError) as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/product',status_code=303)

    @app.post('/product/documents')
    async def publish_document(request: Request):
        session = owner(request)
        data = await form(request, max_bytes=2000000)
        csrf(request,data)
        try:
            await run_in_threadpool(client_repository.publish_document,session['telegram_id'],
                data.get('kind',''),data.get('version',''),data.get('url',''),data.get('body',''))
        except (ValueError, PermissionError) as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/product',status_code=303)

    return app
