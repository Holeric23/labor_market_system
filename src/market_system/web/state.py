"""Local web state, Argon2 passwords, opaque server-side sessions."""
from contextlib import contextmanager
import hashlib
import re
import secrets
import sqlite3
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from market_system.paths import local_path
from market_system.sqlite import connect


class WebState:
    def __init__(self, path):
        self.path = local_path(path)
        self.hasher = PasswordHasher()
        self.dummy_hash = None

    @contextmanager
    def connection(self):
        con = connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS web_users (
                    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('ADMIN','USER')),
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS web_sessions (
                    token_hash TEXT PRIMARY KEY, user_id INTEGER REFERENCES web_users(id),
                    csrf TEXT NOT NULL, expires_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS web_sessions_expiry ON web_sessions(expires_at);
                CREATE TABLE IF NOT EXISTS web_login_attempts (
                    username TEXT PRIMARY KEY, failures INTEGER NOT NULL, retry_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS web_settings (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    page_size INTEGER NOT NULL CHECK(page_size BETWEEN 1 AND 100),
                    post_type TEXT NOT NULL CHECK(post_type IN ('vacancy','resume','')),
                    freshness_days INTEGER NOT NULL CHECK(freshness_days BETWEEN 1 AND 3650),
                    revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO web_settings VALUES(1,20,'vacancy',30,1);
                CREATE TABLE IF NOT EXISTS web_settings_audit (
                    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES web_users(id),
                    changed_at REAL NOT NULL, old_json TEXT NOT NULL, new_json TEXT NOT NULL);
            ''')
            schema = con.execute("SELECT sql FROM sqlite_master WHERE name='web_users'").fetchone()[0]
            if "'OWNER'" in schema:
                # Preserve IDs and dependent audit rows when opening an older installation.
                con.execute('PRAGMA foreign_keys=OFF')
                con.execute('BEGIN IMMEDIATE')
                con.execute('''CREATE TABLE web_users_admin (
                    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('ADMIN','USER')),
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_at REAL NOT NULL)''')
                con.execute("INSERT INTO web_users_admin SELECT id,username,password_hash,CASE role WHEN 'OWNER' THEN 'ADMIN' ELSE role END,active,created_at FROM web_users")
                con.execute('DROP TABLE web_users')
                con.execute('ALTER TABLE web_users_admin RENAME TO web_users')
                if con.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('Account role migration failed')
                con.commit()
                con.execute('PRAGMA foreign_keys=ON')
        if self.dummy_hash is None:
            self.dummy_hash = self.hasher.hash(secrets.token_urlsafe(32))

    def create_user(self, username, password, role):
        username = username.strip().casefold()
        if not re.fullmatch(r'[a-z0-9_.-]{3,64}', username):
            raise ValueError('Логин: 3–64 латинских буквы, цифры, точка, дефис или подчёркивание.')
        if len(password) > 1024:
            raise ValueError('Пароль должен содержать не более 1024 символов; пустой пароль разрешён.')
        if role not in ('ADMIN', 'USER'):
            raise ValueError('Неизвестная роль.')
        encoded = self.hasher.hash(password)
        try:
            with self.connection() as con:
                con.execute('INSERT INTO web_users(username,password_hash,role,created_at) VALUES(?,?,?,?)',
                            (username, encoded, role, time.time()))
        except sqlite3.IntegrityError:
            raise ValueError('Такой логин уже существует; пароль не изменён.') from None

    def authenticate(self, username, password):
        username = username.strip().casefold()[:64]
        now = time.time()
        with self.connection() as con:
            attempt = con.execute('SELECT * FROM web_login_attempts WHERE username=?', (username,)).fetchone()
            if attempt and attempt['failures'] >= 5 and attempt['retry_at'] > now:
                return None
            user = con.execute('SELECT * FROM web_users WHERE username=?', (username,)).fetchone()
        valid = False
        try:
            valid = self.hasher.verify(user['password_hash'] if user else self.dummy_hash, password)
        except (VerificationError, InvalidHashError):
            pass
        if not valid or not user or not user['active']:
            with self.connection() as con:
                con.execute('''INSERT INTO web_login_attempts VALUES(?,1,?)
                    ON CONFLICT(username) DO UPDATE SET
                    failures=CASE WHEN retry_at<=? THEN 1 ELSE failures+1 END,
                    retry_at=CASE WHEN retry_at<=? THEN excluded.retry_at ELSE retry_at END''',
                    (username, now+300, now, now))
            return None
        with self.connection() as con:
            con.execute('DELETE FROM web_login_attempts WHERE username=?', (username,))
            if self.hasher.check_needs_rehash(user['password_hash']):
                con.execute('UPDATE web_users SET password_hash=? WHERE id=?', (self.hasher.hash(password), user['id']))
        return user['id']

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def session(self, token):
        if not token or len(token) > 128:
            return None
        with self.connection() as con:
            row = con.execute('''SELECT s.*,u.username,u.role,u.active FROM web_sessions s
                LEFT JOIN web_users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?''',
                (self.token_hash(token), time.time())).fetchone()
        if row and (row['user_id'] is None or row['active'] == 1):
            return dict(row)
        return None

    def new_session(self, user_id=None, old_token=None):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        lifetime = 8*3600 if user_id is not None else 600
        with self.connection() as con:
            con.execute('DELETE FROM web_sessions WHERE expires_at<=?', (time.time(),))
            if old_token:
                con.execute('DELETE FROM web_sessions WHERE token_hash=?', (self.token_hash(old_token),))
            con.execute('INSERT INTO web_sessions VALUES(?,?,?,?)',
                        (self.token_hash(token), user_id, csrf, time.time()+lifetime))
        return token, csrf, lifetime

    def logout(self, token):
        with self.connection() as con:
            con.execute('DELETE FROM web_sessions WHERE token_hash=?', (self.token_hash(token),))

    def settings(self):
        with self.connection() as con:
            return dict(con.execute('SELECT * FROM web_settings WHERE singleton=1').fetchone())

    def save_settings(self, user_id, page_size, post_type, freshness_days, revision):
        import json
        if not 1 <= page_size <= 100 or post_type not in ('vacancy', 'resume', '') or not 1 <= freshness_days <= 3650:
            raise ValueError('Проверьте размер страницы, тип публикации и срок свежести.')
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            owner = con.execute("SELECT 1 FROM web_users WHERE id=? AND active=1 AND role='ADMIN'", (user_id,)).fetchone()
            if not owner:
                raise PermissionError('Настройки доступны только админу.')
            old = dict(con.execute('SELECT * FROM web_settings WHERE singleton=1').fetchone())
            if old['revision'] != revision:
                raise ValueError('Настройки уже изменены. Обновите страницу и повторите действие.')
            con.execute('UPDATE web_settings SET page_size=?,post_type=?,freshness_days=?,revision=revision+1 WHERE singleton=1',
                        (page_size, post_type, freshness_days))
            new = dict(con.execute('SELECT * FROM web_settings WHERE singleton=1').fetchone())
            con.execute('INSERT INTO web_settings_audit(user_id,changed_at,old_json,new_json) VALUES(?,?,?,?)',
                        (user_id, time.time(), json.dumps(old), json.dumps(new)))
