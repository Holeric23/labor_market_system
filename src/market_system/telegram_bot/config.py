from market_system.paths import DATABASE_PATH, STATE_DIR, local_path
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


def ids(value: str) -> frozenset[int]:
    result = frozenset(int(part.strip()) for part in value.split(",") if part.strip())
    if any(value <= 0 for value in result):
        raise ValueError("Telegram user IDs must be positive")
    return result


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    admins: frozenset[int]
    event_secret: str = field(default="",repr=False)
    events_enabled: bool = False
    event_host: str = "127.0.0.1"
    event_port: int = 8088
    demo_posts_file: Path | None = None
    repository_backend: str = "sqlite"
    research_db_path: Path = DATABASE_PATH
    bot_db_path: Path = STATE_DIR / "bot.db"
    skills_run_id: str | None = None
    profiles_run_id: str | None = None
    telegram_proxy_url: str | None = field(default=None,repr=False)

    def __post_init__(self):
        for name in ('research_db_path', 'bot_db_path', 'demo_posts_file'):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, local_path(value))

    @classmethod
    def load(cls, env_file: Path):
        env_file = local_path(env_file)
        load_dotenv(env_file, override=False)
        backend = os.getenv("REPOSITORY_BACKEND", "sqlite")
        if backend not in ("sqlite", "memory"):
            raise ValueError("REPOSITORY_BACKEND must be sqlite or memory")
        token = os.getenv("BOT_TOKEN", "").strip()
        secret = os.getenv("EVENT_SECRET", "").strip()
        admins = ids(os.getenv("ADMIN_USER_IDS", ""))
        proxy = os.getenv("TELEGRAM_PROXY_URL", "").strip() or None
        if proxy:
            parsed_proxy = urlsplit(proxy)
            if parsed_proxy.scheme not in ("http", "socks4", "socks5") or not parsed_proxy.hostname or not parsed_proxy.port:
                raise ValueError("TELEGRAM_PROXY_URL must be http://host:port or socks5://host:port")
        enabled = os.getenv("EVENTS_ENABLED", "false").strip().lower()
        if enabled not in ("true", "false"):
            raise ValueError("EVENTS_ENABLED must be true or false")
        events_enabled = enabled == "true"
        if not token:
            raise ValueError("Set BOT_TOKEN in .env")
        if not admins:
            raise ValueError("Set ADMIN_USER_IDS to a comma-separated list; access is closed by default")
        if events_enabled and len(secret) < 32:
            raise ValueError("Set EVENT_SECRET (at least 32 characters) when EVENTS_ENABLED=true")
        port = int(os.getenv("EVENT_PORT", "8088"))
        if not 1 <= port <= 65535:
            raise ValueError("Invalid EVENT_PORT")
        demo = os.getenv("DEMO_POSTS_FILE", "").strip()
        demo_path = (env_file.parent / demo).resolve() if demo else None
        research_path = (env_file.parent / os.getenv("RESEARCH_DB_PATH", str(DATABASE_PATH))).resolve()
        state_path = (env_file.parent / os.getenv("BOT_DB_PATH", str(STATE_DIR / "bot.db"))).resolve()
        if research_path == state_path:
            raise ValueError("BOT_DB_PATH must differ from RESEARCH_DB_PATH")
        if research_path.exists() and state_path.exists() and research_path.samefile(state_path):
            raise ValueError("BOT_DB_PATH must not refer to the research database")
        return cls(token=token, admins=admins, event_secret=secret, events_enabled=events_enabled,
                   event_host=os.getenv("EVENT_HOST", "127.0.0.1"), event_port=port, demo_posts_file=demo_path,
                   repository_backend=backend, research_db_path=research_path, bot_db_path=state_path,
                   skills_run_id=os.getenv("SKILLS_RUN_ID", "").strip() or None,
                   profiles_run_id=os.getenv("PROFILES_RUN_ID", "").strip() or None,
                   telegram_proxy_url=proxy)
