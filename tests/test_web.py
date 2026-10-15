import re
import time
from html import unescape
from urllib.parse import urlsplit, parse_qs

import pytest
from fastapi.testclient import TestClient
from market_system.web.app import WebConfig, create_app, COOKIE
from market_system.web.__main__ import main
from test_search_service import catalog

PASSWORD = 'Only-local-test-password-42'


def csrf(response):
    return unescape(re.search(r'name="csrf" value="([^"]+)"', response.text)[1])


@pytest.fixture
def client(catalog, tmp_path):
    app = create_app(WebConfig(catalog.path, tmp_path/'web.db', allowed_hosts=('testserver',)))
    with TestClient(app) as client:
        state = app.state.web_state
        state.create_user('owner', PASSWORD, 'ADMIN')
        state.create_user('reader', PASSWORD, 'USER')
        yield client


def login(client, username='owner'):
    token = csrf(client.get('/login'))
    return client.post('/login', data={'username':username, 'password':PASSWORD, 'csrf':token})


def test_login_logout_and_server_revocation(client):
    response = client.get('/catalog')
    assert response.url.path == '/login'
    anonymous = client.cookies.get(COOKIE)
    token = csrf(client.get('/login'))
    response = client.post('/login', data={'username':'owner','password':PASSWORD,'csrf':token}, headers={'origin':'null'})
    assert response.status_code == 200 and 'Обзор' in response.text
    authenticated = client.cookies.get(COOKIE)
    assert authenticated != anonymous
    assert client.app.state.web_state.session(anonymous) is None
    response = client.post('/logout', data={'csrf':csrf(response)}, follow_redirects=False)
    assert response.status_code == 303
    assert client.app.state.web_state.session(authenticated) is None
    client.cookies.set(COOKIE, authenticated)
    assert client.get('/settings').url.path == '/login'


def test_password_hash_and_invalid_login(client):
    state = client.app.state.web_state
    with state.connection() as con:
        stored = con.execute('SELECT password_hash FROM web_users LIMIT 1').fetchone()[0]
    assert stored.startswith('$argon2id$') and PASSWORD not in stored
    token = csrf(client.get('/login'))
    response = client.post('/login', data={'username':'owner','password':'wrong','csrf':token})
    assert response.status_code == 401
    assert client.get('/catalog').url.path == '/login'


def test_user_settings_denied_on_get_and_post(client):
    response = login(client, 'reader')
    assert response.url.path == '/catalog'
    assert 'href="/settings"' not in response.text
    token = csrf(response)
    assert client.get('/settings').status_code == 403
    old = client.app.state.web_state.settings()
    assert client.post('/settings', data={'csrf':token,'page_size':99,'post_type':'','freshness_days':1,'revision':1}).status_code == 403
    assert client.app.state.web_state.settings() == old


def test_diagnostics_owner_only(client):
    assert client.get('/diagnostics').url.path=='/login'
    login(client,'reader')
    assert client.get('/diagnostics').status_code==403
    login(client,'owner')
    response = client.get('/diagnostics')
    assert response.status_code==200
    assert 'Диагностика системы' in response.text


def test_csrf_all_modifying_routes(client):
    page = client.get('/login')
    assert client.post('/login', data={'username':'owner','password':PASSWORD}).status_code == 403
    assert client.post('/login', data={'username':'owner','password':PASSWORD,'csrf':'неверно'}).status_code == 403
    response = login(client)
    assert client.post('/logout', data={'csrf':'wrong'}, headers={'origin':'null'}).status_code == 403
    assert client.post('/settings', data={'page_size':1}).status_code == 403
    assert client.post('/settings', data={'csrf':csrf(response)}, headers={'origin':'https://evil.example'}).status_code == 403
    assert client.get('/logout', follow_redirects=False).status_code == 405


def test_settings_saved_used_and_audited(client):
    login(client)
    page = client.get('/settings')
    response = client.post('/settings', data={'csrf':csrf(page),'revision':1,'page_size':2,'post_type':'resume','freshness_days':7})
    assert 'Настройки сохранены' in response.text
    settings = client.app.state.web_state.settings()
    assert settings['page_size']==2 and settings['post_type']=='resume'
    page = client.get('/catalog')
    assert 'Документ №8' in page.text and 'Документ №4' not in page.text
    assert client.post('/settings', data={'csrf':csrf(page),'revision':1,'page_size':4,'post_type':'','freshness_days':2}).status_code == 400
    with client.app.state.web_state.connection() as con:
        assert con.execute('SELECT count(*) FROM web_settings_audit').fetchone()[0] == 1


def test_search_pagination_filter_and_bad_cursor(client):
    login(client, 'reader')
    first = client.get('/catalog', params={'post_type':'','skill_ids':'1','skills_mode':'ANY','page_size':2})
    assert 'Документ №2' in first.text and 'Документ №1' in first.text and 'Документ №8' not in first.text
    url = unescape(re.search(r'href="([^"]+)">Следующая страница', first.text)[1])
    values = parse_qs(urlsplit(url).query, keep_blank_values=True)
    assert values['skill_ids']==['1'] and values['skills_mode']==['ANY'] and values['post_type']==['']
    second = client.get(url)
    assert second.status_code==200 and 'Документ №8' in second.text and 'Документ №1' not in second.text
    broken = client.get('/catalog?post_type=vacancy&cursor=invalid')
    assert broken.status_code==400 and 'Начните поиск заново' in broken.text
    changed = client.get(url.replace('skills_mode=ANY','skills_mode=ALL').replace('skill_ids=1','skill_ids=2'))
    assert changed.status_code==400


def test_card_escape_links_and_versions(client, catalog):
    from market_system.search.store import connect
    with connect(catalog.path) as con:
        con.execute("UPDATE contents SET text=? WHERE id=1", ('<script>alert(1)</script><img src=x onerror=alert(2)>',))
        con.execute("UPDATE publications SET url='javascript:alert(1)' WHERE id=1")
        con.commit()
    login(client)
    response = client.get('/cards/1?result_set_id=v1')
    assert response.status_code==200
    assert '<script>alert(1)</script>' not in response.text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in response.text
    assert 'href="javascript:' not in response.text
    assert 'https://test/other' in response.text and 'one' in response.text and 'two' in response.text
    assert 'Обязательный навык' in response.text and 'Желательный навык' in response.text
    assert 'не означают гарантированную точность' in response.text
    assert client.get('/cards/1?result_set_id=missing').status_code==404
    assert client.get('/cards/999?result_set_id=v1').status_code==404
    assert client.get('/cards/bad?result_set_id=v1').status_code==422
    assert 'default-src' in response.headers['content-security-policy']


def test_session_expiry_disable_role_change(client):
    login(client)
    state = client.app.state.web_state
    with state.connection() as con:
        con.execute("UPDATE web_users SET role='USER' WHERE username='owner'")
    assert client.get('/settings').status_code==403
    with state.connection() as con:
        con.execute("UPDATE web_users SET active=0 WHERE username='owner'")
    assert client.get('/catalog').url.path=='/login'
    client.cookies.clear()
    login(client, 'reader')
    with state.connection() as con:
        con.execute('UPDATE web_sessions SET expires_at=?', (time.time()-1,))
    assert client.get('/catalog').url.path=='/login'


def test_throttle(client):
    state=client.app.state.web_state
    for _ in range(5):
        assert state.authenticate('owner', 'wrong') is None
    assert state.authenticate('owner', PASSWORD) is None
    with state.connection() as con:
        con.execute('UPDATE web_login_attempts SET retry_at=0')
    assert state.authenticate('owner', PASSWORD)


@pytest.mark.parametrize('password', [PASSWORD, ''])
def test_cli_owner_and_no_overwrite(tmp_path, monkeypatch, password):
    from market_system.web.state import WebState
    path=tmp_path/'cli.db'
    monkeypatch.setenv('TEST_OWNER_PASSWORD',password)
    import subprocess, sys
    command = [sys.executable, '-m', 'market_system.web', 'create-admin', 'boss', '--state', str(path), '--password-env', 'TEST_OWNER_PASSWORD']
    if password == '':
        # Windows removes empty environment variables; exercise two Enter presses.
        code = "from unittest.mock import patch; from market_system.web.__main__ import main; import sys\nwith patch('getpass.getpass', side_effect=['', '']): main(['create-admin', 'boss', '--state', sys.argv[1]])"
        command = [sys.executable, '-c', code, str(path)]
    assert subprocess.run(command, capture_output=True).returncode == 0
    state=WebState(path);state.initialize()
    assert state.authenticate('boss', password)
    assert subprocess.run(command, capture_output=True).returncode != 0
    with state.connection() as con:
        assert con.execute('SELECT role FROM web_users').fetchone()[0]=='ADMIN'


def test_empty_password_login(client):
    state = client.app.state.web_state
    state.create_user('empty', '', 'ADMIN')
    page = client.get('/login')
    password_input = re.search(r'<input name="password"[^>]*>', page.text)[0]
    assert 'required' not in password_input
    response = client.post('/login', data={'username':'empty', 'password':'', 'csrf':csrf(page)})
    assert response.status_code == 200 and response.url.path == '/'


def test_empty_password_does_not_bypass_existing_password(client):
    response = client.post('/login', data={'username':'owner', 'password':'', 'csrf':csrf(client.get('/login'))})
    assert response.status_code == 401


def test_diagnostics_avoids_full_database_scan(client, monkeypatch):
    from market_system.operations import diagnostics
    statements = []
    original = diagnostics.connect
    def traced(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection
    monkeypatch.setattr(diagnostics, 'connect', traced)
    login(client)
    assert client.get('/diagnostics').status_code == 200
    sql = ' '.join(statements).lower()
    assert 'quick_check' not in sql
    assert 'integrity_check' not in sql
    assert 'foreign_key_check' not in sql
    assert 'begin' not in sql


def test_diagnostics_busy_database_keeps_web_available(client, catalog):
    import sqlite3
    login(client)
    lock = sqlite3.connect(catalog.path)
    try:
        lock.execute('BEGIN EXCLUSIVE')
        started = time.monotonic()
        response = client.get('/diagnostics')
        assert response.status_code == 200
        assert 'Диагностика прервана' in response.text or 'Диагностика временно недоступна' in response.text
        assert time.monotonic() - started < 3
        assert client.get('/settings').status_code == 200
    finally:
        lock.rollback()
        lock.close()


def test_admin_role_migrates_existing_accounts(tmp_path):
    import sqlite3
    from market_system.web.state import WebState
    path = tmp_path/'old-web.db'
    state = WebState(path)
    state.initialize()
    state.create_user('legacy', '', 'ADMIN')
    token, _, _ = state.new_session(state.authenticate('legacy', ''))
    with sqlite3.connect(path) as con:
        con.execute('PRAGMA writable_schema=ON')
        con.execute("UPDATE sqlite_master SET sql=replace(sql, 'ADMIN', 'OWNER') WHERE name='web_users'")
        con.execute('PRAGMA writable_schema=OFF')
    with sqlite3.connect(path) as con:
        con.execute("UPDATE web_users SET role='OWNER'")
    state.initialize()
    state.initialize()
    assert state.session(token)['role'] == 'ADMIN'
    assert state.authenticate('legacy', '')
    with state.connection() as con:
        assert not con.execute('PRAGMA foreign_key_check').fetchall()
