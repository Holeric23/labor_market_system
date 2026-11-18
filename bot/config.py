"""Application configuration; secrets are supplied by the operator."""
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    owner_ids: frozenset[int]
    miniapp_url: str | None
    bot_enabled: bool
    allowed_hosts: tuple[str, ...] = ('localhost','127.0.0.1')

    @classmethod
    def load(cls):
        token = os.environ.get('BOT_TOKEN', '').strip()
        ids = frozenset(int(part.strip()) for part in os.environ.get('OWNER_TELEGRAM_IDS','').split(',') if part.strip())
        if any(identity <= 0 or identity >= 2**63 for identity in ids):
            raise ValueError('OWNER_TELEGRAM_IDS must contain positive Telegram IDs')
        raw = os.environ.get('BOT_ENABLED','false').strip().lower()
        if raw not in ('true','false'):
            raise ValueError('BOT_ENABLED must be true or false')
        enabled = raw == 'true'
        if enabled and (not token or not ids):
            raise ValueError('BOT_TOKEN and OWNER_TELEGRAM_IDS are required when the bot is enabled')
        url = os.environ.get('ADMIN_MINIAPP_URL','').strip() or None
        if url:
            value = urlsplit(url)
            if value.scheme != 'https' or not value.hostname or value.path != '/miniapp' or value.query or value.fragment or value.username or value.password:
                raise ValueError('ADMIN_MINIAPP_URL must be an HTTPS URL ending in /miniapp')
        hosts = tuple(x.strip() for x in os.environ.get('ADMIN_ALLOWED_HOSTS','localhost,127.0.0.1').split(',') if x.strip())
        if not hosts or any('*' in host or '/' in host for host in hosts):
            raise ValueError('ADMIN_ALLOWED_HOSTS must be explicit hostnames')
        return cls(token, ids, url, enabled, hosts)
