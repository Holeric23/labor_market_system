"""FastAPI/Jinja2 adapter for the shared catalog."""
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
import json
import os
from pathlib import Path
import secrets
import sqlite3
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

from market_system.paths import DATABASE_PATH, local_path
from market_system.search import Catalog, InvalidCursor, SearchFilter
from market_system.jobs import Jobs
from market_system.collection.store import Sources
from market_system.review.store import Review, Actor
from market_system.review.projection import SALARY, CONDITIONS, ROLES
from market_system.analytics.store import Analytics, filters as analytics_filters
from market_system.model_management.store import Models
from .state import WebState
from .views import CatalogViews

COOKIE = 'market_session'
HERE = Path(__file__).parent
LABELS = {
    'vacancy':'Вакансия', 'resume':'Резюме', 'ADMIN':'Админ', 'USER':'Пользователь',
    'required':'Обязательный навык', 'preferred':'Желательный навык', 'possessed':'Навык соискателя',
    'negated':'Отрицание', 'mentioned':'Упоминание', 'task':'Рабочая задача', 'none':'Без специальной роли',
    'remote':'Удалённо', 'office':'В офисе', 'onsite':'В офисе', 'hybrid':'Гибридный формат',
    'full_time':'Полная занятость', 'part_time':'Частичная занятость', 'project':'Проектная работа',
    'internship':'Стажировка', 'freelance':'Фриланс', 'full':'Полный день', 'flexible':'Гибкий график',
    'shift':'Сменный график', 'five_two':'5/2', 'month':'месяц', 'hour':'час', 'year':'год',
    'day':'день', 'week':'неделя', 'gross':'До вычета налогов', 'net':'На руки',
    'unreviewed':'Не проверено человеком', 'needs_review':'Требует проверки',
    'processed':'Обработано, классы найдены', 'no_classes':'Обработано, классы не найдены',
    'document':'Относится к документу', 'unattributed':'Неоднозначная привязка зарплаты',
    'ready':'Готов', 'building':'Формируется', 'failed':'Ошибка',
    'missing':'Отсутствует', 'ambiguous':'Неоднозначно', 'known':'Известно', 'verified':'Проверено человеком',
    'post_type':'Тип документа', 'date_utc':'Дата UTC', 'source_id':'ID источника', 'source':'Источник',
    'kind':'Вид класса', 'class_id':'ID класса', 'occupation_id':'Профессия', 'skill_id':'Навык',
    'occupation':'Профессия', 'skill':'Навык', 'field':'Поле', 'status':'Состояние', 'value':'Значение',
    'salary':'Зарплата', 'salary_from':'Нижняя граница', 'salary_to':'Верхняя граница',
    'currency':'Валюта', 'period':'Период', 'salary_period':'Период оплаты', 'tax_mode':'Налоги',
    'bound':'Граница суммы', 'amount':'Сумма', 'published_at_utc':'Дата публикации',
    'work_format':'Формат работы', 'schedule':'График', 'employment_type':'Занятость',
    'working_hours':'Рабочие часы', 'hours_per_week':'Часов в неделю',
}
FIELDS = {'post_type':'Тип публикации', 'work_format':'Формат работы', 'schedule':'График',
          'employment_type':'Занятость', 'tax_mode':'Налоги', 'published_at_utc':'Дата публикации'}


@dataclass(frozen=True)
class WebConfig:
    catalog_path: Path = DATABASE_PATH
    state_path: Path = local_path('state/web.db')
    secure_cookie: bool = False
    allowed_hosts: tuple[str, ...] = ('127.0.0.1', 'localhost', '[::1]')

    @classmethod
    def from_env(cls):
        return cls(local_path(os.environ.get('MARKET_WEB_CATALOG', str(DATABASE_PATH))),
                   local_path(os.environ.get('MARKET_WEB_STATE', 'state/web.db')),
                   os.environ.get('MARKET_WEB_SECURE_COOKIE', '0') == '1',
                   tuple(x.strip() for x in os.environ.get('MARKET_WEB_HOSTS', '127.0.0.1,localhost,[::1]').split(',') if x.strip()))


def label(value):
    if value is None or value == '':
        return 'Не указано'
    return LABELS.get(value, str(value))


def safe_url(value):
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme.lower() in ('http', 'https') and parsed.hostname and not any(ord(c)<32 for c in value) else None
    except ValueError:
        return None


def filter_input(params, settings):
    """Convert form transport types only; all matching rules stay in SearchFilter."""
    values = dict(params)
    values.setdefault('post_type', settings['post_type'])
    values.setdefault('page_size', str(settings['page_size']))
    result = {}
    for key in ('post_type','work_format','schedule','employment_type','tax_mode','currency','salary_period'):
        result[key] = values.get(key) or None
    for key in ('occupation_ids','skill_ids'):
        result[key] = tuple(int(x) for x in params.getlist(key) if x)
    result['skills_mode'] = values.get('skills_mode', 'ALL')
    result['skill_roles'] = tuple(params.getlist('skill_roles')) or ('required','preferred','possessed')
    for key in ('published_from','published_until'):
        raw = values.get(key)
        result[key] = date.fromisoformat(raw).isoformat()+'T00:00:00Z' if raw else None
    result['salary_min'] = float(values['salary_min']) if values.get('salary_min') else None
    result['unknown'] = {key:values.get('unknown_'+key,'exclude') for key in FIELDS}
    return SearchFilter(**result), int(values['page_size']), values


def create_app(config: WebConfig | None = None):
    config = config or WebConfig.from_env()
    if local_path(config.catalog_path) == local_path(config.state_path):
        raise ValueError('Хранилище веб-состояния должно отличаться от каталога.')
    state = WebState(config.state_path)
    catalog = Catalog(config.catalog_path)
    jobs = Jobs(config.catalog_path)
    sources = Sources(config.catalog_path)
    review = Review(config.catalog_path)
    analytics = Analytics(config.catalog_path)
    models = Models(config.catalog_path)
    views = CatalogViews(config.catalog_path)
    environment = Environment(loader=FileSystemLoader(HERE/'templates'), autoescape=select_autoescape(['html']))
    environment.filters.update(label=label, safe_url=safe_url,
                               pretty=lambda v: json.dumps(v, ensure_ascii=False, indent=2),
                               number=lambda v: f'{v:,.0f}'.replace(',', ' ') if v is not None else 'Не указано')
    templates = Jinja2Templates(env=environment)

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(state.initialize)
        await run_in_threadpool(models.initialize)
        yield

    app = FastAPI(title='Рынок труда', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(config.allowed_hosts))
    app.mount('/static', StaticFiles(directory=HERE/'static'), name='static')
    app.state.web_state = state
    app.state.catalog = catalog

    def render(request, template, *, status=200, **context):
        return templates.TemplateResponse(request=request, name=template, context={
            'session':getattr(request.state, 'session', None), 'fields':FIELDS, **context}, status_code=status)

    @app.middleware('http')
    async def security(request, call_next):
        request.state.session = await run_in_threadpool(state.session, request.cookies.get(COOKIE))
        response = await call_next(request)
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        if not request.url.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    def authenticated(request, owner=False):
        session = request.state.session
        if not session or session['user_id'] is None:
            raise HTTPException(401, 'Войдите в систему.')
        if owner and session['role'] != 'ADMIN':
            raise HTTPException(403, 'Этот раздел доступен только админу.')
        return session

    async def form(request, repeated=()):
        if request.headers.get('content-type','').split(';')[0] != 'application/x-www-form-urlencoded':
            raise HTTPException(415, 'Поддерживаются обычные формы.')
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > (131072 if repeated else 16384):
                raise HTTPException(413, 'Форма слишком велика.')
        try:
            parsed = parse_qs(body.decode('utf-8'), keep_blank_values=True, max_num_fields=600 if repeated else 30, strict_parsing=True)
            if any(len(v) != 1 for k,v in parsed.items() if k not in repeated):
                raise ValueError()
            return {k:v if k in repeated else v[0] for k,v in parsed.items()}
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'Некорректная форма.') from None

    def csrf(request, data):
        session = request.state.session
        supplied = data.get('csrf', '')
        origin = request.headers.get('origin')
        if (not session or not secrets.compare_digest(supplied.encode(), session['csrf'].encode()) or
                (origin and origin != 'null' and origin.rstrip('/') != str(request.base_url).rstrip('/'))):
            raise HTTPException(403, 'Проверка безопасности формы не пройдена. Обновите страницу.')

    def set_cookie(response, token, lifetime):
        response.set_cookie(COOKIE, token, httponly=True, samesite='lax', secure=config.secure_cookie,
                            max_age=lifetime, path='/')

    @app.exception_handler(RequestValidationError)
    async def invalid_parameters(request, exc):
        return render(request, 'error.html', status=422, message='Некорректные параметры страницы.')

    @app.exception_handler(HTTPException)
    async def errors(request, exc):
        if exc.status_code == 401:
            return RedirectResponse('/login', status_code=303)
        return render(request, 'error.html', status=exc.status_code, message=exc.detail)

    @app.get('/login')
    def login_page(request: Request):
        session = request.state.session
        if session and session['user_id'] is not None:
            return RedirectResponse('/catalog', status_code=303)
        response = None
        if session is None:
            token, csrf_token, lifetime = state.new_session()
            request.state.session = state.session(token)
            response = render(request, 'login.html', error=None)
            set_cookie(response, token, lifetime)
        return response or render(request, 'login.html', error=None)

    @app.post('/login')
    async def login(request: Request):
        data = await form(request)
        csrf(request, data)
        username, password = data.get('username', ''), data.get('password', '')
        if len(username)>64 or len(password)>1024:
            user_id = None
        else:
            user_id = await run_in_threadpool(state.authenticate, username, password)
        if user_id is None:
            return render(request, 'login.html', status=401,
                          error='Вход не выполнен. Проверьте логин и пароль. После пяти ошибок подождите пять минут.')
        token, _, lifetime = await run_in_threadpool(state.new_session, user_id, request.cookies.get(COOKIE))
        session = await run_in_threadpool(state.session, token)
        response = RedirectResponse('/' if session['role']=='ADMIN' else '/catalog', status_code=303)
        set_cookie(response, token, lifetime)
        return response

    @app.post('/logout')
    async def logout(request: Request):
        authenticated(request)
        data = await form(request)
        csrf(request, data)
        await run_in_threadpool(state.logout, request.cookies[COOKIE])
        response = RedirectResponse('/login', status_code=303)
        response.delete_cookie(COOKIE, path='/', secure=config.secure_cookie, httponly=True, samesite='lax')
        return response

    @app.get('/diagnostics')
    def diagnostics(request: Request):
        authenticated(request, owner=True)
        from market_system.operations.diagnostics import inspect
        result = inspect(database=config.catalog_path, freshness_hours=state.settings()['freshness_days']*24, lightweight=True)
        return render(request, 'diagnostics.html', diagnostics=result)

    @app.get('/')
    def overview(request: Request):
        session = authenticated(request)
        if session['role'] != 'ADMIN':
            return RedirectResponse('/catalog', status_code=303)
        settings = state.settings()
        return render(request, 'overview.html', overview=views.overview(settings['freshness_days']), settings=settings)

    @app.get('/catalog')
    def search(request: Request):
        authenticated(request)
        params = request.query_params
        settings = state.settings()
        values = dict(params)
        values.setdefault('post_type', settings['post_type'])
        values.setdefault('page_size', str(settings['page_size']))
        pairs = [(k,v) for k,v in params.multi_items() if k!='cursor']
        for key in ('post_type','page_size'):
            if key not in params:
                pairs.append((key, values[key]))
        restart = '/catalog?' + urlencode(pairs)
        choices = views.choices()
        context = {'values':values,'selected':{k:params.getlist(k) for k in ('skill_ids','occupation_ids','skill_roles')},
                   'choices':choices,'restart':restart,'page':None,'next_url':None,'error':None}
        try:
            if len(params)>150 or len(str(request.url))>20000:
                raise ValueError()
            filters, page_size, values = filter_input(params, settings)
            page = catalog.search(filters, params.get('cursor'), page_size=page_size)
            context.update(page=page, values=values,
                           next_url=restart+'&'+urlencode({'cursor':page.next_cursor}) if page.next_cursor else None)
        except InvalidCursor as exc:
            return render(request, 'catalog.html', status=400, **(context | {'error':str(exc)}))
        except (ValueError, OverflowError):
            return render(request, 'catalog.html', status=400, **(context | {'error':
                'Не удалось выполнить поиск. Проверьте классы, даты, размер страницы и зарплату: минимум требует валюту и период. Набор должен быть готов.'}))
        return render(request, 'catalog.html', **context)

    @app.get('/cards/{content_id}')
    def detail(request: Request, content_id: int, result_set_id: str):
        authenticated(request)
        try:
            card = catalog.get(content_id, result_set_id)
        except ValueError:
            card = None
        if card is None:
            raise HTTPException(404, 'Карточка в выбранном готовом наборе не найдена.')
        return render(request, 'card.html', card=card)

    @app.get('/settings')
    def settings_page(request: Request):
        authenticated(request, owner=True)
        return render(request, 'settings.html', settings=state.settings(), saved=request.query_params.get('saved')=='1', error=None,
                      catalog_path=str(config.catalog_path), state_path=str(config.state_path))

    @app.post('/settings')
    async def settings_save(request: Request):
        session = authenticated(request, owner=True)
        data = await form(request)
        csrf(request, data)
        try:
            await run_in_threadpool(state.save_settings, session['user_id'], int(data.get('page_size','')),
                                    data.get('post_type',''), int(data.get('freshness_days','')), int(data.get('revision','')))
        except PermissionError:
            raise HTTPException(403, 'Этот раздел доступен только админу.') from None
        except ValueError as exc:
            return render(request, 'settings.html', status=400, settings=state.settings(), saved=False,
                          error='Настройки не сохранены. Проверьте значения и актуальность страницы.',
                          catalog_path=str(config.catalog_path), state_path=str(config.state_path))
        return RedirectResponse('/settings?saved=1', status_code=303)

    @app.get('/jobs')
    def jobs_page(request: Request):
        authenticated(request, owner=True)
        return render(request, 'jobs.html', jobs=jobs.list(), error=None)

    @app.get('/jobs/{identity}')
    def job_page(request: Request, identity: str):
        authenticated(request, owner=True)
        try:
            job = jobs.get(identity)
        except ValueError:
            raise HTTPException(404, 'Задание не найдено.') from None
        return render(request, 'job.html', job=job)

    @app.post('/jobs')
    async def job_create(request: Request):
        session = authenticated(request, owner=True)
        data = await form(request)
        csrf(request, data)
        try:
            ids = [int(v.strip()) for v in data['ids'].split(',')] if data.get('ids','').strip() else None
            identity = await run_in_threadpool(jobs.submit, data.get('kind','incremental'), ids,
                int(data.get('batch_size','64')), actor='web:'+str(session['user_id']),
                idempotency_key=data.get('key') or None)
        except ValueError as exc:
            return render(request, 'jobs.html', status=400, jobs=jobs.list(), error=str(exc))
        return RedirectResponse('/jobs/'+identity, status_code=303)

    @app.post('/jobs/{identity}/{action}')
    async def job_action(request: Request, identity: str, action: str):
        authenticated(request, owner=True)
        data = await form(request)
        csrf(request, data)
        if action not in ('retry','cancel'):
            raise HTTPException(404, 'Действие не найдено.')
        try:
            await run_in_threadpool(getattr(jobs, action), identity)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return RedirectResponse('/jobs/'+identity, status_code=303)

    @app.get('/models')
    def models_page(request: Request):
        actor = review_actor(request)
        authenticated(request, owner=True)
        return render(request,'models.html',models=models.list(actor),results=models.results(actor),
                      experiments=models.experiments(actor))

    @app.get('/models/{version}')
    def model_page(request: Request, version: str):
        authenticated(request, owner=True)
        try:
            item = models.get(version,review_actor(request))
        except ValueError as exc:
            raise HTTPException(404,str(exc)) from None
        return render(request,'model.html',model=item,manifest=json.loads(item['manifest_json']),
                      dependencies=json.loads(item['dependencies_json']),classes=json.loads(item['class_space_json']))

    @app.post('/models/{version}/{action}')
    async def model_action(request: Request, version: str, action: str):
        authenticated(request, owner=True)
        data = await form(request)
        csrf(request,data)
        actor = review_actor(request)
        try:
            if action=='designate':
                job = await run_in_threadpool(models.designate,version,actor)
                return RedirectResponse('/jobs/'+job,status_code=303)
            if action not in ('approve','retire'):
                raise HTTPException(404,'Действие не найдено.')
            await run_in_threadpool(models.status,version,'approved' if action=='approve' else 'retired',actor)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/models/'+version,status_code=303)

    @app.post('/models/results/{identity}/activate')
    async def model_publish(request: Request, identity: str):
        authenticated(request, owner=True)
        data = await form(request)
        csrf(request,data)
        try:
            await run_in_threadpool(models.publish,identity,review_actor(request))
        except (ValueError, sqlite3.IntegrityError) as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/models',status_code=303)

    @app.get('/experiments/{identity}')
    def experiment_page(request: Request, identity: str):
        authenticated(request, owner=True)
        try:
            item = models.experiments(review_actor(request),identity)
        except ValueError as exc:
            raise HTTPException(404,str(exc)) from None
        rendered = json.dumps({k:json.loads(v) if k.endswith('_json') and v else v for k,v in item.items()},ensure_ascii=False,indent=2)
        return render(request,'experiment.html',experiment=item,details=rendered[:50000],truncated=len(rendered)>50000)

    @app.get('/experiments/{identity}/export')
    def experiment_export(request: Request, identity: str):
        authenticated(request, owner=True)
        try:
            item = models.experiments(review_actor(request),identity)
        except ValueError as exc:
            raise HTTPException(404,str(exc)) from None
        return Response(json.dumps({k:json.loads(v) if k.endswith('_json') and v else v for k,v in item.items()},ensure_ascii=False),
                        media_type='application/json',headers={'Content-Disposition':'attachment; filename="experiment.json"'})

    @app.get('/sources')
    def sources_page(request: Request):
        authenticated(request, owner=True)
        return render(request, 'sources.html', sources=sources.list(), jobs=sources.history(),
                      handoffs=sources.handoffs(), error=None)

    @app.post('/sources')
    async def source_save(request: Request):
        authenticated(request, owner=True)
        data = await form(request)
        csrf(request, data)
        try:
            source_id = int(data['source_id']) if data.get('source_id') else None
            await run_in_threadpool(sources.save, data.get('kind'),data.get('name',''),data.get('address',''),
                json.loads(data.get('parameters','{}')),data.get('enabled')=='1',data.get('schedule_enabled')=='1',
                int(data.get('interval_seconds','3600')),source_id,int(data['revision']) if source_id else None)
        except (ValueError, OSError) as exc:
            return render(request, 'sources.html', status=400, sources=sources.list(),jobs=sources.history(),
                          handoffs=sources.handoffs(),error=str(exc))
        return RedirectResponse('/sources', status_code=303)

    @app.post('/sources/{source_id}/collect')
    async def source_collect(request: Request, source_id: int):
        session = authenticated(request, owner=True)
        data = await form(request)
        csrf(request, data)
        try:
            await run_in_threadpool(sources.enqueue,source_id,data.get('mode','new'),'web:'+str(session['user_id']))
        except (ValueError,OSError) as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sources',status_code=303)

    @app.post('/collection/{identity}/retry')
    async def collection_retry(request: Request, identity: str):
        authenticated(request, owner=True)
        data = await form(request)
        csrf(request, data)
        try:
            await run_in_threadpool(sources.retry,identity)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sources',status_code=303)

    @app.post('/collection/dispatch')
    async def collection_dispatch(request: Request):
        authenticated(request, owner=True)
        data = await form(request)
        csrf(request, data)
        # Retry the durable handoff only; no ML or collection runs in this request.
        await run_in_threadpool(sources.dispatch,force=True)
        return RedirectResponse('/sources',status_code=303)

    def review_actor(request):
        session = authenticated(request, owner=True)
        return Actor('web:'+str(session['user_id']), session['role'])

    def analytics_conditions(params):
        return analytics_filters(params.get('start'), params.get('until'), params.getlist('sources'), params.get('post_type') or None)

    def analytics_query(result):
        values = {k: v for k,v in result['inputs']['filters'].items() if v is not None}
        values['result_set_id'] = result['inputs']['result_set']['result_set_id']
        return urlencode(values, doseq=True)

    def analytics_result(request, slice_id=None):
        actor = review_actor(request)
        try:
            return analytics.load(slice_id,actor) if slice_id else analytics.report(
                analytics_conditions(request.query_params),actor,request.query_params.get('result_set_id'))
        except ValueError as error:
            raise HTTPException(400,str(error)) from None

    def analytics_page(request, result):
        actor = review_actor(request)
        prefix = '/analytics/slices/'+result['slice_id'] if result['slice_id'] else '/analytics'
        query = analytics_query(result)
        try:
            offset = int(request.query_params.get('offset','0'))
            if offset<0:
                raise ValueError()
        except ValueError:
            raise HTTPException(400,'Некорректная страница.') from None
        classes = {c['class_id']: c for c in result['inputs']['class_definitions']}
        return render(request,'analytics.html',result=result,options=analytics.options(actor),prefix=prefix,query=query,
                      classes=classes,offset=offset,display_manifest={k:v for k,v in result['manifest'].items() if k!='documents'})

    @app.get('/analytics')
    def analytics_live(request: Request):
        return analytics_page(request,analytics_result(request))

    @app.post('/analytics/slices')
    async def analytics_save(request: Request):
        actor=review_actor(request)
        data=await form(request,repeated=('sources',))
        csrf(request,data)
        try:
            conditions=analytics_filters(data.get('start'),data.get('until'),data.get('sources',[]),data.get('post_type') or None)
            identity=await run_in_threadpool(analytics.save,conditions,actor,data.get('result_set_id'))
        except ValueError as error:
            raise HTTPException(400,str(error)) from None
        return RedirectResponse('/analytics/slices/'+identity,status_code=303)

    @app.get('/analytics/slices/{slice_id}')
    def analytics_saved(request: Request,slice_id: str):
        return analytics_page(request,analytics_result(request,slice_id))

    def analytics_export_response(request,result,report):
        try:
            data=analytics.export(result,review_actor(request),report)
        except ValueError as error:
            raise HTTPException(400,str(error)) from None
        return Response(data,media_type='application/zip',headers={'Content-Disposition':'attachment; filename="analytics.zip"'})

    @app.get('/analytics/export')
    def analytics_export(request: Request,report: str | None=None):
        return analytics_export_response(request,analytics_result(request),report)

    @app.get('/analytics/slices/{slice_id}/export')
    def analytics_saved_export(request: Request,slice_id: str,report: str | None=None):
        return analytics_export_response(request,analytics_result(request,slice_id),report)

    def analytics_documents_page(request,result,report,row):
        try:
            data=result['reports'][report]
            if row=='excluded':
                ids=data['excluded_document_ids']
            elif row=='unknown_units' and report=='salary':
                ids=data['unknown_units_document_ids']
            else:
                index=int(row)
                if index<0:
                    raise ValueError()
                ids=data['rows'][index]['document_ids']
            offset=int(request.query_params.get('offset','0'))
            if offset<0:
                raise ValueError()
        except (KeyError,IndexError,ValueError):
            raise HTTPException(400,'Показатель не найден.') from None
        prefix='/analytics/slices/'+result['slice_id'] if result['slice_id'] else '/analytics'
        return render(request,'analytics_documents.html',result=result,ids=ids,offset=offset,
                      prefix=prefix,query=analytics_query(result),report=data)

    @app.get('/analytics/documents/{report}/{row}')
    def analytics_documents(request: Request,report: str,row: str):
        return analytics_documents_page(request,analytics_result(request),report,row)

    @app.get('/analytics/slices/{slice_id}/documents/{report}/{row}')
    def analytics_saved_documents(request: Request,slice_id: str,report: str,row: str):
        return analytics_documents_page(request,analytics_result(request,slice_id),report,row)

    @app.get('/analytics/slices/{slice_id}/document/{content_id}')
    def analytics_saved_document(request: Request,slice_id: str,content_id: int):
        result=analytics_result(request,slice_id)
        try:
            document=analytics.document(result,content_id,review_actor(request))
        except ValueError as error:
            raise HTTPException(404,str(error)) from None
        return render(request,'analytics_document.html',result=result,document=document)

    @app.get('/review')
    def review_queue(request: Request, reason: str = 'audit'):
        actor = review_actor(request)
        try:
            rows = review.queue(reason, actor)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from None
        return render(request, 'review_queue.html', rows=rows, reason=reason)

    @app.post('/review/audit')
    async def review_audit(request: Request):
        actor = review_actor(request)
        data = await form(request)
        csrf(request,data)
        await run_in_threadpool(review.audit,actor)
        return RedirectResponse('/review?reason=audit',status_code=303)

    @app.get('/review/{content_id}')
    def review_document(request: Request, content_id: int):
        actor = review_actor(request)
        try:
            document = review.get(content_id,actor)
        except ValueError as exc:
            raise HTTPException(404,str(exc)) from None
        return render(request, 'review_document.html', document=document, roles=ROLES, conditions=CONDITIONS,
                      salary_fields=SALARY, saved=request.query_params.get('saved')=='1')

    @app.post('/review/{content_id}')
    async def review_save(request: Request, content_id: int):
        actor = review_actor(request)
        repeated=('occupations','skill_id','skill_role','salary_from','salary_to','currency','salary_period','tax_mode','term_id','term_role')
        data = await form(request,repeated)
        csrf(request,data)
        try:
            skills = data.get('skill_id',[])
            roles = data.get('skill_role',[])
            terms = data.get('term_id',[])
            term_roles = data.get('term_role',[])
            salary_values = [data.get(field,[]) for field in SALARY]
            if len(skills)!=len(roles) or len(terms)!=len(term_roles) or len({len(v) for v in salary_values})!=1:
                raise ValueError('Повреждённые строки формы.')
            salaries = []
            for values in zip(*salary_values):
                if not any(v.strip() for v in values):
                    continue
                salary = {k:v.strip() or None for k,v in zip(SALARY,values)}
                for key in SALARY[:2]:
                    salary[key] = float(salary[key]) if salary[key] is not None else None
                salaries.append(salary)
            annotation = {'post_type':data.get('post_type') or None,
                'occupations':[int(v) for v in data.get('occupations',[]) if v],
                'skills':[{'class_id':int(i),'role':r} for i,r in zip(skills,roles) if i],
                'salary':salaries,'conditions':{},'multiple_offers':data.get('multiple_offers')=='1',
                'pending_terms':[{'term_id':i,'role':r} for i,r in zip(terms,term_roles) if i],
                'comment':data.get('comment','')}
            for field in CONDITIONS:
                values = [v.strip() for v in data.get('condition_'+field,'').split(';') if v.strip()]
                annotation['conditions'][field]=[int(v) for v in values] if field=='hours_per_week' else values
            await run_in_threadpool(review.save,content_id,int(data['revision']),data.get('status'),annotation,actor,data.get('active',''))
        except (ValueError,KeyError,TypeError) as exc:
            raise HTTPException(409 if 'Ревизия' in str(exc) or 'каталог изменился' in str(exc) else 400,str(exc)) from None
        return RedirectResponse('/review/'+str(content_id)+'?saved=1',status_code=303)

    @app.post('/review/{content_id}/terms')
    async def term_propose(request: Request, content_id: int):
        actor = review_actor(request)
        data = await form(request)
        csrf(request,data)
        try:
            await run_in_threadpool(review.propose,content_id,data.get('kind'),data.get('code'),data.get('name'),data.get('definition'),actor)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/review/'+str(content_id),status_code=303)

    @app.post('/review/terms/{term_id}/approve')
    async def term_approve(request: Request, term_id: str):
        actor = review_actor(request)
        data = await form(request)
        csrf(request,data)
        try:
            await run_in_threadpool(review.approve,term_id,actor)
            content_id = await run_in_threadpool(review.term_document,term_id,actor)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/review/'+str(content_id),status_code=303)

    return app
