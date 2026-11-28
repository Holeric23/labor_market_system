"""Telegram parser login tests without contacting Telegram."""
import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from parsers.telegram_auth import TelegramLogin, LoginError
from parsers.telegram_auth_service import LoginServer
from database.parser_auth import shared_proof


class SessionPasswordNeededError(Exception): pass
class PhoneCodeInvalidError(Exception): pass
class PasswordHashInvalidError(Exception): pass
class PhoneCodeExpiredError(Exception): pass


class FakeTelegram:
    require_password = False
    code_expired = False
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.exists(): self.path.write_text('staged session',encoding='utf-8')
        self.disconnected = False
    async def connect(self): pass
    async def is_user_authorized(self): return self.path.read_text(encoding='utf-8') == 'authorized session'
    async def get_me(self): return SimpleNamespace(first_name='Test',username='test_account')
    async def disconnect(self): self.disconnected=True
    async def send_code_request(self, phone): return SimpleNamespace(phone_code_hash='private-hash')
    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        if code:
            if self.code_expired: raise PhoneCodeExpiredError()
            if code != '12345': raise PhoneCodeInvalidError()
            if self.require_password: raise SessionPasswordNeededError()
        elif password != 'fixture-password':
            raise PasswordHashInvalidError()
        self.path.write_text('authorized session',encoding='utf-8')


def workflow(tmp_path, two_factor=False, timeout=600):
    class Client(FakeTelegram): require_password=two_factor
    return TelegramLogin(tmp_path/'telegram.session','123','fixture-hash',Client,timeout)


def test_login_code_2fa_and_existing_session(tmp_path):
    login=workflow(tmp_path,True)
    async def run():
        assert (await login.status(42))['stage']=='idle'
        with pytest.raises(LoginError): await login.start(42,'not-a-phone')
        assert (await login.start(42,'+79991234567'))['stage']=='code'
        with pytest.raises(LoginError): await login.status(43)
        with pytest.raises(LoginError): await login.code(42,'00000')
        assert (await login.code(42,'12345'))['stage']=='password'
        with pytest.raises(LoginError): await login.password(42,'wrong')
        assert (await login.password(42,'fixture-password'))['stage']=='session_present'
        assert (tmp_path/'telegram.session').read_text()=='authorized session'
        assert list(tmp_path.glob('.login-*')) == []
        with pytest.raises(LoginError): await login.start(42,'+79991234567')
        assert (tmp_path/'telegram.session').read_text()=='authorized session'
        await login.shutdown()
    asyncio.run(run())


def test_code_only_cancel_and_expiry(tmp_path):
    async def run():
        cancelled=workflow(tmp_path,timeout=0.05)
        await cancelled.start(42,'+79991234567')
        await asyncio.sleep(0.1)
        assert (await cancelled.status(42))['stage']=='idle'
        assert list(tmp_path.glob('.login-*')) == []
        await cancelled.start(42,'+79991234567')
        assert (await cancelled.cancel(42))['stage']=='idle'
        assert list(tmp_path.glob('.login-*')) == []
        await cancelled.start(42,'+79991234567')
        assert (await cancelled.code(42,'12345'))['stage']=='session_present'
        await cancelled.shutdown()
    asyncio.run(run())


def test_expired_code_resets_login(tmp_path):
    class Expired(FakeTelegram): code_expired=True
    login=TelegramLogin(tmp_path/'telegram.session','123','hash',Expired)
    async def run():
        await login.start(42,'+79991234567')
        with pytest.raises(LoginError): await login.code(42,'12345')
        assert (await login.status(42))['stage']=='idle'
        assert list(tmp_path.glob('.login-*')) == []
        await login.shutdown()
    asyncio.run(run())


def test_internal_login_endpoint_requires_proof(tmp_path):
    login=workflow(tmp_path); proof=shared_proof('fixture-db')
    server=LoginServer(login,proof,('127.0.0.1',0)); server.start()
    url='http://127.0.0.1:'+str(server.http.server_port)+'/internal/parser-login'
    def call(action,value='',auth=proof):
        body=json.dumps({'action':action,'actor':42,'value':value}).encode()
        req=urllib.request.Request(url,data=body,headers={'X-Parser-Auth':auth},method='POST')
        with urllib.request.urlopen(req,timeout=3) as response: return json.load(response)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc: call('status',auth='incorrect')
        assert exc.value.code==403
        assert call('start','+79991234567')=={'ok':True,'stage':'code'}
        assert call('code','12345')=={'ok':True,'stage':'session_present'}
        assert (tmp_path/'telegram.session').exists()
    finally:
        server.close()


def test_existing_session_created_during_login_is_not_overwritten(tmp_path):
    login=workflow(tmp_path)
    async def run():
        await login.start(42,'+79991234567')
        (tmp_path/'telegram.session').write_text('existing account')
        with pytest.raises(LoginError): await login.code(42,'12345')
        assert (tmp_path/'telegram.session').read_text()=='existing account'
        assert list(tmp_path.glob('.login-*')) == []
        await login.shutdown()
    asyncio.run(run())


def test_constructor_failure_removes_temporary_session(tmp_path):
    def fail_after_creating(path):
        Path(path).write_text('partial session')
        raise RuntimeError('fixture')
    login=TelegramLogin(tmp_path/'telegram.session','123','hash',fail_after_creating)
    async def run():
        with pytest.raises(LoginError): await login.start(42,'+79991234567')
        assert list(tmp_path.glob('.login-*')) == []
        await login.shutdown()
    asyncio.run(run())


def test_private_account_management_and_session_check(tmp_path):
    login=workflow(tmp_path)
    async def run():
        await login.start(42,'+79991234567',True)
        await login.code(42,'12345')
        assert (await login.status(42))['private'] is True
        for action in (lambda:login.status(43),lambda:login.check(43),lambda:login.privacy(43,'shared'),lambda:login.remove(43)):
            with pytest.raises(LoginError): await action()
        assert (await login.check(42))['checked']['authorized']
        assert (await login.status(42))['checked']['username']=='test_account'
        await login.privacy(42,'shared')
        assert not (await login.status(43))['private']
        with pytest.raises(LoginError): await login.privacy(43,'private')
        await login.privacy(42,'private')
        assert (await login.remove(42))['stage']=='idle'
        assert not (tmp_path/'telegram.session').exists()
        await login.shutdown()
    asyncio.run(run())


def test_incomplete_authorization_is_not_saved(tmp_path):
    class Unauthorized(FakeTelegram):
        async def is_user_authorized(self): return False
    login=TelegramLogin(tmp_path/'telegram.session','123','hash',Unauthorized)
    async def run():
        await login.start(42,'+79991234567')
        with pytest.raises(LoginError): await login.code(42,'12345')
        assert not (tmp_path/'telegram.session').exists()
        assert (await login.status(42))['stage']=='idle'
    asyncio.run(run())
