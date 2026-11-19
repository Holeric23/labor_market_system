from market_system.paths import local_path, DATABASE_PATH
from market_system.paths import PROJECT_ROOT, STATE_DIR
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from python_socks import ProxyType
from telethon import TelegramClient

from market_system.core.parsers.base_logger import ParserLogReport, write_parser_log
from market_system.core.parsers.parser import Parser
from market_system.core.utils.dataclasses import RawPublication


DEFAULT_SOURCE_TYPE_NAME = "telegram"
DEFAULT_LIMIT = 100000

DEFAULT_SESSION_NAME = str(STATE_DIR / "posts_collector_session")

DEFAULT_DEVICE_MODEL = "PC"
DEFAULT_SYSTEM_VERSION = "Windows 10"
DEFAULT_APP_VERSION = "4.15.2"
DEFAULT_LANG_CODE = "ru"
DEFAULT_SYSTEM_LANG_CODE = "ru-RU"

DEFAULT_CONNECTION_RETRIES = 5
DEFAULT_RETRY_DELAY = 3
DEFAULT_TIMEOUT = 30

PARSER_DIRECTORY = Path(__file__).resolve().parent
ENV_FILE_PATH = PROJECT_ROOT / ".env"
LOG_DIRECTORY = STATE_DIR / "logs" / "telegram_parser"


class TelegramParser(Parser):
    """Parser for Telegram channels."""

    def __init__(
        self,
        channels: dict[str, str],
        limit: int = DEFAULT_LIMIT,
        use_proxy: bool = True,
        proxy_host: str = "127.0.0.1",
        proxy_port: int = 12334,
        session_name: str = DEFAULT_SESSION_NAME,
    ) -> None:
        if not channels:
            raise ValueError("channels must not be empty")

        if limit <= 0:
            raise ValueError("limit must be greater than zero")

        self._channels = channels
        self._limit = limit
        self._use_proxy = use_proxy
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        self._session_name = str(local_path(session_name))

    async def parse(self) -> list[RawPublication]:
        """Parse all configured Telegram channels."""
        statistics: dict[str, int] = {
            "channels_total": len(self._channels),
            "channels_successful": 0,
            "channels_failed": 0,
            "messages_received": 0,
            "messages_with_text": 0,
            "publications_created": 0,
            "messages_skipped": 0,
        }

        errors: list[str] = []

        publications: list[RawPublication] = []
        client: TelegramClient | None = None
        started_at = datetime.now()

        try:
            self._load_environment()

            api_id = self._get_required_environment_variable(
                "API_ID"
            )
            api_hash = self._get_required_environment_variable(
                "API_HASH"
            )
            phone_number = self._get_required_environment_variable(
                "PHONE_NUMBER"
            )
            password = os.getenv("PASSWORD")

            proxy = self._create_proxy()

            Path(self._session_name).parent.mkdir(parents=True, exist_ok=True)
            client = TelegramClient(
                self._session_name,
                int(api_id),
                api_hash,
                device_model=DEFAULT_DEVICE_MODEL,
                system_version=DEFAULT_SYSTEM_VERSION,
                app_version=DEFAULT_APP_VERSION,
                lang_code=DEFAULT_LANG_CODE,
                system_lang_code=DEFAULT_SYSTEM_LANG_CODE,
                connection_retries=DEFAULT_CONNECTION_RETRIES,
                retry_delay=DEFAULT_RETRY_DELAY,
                timeout=DEFAULT_TIMEOUT,
                proxy=proxy,
            )

            await client.start(
                phone=phone_number,
                password=password,
            )

            print("Telegram client successfully authenticated.")

            for source_name, channel_url in self._channels.items():
                channel_publications = await self._parse_channel(
                    client=client,
                    source_name=source_name,
                    channel_url=channel_url,
                    statistics=statistics,
                    errors=errors,
                )

                publications.extend(channel_publications)

        except Exception as error:
            error_message = (
                f"{type(error).__name__}: {error}"
            )

            errors.append(
                f"Parser error: {error_message}"
            )

            raise

        finally:
            if client is not None:
                await client.disconnect()

            log_report = ParserLogReport(
                parser_name="telegram",
                started_at=started_at,
                finished_at=datetime.now(),
                statistics=statistics,
                errors=errors,
            )

            write_parser_log(LOG_DIRECTORY, log_report)

        return publications

    async def _parse_channel(
        self,
        client: TelegramClient,
        source_name: str,
        channel_url: str,
        statistics: dict[str, int],
        errors: list[str],
    ) -> list[RawPublication]:
        print(
            f"Parsing channel: "
            f"{source_name} ({channel_url})"
        )

        publications: list[RawPublication] = []

        try:
            entity = await client.get_entity(channel_url)

            async for message in client.iter_messages(
                entity,
                limit=self._limit,
            ):
                statistics["messages_received"] += 1

                if not message.text:
                    statistics["messages_skipped"] += 1
                    continue

                statistics["messages_with_text"] += 1

                url = self._build_message_url(
                    channel_url,
                    message.id,
                )

                publications.append(
                    RawPublication(
                        source_type_name=DEFAULT_SOURCE_TYPE_NAME,
                        source_name=source_name,
                        text=message.text,
                        url=url,
                        published_at=message.date,
                    )
                )

                statistics["publications_created"] += 1

            statistics["channels_successful"] += 1

            print(
                "  -> Successfully received "
                f"{len(publications)} text publications"
            )

        except Exception as error:
            statistics["channels_failed"] += 1

            error_message = (
                f"Channel: {source_name}\n"
                f"URL: {channel_url}\n"
                f"Error type: {type(error).__name__}\n"
                f"Error: {error}"
            )

            errors.append(error_message)

            print(
                f"  -> Error while parsing "
                f"{source_name}: {error}"
            )

        return publications

    def _load_environment(self) -> None:
        if not ENV_FILE_PATH.exists():
            raise FileNotFoundError(
                f"Telegram parser environment file not found: "
                f"{ENV_FILE_PATH}"
            )

        load_dotenv(
            dotenv_path=ENV_FILE_PATH,
            override=False,
        )

    @staticmethod
    def _get_required_environment_variable(
        variable_name: str,
    ) -> str:
        value = os.getenv(variable_name)

        if not value:
            raise ValueError(
                f"Required environment variable "
                f"'{variable_name}' is not configured"
            )

        return value

    def _create_proxy(self) -> dict | None:
        if not self._use_proxy:
            return None

        return {
            "proxy_type": ProxyType.SOCKS5,
            "addr": self._proxy_host,
            "port": self._proxy_port,
            "rdns": True,
        }

    @staticmethod
    def _build_message_url(
        channel_url: str,
        message_id: int,
    ) -> str:
        parsed_url = urlparse(channel_url)

        if parsed_url.netloc != "t.me":
            raise ValueError(
                f"Unsupported Telegram channel URL: {channel_url}"
            )

        channel_name = (
            parsed_url.path.strip("/").split("/")[0]
        )

        if not channel_name:
            raise ValueError(
                f"Cannot determine channel name "
                f"from URL: {channel_url}"
            )

        return (
            f"https://t.me/"
            f"{channel_name}/"
            f"{message_id}"
        )

    async def managed_batches(self, client, source_name, channel_url, mode, cursor, parameters):
        """Collect the configured fresh window, newest first, with durable offsets."""
        from parsers.telegram_window import telegram_batches
        async for batch in telegram_batches(client,source_name,channel_url,mode,cursor,parameters,self._build_message_url):
            yield batch
