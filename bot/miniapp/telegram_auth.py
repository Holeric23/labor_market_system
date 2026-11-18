"""Validate Telegram Mini App launch data on the server."""
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


@dataclass(frozen=True)
class TelegramAdminAuth:
    token: str = field(repr=False)
    admins: frozenset[int]
    max_age: int = 300

    def validate(self, raw):
        if not self.token or not self.admins or not isinstance(raw, str) or len(raw) > 8192:
            raise ValueError('Недопустимые данные Telegram.')
        try:
            pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=64)
            data = dict(pairs)
            if len(data) != len(pairs):
                raise ValueError()
            supplied = data.pop('hash')
            if len(supplied) != 64 or any(c not in '0123456789abcdef' for c in supplied):
                raise ValueError()
            check = '\n'.join(f'{key}={value}' for key, value in sorted(data.items()))
            secret = hmac.new(b'WebAppData', self.token.encode(), hashlib.sha256).digest()
            expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, supplied):
                raise ValueError()
            stamp = int(data['auth_date'])
            current = time.time()
            if stamp > current + 30 or current - stamp > self.max_age:
                raise ValueError()
            user = json.loads(data['user'])
            identity = user['id']
            if type(identity) is not int or not 0 < identity < 2**63 or identity not in self.admins:
                raise ValueError()
            return identity, supplied, stamp + self.max_age + 30
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise ValueError('Проверка доступа Telegram не пройдена.') from None
