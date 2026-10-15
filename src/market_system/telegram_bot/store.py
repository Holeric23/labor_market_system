"""Persistent access and subscriptions in the same database as ready data/outbox."""
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from market_system.paths import DATABASE_PATH, local_path
from market_system.review.store import Review
from market_system.search.service import Catalog
from market_system.search.store import now, connect
from .catalog import parse_filter


class BotStore:
    def __init__(self,path=DATABASE_PATH):
        self.path=local_path(path)
        self.review=Review(path)
        self.transaction=self.review.jobs.transaction

    def initialize(self):
        self.review.initialize()
        sql=Path(__file__).with_name('006_delivery.sql').read_text(encoding='utf-8')
        checksum=hashlib.sha256(sql.encode()).hexdigest()
        with self.transaction() as con:
            prior=con.execute('SELECT sha256 FROM schema_migrations WHERE version=6').fetchone()
            if prior:
                if prior[0]!=checksum:
                    raise ValueError('Migration checksum changed')
                return
            statement=''
            for line in sql.splitlines(True):
                statement+=line
                if sqlite3.complete_statement(statement):
                    con.execute(statement);statement=''
            # Existing corpus is a baseline, never a backlog to broadcast.
            for row in con.execute('SELECT id,text FROM contents'):
                con.execute('INSERT OR IGNORE INTO telegram_seen VALUES(?,?,NULL,?)',
                            (hashlib.sha256(row['text'].encode()).hexdigest(),row['id'],now()))
            con.execute('INSERT INTO schema_migrations VALUES(6,?,?)',(checksum,now()))

    @staticmethod
    def access(con,user,owner=False):
        row=con.execute('SELECT role FROM telegram_access WHERE user_id=? AND enabled=1',(user,)).fetchone()
        if row is None or (owner and row[0]!='OWNER'):
            raise PermissionError('Доступ ограничен.')
        return row[0]

    def role(self,user):
        with connect(self.path) as con:
            return self.access(con,user)

    def bootstrap_owners(self,ids):
        # Local trusted configuration is the only way to create an OWNER.
        if not ids or any(type(i) is not int or i<=0 for i in ids):
            raise ValueError('Нужен положительный Telegram ID владельца.')
        with self.transaction() as con:
            for identity in ids:
                con.execute("INSERT INTO telegram_access VALUES(?,'OWNER',1,NULL,?) ON CONFLICT(user_id) DO UPDATE SET role='OWNER',enabled=1",(identity,now()))

    def grant(self,actor,user,enabled=True):
        if type(user) is not int or user<=0:
            raise ValueError('Нужен положительный Telegram ID.')
        with self.transaction() as con:
            self.access(con,actor,True)
            row=con.execute('SELECT role FROM telegram_access WHERE user_id=?',(user,)).fetchone()
            if row and row[0]=='OWNER':
                raise ValueError('Владельцы управляются локальной настройкой.')
            con.execute("INSERT INTO telegram_access VALUES(?,'USER',?,?,?) ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled,granted_by=excluded.granted_by",(user,int(enabled),actor,now()))

    def subscribe(self,user,value):
        value=parse_filter(value)
        if value['post_type']!='vacancy' or value['unknown'].get('post_type') in ('include','only'):
            raise ValueError('Уведомления первой версии — только вакансии. Укажите post_type: vacancy.')
        encoded=json.dumps(value,sort_keys=True,ensure_ascii=False)
        with self.transaction() as con:
            self.access(con,user)
            Catalog._validate_classes(con,value)
            row=con.execute('SELECT r.model_version FROM active_result_set a JOIN result_sets r USING(result_set_id)').fetchone()
            if not row:
                raise ValueError('Нет активного каталога.')
            prior=con.execute("SELECT id FROM telegram_subscriptions WHERE user_id=? AND filter_json=? AND model_version=? AND status='active'",(user,encoded,row[0])).fetchone()
            if prior:
                return prior[0]
            identity=str(uuid.uuid4())
            con.execute("INSERT INTO telegram_subscriptions VALUES(?,?,?,?,'active',NULL,?)",(identity,user,encoded,row[0],now()))
            return identity

    def subscriptions(self,user):
        with connect(self.path) as con:
            self.access(con,user)
            return [dict(r) for r in con.execute("SELECT * FROM telegram_subscriptions WHERE user_id=? AND status<>'deleted' ORDER BY created_at,id",(user,))]

    def delete(self,user,identity):
        with self.transaction() as con:
            self.access(con,user)
            return bool(con.execute("UPDATE telegram_subscriptions SET status='deleted' WHERE id=? AND user_id=? AND status<>'deleted'",(identity,user)).rowcount)

    def confirm_mapping(self,actor,old_id,new_id,model):
        with self.transaction() as con:
            self.access(con,actor,True)
            if not con.execute('SELECT 1 FROM result_sets WHERE model_version=?',(model,)).fetchone():
                raise ValueError('Неизвестная модель.')
            rows=[con.execute('SELECT namespace FROM classes WHERE class_id=?',(i,)).fetchone() for i in (old_id,new_id)]
            if any(r is None for r in rows) or rows[0][0].split(':')[0]!=rows[1][0].split(':')[0] or rows[0][0].split(':')[0] not in ('skill','occupation'):
                raise ValueError('Нужны классы одного вида.')
            con.execute('INSERT INTO telegram_class_mappings VALUES(?,?,?,?,?) ON CONFLICT(old_id,model_version) DO UPDATE SET new_id=excluded.new_id,confirmed_by=excluded.confirmed_by,confirmed_at=excluded.confirmed_at',(old_id,model,new_id,actor,now()))
            from .outbox import reconcile
            active=con.execute('SELECT result_set_id FROM active_result_set').fetchone()
            if active:
                reconcile(con,active[0])

    def import_legacy(self,path):
        # Preserve old files and own subscriptions; name-only filters cannot be mapped safely.
        path=local_path(path)
        if not path.is_file():
            return 0
        checksum=hashlib.sha256(path.read_bytes()).hexdigest()
        from market_system.sqlite import connect as sqlite_connect
        source=sqlite_connect(path.as_uri()+'?mode=ro',uri=True)
        try:
            rows=source.execute('SELECT id,user_id,filter_json FROM bot_subscriptions').fetchall()
        finally:
            source.close()
        with self.transaction() as con:
            if con.execute('SELECT 1 FROM telegram_legacy_imports WHERE path=?',(str(path),)).fetchone():
                return 0
            for identity,user,value in rows:
                con.execute("INSERT OR IGNORE INTO telegram_access VALUES(?,'USER',0,NULL,?)",(user,now()))
                con.execute("INSERT OR IGNORE INTO telegram_subscriptions VALUES(?,?,?,'legacy','needs_update',?,?)",(identity,user,value,'Перенесённый фильтр с названиями: создайте фильтр с точными ID общего каталога.',now()))
            con.execute('INSERT INTO telegram_legacy_imports VALUES(?,?,?)',(str(path),checksum,now()))
        return len(rows)
