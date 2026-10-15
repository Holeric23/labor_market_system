import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from market_system.core.parsers.designhunters import DesignHuntersParser
from market_system.core.parsers.designhunters.designhunters_parser import (
    DesignHuntersSelectors,
)


class TestDesignHuntersParser(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_missing_csv_file_raises_error(self) -> None:
        with self.assertRaises(FileNotFoundError):
            DesignHuntersParser(
                csv_path=self.temp_path / "nonexistent.csv"
            )

    def test_empty_csv_file_raises_error(self) -> None:
        csv_file = self.temp_path / "empty.csv"
        csv_file.write_text("", encoding="utf-8")

        with self.assertRaises(ValueError):
            DesignHuntersParser(csv_path=csv_file)

    def test_read_urls_from_csv(self) -> None:
        csv_file = self.temp_path / "urls.csv"
        csv_file.write_text(
            "https://designhunters.ru/vacancy/1\n"
            "https://designhunters.ru/vacancy/2\n"
            "https://designhunters.ru/vacancy/3\n",
            encoding="utf-8",
        )

        parser = DesignHuntersParser(csv_path=csv_file)
        urls = parser._read_urls_from_csv()

        self.assertEqual(len(urls), 3)
        self.assertEqual(
            urls[0],
            "https://designhunters.ru/vacancy/1",
        )
        self.assertEqual(
            urls[1],
            "https://designhunters.ru/vacancy/2",
        )
        self.assertEqual(
            urls[2],
            "https://designhunters.ru/vacancy/3",
        )

    def test_custom_selectors(self) -> None:
        csv_file = self.temp_path / "urls.csv"
        csv_file.write_text(
            "https://designhunters.ru/vacancy/1\n",
            encoding="utf-8",
        )

        custom_selectors = DesignHuntersSelectors(
            title=".custom-title",
            company=".custom-company",
        )

        parser = DesignHuntersParser(
            csv_path=csv_file,
            selectors=custom_selectors,
        )

        self.assertEqual(
            parser._selectors.title,
            ".custom-title",
        )

        self.assertEqual(
            parser._selectors.company,
            ".custom-company",
        )

    async def test_extract_text_from_element(self) -> None:
        mock_element = AsyncMock()
        mock_element.inner_text = AsyncMock(
            return_value="  Test text  "
        )

        mock_page = AsyncMock()
        mock_page.query_selector = AsyncMock(
            return_value=mock_element
        )

        text = await DesignHuntersParser._extract_text(
            mock_page,
            ".test-selector",
        )

        self.assertEqual(text, "Test text")

    async def test_extract_text_optional_returns_none(
        self,
    ) -> None:
        mock_page = AsyncMock()
        mock_page.query_selector = AsyncMock(
            return_value=None
        )

        text = await DesignHuntersParser._extract_text(
            mock_page,
            ".missing-selector",
            optional=True,
        )

        self.assertIsNone(text)

    async def test_extract_text_required_raises_error(
        self,
    ) -> None:
        mock_page = AsyncMock()
        mock_page.query_selector = AsyncMock(
            return_value=None
        )

        with self.assertRaises(ValueError):
            await DesignHuntersParser._extract_text(
                mock_page,
                ".missing-selector",
                optional=False,
            )

    @patch(
        "market_system.core.parsers.designhunters.designhunters_parser.write_parser_log"
    )
    @patch(
        "market_system.core.parsers.designhunters.designhunters_parser.async_playwright"
    )
    async def test_parse_creates_publications(
        self,
        playwright_mock,
        write_log_mock,
    ) -> None:
        csv_file = self.temp_path / "urls.csv"
        csv_file.write_text(
            "https://designhunters.ru/vacancy/1\n",
            encoding="utf-8",
        )

        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_page = AsyncMock()

        mock_page.goto = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()
        mock_page.close = AsyncMock()

        mock_element = AsyncMock()
        mock_element.inner_text = AsyncMock(
            return_value="Senior Designer"
        )

        mock_page.query_selector = AsyncMock(
            return_value=mock_element
        )

        mock_context.new_page = AsyncMock(
            return_value=mock_page
        )

        mock_browser.new_context = AsyncMock(
            return_value=mock_context
        )

        mock_browser.close = AsyncMock()

        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch = AsyncMock(
            return_value=mock_browser
        )

        playwright_mock.return_value.__aenter__ = AsyncMock(
            return_value=mock_playwright
        )

        playwright_mock.return_value.__aexit__ = AsyncMock()

        parser = DesignHuntersParser(csv_path=csv_file)

        publications = await parser.parse()

        self.assertEqual(len(publications), 1)
        self.assertEqual(
            publications[0].source_type_name,
            "designhunters",
        )

        self.assertEqual(
            publications[0].source_name,
            "designhunters.ru",
        )

        self.assertEqual(
            publications[0].url,
            "https://designhunters.ru/vacancy/1",
        )

        self.assertIn(
            "Senior Designer",
            publications[0].text,
        )

        write_log_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()