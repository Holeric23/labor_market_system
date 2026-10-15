import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from market_system.telegram_bot.config import Settings
from market_system.telegram_bot.main import run


class SettingsTests(unittest.TestCase):
    def load(self, **changes):
        env = {"BOT_TOKEN": "123456:TEST_TOKEN", "ADMIN_USER_IDS": "123, 456,123", **changes}
        with patch.dict(os.environ, env, clear=True), patch("market_system.telegram_bot.config.load_dotenv"):
            return Settings.load(Path(__file__).resolve().parents[1] / ".env")

    def test_multiple_admins_and_optional_secret(self):
        settings = self.load()
        self.assertEqual(settings.admins, frozenset({123, 456}))
        self.assertFalse(settings.events_enabled)
        self.assertEqual(settings.event_secret, "")
        self.assertEqual(settings.repository_backend, "sqlite")
        self.assertEqual(settings.research_db_path.name, "app.db")
        self.assertNotEqual(settings.research_db_path, settings.bot_db_path)

    def test_old_allow_list_cannot_grant_access(self):
        with self.assertRaisesRegex(ValueError, "ADMIN_USER_IDS"):
            self.load(ADMIN_USER_IDS="", ALLOWED_USER_IDS="123")

    def test_secret_required_only_when_webhook_enabled(self):
        self.load(EVENT_SECRET="")
        with self.assertRaisesRegex(ValueError, "EVENT_SECRET"):
            self.load(EVENTS_ENABLED="true", EVENT_SECRET="short")
        self.assertTrue(self.load(EVENTS_ENABLED="true", EVENT_SECRET="s" * 32).events_enabled)

    def test_invalid_admin_ids_and_same_database(self):
        for value in ("0", "-123", "username"):
            with self.assertRaises(ValueError):
                self.load(ADMIN_USER_IDS=value)
        with self.assertRaisesRegex(ValueError, "must differ"):
            self.load(BOT_DB_PATH="../data/app.db")

    def test_proxy_setting(self):
        self.assertIsNone(self.load().telegram_proxy_url)
        self.assertEqual(self.load(TELEGRAM_PROXY_URL="http://127.0.0.1:10809").telegram_proxy_url,
                         "http://127.0.0.1:10809")
        self.assertEqual(self.load(TELEGRAM_PROXY_URL="socks5://127.0.0.1:10809").telegram_proxy_url,
                         "socks5://127.0.0.1:10809")
        with self.assertRaises(ValueError):
            self.load(TELEGRAM_PROXY_URL="127.0.0.1:10809")


class OptionalWebhookStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_webhook_opens_no_http_server(self):
        bot = AsyncMock()
        bot.__aenter__.return_value = bot
        dispatcher = MagicMock()
        dispatcher.start_polling = AsyncMock()
        dispatcher.storage.close = AsyncMock()
        with patch("market_system.telegram_bot.main.Bot", return_value=bot) as bot_factory, patch("market_system.telegram_bot.main.Dispatcher", return_value=dispatcher), \
                patch("market_system.telegram_bot.main.AiohttpSession") as session_factory, \
                patch("market_system.telegram_bot.main.web.AppRunner") as server:
            await run(Settings(token="123456:TEST_TOKEN", admins=frozenset({123, 456}), repository_backend="memory",
                               telegram_proxy_url="http://127.0.0.1:10809"))
            session_factory.assert_called_once_with(proxy="http://127.0.0.1:10809")
            self.assertIs(bot_factory.call_args.kwargs["session"], session_factory.return_value)
            server.assert_not_called()
            dispatcher.start_polling.assert_awaited_once()
            dispatcher.storage.close.assert_awaited_once()
