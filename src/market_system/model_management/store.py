"""Immutable registry and OWNER actions sharing the catalog transaction boundary."""
import hashlib
import json
from pathlib import Path
import sqlite3

from market_system.analytics.store import Analytics
from market_system.jobs.store import specification
from market_system.paths import DATABASE_PATH
from market_system.review.store import owner
from market_system.search.store import connect, encode, now, add_class, activate
from .adapters import ADAPTER, verify, class_space


def active_spec(path):
    with connect(path) as con:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='model_registry'").fetchone():
            return None
        row = con.execute('''SELECT m.spec_json FROM model_registry m JOIN result_sets r USING(model_version)
          JOIN active_result_set a USING(result_set_id)''').fetchone()
        return json.loads(row[0]) if row else None


class Models:
    def __init__(self, path=DATABASE_PATH):
        self.analytics = Analytics(path)
        self.jobs = self.analytics.bot.review.jobs
        self.path = self.jobs.path

    def initialize(self):
        self.analytics.initialize()
        sql = Path(__file__).with_name('008_models.sql').read_text(encoding='utf-8')
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        with self.jobs.transaction() as con:
            old = con.execute('SELECT sha256 FROM schema_migrations WHERE version=8').fetchone()
            if old:
                if old[0] != checksum:
                    raise ValueError('Migration checksum changed')
                return
            statement = ''
            for line in sql.splitlines(True):
                statement += line
                if sqlite3.complete_statement(statement):
                    con.execute(statement)
                    statement = ''
            con.execute('INSERT INTO schema_migrations VALUES(8,?,?)',(checksum,now()))

    def register(self, actor, config=None, adapter=ADAPTER):
        owner(actor)
        if adapter != ADAPTER:
            from .adapters import IncompatibleArtifact
            raise IncompatibleArtifact('Непроверенный адаптер: '+adapter+'. Поддерживается только ensemble-v1.')
        try:
            spec = specification(config)
            spec.update(adapter=adapter, class_space=class_space(spec))
        except (KeyError, TypeError, OSError) as exc:
            from .adapters import IncompatibleArtifact
            raise IncompatibleArtifact('Неполный локальный комплект модели/словарей: '+str(exc)) from exc
        verify(spec)
        version = spec['model_version']
        scope = spec['model_manifest'].get('evaluation_scope','unknown historical sample')
        with self.jobs.transaction() as con:
            old = con.execute('SELECT spec_json FROM model_registry WHERE model_version=?',(version,)).fetchone()
            if old:
                if old[0] != encode(spec):
                    raise ValueError('Версия уже зарегистрирована с другими артефактами/определениями. Нужна новая версия модели.')
                return version
            active = con.execute('SELECT r.model_version FROM result_sets r JOIN active_result_set a USING(result_set_id)').fetchone()
            status = 'active' if active and active[0] == version else 'candidate'
            con.execute('INSERT INTO model_registry VALUES(?,?,?,?,?,?,?,?,?,?)',
                (version,adapter,encode(spec['model_manifest']),encode(spec),encode(spec['class_space']),scope,
                 encode({'packages':spec['model_manifest']['packages'],'historical_python':spec['model_manifest'].get('python')}),
                 status,actor.identity,now()))
            for item in spec['class_space']:
                cid = add_class(con,item['namespace'],item['key'],item['name'],item['version'])
                old_definition = con.execute('SELECT definition_json FROM model_class_definitions WHERE class_id=?',(cid,)).fetchone()
                if old_definition and old_definition[0] != encode(item['definition']):
                    raise ValueError('Смысл существующего класса изменён; нужен новый namespace.')
                con.execute('INSERT OR IGNORE INTO model_class_definitions VALUES(?,?)',(cid,encode(item['definition'])))
            con.execute('INSERT INTO model_events(model_version,action,actor,created_at) VALUES(?,?,?,?)',
                        (version,'register:'+status,actor.identity,now()))
        return version

    def get(self, version, actor):
        owner(actor)
        with connect(self.path) as con:
            row = con.execute('SELECT * FROM model_registry WHERE model_version=?',(version,)).fetchone()
            if not row:
                raise ValueError('Модель не зарегистрирована.')
            return dict(row)

    def list(self, actor):
        owner(actor)
        with connect(self.path) as con:
            return [dict(r) for r in con.execute('SELECT * FROM model_registry ORDER BY created_at DESC')]

    def status(self, version, value, actor):
        owner(actor)
        if value not in ('approved','retired'):
            raise ValueError('Допустимы approved или retired; active назначается только переключением ready-набора.')
        with self.jobs.transaction() as con:
            row = con.execute('SELECT status FROM model_registry WHERE model_version=?',(version,)).fetchone()
            if not row:
                raise ValueError('Модель не зарегистрирована.')
            if row[0] == 'active':
                raise ValueError('Сначала переключите рабочий каталог на другую модель.')
            con.execute('UPDATE model_registry SET status=? WHERE model_version=?',(value,version))
            con.execute('INSERT INTO model_events(model_version,action,actor,created_at) VALUES(?,?,?,?)',
                        (version,value,actor.identity,now()))

    def designate(self, version, actor, batch_size=64):
        owner(actor)
        with self.jobs.transaction() as con:
            row = con.execute('SELECT * FROM model_registry WHERE model_version=?',(version,)).fetchone()
            if not row or row['status'] == 'retired':
                raise ValueError('Кандидат отсутствует или retired; сначала явно одобрите его.')
            spec = json.loads(row['spec_json'])
            verify(spec)
            if row['status'] == 'candidate':
                con.execute("UPDATE model_registry SET status='approved' WHERE model_version=?",(version,))
            job = self.jobs.submit('full',batch_size=batch_size,actor=actor.identity,spec=spec,_connection=con)
            con.execute('INSERT INTO model_events(model_version,action,actor,created_at,job_id) VALUES(?,?,?,?,?)',
                        (version,'designate:full',actor.identity,now(),job))
            return job

    def publish(self, identity, actor):
        owner(actor)
        # activate verifies ready, overlays current confirmed corrections, reconciles
        # subscriptions and changes the pointer + registry status in one transaction.
        activate(self.path,identity)
        with connect(self.path) as con:
            return con.execute('SELECT result_set_id FROM active_result_set').fetchone()[0]

    def results(self, actor):
        owner(actor)
        with connect(self.path) as con:
            return [dict(r) for r in con.execute('''SELECT r.*,j.state job_state,j.error job_error,
                a.result_set_id IS NOT NULL is_active FROM result_sets r
                LEFT JOIN jobs j USING(result_set_id) LEFT JOIN active_result_set a USING(result_set_id)
                ORDER BY r.created_at DESC LIMIT 100''')]

    def experiments(self, actor, identity=None):
        owner(actor)
        with connect(self.path) as con:
            if identity:
                row = con.execute('SELECT * FROM model_experiments WHERE experiment_id=?',(identity,)).fetchone()
                if not row:
                    raise ValueError('Эксперимент не найден.')
                return dict(row)
            return [dict(r) for r in con.execute('SELECT experiment_id,model_version,kind,state,error,created_at FROM model_experiments ORDER BY created_at DESC LIMIT 100')]
