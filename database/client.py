"""Customer-facing operations; caller identity is always a Telegram user ID."""
import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb
from database.connection import connect
from database.repository import _owner, _positive_id
from database.search import normalize, _query

REQUIRED = ('terms', 'consent', 'privacy')
DOCUMENT_LABELS = {'terms': 'Условия использования', 'privacy': 'Политика обработки ПД',
                   'consent': 'Согласие на обработку ПД', 'offer': 'Оферта'}


def _url(value):
    value = value.strip()
    if not value:
        return None
    parsed = urlsplit(value)
    if len(value) > 1000 or parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or any(c.isspace() for c in value):
        raise ValueError('Укажите корректную HTTPS-ссылку.')
    return value


def _documents(con, user):
    return con.execute('''SELECT d.*, (a.document_id IS NOT NULL) AS accepted
        FROM current_product_documents c JOIN product_documents d ON d.id=c.document_id
        LEFT JOIN product_document_acceptances a ON a.document_id=d.id AND a.telegram_id=%s''', (user,)).fetchall()


def _ready(con, user):
    client = con.execute('SELECT * FROM clients WHERE telegram_id=%s', (user,)).fetchone()
    if not client or client['status'] != 'active':
        raise PermissionError('Доступ ограничен. Обратитесь в поддержку.')
    docs = {d['kind']: d for d in _documents(con,user)}
    if any(kind not in docs for kind in REQUIRED):
        raise ValueError('Документы ещё не настроены. Обратитесь в поддержку.')
    if any(not docs[kind]['accepted'] for kind in ('terms','consent')):
        raise PermissionError('Сначала ознакомьтесь с документами и подтвердите согласие.')
    return client


class ClientRepository:
    def register(self, user):
        _positive_id(user)
        with connect() as con:
            con.execute('INSERT INTO clients(telegram_id) VALUES (%s) ON CONFLICT DO NOTHING', (user,))
            con.execute('INSERT INTO client_interface_state(telegram_id) VALUES (%s) ON CONFLICT DO NOTHING', (user,))

    def documents(self, user):
        with connect() as con:
            return _documents(con,user)

    def ready(self, user):
        with connect() as con:
            _ready(con,user)

    def accept(self, user, document_id):
        with connect() as con:
            client = con.execute('SELECT status FROM clients WHERE telegram_id=%s', (user,)).fetchone()
            if not client or client['status'] != 'active':
                raise PermissionError('Доступ ограничен.')
            doc = con.execute('''SELECT d.kind FROM current_product_documents c
              JOIN product_documents d ON d.id=c.document_id WHERE d.id=%s''', (document_id,)).fetchone()
            if not doc or doc['kind'] not in ('terms','consent'):
                raise ValueError('Редакция документа изменилась. Откройте документы снова.')
            con.execute('INSERT INTO product_document_acceptances(telegram_id,document_id) VALUES (%s,%s) ON CONFLICT DO NOTHING', (user,document_id))

    def state(self, user):
        with connect() as con:
            _ready(con,user)
            return con.execute('SELECT * FROM client_interface_state WHERE telegram_id=%s', (user,)).fetchone()

    def draft(self, user, value, *, awaiting=None, name=None):
        value = normalize(value)
        if awaiting not in (None,'occupation','skill','salary','name'):
            raise ValueError('Неизвестный шаг.')
        if name is not None and (not name.strip() or len(name)>80):
            raise ValueError('Название должно содержать от 1 до 80 символов.')
        with connect() as con:
            _ready(con,user)
            con.execute('''UPDATE client_interface_state SET draft_filter=%s,awaiting=%s,
              draft_name=COALESCE(%s,draft_name),updated_at=now() WHERE telegram_id=%s''',
              (Jsonb(value),awaiting,name.strip() if name else None,user))

    def save_filter(self, user):
        with connect() as con:
            _ready(con,user)
            state = con.execute('SELECT * FROM client_interface_state WHERE telegram_id=%s FOR UPDATE', (user,)).fetchone()
            identity = uuid.uuid4()
            con.execute('INSERT INTO saved_filters(id,telegram_id,name,filter_json) VALUES (%s,%s,%s,%s)',
                        (identity,user,state['draft_name'],Jsonb(normalize(state['draft_filter']))))
            con.execute('UPDATE client_interface_state SET active_filter_id=%s,editing_filter_id=%s,awaiting=NULL,wizard_step=NULL WHERE telegram_id=%s', (identity,identity,user))
            return identity

    def new_draft(self, user):
        with connect() as con:
            _ready(con,user)
            con.execute("UPDATE client_interface_state SET editing_filter_id=NULL,draft_filter='{}'::jsonb,draft_name='Мой поиск',awaiting=NULL,wizard_step=0 WHERE telegram_id=%s", (user,))

    def wizard_step(self, user, step):
        if step is not None and (type(step) is not int or step not in range(5)):
            raise ValueError('Неизвестный шаг.')
        with connect() as con:
            _ready(con,user)
            con.execute('UPDATE client_interface_state SET wizard_step=%s,awaiting=NULL,updated_at=now() WHERE telegram_id=%s', (step,user))

    def update_filter(self, user):
        with connect() as con:
            _ready(con,user)
            state = con.execute('SELECT * FROM client_interface_state WHERE telegram_id=%s FOR UPDATE', (user,)).fetchone()
            if not state['editing_filter_id']:
                raise ValueError('Сначала создайте фильтр.')
            row = con.execute('''UPDATE saved_filters SET name=%s,filter_json=%s,updated_at=now()
                WHERE id=%s AND telegram_id=%s AND deleted_at IS NULL RETURNING id''',
                (state['draft_name'],Jsonb(normalize(state['draft_filter'])),state['editing_filter_id'],user)).fetchone()
            if not row:
                raise ValueError('Фильтр не найден.')

    def class_names(self, ids):
        if not ids:
            return []
        with connect() as con:
            return con.execute('SELECT id,name,namespace FROM classes WHERE id=ANY(%s) ORDER BY name,id', (ids,)).fetchall()

    def class_by_id(self, kind, identity):
        if kind not in ('skill','occupation') or type(identity) is not int or identity <= 0:
            raise ValueError('Некорректный выбор.')
        with connect() as con:
            row = con.execute('SELECT id,name FROM classes WHERE id=%s AND namespace LIKE %s', (identity,kind+':%')).fetchone()
            if not row:
                raise ValueError('Значение больше недоступно.')
            return row

    def filters(self, user):
        with connect() as con:
            _ready(con,user)
            return con.execute('SELECT id,name,filter_json FROM saved_filters WHERE telegram_id=%s AND deleted_at IS NULL ORDER BY updated_at DESC,id DESC LIMIT 30', (user,)).fetchall()

    def select_filter(self, user, identity):
        identity = uuid.UUID(str(identity))
        with connect() as con:
            _ready(con,user)
            row = con.execute('SELECT * FROM saved_filters WHERE id=%s AND telegram_id=%s AND deleted_at IS NULL', (identity,user)).fetchone()
            if not row:
                raise ValueError('Фильтр не найден.')
            con.execute('''UPDATE client_interface_state SET active_filter_id=%s,editing_filter_id=%s,draft_filter=%s,
                draft_name=%s,awaiting=NULL,wizard_step=NULL,updated_at=now() WHERE telegram_id=%s''',
                (identity,identity,Jsonb(normalize(row['filter_json'])),row['name'],user))

    def delete_filter(self, user, identity):
        identity = uuid.UUID(str(identity))
        with connect() as con:
            _ready(con,user)
            row = con.execute('''UPDATE saved_filters SET deleted_at=now(),updated_at=now()
              WHERE id=%s AND telegram_id=%s AND deleted_at IS NULL RETURNING id''', (identity,user)).fetchone()
            if not row:
                raise ValueError('Фильтр не найден.')
            con.execute('''UPDATE client_interface_state SET
              active_filter_id=CASE WHEN active_filter_id=%s THEN NULL ELSE active_filter_id END,
              editing_filter_id=CASE WHEN editing_filter_id=%s THEN NULL ELSE editing_filter_id END
              WHERE telegram_id=%s''', (identity,identity,user))

    def can_issue(self, actor):
        _positive_id(actor)
        with connect() as con:
            return bool(con.execute("SELECT 1 FROM staff WHERE telegram_id=%s AND active AND role IN ('owner','manager')", (actor,)).fetchone())

    def create_activation(self, actor, days):
        if type(days) is not int or not 1 <= days <= 3650:
            raise ValueError('Срок должен быть от 1 до 3650 дней.')
        token = secrets.token_urlsafe(24)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        identity = uuid.uuid4()
        with connect() as con:
            staff = con.execute("SELECT 1 FROM staff WHERE telegram_id=%s AND active AND role IN ('owner','manager')", (actor,)).fetchone()
            if not staff:
                raise PermissionError('Доступ ограничен.')
            con.execute("INSERT INTO access_grants(id,issued_by,duration_days,status) VALUES (%s,%s,%s,'pending')", (identity,actor,days))
            con.execute("INSERT INTO activation_tokens(token_hash,grant_id,expires_at) VALUES (%s,%s,now()+interval '24 hours')", (token_hash,identity))
            con.execute("INSERT INTO access_events(grant_id,actor_id,event_type) VALUES (%s,%s,'activation_prepared')", (identity,actor))
        return token

    def remember_activation(self, user, token):
        if not isinstance(token,str) or not 16 <= len(token) <= 48:
            raise ValueError('Ссылка активации недействительна.')
        digest = hashlib.sha256(token.encode()).hexdigest()
        with connect() as con:
            row = con.execute('''SELECT 1 FROM activation_tokens t JOIN access_grants g ON g.id=t.grant_id
                WHERE t.token_hash=%s AND t.consumed_at IS NULL AND t.expires_at>now() AND g.status='pending' ''', (digest,)).fetchone()
            if not row:
                raise ValueError('Ссылка истекла или уже использована. Свяжитесь с менеджером.')
            con.execute('UPDATE client_interface_state SET pending_token_hash=%s WHERE telegram_id=%s', (digest,user))

    def redeem_activation(self, user):
        with connect() as con:
            con.execute('SELECT telegram_id FROM clients WHERE telegram_id=%s FOR UPDATE', (user,)).fetchone()
            client = _ready(con,user)
            state = con.execute('SELECT pending_token_hash FROM client_interface_state WHERE telegram_id=%s FOR UPDATE', (user,)).fetchone()
            if not state or not state['pending_token_hash']:
                raise ValueError('Ссылка активации не найдена. Свяжитесь с менеджером.')
            token = con.execute('''SELECT t.*,g.issued_by,g.duration_days,g.status FROM activation_tokens t
                JOIN access_grants g ON g.id=t.grant_id WHERE t.token_hash=%s FOR UPDATE OF t,g''', (state['pending_token_hash'],)).fetchone()
            now = datetime.now(timezone.utc)
            if not token or token['consumed_at'] or token['expires_at'] <= now or token['status'] != 'pending':
                raise ValueError('Ссылка истекла или уже использована. Свяжитесь с менеджером.')
            if not con.execute("SELECT 1 FROM staff WHERE telegram_id=%s AND active AND role IN ('owner','manager')", (token['issued_by'],)).fetchone():
                raise ValueError('Выдача больше не действует. Свяжитесь с менеджером.')
            previous = client['access_until']
            start = max(now,previous) if previous else now
            end = start + timedelta(days=token['duration_days'])
            con.execute("UPDATE access_grants SET recipient_id=%s,status='active',activated_at=%s,starts_at=%s,ends_at=%s WHERE id=%s", (user,now,start,end,token['grant_id']))
            con.execute('UPDATE activation_tokens SET consumed_at=%s,consumed_by=%s WHERE token_hash=%s', (now,user,state['pending_token_hash']))
            con.execute('UPDATE client_interface_state SET pending_token_hash=NULL WHERE telegram_id=%s', (user,))
            con.execute('UPDATE clients SET access_until=%s,revision=revision+1,updated_at=now() WHERE telegram_id=%s', (end,user))
            con.execute("INSERT INTO access_events(grant_id,recipient_id,actor_id,event_type,details) VALUES (%s,%s,%s,'activated',%s)",
                (token['grant_id'],user,user,Jsonb({'previous_until':previous.isoformat() if previous else None,'resulting_until':end.isoformat(),'issued_by':token['issued_by']})))
            return end

    def status(self, user, owner=False):
        with connect() as con:
            client = _ready(con,user)
            used = con.execute("SELECT count(*) AS n FROM selections WHERE telegram_id=%s AND trial AND status='delivered'", (user,)).fetchone()['n']
            paid = con.execute('SELECT %s::timestamptz > now() AS paid', (client['access_until'],)).fetchone()['paid'] or False
            return {'used':used, 'remaining':max(0,3-used), 'paid':paid, 'manual':client['manual_access'] or owner, 'until':client['access_until']}

    def prepare_selection(self, user, owner=False, request_message_id=None):
        with connect() as con:
            con.execute('SELECT telegram_id FROM clients WHERE telegram_id=%s FOR UPDATE', (user,)).fetchone()
            client = _ready(con,user)
            if request_message_id is not None:
                previous = con.execute('''SELECT id,status FROM selections WHERE telegram_id=%s AND request_message_id=%s
                  AND status <> 'failed' ''', (user,request_message_id)).fetchone()
                if previous and previous['status'] == 'delivered':
                    return {'existing_id':previous['id']}
            pending = con.execute("SELECT status FROM selections WHERE telegram_id=%s AND status IN ('pending','uncertain')", (user,)).fetchone()
            if pending:
                raise ValueError('Предыдущая выдача ещё обрабатывается. Обратитесь в поддержку, если ожидание затянулось.')
            used = con.execute("SELECT count(*) AS n FROM selections WHERE telegram_id=%s AND trial AND status='delivered'", (user,)).fetchone()['n']
            paid = con.execute('SELECT %s::timestamptz > now() AS paid', (client['access_until'],)).fetchone()['paid'] or False
            trial = not (paid or client['manual_access'] or owner)
            if trial and used >= 3:
                raise PermissionError('Бесплатные подборки закончились. Посмотрите стоимость доступа.')
            state = con.execute('SELECT * FROM client_interface_state WHERE telegram_id=%s', (user,)).fetchone()
            if not state or not state['active_filter_id']:
                raise ValueError('Сначала сохраните настройки поиска.')
            chosen = con.execute('SELECT filter_json FROM saved_filters WHERE id=%s AND telegram_id=%s AND deleted_at IS NULL', (state['active_filter_id'],user)).fetchone()
            if not chosen:
                raise ValueError('Выберите сохранённый фильтр.')
            filters = normalize(chosen['filter_json'])
            active = con.execute('SELECT result_set_id FROM active_result_set WHERE singleton=true').fetchone()
            if not active:
                return None
            limit = con.execute('SELECT selection_limit FROM app_settings WHERE singleton=true').fetchone()['selection_limit']
            rows, _ = _query(con,active['result_set_id'],filters,0,limit,exclude_user=user)
            if not rows:
                return None
            identity = uuid.uuid4()
            con.execute("INSERT INTO selections(id,telegram_id,result_set_id,filter_json,trial,status,request_message_id) VALUES (%s,%s,%s,%s,%s,'pending',%s)", (identity,user,active['result_set_id'],Jsonb(filters),trial,request_message_id))
            for ordinal, row in enumerate(rows,1):
                con.execute('INSERT INTO selection_items(selection_id,ordinal,result_set_id,content_id) VALUES (%s,%s,%s,%s)', (identity,ordinal,active['result_set_id'],row['id']))
            return {'id':identity,'items':rows,'trial':trial,'number':used+1 if trial else None}

    def finish_selection(self, user, identity, status, message_id=None):
        if status not in ('delivered','failed','uncertain'):
            raise ValueError('Некорректный результат доставки.')
        with connect() as con:
            changed = con.execute('''UPDATE selections SET status=%s,message_id=%s,
                delivered_at=CASE WHEN %s='delivered' THEN now() ELSE NULL END
                WHERE id=%s AND telegram_id=%s AND status='pending' RETURNING id''',
                (status,message_id,status,identity,user)).fetchone()
            if not changed:
                raise ValueError('Выдача уже обработана.')

    def item(self, user, identity, ordinal):
        with connect() as con:
            _ready(con,user)
            return con.execute('''SELECT c.id,c.text,r.salary_from,r.salary_to,r.currency,r.salary_period,
              r.work_format,cv.saved,cv.hidden,cv.applied,si.ordinal,s.id AS selection_id,
              (SELECT min(p.url) FROM result_publications rp JOIN publications p ON p.id=rp.publication_id
               WHERE rp.result_set_id=si.result_set_id AND rp.content_id=si.content_id) AS source_url
              FROM selections s JOIN selection_items si ON si.selection_id=s.id
              JOIN contents c ON c.id=si.content_id JOIN document_results r ON r.result_set_id=si.result_set_id AND r.content_id=si.content_id
              LEFT JOIN client_vacancies cv ON cv.telegram_id=s.telegram_id AND cv.content_id=c.id
              WHERE s.id=%s AND s.telegram_id=%s AND s.status='delivered' AND si.ordinal=%s''', (identity,user,ordinal)).fetchone()

    def selection(self, user, identity):
        with connect() as con:
            _ready(con,user)
            return con.execute('''SELECT si.ordinal,c.text FROM selections s
              JOIN selection_items si ON si.selection_id=s.id JOIN contents c ON c.id=si.content_id
              WHERE s.id=%s AND s.telegram_id=%s AND s.status='delivered' ORDER BY si.ordinal''', (identity,user)).fetchall()

    def mark(self, user, identity, ordinal, action, reason=None):
        if action not in ('save','unsave','hide','applied'):
            raise ValueError('Неизвестное действие.')
        if action == 'hide' and reason not in ('specialty','salary','experience','format','other'):
            raise ValueError('Укажите причину.')
        with connect() as con:
            _ready(con,user)
            row = con.execute('''SELECT si.content_id FROM selection_items si JOIN selections s ON s.id=si.selection_id
              WHERE s.id=%s AND s.telegram_id=%s AND s.status='delivered' AND si.ordinal=%s''', (identity,user,ordinal)).fetchone()
            if not row:
                raise ValueError('Вакансия не найдена.')
            con.execute('INSERT INTO client_vacancies(telegram_id,content_id) VALUES (%s,%s) ON CONFLICT DO NOTHING', (user,row['content_id']))
            column = {'save':'saved','unsave':'saved','hide':'hidden','applied':'applied'}[action]
            con.execute(f'UPDATE client_vacancies SET {column}=%s,reason=COALESCE(%s,reason),updated_at=now() WHERE telegram_id=%s AND content_id=%s', (action != 'unsave',reason,user,row['content_id']))

    def saved(self, user, page=0, page_size=10):
        if type(page) is not int or page < 0 or page > 1000000 or type(page_size) is not int or not 1 <= page_size <= 30:
            raise ValueError('Некорректная страница сохранённых вакансий.')
        with connect() as con:
            _ready(con,user)
            return con.execute('''SELECT c.id,c.text,selected.selection_id,selected.ordinal
              FROM client_vacancies cv JOIN contents c ON c.id=cv.content_id
              JOIN LATERAL (
                SELECT s.id AS selection_id,si.ordinal FROM selection_items si
                JOIN selections s ON s.id=si.selection_id
                WHERE si.content_id=c.id AND s.telegram_id=cv.telegram_id AND s.status='delivered'
                ORDER BY s.created_at DESC,s.id DESC LIMIT 1
              ) selected ON true
              WHERE cv.telegram_id=%s AND cv.saved ORDER BY cv.updated_at DESC,c.id DESC LIMIT %s OFFSET %s''',
              (user,page_size+1,page*page_size)).fetchall()

    def settings(self):
        with connect() as con:
            return con.execute('SELECT manager_url,selection_limit,pricing_text,services_text,services_url FROM app_settings WHERE singleton=true').fetchone()

    def save_product_settings(self, actor, limit, pricing, services=None, services_url=None):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('Размер подборки должен быть от 1 до 20.')
        if len(pricing)>2000 or (services is not None and len(services)>2000):
            raise ValueError('Слишком длинный текст.')
        if services_url is not None:
            services_url = _url(services_url)
        with connect() as con:
            _owner(con,actor)
            old = con.execute('SELECT selection_limit,pricing_text,services_text,services_url FROM app_settings WHERE singleton=true FOR UPDATE').fetchone()
            new = {'selection_limit':limit,'pricing_text':pricing.strip() or None,
                   'services_text':old['services_text'] if services is None else services.strip() or None,
                   'services_url':old['services_url'] if services_url is None else services_url}
            con.execute('''UPDATE app_settings SET selection_limit=%s,pricing_text=%s,services_text=%s,services_url=%s,
              revision=revision+1,updated_at=now(),updated_by=%s WHERE singleton=true''', (limit,new['pricing_text'],new['services_text'],new['services_url'],actor))
            con.execute('INSERT INTO settings_history(actor_id,previous_values,new_values) VALUES (%s,%s,%s)', (actor,Jsonb(old),Jsonb(new)))

    def publish_document(self, actor, kind, version, url, body):
        if kind not in DOCUMENT_LABELS or not version.strip() or len(version)>80 or not body.strip() or len(body)>200000:
            raise ValueError('Укажите вид, версию и полный текст документа.')
        url = _url(url)
        if not url:
            raise ValueError('Укажите ссылку на документ.')
        digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
        with connect() as con:
            _owner(con,actor)
            existing = con.execute('SELECT * FROM product_documents WHERE kind=%s AND version=%s', (kind,version.strip())).fetchone()
            if existing:
                if existing['body_sha256'] != digest or existing['url'] != url:
                    raise ValueError('Для изменённого документа укажите новую версию.')
                identity = existing['id']
            else:
                identity = con.execute('''INSERT INTO product_documents(kind,version,url,body,body_sha256,author_id)
                  VALUES (%s,%s,%s,%s,%s,%s) RETURNING id''', (kind,version.strip(),url,body,digest,actor)).fetchone()['id']
            con.execute('''INSERT INTO current_product_documents(kind,document_id) VALUES (%s,%s)
              ON CONFLICT(kind) DO UPDATE SET document_id=excluded.document_id''', (kind,identity))
