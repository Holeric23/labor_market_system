"""Serialize access to the shared Telethon session within the parser worker."""
import threading
from contextlib import contextmanager

_lock = threading.Lock()


class SessionBusyError(Exception):
    pass


@contextmanager
def session_guard():
    if not _lock.acquire(blocking=False):
        raise SessionBusyError('Аккаунт Telegram сейчас используется сбором. Попробуйте позже.')
    try:
        yield
    finally:
        _lock.release()
