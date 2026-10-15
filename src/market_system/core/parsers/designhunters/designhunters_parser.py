from market_system.paths import PROJECT_ROOT, STATE_DIR
import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page, Browser

from market_system.core.parsers.base_logger import ParserLogReport, write_parser_log
from market_system.core.parsers.parser import Parser
from market_system.core.utils.dataclasses import RawPublication


DEFAULT_SOURCE_TYPE_NAME = "designhunters"
DEFAULT_TIMEOUT = 30000
DEFAULT_DELAY_BETWEEN_REQUESTS = 2.0

PARSER_DIRECTORY = Path(__file__).resolve().parent
LOG_DIRECTORY = STATE_DIR / "logs" / "designhunters_parser"


@dataclass
class DesignHuntersSelectors:
    """CSS selectors for extracting vacancy data from designhunters.ru."""
    
    title: str = "h1"
    company: str = ".company-name"
    description: str = ".vacancy-description"
    requirements: str = ".vacancy-requirements"
    conditions: str = ".vacancy-conditions"
    salary: str = ".salary"
    location: str = ".location"


class DesignHuntersParser(Parser):
    """Parser for designhunters.ru vacancies using dynamic rendering."""

    def __init__(
        self,
        csv_path: str | Path | None = None,
        selectors: DesignHuntersSelectors | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        delay_between_requests: float = DEFAULT_DELAY_BETWEEN_REQUESTS,
        urls: list[str] | None = None,
    ) -> None:
        self._csv_path = Path(csv_path) if csv_path is not None else None
        self._selectors = selectors or DesignHuntersSelectors()
        self._timeout = timeout
        self._delay_between_requests = delay_between_requests

        if urls is None and (self._csv_path is None or not self._csv_path.exists()):
            raise FileNotFoundError(
                f"CSV file not found: {self._csv_path}"
            )

        self._urls = list(urls) if urls is not None else self._read_urls_from_csv()

        if not self._urls:
            raise ValueError(
                f"No URLs found in CSV file: {self._csv_path}"
            )

    async def parse(self) -> list[RawPublication]:
        """Parse all URLs from CSV file."""
        statistics: dict[str, int] = {
            "urls_total": len(self._urls),
            "urls_successful": 0,
            "urls_failed": 0,
            "publications_created": 0,
        }

        errors: list[str] = []
        publications: list[RawPublication] = []
        started_at = datetime.now()

        print(f"Found {len(self._urls)} URLs in CSV file")

        browser: Browser | None = None

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                )

                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                )

                for index, url in enumerate(self._urls, start=1):
                    print(
                        f"[{index}/{len(self._urls)}] "
                        f"Parsing: {url}"
                    )

                    page = await context.new_page()

                    try:
                        publication = await self._parse_page(
                            page,
                            url,
                        )

                        publications.append(publication)
                        statistics["urls_successful"] += 1
                        statistics["publications_created"] += 1

                        print(
                            f"  -> Successfully parsed "
                            f"(title: {publication.text[:50]}...)"
                        )

                    except Exception as error:
                        statistics["urls_failed"] += 1

                        error_message = (
                            f"URL: {url}\n"
                            f"Error type: {type(error).__name__}\n"
                            f"Error: {error}"
                        )

                        errors.append(error_message)

                        print(
                            f"  -> Error: {error}"
                        )

                    finally:
                        await page.close()

                    if index < len(self._urls):
                        import asyncio
                        await asyncio.sleep(
                            self._delay_between_requests
                        )

        except Exception as error:
            error_message = (
                f"{type(error).__name__}: {error}"
            )

            errors.append(
                f"Parser error: {error_message}"
            )

            raise

        finally:
            if browser is not None:
                await browser.close()

            log_report = ParserLogReport(
                parser_name="designhunters",
                started_at=started_at,
                finished_at=datetime.now(),
                statistics=statistics,
                errors=errors,
            )

            write_parser_log(LOG_DIRECTORY, log_report)

        return publications

    async def _parse_page(
        self,
        page: Page,
        url: str,
    ) -> RawPublication:
        """Parse a single vacancy page."""
        await page.goto(
            url,
            wait_until="networkidle",
            timeout=self._timeout,
        )

        await page.wait_for_selector(
            self._selectors.title,
            timeout=self._timeout,
        )

        title = await self._extract_text(
            page,
            self._selectors.title,
        )

        company = await self._extract_text(
            page,
            self._selectors.company,
            optional=True,
        )

        description = await self._extract_text(
            page,
            self._selectors.description,
            optional=True,
        )

        requirements = await self._extract_text(
            page,
            self._selectors.requirements,
            optional=True,
        )

        conditions = await self._extract_text(
            page,
            self._selectors.conditions,
            optional=True,
        )

        salary = await self._extract_text(
            page,
            self._selectors.salary,
            optional=True,
        )

        location = await self._extract_text(
            page,
            self._selectors.location,
            optional=True,
        )

        text_parts = [
            f"Вакансия: {title}",
        ]

        if company:
            text_parts.append(f"Компания: {company}")

        if salary:
            text_parts.append(f"Зарплата: {salary}")

        if location:
            text_parts.append(f"Локация: {location}")

        if description:
            text_parts.append(f"\nОписание:\n{description}")

        if requirements:
            text_parts.append(f"\nТребования:\n{requirements}")

        if conditions:
            text_parts.append(f"\nУсловия:\n{conditions}")

        text = "\n".join(text_parts)

        return RawPublication(
            source_type_name=DEFAULT_SOURCE_TYPE_NAME,
            source_name="designhunters.ru",
            text=text,
            url=url,
            published_at=None,
        )

    @staticmethod
    async def _extract_text(
        page: Page,
        selector: str,
        optional: bool = False,
    ) -> str | None:
        """Extract text from a single element."""
        try:
            element = await page.query_selector(selector)

            if element is None:
                if optional:
                    return None

                raise ValueError(
                    f"Required element not found: {selector}"
                )

            text = await element.inner_text()
            return text.strip() if text else None

        except Exception as error:
            if optional:
                return None

            raise ValueError(
                f"Failed to extract text from {selector}: {error}"
            )

    def _read_urls_from_csv(self) -> list[str]:
        """Read URLs from CSV file."""
        urls: list[str] = []

        with self._csv_path.open(
            "r",
            encoding="utf-8",
        ) as file:
            reader = csv.reader(file)

            for row in reader:
                if not row:
                    continue

                url = row[0].strip()

                if url:
                    urls.append(url)

        return urls

    async def managed_batches(self, mode, cursor, parameters):
        """Frozen URL list, durable offset, strict failure propagation to the job."""
        import asyncio
        import hashlib
        from copy import deepcopy
        from market_system.collection.runner import Batch
        from market_system.search.store import encode
        cursor = deepcopy(cursor)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                while True:
                    publications = []
                    remaining = parameters['max_items']-cursor['seen']
                    stop = min(len(self._urls),cursor['index']+min(parameters['batch_size'],remaining))
                    while cursor['index']<stop:
                        page = await context.new_page()
                        try:
                            publication = await self._parse_page(page,self._urls[cursor['index']])
                            publications.append(publication)
                        finally:
                            await page.close()
                        cursor['index']+=1
                        cursor['seen']+=1
                        if self._delay_between_requests:
                            await asyncio.sleep(self._delay_between_requests)
                    exhausted = cursor['index']>=len(self._urls)
                    state = cursor['source']
                    state['urls_hash']=hashlib.sha256(encode(self._urls).encode()).hexdigest()
                    if mode=='history':
                        state['history_index']=cursor['index']
                        state['history_done']=exhausted
                    else:
                        state['regular_index']=0 if exhausted else cursor['index']
                    done = exhausted or cursor['seen']>=parameters['max_items']
                    yield Batch(publications,deepcopy(cursor),done)
                    if done:
                        return
            finally:
                await browser.close()
