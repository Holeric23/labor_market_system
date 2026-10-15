import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from market_system.core.parsers import Parser
from market_system.core.parsers.telegram import TelegramParser


class TestParserContract(unittest.TestCase):
    def test_parser_is_abstract(self) -> None:
        with self.assertRaises(TypeError):
            Parser()


class TestTelegramParser(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_path = Path(self.temp_dir.name) / '.env'
        self.env_path.write_text('', encoding='utf-8')
        self.env_patch = patch(
            'market_system.core.parsers.telegram.telegram_parser.ENV_FILE_PATH',
            self.env_path,
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_empty_channels_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TelegramParser(channels={})

    def test_invalid_limit_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TelegramParser(
                channels={
                    "test": "https://t.me/test"
                },
                limit=0,
            )

    def test_build_message_url(self) -> None:
        url = TelegramParser._build_message_url(
            "https://t.me/digital_jobster",
            12345,
        )

        self.assertEqual(
            url,
            "https://t.me/digital_jobster/12345",
        )

    def test_build_message_url_rejects_non_telegram_url(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            TelegramParser._build_message_url(
                "https://example.com/channel",
                12345,
            )

    def test_create_proxy_when_enabled(self) -> None:
        parser = TelegramParser(
            channels={
                "test": "https://t.me/test"
            },
            use_proxy=True,
            proxy_host="127.0.0.1",
            proxy_port=12334,
        )

        proxy = parser._create_proxy()

        self.assertIsNotNone(proxy)
        self.assertEqual(proxy["addr"], "127.0.0.1")
        self.assertEqual(proxy["port"], 12334)
        self.assertTrue(proxy["rdns"])

    def test_create_proxy_returns_none_when_disabled(
        self,
    ) -> None:
        parser = TelegramParser(
            channels={
                "test": "https://t.me/test"
            },
            use_proxy=False,
        )

        self.assertIsNone(parser._create_proxy())

    @patch(
        "market_system.core.parsers.telegram.telegram_parser.load_dotenv"
    )
    def test_load_environment_uses_configured_env(
        self,
        load_dotenv_mock,
    ) -> None:
        parser = TelegramParser(
            channels={
                "test": "https://t.me/test"
            }
        )

        parser._load_environment()

        load_dotenv_mock.assert_called_once()

        kwargs = load_dotenv_mock.call_args.kwargs

        self.assertEqual(
            kwargs["dotenv_path"],
            self.env_path,
        )

        self.assertFalse(
            kwargs["override"]
        )

    async def test_parse_channel_returns_raw_publications(
        self,
    ) -> None:
        message_date = datetime(
            2026,
            8,
            17,
            12,
            30,
            tzinfo=timezone.utc,
        )

        class FakeMessage:
            def __init__(
                self,
                message_id: int,
                text: str | None,
                date: datetime | None,
            ) -> None:
                self.id = message_id
                self.text = text
                self.date = date

        messages = [
            FakeMessage(
                100,
                "First publication",
                message_date,
            ),
            FakeMessage(
                101,
                None,
                message_date,
            ),
            FakeMessage(
                102,
                "Second publication",
                None,
            ),
        ]

        async def iter_messages(
            *args,
            **kwargs,
        ):
            for message in messages:
                yield message

        fake_client = AsyncMock()

        fake_client.get_entity = AsyncMock(
            return_value=object()
        )

        fake_client.iter_messages = iter_messages

        parser = TelegramParser(
            channels={
                "digital_jobster":
                    "https://t.me/digital_jobster"
            },
            limit=100,
        )

        statistics = {
            "channels_total": 1,
            "channels_successful": 0,
            "channels_failed": 0,
            "messages_received": 0,
            "messages_with_text": 0,
            "publications_created": 0,
            "messages_skipped": 0,
        }

        errors: list[str] = []

        publications = await parser._parse_channel(
            client=fake_client,
            source_name="digital_jobster",
            channel_url="https://t.me/digital_jobster",
            statistics=statistics,
            errors=errors,
        )

        self.assertEqual(
            len(publications),
            2,
        )

        self.assertEqual(
            publications[0].source_type_name,
            "telegram",
        )

        self.assertEqual(
            publications[0].source_name,
            "digital_jobster",
        )

        self.assertEqual(
            publications[0].text,
            "First publication",
        )

        self.assertEqual(
            publications[0].url,
            "https://t.me/digital_jobster/100",
        )

        self.assertEqual(
            publications[0].published_at,
            message_date,
        )

        self.assertEqual(
            publications[1].url,
            "https://t.me/digital_jobster/102",
        )

        self.assertIsNone(
            publications[1].published_at
        )

        self.assertEqual(
            statistics["messages_received"],
            3,
        )

        self.assertEqual(
            statistics["messages_with_text"],
            2,
        )

        self.assertEqual(
            statistics["messages_skipped"],
            1,
        )

        self.assertEqual(
            statistics["publications_created"],
            2,
        )

        self.assertEqual(
            statistics["channels_successful"],
            1,
        )

        self.assertEqual(
            statistics["channels_failed"],
            0,
        )

        self.assertEqual(
            errors,
            [],
        )

    async def test_parse_channel_records_channel_error(
        self,
    ) -> None:
        fake_client = AsyncMock()

        fake_client.get_entity = AsyncMock(
            side_effect=RuntimeError(
                "Telegram connection failed"
            )
        )

        parser = TelegramParser(
            channels={
                "digital_jobster":
                    "https://t.me/digital_jobster"
            }
        )

        statistics = {
            "channels_total": 1,
            "channels_successful": 0,
            "channels_failed": 0,
            "messages_received": 0,
            "messages_with_text": 0,
            "publications_created": 0,
            "messages_skipped": 0,
        }

        errors: list[str] = []

        publications = await parser._parse_channel(
            client=fake_client,
            source_name="digital_jobster",
            channel_url="https://t.me/digital_jobster",
            statistics=statistics,
            errors=errors,
        )

        self.assertEqual(
            publications,
            [],
        )

        self.assertEqual(
            statistics["channels_failed"],
            1,
        )

        self.assertEqual(
            statistics["channels_successful"],
            0,
        )

        self.assertEqual(
            len(errors),
            1,
        )

        self.assertIn(
            "digital_jobster",
            errors[0],
        )

        self.assertIn(
            "Telegram connection failed",
            errors[0],
        )

    @patch(
        "market_system.core.parsers.telegram.telegram_parser.write_parser_log"
    )
    @patch(
        "market_system.core.parsers.telegram.telegram_parser.TelegramClient"
    )
    @patch(
        "market_system.core.parsers.telegram.telegram_parser.os.getenv"
    )
    @patch(
        "market_system.core.parsers.telegram.telegram_parser.load_dotenv"
    )
    async def test_parse_writes_log_via_base_logger(
        self,
        load_dotenv_mock,
        getenv_mock,
        telegram_client_mock,
        write_log_mock,
    ) -> None:
        def getenv_side_effect(
            name: str,
        ):
            values = {
                "API_ID": "12345",
                "API_HASH": "test_hash",
                "PHONE_NUMBER": "+49123456789",
                "PASSWORD": "",
            }

            return values.get(name)

        getenv_mock.side_effect = getenv_side_effect

        fake_client = AsyncMock()

        fake_client.start = AsyncMock()
        fake_client.disconnect = AsyncMock()

        async def iter_messages(
            *args,
            **kwargs,
        ):
            if False:
                yield None

        fake_client.get_entity = AsyncMock(
            return_value=object()
        )

        fake_client.iter_messages = iter_messages

        telegram_client_mock.return_value = fake_client

        parser = TelegramParser(
            channels={
                "test": "https://t.me/test"
            },
            use_proxy=False,
        )

        await parser.parse()

        write_log_mock.assert_called_once()

        call_args = write_log_mock.call_args
        log_directory = call_args.args[0]
        log_report = call_args.args[1]

        self.assertEqual(
            log_directory,
            Path("state/logs/telegram_parser").resolve(),
        )

        self.assertEqual(
            log_report.parser_name,
            "telegram",
        )

        self.assertEqual(
            log_report.statistics["channels_total"],
            1,
        )

        self.assertEqual(
            log_report.statistics["channels_successful"],
            1,
        )

        self.assertEqual(
            log_report.errors,
            [],
        )

        self.assertIsNotNone(log_report.started_at)
        self.assertIsNotNone(log_report.finished_at)

    @patch(
        "market_system.core.parsers.telegram.telegram_parser.TelegramClient"
    )
    @patch(
        "market_system.core.parsers.telegram.telegram_parser.os.getenv"
    )
    @patch(
        "market_system.core.parsers.telegram.telegram_parser.load_dotenv"
    )
    async def test_parse_disconnects_client(
        self,
        load_dotenv_mock,
        getenv_mock,
        telegram_client_mock,
    ) -> None:
        def getenv_side_effect(
            name: str,
        ):
            values = {
                "API_ID": "12345",
                "API_HASH": "test_hash",
                "PHONE_NUMBER": "+49123456789",
                "PASSWORD": "",
            }

            return values.get(name)

        getenv_mock.side_effect = (
            getenv_side_effect
        )

        fake_client = AsyncMock()

        fake_client.start = AsyncMock()
        fake_client.disconnect = AsyncMock()

        async def iter_messages(
            *args,
            **kwargs,
        ):
            if False:
                yield None

        fake_client.get_entity = AsyncMock(
            return_value=object()
        )

        fake_client.iter_messages = (
            iter_messages
        )

        telegram_client_mock.return_value = (
            fake_client
        )

        parser = TelegramParser(
            channels={
                "test": "https://t.me/test"
            },
            use_proxy=False,
        )

        log_directory = (
            Path(self.temp_dir.name) / "logs"
        )

        with patch(
            "market_system.core.parsers.telegram.telegram_parser.LOG_DIRECTORY",
            log_directory,
        ):
            result = await parser.parse()

        self.assertEqual(
            result,
            [],
        )

        fake_client.start.assert_awaited_once()
        fake_client.disconnect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
