from market_system.sqlite import connect as sqlite_connect
from market_system.paths import local_path, DATABASE_PATH
"""Bot-owned SQLite state. This file never opens the research database."""
import asyncio
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from market_system.telegram_bot.domain.matching import matches
from market_system.telegram_bot.domain.models import Role, SearchFilter, Subscription, User


class StateDatabase:
    def __init__(self, path: Path):
        self.path = local_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bot_users (
                    user_id INTEGER PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bot_subscriptions (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    filter_json TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(user_id, filter_json)
                );
                CREATE INDEX IF NOT EXISTS bot_subscriptions_user ON bot_subscriptions(user_id);
                CREATE TABLE IF NOT EXISTS bot_events (
                    event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, recipients TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bot_deliveries (
                    post_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                    PRIMARY KEY(post_id, user_id)
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite_connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    async def call(self, operation):
        def execute():
            with self.connect() as db:
                return operation(db)
        return await asyncio.to_thread(execute)


class SQLiteUserRepository:
    def __init__(self, database: StateDatabase, admins=()):
        self.database = database
        self.admins = set(admins)

    async def is_admin(self, user_id):
        return user_id in self.admins

    async def get_user(self, user_id):
        row = await self.database.call(lambda db: db.execute(
            "SELECT payload FROM bot_users WHERE user_id=?", (user_id,)).fetchone())
        if row is None:
            return None
        user = User.model_validate_json(row["payload"])
        user.role = Role.ADMIN if user_id in self.admins else Role.USER
        return user

    async def update(self, user):
        user = user.model_copy(deep=True)
        user.role = Role.ADMIN if user.user_id in self.admins else Role.USER
        await self.database.call(lambda db: db.execute(
            "INSERT INTO bot_users VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload",
            (user.user_id, user.model_dump_json())).rowcount)


class SQLiteSubscriptionRepository:
    def __init__(self, database: StateDatabase):
        self.database = database

    async def create_subscription(self, user_id, filter):
        payload = filter.subscription_payload()
        # Skill order has no meaning in an AND filter.
        payload["skills"] = sorted(payload["skills"])
        key = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        sub = Subscription(user_id=user_id, filter=payload)

        def create(db):
            db.execute("INSERT OR IGNORE INTO bot_subscriptions VALUES(?,?,?,?)",
                       (str(sub.id), user_id, key, sub.model_dump_json()))
            return db.execute("SELECT payload FROM bot_subscriptions WHERE user_id=? AND filter_json=?",
                              (user_id, key)).fetchone()[0]
        return Subscription.model_validate_json(await self.database.call(create))

    async def get_user_subscriptions(self, user_id):
        rows = await self.database.call(lambda db: db.execute(
            "SELECT payload FROM bot_subscriptions WHERE user_id=? ORDER BY rowid", (user_id,)).fetchall())
        return [Subscription.model_validate_json(row[0]) for row in rows]

    async def delete_subscription(self, sub_id: UUID, user_id: int):
        return bool(await self.database.call(lambda db: db.execute(
            "DELETE FROM bot_subscriptions WHERE id=? AND user_id=?", (str(sub_id), user_id)).rowcount))

    async def find_matching_subscriptions(self, post):
        def find(db):
            users = set()
            for row in db.execute("SELECT payload FROM bot_subscriptions"):
                sub = Subscription.model_validate_json(row[0])
                if sub.is_active and matches(post, SearchFilter.model_validate(sub.filter)):
                    users.add(sub.user_id)
            return sorted(users)
        return await self.database.call(find)


class SQLiteNotificationReceipts:
    def __init__(self, database: StateDatabase):
        self.database = database

    async def register(self, event_id, payload, recipients):
        def register(db):
            db.execute("INSERT OR IGNORE INTO bot_events VALUES(?,?,?)",
                       (str(event_id), payload, json.dumps(recipients)))
            row = db.execute("SELECT payload,recipients FROM bot_events WHERE event_id=?", (str(event_id),)).fetchone()
            if row[0] != payload:
                raise ValueError("event_id already belongs to a different payload")
            return json.loads(row[1])
        return await self.database.call(register)

    async def was_delivered(self, post_id, user_id):
        return bool(await self.database.call(lambda db: db.execute(
            "SELECT 1 FROM bot_deliveries WHERE post_id=? AND user_id=?", (str(post_id), user_id)).fetchone()))

    async def mark_delivered(self, post_id, user_id):
        await self.database.call(lambda db: db.execute(
            "INSERT OR IGNORE INTO bot_deliveries VALUES(?,?)", (str(post_id), user_id)).rowcount)
