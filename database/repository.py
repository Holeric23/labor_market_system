"""Product operations shared by the Telegram and Mini App adapters."""
from datetime import datetime, timedelta, timezone
import json
import re
import uuid
from urllib.parse import urlsplit

from database.connection import connect


def _positive_id(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError('Укажите положительный Telegram ID.')
    return value


def _owner(con, actor):
    _positive_id(actor)
    row = con.execute("SELECT role FROM staff WHERE telegram_id=%s AND active", (actor,)).fetchone()
    if not row or row['role'] != 'owner':
        raise PermissionError('Доступ ограничен.')


def _source_fields(kind, name, address, interval_seconds, enabled, schedule_enabled, parameters):
    if kind not in ('telegram','designhunters') or not isinstance(name,str) or not isinstance(address,str) or not name.strip() or not address.strip() or len(name)>160:
        raise ValueError('Укажите поддерживаемый источник, название и адрес.')
    if type(interval_seconds) is not int or not 60 <= interval_seconds <= 31536000:
        raise ValueError('Интервал должен быть от 60 до 31536000 секунд.')
    if type(enabled) is not bool or type(schedule_enabled) is not bool:
        raise ValueError('Некорректное состояние источника.')
    parameters = {} if parameters is None else parameters
    limits = {'batch_size':(1,200),'max_items':(1,100000),'overlap_days':(1,30),
              'overlap_limit':(1,5000),'timeout_ms':(1000,120000),'delay_seconds':(0,60)}
    if not isinstance(parameters,dict) or set(parameters)-set(limits):
        raise ValueError('Некорректные параметры парсера.')
    for key,value in parameters.items():
        low,high = limits[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError('Некорректный параметр парсера: '+key)
    parsed = urlsplit(address.strip())
    hostname = 't.me' if kind == 'telegram' else 'designhunters.ru'
    if len(address)>1000 or parsed.scheme != 'https' or parsed.netloc != hostname or parsed.query or parsed.fragment or any(c.isspace() for c in address):
        raise ValueError('Поддерживается только HTTPS-адрес источника '+hostname+'.')
    if kind == 'telegram':
        if not re.fullmatch(r'/[A-Za-z][A-Za-z0-9_]{3,31}/?',parsed.path):
            raise ValueError('Укажите адрес публичного Telegram-канала.')
        address = 'https://t.me/'+parsed.path.strip('/').lower()
    return name.strip(),address.strip(),parameters


def _source_identity(con, kind, address):
    key = address.rsplit('/',1)[-1] if kind == 'telegram' else address
    namespace = 174925604 if kind == 'telegram' else 174925605
    con.execute('SELECT pg_advisory_xact_lock(%s, hashtext(%s))', (namespace,key))
    if kind == 'telegram':
        return con.execute('''SELECT s.* FROM sources s JOIN telegram_sources t ON t.source_id=s.id
                              WHERE t.username=%s''', (key,)).fetchone()
    return con.execute('SELECT * FROM sources WHERE kind=%s AND address=%s', (kind,address)).fetchone()


def _source_event(con, actor, source, event, details):
    con.execute('INSERT INTO source_events(source_id,actor_id,event_type,details) VALUES (%s,%s,%s,%s::jsonb)',
                (source,actor,event,json.dumps(details,ensure_ascii=False)))


def _source_version(source_id, revision):
    if type(source_id) is not int or not 0 < source_id < 2**63 or type(revision) is not int or not 1 <= revision < 2**31:
        raise ValueError('Некорректный источник.')


def _insert_source(con, actor, kind, name, address, interval, enabled, scheduled, parameters):
    if _source_identity(con,kind,address):
        raise ValueError('Источник с таким адресом уже существует. Если он удалён, восстановите его из списка удалённых.')
    row = con.execute('''INSERT INTO sources(kind,name,address,enabled,interval_seconds,schedule_enabled,next_run_at,parameters)
      VALUES (%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN now() ELSE NULL END,%s::jsonb) RETURNING id''',
      (kind,name,address,enabled,interval,scheduled,scheduled,json.dumps(parameters))).fetchone()
    _source_event(con,actor,row['id'],'created',{'name':name,'address':address,'kind':kind})
    return row['id']


class Repository:
    def managers(self, actor):
        with connect() as con:
            _owner(con,actor)
            return con.execute("SELECT telegram_id,active,created_at FROM staff WHERE role='manager' ORDER BY telegram_id").fetchall()

    def set_manager(self, actor, user, enabled):
        _positive_id(user)
        if type(enabled) is not bool:
            raise ValueError('Некорректное состояние.')
        with connect() as con:
            _owner(con,actor)
            existing = con.execute('SELECT role FROM staff WHERE telegram_id=%s FOR UPDATE', (user,)).fetchone()
            if existing and existing['role'] == 'owner':
                raise ValueError('Нельзя изменить права владельца.')
            con.execute("INSERT INTO staff(telegram_id,role,active) VALUES (%s,'manager',%s) ON CONFLICT(telegram_id) DO UPDATE SET active=excluded.active", (user,enabled))
            con.execute("INSERT INTO access_events(actor_id,event_type,details) VALUES (%s,'manager_rights_changed',%s::jsonb)", (actor,json.dumps({'manager_id':user,'enabled':enabled})))

    def bootstrap_owners(self, ids):
        if not ids:
            raise ValueError('Нужен Telegram ID владельца.')
        for identity in ids:
            _positive_id(identity)
        with connect() as con:
            for identity in ids:
                con.execute("INSERT INTO staff(telegram_id,role) VALUES (%s,'owner') ON CONFLICT (telegram_id) DO UPDATE SET role='owner',active=true", (identity,))

    def authorize(self, telegram_id, configured_owners):
        _positive_id(telegram_id)
        if telegram_id in configured_owners:
            return 'owner'
        with connect() as con:
            row = con.execute('SELECT status,manual_access FROM clients WHERE telegram_id=%s', (telegram_id,)).fetchone()
        return 'client' if row and row['status'] == 'active' and row['manual_access'] else None

    def list_clients(self, actor):
        with connect() as con:
            _owner(con, actor)
            return con.execute('SELECT * FROM clients ORDER BY created_at DESC, telegram_id DESC').fetchall()

    def public_settings(self):
        with connect() as con:
            return con.execute('SELECT manager_url,default_access_days,revision FROM app_settings WHERE singleton=true').fetchone()

    def save_settings(self, actor, manager_url, default_days, revision):
        manager_url = manager_url.strip() or None
        if manager_url and not re.fullmatch(r'https://t\.me/[A-Za-z][A-Za-z0-9_]{4,31}',manager_url):
            raise ValueError('Нужна ссылка https://t.me/username менеджера.')
        if type(default_days) is not int or not 1 <= default_days <= 3650:
            raise ValueError('Срок должен быть от 1 до 3650 дней.')
        with connect() as con:
            _owner(con, actor)
            prior = con.execute('SELECT * FROM app_settings WHERE singleton=true FOR UPDATE').fetchone()
            if not prior or prior['revision'] != revision:
                raise ValueError('Настройки изменились; обновите страницу.')
            before = {'manager_url':prior['manager_url'],'default_access_days':prior['default_access_days']}
            after = {'manager_url':manager_url,'default_access_days':default_days}
            con.execute('UPDATE app_settings SET manager_url=%s,default_access_days=%s,revision=revision+1,updated_at=now(),updated_by=%s WHERE singleton=true', (manager_url,default_days,actor))
            con.execute('INSERT INTO settings_history(actor_id,previous_values,new_values) VALUES (%s,%s::jsonb,%s::jsonb)', (actor,json.dumps(before),json.dumps(after)))

    def client(self, actor, user):
        _positive_id(user)
        with connect() as con:
            _owner(con, actor)
            row = con.execute('SELECT * FROM clients WHERE telegram_id=%s', (user,)).fetchone()
            if not row:
                return None
            row['grants'] = con.execute('SELECT * FROM access_grants WHERE recipient_id=%s ORDER BY issued_at DESC', (user,)).fetchall()
            row['history'] = con.execute('SELECT * FROM access_events WHERE recipient_id=%s ORDER BY id DESC', (user,)).fetchall()
            return row

    def allow(self, actor, user, enabled):
        _positive_id(user)
        with connect() as con:
            _owner(con, actor)
            con.execute("INSERT INTO clients(telegram_id,status,manual_access) VALUES (%s,%s,%s) ON CONFLICT (telegram_id) DO UPDATE SET status=excluded.status,manual_access=excluded.manual_access,updated_at=now()", (user,'active' if enabled else 'blocked',enabled))
            con.execute('INSERT INTO access_events(recipient_id,actor_id,event_type) VALUES (%s,%s,%s)', (user,actor,'manual_allow' if enabled else 'manual_block'))

    def issue(self, actor, user, days, identity):
        _positive_id(user)
        if type(days) is not int or not 1 <= days <= 3650:
            raise ValueError('Срок должен быть от 1 до 3650 дней.')
        try:
            identity = uuid.UUID(str(identity))
        except (ValueError, TypeError):
            raise ValueError('Некорректный идентификатор операции.') from None
        with connect() as con:
            _owner(con, actor)
            con.execute('INSERT INTO clients(telegram_id) VALUES (%s) ON CONFLICT DO NOTHING', (user,))
            inserted = con.execute("INSERT INTO access_grants(id,issued_by,recipient_id,duration_days,status) VALUES (%s,%s,%s,%s,'pending') ON CONFLICT DO NOTHING RETURNING id", (identity,actor,user,days)).fetchone()
            if not inserted:
                existing = con.execute('SELECT issued_by,recipient_id,duration_days FROM access_grants WHERE id=%s', (identity,)).fetchone()
                if not existing or (existing['issued_by'],existing['recipient_id'],existing['duration_days']) != (actor,user,days):
                    raise ValueError('Идентификатор уже использован.')
                return identity
            con.execute("INSERT INTO access_events(grant_id,recipient_id,actor_id,event_type) VALUES (%s,%s,%s,'issued')", (identity,user,actor))
        return identity

    def activate(self, actor, user, identity):
        _positive_id(user)
        try:
            identity = uuid.UUID(str(identity))
        except (ValueError, TypeError):
            raise ValueError('Некорректная выдача.') from None
        with connect() as con:
            _owner(con, actor)
            # Lock client first so simultaneous different grants extend rather than overwrite.
            client = con.execute('SELECT * FROM clients WHERE telegram_id=%s FOR UPDATE', (user,)).fetchone()
            grant = con.execute('SELECT * FROM access_grants WHERE id=%s AND recipient_id=%s FOR UPDATE', (identity,user)).fetchone()
            if not client or not grant:
                raise ValueError('Выдача не найдена.')
            if grant['status'] == 'active':
                return False
            if grant['status'] != 'pending':
                raise ValueError('Выдача уже отменена.')
            instant = datetime.now(timezone.utc)
            previous = client['access_until']
            start = max(instant, previous) if previous else instant
            end = start + timedelta(days=grant['duration_days'])
            con.execute("UPDATE access_grants SET status='active', activated_at=%s, starts_at=%s, ends_at=%s WHERE id=%s", (instant,start,end,identity))
            con.execute('UPDATE clients SET access_until=%s, revision=revision+1, updated_at=now() WHERE telegram_id=%s', (end,user))
            con.execute("INSERT INTO access_events(grant_id,recipient_id,actor_id,event_type,details) VALUES (%s,%s,%s,'activated',%s::jsonb)", (identity,user,actor,json.dumps({'previous_until':previous.isoformat() if previous else None,'resulting_until':end.isoformat()})))
        return True

    def cancel(self, actor, user, identity):
        _positive_id(user)
        try:
            identity = uuid.UUID(str(identity))
        except (ValueError, TypeError):
            raise ValueError('Некорректная выдача.') from None
        with connect() as con:
            _owner(con, actor)
            row = con.execute("UPDATE access_grants SET status='cancelled' WHERE id=%s AND recipient_id=%s AND status='pending' RETURNING id", (identity,user)).fetchone()
            if not row:
                raise ValueError('Выдача не найдена или уже активирована.')
            con.execute("INSERT INTO access_events(grant_id,recipient_id,actor_id,event_type) VALUES (%s,%s,%s,'cancelled')", (identity,user,actor))

    def revoke(self, actor, user, revision):
        _positive_id(user)
        with connect() as con:
            _owner(con, actor)
            client = con.execute('SELECT * FROM clients WHERE telegram_id=%s FOR UPDATE', (user,)).fetchone()
            if not client or client['revision'] != revision:
                raise ValueError('Данные изменились; обновите страницу.')
            if not client['access_until'] or client['access_until'] <= datetime.now(timezone.utc):
                raise ValueError('Действующего доступа нет.')
            instant = datetime.now(timezone.utc)
            con.execute('UPDATE clients SET access_until=%s,revision=revision+1,updated_at=now() WHERE telegram_id=%s', (instant,user))
            con.execute("INSERT INTO access_events(recipient_id,actor_id,event_type,details) VALUES (%s,%s,'revoked',%s::jsonb)", (user,actor,json.dumps({'previous_until':client['access_until'].isoformat(),'resulting_until':instant.isoformat()})))

    def revert(self, actor, user, identity, revision):
        _positive_id(user)
        try:
            identity = uuid.UUID(str(identity))
        except (ValueError, TypeError):
            raise ValueError('Некорректная выдача.') from None
        with connect() as con:
            _owner(con, actor)
            client = con.execute('SELECT * FROM clients WHERE telegram_id=%s FOR UPDATE', (user,)).fetchone()
            last = con.execute("SELECT * FROM access_events WHERE recipient_id=%s AND event_type IN ('activated','revoked','reverted') ORDER BY id DESC LIMIT 1", (user,)).fetchone()
            if not client or client['revision'] != revision or not last or last['event_type'] != 'activated' or last['grant_id'] != identity:
                raise ValueError('Можно откатить только последнюю активацию; обновите страницу.')
            previous = last['details']['previous_until']
            previous = datetime.fromisoformat(previous) if previous else None
            changed = con.execute("UPDATE access_grants SET status='reverted' WHERE id=%s AND recipient_id=%s AND status='active' RETURNING id", (identity,user)).fetchone()
            if not changed:
                raise ValueError('Выдача уже изменилась.')
            con.execute('UPDATE clients SET access_until=%s,revision=revision+1,updated_at=now() WHERE telegram_id=%s', (previous,user))
            con.execute("INSERT INTO access_events(grant_id,recipient_id,actor_id,event_type,details) VALUES (%s,%s,%s,'reverted',%s::jsonb)", (identity,user,actor,json.dumps({'previous_until':client['access_until'].isoformat() if client['access_until'] else None,'resulting_until':previous.isoformat() if previous else None})))

    def list_sources(self, actor, *, archived=False):
        with connect() as con:
            _owner(con, actor)
            return con.execute('SELECT * FROM sources WHERE (deleted_at IS NOT NULL)=%s ORDER BY id', (archived,)).fetchall()

    def create_source(self, actor, kind, name, address, interval_seconds, enabled, schedule_enabled=False, parameters=None):
        name,address,parameters = _source_fields(kind,name,address,interval_seconds,enabled,schedule_enabled,parameters)
        with connect() as con:
            _owner(con,actor)
            return _insert_source(con,actor,kind,name,address,interval_seconds,enabled,schedule_enabled,parameters)

    def edit_source(self, actor, source_id, revision, kind, name, address, interval_seconds, enabled, schedule_enabled=False, parameters=None):
        name,address,parameters = _source_fields(kind,name,address,interval_seconds,enabled,schedule_enabled,parameters)
        _source_version(source_id,revision)
        with connect() as con:
            _owner(con,actor)
            old = con.execute('SELECT * FROM sources WHERE id=%s FOR UPDATE', (source_id,)).fetchone()
            if not old or old['deleted_at'] is not None:
                raise ValueError('Источник не найден или уже удалён.')
            if old['revision'] != revision:
                raise ValueError('Источник изменился; обновите страницу.')
            same_address = old['address'] == address
            if kind == 'telegram' and old['kind'] == kind:
                old_username = con.execute('SELECT username FROM telegram_sources WHERE source_id=%s', (source_id,)).fetchone()
                same_address = bool(old_username and old_username['username'] == address.rsplit('/',1)[-1])
            if old['kind'] != kind:
                raise ValueError('Тип источника изменить нельзя. Добавьте новый источник.')
            if not same_address:
                new_id = _insert_source(con,actor,kind,name,address,interval_seconds,enabled,schedule_enabled,parameters)
                con.execute('''UPDATE sources SET deleted_at=now(),enabled=false,schedule_enabled=false,
                  next_run_at=NULL,revision=revision+1 WHERE id=%s''', (source_id,))
                _source_event(con,actor,source_id,'replaced',{'replacement_source_id':new_id})
                return new_id
            con.execute('''UPDATE sources SET name=%s,enabled=%s,interval_seconds=%s,schedule_enabled=%s,
              parameters=%s::jsonb,next_run_at=CASE WHEN %s THEN COALESCE(next_run_at,now()) ELSE NULL END,
              revision=revision+1 WHERE id=%s''',
              (name,enabled,interval_seconds,schedule_enabled,json.dumps(parameters),schedule_enabled,source_id))
            _source_event(con,actor,source_id,'updated',{'previous':{'name':old['name'],'enabled':old['enabled'],
              'interval_seconds':old['interval_seconds'],'schedule_enabled':old['schedule_enabled'],'parameters':old['parameters']},
              'new':{'name':name,'enabled':enabled,'interval_seconds':interval_seconds,
              'schedule_enabled':schedule_enabled,'parameters':parameters}})
            return source_id

    def archive_source(self, actor, source_id, revision):
        _source_version(source_id,revision)
        with connect() as con:
            _owner(con,actor)
            row = con.execute('''UPDATE sources SET deleted_at=now(),enabled=false,schedule_enabled=false,
              next_run_at=NULL,revision=revision+1 WHERE id=%s AND revision=%s AND deleted_at IS NULL RETURNING id''',
              (source_id,revision)).fetchone()
            if not row:
                raise ValueError('Источник изменился или уже удалён; обновите страницу.')
            _source_event(con,actor,source_id,'deleted',{})

    def restore_source(self, actor, source_id, revision):
        _source_version(source_id,revision)
        with connect() as con:
            _owner(con,actor)
            row = con.execute('''UPDATE sources SET deleted_at=NULL,enabled=false,schedule_enabled=false,
              next_run_at=NULL,revision=revision+1 WHERE id=%s AND revision=%s AND deleted_at IS NOT NULL RETURNING id''',
              (source_id,revision)).fetchone()
            if not row:
                raise ValueError('Источник изменился или уже восстановлен; обновите страницу.')
            _source_event(con,actor,source_id,'restored',{})

    def activate_result_set(self, actor, result_set_id):
        try:
            result_set_id = uuid.UUID(str(result_set_id))
        except (ValueError, TypeError):
            raise ValueError('Некорректный набор результатов.') from None
        with connect() as con:
            _owner(con, actor)
            con.execute('SELECT pg_advisory_xact_lock(%s)', (174925603,))
            target = con.execute("SELECT id FROM result_sets WHERE id=%s AND state='ready'", (result_set_id,)).fetchone()
            if not target:
                raise ValueError('Готовый набор не найден.')
            previous = con.execute('SELECT result_set_id FROM active_result_set WHERE singleton=true FOR UPDATE').fetchone()
            if previous and previous['result_set_id'] == result_set_id:
                return False
            con.execute('''INSERT INTO active_result_set(singleton,result_set_id) VALUES (true,%s)
              ON CONFLICT(singleton) DO UPDATE SET result_set_id=excluded.result_set_id''', (result_set_id,))
            con.execute('INSERT INTO result_set_switches(previous_id,result_set_id,actor) VALUES (%s,%s,%s)', (previous['result_set_id'] if previous else None,result_set_id,'owner:'+str(actor)))
            return True

    def save_source(self, actor, kind, name, address, interval_seconds, enabled, schedule_enabled=False, parameters=None):
        name,address,parameters = _source_fields(kind,name,address,interval_seconds,enabled,schedule_enabled,parameters)
        with connect() as con:
            _owner(con, actor)
            if kind == 'telegram':
                username = address.rsplit('/',1)[-1]
                con.execute('SELECT pg_advisory_xact_lock(%s, hashtext(%s))', (174925604,username))
                existing = con.execute('''SELECT t.source_id,s.deleted_at FROM telegram_sources t
                  JOIN sources s ON s.id=t.source_id WHERE t.username=%s FOR UPDATE OF s''', (username,)).fetchone()
                if existing:
                    if existing['deleted_at'] is not None:
                        raise ValueError('Источник удалён. Восстановите его из списка удалённых.')
                    return con.execute('''UPDATE sources SET name=%s,enabled=%s,interval_seconds=%s,
                      schedule_enabled=%s,parameters=%s::jsonb,
                      next_run_at=CASE WHEN %s THEN COALESCE(next_run_at,now()) ELSE NULL END,
                      revision=revision+1 WHERE id=%s AND deleted_at IS NULL RETURNING id''',
                      (name.strip(),enabled,interval_seconds,schedule_enabled,json.dumps(parameters),schedule_enabled,existing['source_id'])).fetchone()['id']
            row = con.execute('''INSERT INTO sources(kind,name,address,enabled,interval_seconds,schedule_enabled,next_run_at,parameters)
              VALUES (%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN now() ELSE NULL END,%s::jsonb) ON CONFLICT(kind,address) DO UPDATE SET
              name=excluded.name, enabled=excluded.enabled, interval_seconds=excluded.interval_seconds,
              schedule_enabled=excluded.schedule_enabled, parameters=excluded.parameters,
              next_run_at=CASE WHEN excluded.schedule_enabled THEN COALESCE(sources.next_run_at,now()) ELSE NULL END,
              revision=sources.revision+1 WHERE sources.deleted_at IS NULL RETURNING id''', (kind,name,address,enabled,interval_seconds,schedule_enabled,schedule_enabled,json.dumps(parameters))).fetchone()
            if not row:
                raise ValueError('Источник удалён. Восстановите его из списка удалённых.')
            return row['id']

    def search_vacancies(self, *, limit=10, offset=0):
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 30 or offset < 0:
            raise ValueError('Некорректная страница поиска.')
        with connect() as con:
            return con.execute('''SELECT c.id,c.text,r.salary_from,r.salary_to,r.currency,r.salary_period,
                     r.work_format, max(rp.published_at) AS published_at, min(p.url) AS source_url
              FROM active_result_set a JOIN document_results r ON r.result_set_id=a.result_set_id
              JOIN contents c ON c.id=r.content_id
              LEFT JOIN result_publications rp ON rp.result_set_id=r.result_set_id AND rp.content_id=c.id
              LEFT JOIN publications p ON p.id=rp.publication_id
              WHERE r.post_type='vacancy'
              GROUP BY c.id,r.salary_from,r.salary_to,r.currency,r.salary_period,r.work_format
              ORDER BY max(rp.published_at) DESC NULLS LAST,c.id DESC LIMIT %s OFFSET %s''', (limit,offset)).fetchall()

    def find_classes(self, kind, query):
        if kind not in ('skill','occupation') or not isinstance(query,str) or len(query)>160:
            raise ValueError('Используйте /classes skill или /classes occupation.')
        with connect() as con:
            return con.execute('SELECT id,name,namespace FROM classes WHERE namespace LIKE %s AND strpos(lower(name),lower(%s))>0 ORDER BY id LIMIT 30', (kind+':%',query)).fetchall()
