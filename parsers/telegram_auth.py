"""Short-lived, owner-scoped Telegram login; secrets never enter PostgreSQL."""
import asyncio
import os
import re
import secrets
from pathlib import Path


class LoginError(Exception):
    pass


class TelegramLogin:
    def __init__(self, session_file, api_id, api_hash, client_factory=None, timeout=600):
        self.session_file = Path(session_file)
        self.api_id, self.api_hash = api_id, api_hash
        self.client_factory = client_factory
        self.timeout = timeout
        self.lock = asyncio.Lock()
        self.active = None
        self.expiry_task = None

    def _make_client(self, path):
        if self.client_factory:
            return self.client_factory(path)
        from telethon import TelegramClient
        return TelegramClient(str(path),int(self.api_id),self.api_hash,timeout=15,connection_retries=1,flood_sleep_threshold=0)

    @staticmethod
    def _error(exc):
        name = type(exc).__name__
        messages = {
            'PhoneNumberInvalidError': 'Номер телефона указан неверно.',
            'PhoneNumberBannedError': 'Telegram не разрешает вход для этого номера.',
            'PhoneCodeInvalidError': 'Код неверный. Проверьте его и попробуйте ещё раз.',
            'PhoneCodeExpiredError': 'Код истёк. Начните вход заново.',
            'PasswordHashInvalidError': 'Пароль неверный. Попробуйте ещё раз.',
            'ApiIdInvalidError': 'Telegram не принял API ID или API hash парсера.',
            'PhoneNumberFloodError': 'Слишком много попыток входа. Повторите позже.',
            'FloodWaitError': 'Telegram временно ограничил попытки входа. Повторите позже.',
        }
        return messages.get(name,'Не удалось связаться с Telegram или выполнить вход. Попробуйте позже.')

    def _owner(self, actor):
        if type(actor) is not int or not 0 < actor < 2**63:
            raise LoginError('Некорректный пользователь.')
        if self.active and self.active['owner'] != actor:
            raise LoginError('Другой владелец уже выполняет вход. Дождитесь окончания попытки.')

    async def _clear(self):
        active, self.active = self.active, None
        task, self.expiry_task = self.expiry_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
        if active:
            try:
                await active['client'].disconnect()
            except Exception:
                pass
            for suffix in ('','-journal','-wal','-shm'):
                try:
                    Path(str(active['path'])+suffix).unlink(missing_ok=True)
                except OSError:
                    pass

    async def _expire(self, marker):
        try:
            await asyncio.sleep(self.timeout)
            async with self.lock:
                if self.active and self.active['marker'] == marker:
                    await self._clear()
        except asyncio.CancelledError:
            pass

    async def status(self, actor):
        async with self.lock:
            self._owner(actor)
            if self.active:
                return {'stage':self.active['stage']}
            return {'stage':'session_present' if self.session_file.is_file() else 'idle'}

    async def start(self, actor, phone):
        async with self.lock:
            self._owner(actor)
            if self.active:
                raise LoginError('Вход уже начат. Продолжите его или отмените.')
            if self.session_file.exists():
                raise LoginError('Файл сессии уже существует. Повторный вход сейчас недоступен.')
            if not self.api_id or not str(self.api_id).isdigit() or not self.api_hash:
                raise LoginError('Не настроены TELEGRAM_API_ID и TELEGRAM_API_HASH парсера.')
            phone = phone.strip() if isinstance(phone,str) else ''
            if not re.fullmatch(r'\+[1-9][0-9]{6,14}',phone):
                raise LoginError('Укажите номер в международном формате, например +79991234567.')
            client = None
            path = None
            try:
                self.session_file.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                path = self.session_file.parent / ('.login-'+secrets.token_hex(16)+'.session')
                client = self._make_client(path)
                self.active = {'owner':actor,'client':client,'path':path,'phone':phone,'stage':'starting','marker':secrets.token_hex(16)}
                self.expiry_task = asyncio.create_task(self._expire(self.active['marker']))
                await client.connect()
                if path.exists():
                    try: path.chmod(0o600)
                    except OSError: pass
                sent = await client.send_code_request(phone)
                self.active.update(stage='code',code_hash=sent.phone_code_hash)
                return {'stage':'code'}
            except asyncio.CancelledError:
                await self._clear()
                raise
            except Exception as exc:
                if self.active:
                    await self._clear()
                elif client:
                    try: await client.disconnect()
                    except Exception: pass
                if path:
                    for suffix in ('','-journal','-wal','-shm'):
                        try: Path(str(path)+suffix).unlink(missing_ok=True)
                        except OSError: pass
                raise LoginError(self._error(exc)) from None

    async def _finish(self):
        active = self.active
        try:
            await active['client'].disconnect()
            # An exclusive link cannot overwrite an existing session, even if
            # another process creates it between the existence check and save.
            os.link(active['path'],self.session_file)
        except FileExistsError:
            await self._clear()
            raise LoginError('Файл сессии уже появился; существующая сессия сохранена.') from None
        except Exception:
            await self._clear()
            raise LoginError('Не удалось сохранить сессию парсера. Проверьте доступ к папке state.') from None
        try: active['path'].unlink(missing_ok=True)
        except OSError: pass
        self.active = None
        if self.expiry_task:
            self.expiry_task.cancel()
            self.expiry_task = None
        return {'stage':'session_present'}

    async def code(self, actor, value):
        async with self.lock:
            self._owner(actor)
            if not self.active or self.active['stage'] != 'code':
                raise LoginError('Сначала запросите код для входа.')
            value = value.strip() if isinstance(value,str) else ''
            if not re.fullmatch(r'[0-9]{3,12}',value):
                raise LoginError('Введите код из Telegram цифрами.')
            try:
                await self.active['client'].sign_in(phone=self.active['phone'],code=value,phone_code_hash=self.active['code_hash'])
                return await self._finish()
            except LoginError:
                raise
            except asyncio.CancelledError:
                await self._clear()
                raise
            except Exception as exc:
                name = type(exc).__name__
                if name == 'SessionPasswordNeededError':
                    self.active['stage'] = 'password'
                    return {'stage':'password'}
                message = self._error(exc)
                if name == 'PhoneCodeExpiredError': await self._clear()
                raise LoginError(message) from None

    async def password(self, actor, value):
        async with self.lock:
            self._owner(actor)
            if not self.active or self.active['stage'] != 'password':
                raise LoginError('Сначала введите код из Telegram.')
            if not isinstance(value,str) or not 1 <= len(value) <= 1024:
                raise LoginError('Введите пароль двухэтапной защиты.')
            try:
                await self.active['client'].sign_in(password=value)
                return await self._finish()
            except LoginError:
                raise
            except asyncio.CancelledError:
                await self._clear()
                raise
            except Exception as exc:
                raise LoginError(self._error(exc)) from None

    async def cancel(self, actor):
        async with self.lock:
            self._owner(actor)
            await self._clear()
            return {'stage':'session_present' if self.session_file.is_file() else 'idle'}

    async def shutdown(self):
        async with self.lock:
            await self._clear()
