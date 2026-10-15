import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from market_system.telegram_bot.application.services import AuthService, NotifierWorker, DeliveryService
from market_system.telegram_bot.domain.models import SearchFilter, User, utcnow
from market_system.telegram_bot.domain.ports import UnsupportedFilter
from market_system.telegram_bot.infrastructure.research import ResearchPostRepository, telegram_reference
from market_system.telegram_bot.infrastructure.sqlite_state import (
    StateDatabase, SQLiteUserRepository, SQLiteSubscriptionRepository, SQLiteNotificationReceipts,
)
from test_system import FakeTelegram, post


def make_research(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE contents(id INTEGER PRIMARY KEY, text TEXT, text_hash TEXT UNIQUE);
            CREATE TABLE publications(id INTEGER PRIMARY KEY,content_id INTEGER,url TEXT,published_at TEXT);
            CREATE TABLE labeled_contents(text_hash TEXT,model_version TEXT,final_label TEXT,
                PRIMARY KEY(text_hash,model_version));
            CREATE TABLE skill_extraction_runs(run_id TEXT PRIMARY KEY,model_version TEXT,imported_at TEXT);
            CREATE TABLE extracted_skill_dictionary(run_id TEXT,skill_id TEXT,name TEXT,PRIMARY KEY(run_id,skill_id));
            CREATE TABLE extracted_skill_mentions(run_id TEXT,text_hash TEXT,skill_id TEXT,role TEXT);
            CREATE TABLE profile_extraction_runs(run_id TEXT PRIMARY KEY,model_version TEXT,created_at TEXT);
            CREATE TABLE extracted_profiles(run_id TEXT,text_hash TEXT,model_version TEXT,post_type TEXT,
                salary_from REAL,salary_to REAL,currency TEXT,salary_period TEXT,tax_mode TEXT,
                occupation_names_json TEXT,work_format TEXT,schedule TEXT,employment_type TEXT,
                working_hours TEXT,hours_per_week INTEGER,PRIMARY KEY(run_id,text_hash));
            INSERT INTO profile_extraction_runs VALUES('profiles','v2','2026-01-01'),('old_profiles','v1','2025-01-01');
            INSERT INTO extracted_profiles VALUES
                ('profiles','hash1','v2','vacancy',100000.25,150000,'RUB','month','net',
                 '["Разработчик", "Аналитик"]','remote','flexible','full_time','09:00–18:00',40),
                ('profiles','hash2','v2','resume',NULL,NULL,NULL,NULL,NULL,'["Дизайнер"]',NULL,NULL,NULL,NULL,NULL),
                ('old_profiles','hash1','v1','resume',NULL,NULL,NULL,NULL,NULL,'[]',NULL,NULL,NULL,NULL,NULL);
            INSERT INTO skill_extraction_runs VALUES('old','v1','2025-01-01'),('latest','v2','2026-01-01');
            INSERT INTO contents VALUES(1,'Developer','hash1'),(2,'Designer','hash2'),(3,'Old','hash3');
            INSERT INTO labeled_contents VALUES('hash1','v2','vacancy'),('hash2','v2','resume'),
                ('hash3','v2',NULL),('hash1','v1','resume');
            INSERT INTO extracted_skill_dictionary VALUES('latest','python','Python'),('latest','figma','Фигма'),
                ('latest','sql','SQL'),('old','java','Java');
            INSERT INTO extracted_skill_mentions VALUES
                ('latest','hash1','python','required'),('latest','hash1','python','preferred'),
                ('latest','hash1','figma','negated'),('latest','hash1','sql','task'),
                ('latest','hash2','figma','possessed'),('latest','hash2','python','learning'),
                ('old','hash1','java','possessed');
        """)
        now = utcnow().isoformat()
        db.executemany("INSERT INTO publications VALUES(?,?,?,?)", [
            (1, 1, "https://t.me/example/1", now),
            (2, 1, "https://t.me/c/1234567890/2", now),
            (3, 2, "https://example.org/resume", None),
            (4, 3, "https://t.me/example/4", now),
        ])


class ResearchRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.path = Path(self.directory.name) / "market_system.research.db"
        make_research(self.path)
        self.repository = ResearchPostRepository(self.path)

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_run_isolation_skill_roles_and_sql_injection(self):
        self.assertEqual(self.repository.model_version, "v2")
        cases = [("VACANCY", [], 2), ("VACANCY", ["python"], 2), ("VACANCY", ["фигма"], 0),
                 ("VACANCY", ["sql"], 0), ("VACANCY", ["python", "sql"], 0),
                 ("RESUME", ["ФИГМА"], 1), ("RESUME", ["python"], 0),
                 ("VACANCY", ["' OR 1=1 --"], 0)]
        for kind, skills, expected in cases:
            self.assertEqual(await self.repository.count_posts(SearchFilter(post_type=kind, skills=skills)), expected)
        old = ResearchPostRepository(self.path, "old")
        self.assertEqual(await old.count_posts(SearchFilter(post_type="RESUME", skills=["java"])), 2)

    async def test_paging_mapping_and_read_only(self):
        before = hashlib.sha256(self.path.read_bytes()).digest()
        filter = SearchFilter(post_type="VACANCY")
        first = (await self.repository.search_posts(filter, 1, 0))[0]
        second = (await self.repository.search_posts(filter, 1, 1))[0]
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.tg_channel_id, -1001234567890)
        self.assertEqual(second.tg_channel_username, "@example")
        self.assertEqual(first.skills, ["Python"])
        self.assertEqual((await self.repository.search_posts(filter, 1, 0))[0].id, first.id)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), before)
        with self.repository.connect() as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM publications")

    async def test_unknown_date_is_not_fabricated(self):
        filter = SearchFilter(post_type="RESUME")
        value = (await self.repository.search_posts(filter, 3, 0))[0]
        self.assertIsNone(value.parsed_date)
        self.assertIsNone(value.salary_min)
        filter.published_within_days = 3
        self.assertEqual(await self.repository.count_posts(filter), 0)

    async def test_reads_new_committed_publications(self):
        filter = SearchFilter(post_type="VACANCY")
        self.assertEqual(await self.repository.count_posts(filter), 2)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO publications VALUES(5,1,'https://t.me/example/5',?)", (utcnow().isoformat(),))
        self.assertEqual(await self.repository.count_posts(filter), 3)

    async def test_profile_filters_and_missing_relocation(self):
        for values, expected in [(dict(specialty="АНАЛИТИК"), 2), (dict(salary_min=100000), 2),
                                 (dict(salary_min=100001), 0), (dict(work_format="REMOTE"), 2),
                                 (dict(work_format="OFFICE"), 0), (dict(specialty="Дизайнер"), 0)]:
            self.assertEqual(await self.repository.count_posts(SearchFilter(post_type="VACANCY", **values)), expected)
        with self.assertRaises(UnsupportedFilter):
            await self.repository.count_posts(SearchFilter(post_type="VACANCY", relocation=False))
        value = (await self.repository.search_posts(SearchFilter(post_type="VACANCY"), 1, 0))[0]
        self.assertEqual(value.salary_min, 100000.25)
        self.assertEqual(value.salary_period, "month")
        self.assertEqual(value.schedule, "flexible")
        self.assertEqual(value.hours_per_week, 40)

    async def test_incompatible_explicit_runs_rejected(self):
        with self.assertRaises(ValueError):
            ResearchPostRepository(self.path, "old", "profiles")

    async def test_event_reference_uses_same_post_mapping(self):
        value = await self.repository.get_publication(2)
        page = await self.repository.search_posts(SearchFilter(post_type="VACANCY"), 1, 0)
        self.assertEqual(value, page[0])
        self.assertIsNone(await self.repository.get_publication(4))
        self.assertIsNone(await self.repository.get_publication(999))

    async def test_missing_database_is_not_created(self):
        missing = self.path.with_name("missing.db")
        with self.assertRaises(sqlite3.OperationalError):
            ResearchPostRepository(missing)
        self.assertFalse(missing.exists())

    def test_telegram_links(self):
        self.assertEqual(telegram_reference("https://t.me/s/example/12"), (None, "@example", 12))
        self.assertEqual(telegram_reference("https://evil.test/example/12"), (None, None, None))


class PersistentStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.path = Path(self.directory.name) / "bot.db"
        self.database = StateDatabase(self.path)

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_user_and_subscription_survive_reopening(self):
        users = SQLiteUserRepository(self.database, [1, 2])
        await users.update(User(user_id=1, last_menu_message_id=123))
        subscriptions = SQLiteSubscriptionRepository(self.database)
        sub = await subscriptions.create_subscription(1, SearchFilter(post_type="VACANCY", skills=["SQL", "Python"], offset=99))
        database = StateDatabase(self.path)
        users = SQLiteUserRepository(database, [1, 2])
        subscriptions = SQLiteSubscriptionRepository(database)
        self.assertEqual((await users.get_user(1)).last_menu_message_id, 123)
        self.assertEqual((await users.get_user(1)).role, "ADMIN")
        again = await subscriptions.create_subscription(1, SearchFilter(post_type="VACANCY", skills=["python", "sql"]))
        self.assertEqual(sub.id, again.id)
        self.assertNotIn("offset", again.filter)
        self.assertFalse(await subscriptions.delete_subscription(sub.id, 2))
        self.assertEqual(len(await subscriptions.get_user_subscriptions(1)), 1)
        self.assertEqual(await subscriptions.find_matching_subscriptions(post()), [1])
        self.assertTrue(await subscriptions.delete_subscription(sub.id, 1))

    async def test_receipts_survive_restart_and_reject_event_conflict(self):
        users = SQLiteUserRepository(self.database, [1])
        subscriptions = SQLiteSubscriptionRepository(self.database)
        await subscriptions.create_subscription(1, SearchFilter(post_type="VACANCY"))
        telegram = FakeTelegram()
        event_id, value = uuid4(), post()
        def worker():
            return NotifierWorker(subscriptions, DeliveryService(telegram), AuthService(users),
                                  SQLiteNotificationReceipts(StateDatabase(self.path)))
        self.assertTrue(await worker().on_new_post_created(event_id, value))
        self.assertTrue(await worker().on_new_post_created(event_id, value))
        self.assertEqual(len(telegram.forwards), 1)
        with self.assertRaises(ValueError):
            await worker().on_new_post_created(event_id, post())

    async def test_recipients_are_frozen_for_event_retry(self):
        store = SQLiteNotificationReceipts(self.database)
        event_id = uuid4()
        self.assertEqual(await store.register(event_id, "payload", [1]), [1])
        self.assertEqual(await store.register(event_id, "payload", [1, 2]), [1])

    async def test_admin_list_is_only_authority_after_restart(self):
        users = SQLiteUserRepository(self.database, [1, 2])
        for uid in (1, 2):
            self.assertTrue(await users.is_admin(uid))
            await users.update(User(user_id=uid))
        restarted = SQLiteUserRepository(StateDatabase(self.path), [2])
        self.assertFalse(await AuthService(restarted).check_access(1))
        self.assertEqual((await restarted.get_user(1)).role, "USER")
        self.assertTrue(await AuthService(restarted).check_access(2))
        self.assertFalse(await AuthService(restarted).check_access(999))
