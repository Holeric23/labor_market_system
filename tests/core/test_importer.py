import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from market_system.core.database import (
    DatabaseManager,
    initialize_database,
)
from market_system.core.importer import Importer, ImportReport
from market_system.core.parsers import Parser
from market_system.core.utils.dataclasses import RawPublication


class FakeParser(Parser):
    def __init__(
        self,
        publications: list[RawPublication],
    ) -> None:
        self._publications = publications

    async def parse(
        self,
    ) -> list[RawPublication]:
        return self._publications


class FailingParser(Parser):
    async def parse(
        self,
    ) -> list[RawPublication]:
        raise RuntimeError(
            "Parser failed"
        )


class TestImporter(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()

        self.database_path = (
            Path(self.temp_dir.name)
            / "test.sqlite3"
        )

        initialize_database(
            self.database_path
        )

        self.db = DatabaseManager(
            self.database_path
        )

        self.importer = Importer(
            self.db
        )

    def tearDown(self) -> None:
        try:
            self.db.close()
        finally:
            self.temp_dir.cleanup()

    async def test_imports_publications(
        self,
    ) -> None:
        published_at = datetime(
            2026,
            8,
            17,
            12,
            0,
            tzinfo=timezone.utc,
        )

        publications = [
            RawPublication(
                source_type_name="telegram",
                source_name="channel_one",
                text="First publication",
                url="https://t.me/channel_one/1",
                published_at=published_at,
            ),
            RawPublication(
                source_type_name="telegram",
                source_name="channel_one",
                text="Second publication",
                url="https://t.me/channel_one/2",
                published_at=None,
            ),
        ]

        parser = FakeParser(
            publications
        )

        report = await self.importer.import_from(
            parser
        )

        self.assertIsInstance(
            report,
            ImportReport,
        )

        self.assertEqual(
            report.processed,
            2,
        )

        self.assertEqual(
            report.created,
            2,
        )

        self.assertEqual(
            report.skipped,
            0,
        )

        self.assertEqual(
            report.failed,
            0,
        )

        self.assertEqual(
            report.errors,
            [],
        )

        self.assertEqual(
            self.db._connection.execute(
                "SELECT COUNT(*) FROM publications"
            ).fetchone()[0],
            2,
        )

    async def test_duplicate_url_is_skipped(
        self,
    ) -> None:
        publications = [
            RawPublication(
                source_type_name="telegram",
                source_name="channel_one",
                text="Same text",
                url="https://t.me/channel_one/1",
            ),
            RawPublication(
                source_type_name="telegram",
                source_name="channel_two",
                text="Same text",
                url="https://t.me/channel_one/1",
            ),
        ]

        parser = FakeParser(
            publications
        )

        report = await self.importer.import_from(
            parser
        )

        self.assertEqual(
            report.processed,
            2,
        )

        self.assertEqual(
            report.created,
            1,
        )

        self.assertEqual(
            report.skipped,
            1,
        )

        self.assertEqual(
            report.failed,
            0,
        )

        self.assertEqual(
            self.db._connection.execute(
                "SELECT COUNT(*) FROM publications"
            ).fetchone()[0],
            1,
        )

        self.assertEqual(
            self.db._connection.execute(
                "SELECT COUNT(*) FROM contents"
            ).fetchone()[0],
            1,
        )

    async def test_identical_text_from_different_publications_uses_one_content(
        self,
    ) -> None:
        publications = [
            RawPublication(
                source_type_name="telegram",
                source_name="channel_one",
                text="Same text",
                url="https://t.me/channel_one/1",
            ),
            RawPublication(
                source_type_name="telegram",
                source_name="channel_two",
                text="Same text",
                url="https://t.me/channel_two/1",
            ),
        ]

        parser = FakeParser(
            publications
        )

        report = await self.importer.import_from(
            parser
        )

        self.assertEqual(
            report.created,
            2,
        )

        self.assertEqual(
            self.db._connection.execute(
                "SELECT COUNT(*) FROM publications"
            ).fetchone()[0],
            2,
        )

        self.assertEqual(
            self.db._connection.execute(
                "SELECT COUNT(*) FROM contents"
            ).fetchone()[0],
            1,
        )

    async def test_parser_error_is_propagated(
        self,
    ) -> None:
        parser = FailingParser()

        with self.assertRaises(RuntimeError):
            await self.importer.import_from(
                parser
            )

        self.assertEqual(
            self.db._connection.execute(
                "SELECT COUNT(*) FROM publications"
            ).fetchone()[0],
            0,
        )

    async def test_import_continues_after_publication_error(
        self,
    ) -> None:
        publications = [
            RawPublication(
                source_type_name="telegram",
                source_name="channel",
                text="Valid publication",
                url="https://t.me/channel/1",
            ),
            RawPublication(
                source_type_name="telegram",
                source_name="channel",
                text="Problematic publication",
                url="https://t.me/channel/2",
            ),
            RawPublication(
                source_type_name="telegram",
                source_name="channel",
                text="Another valid publication",
                url="https://t.me/channel/3",
            ),
        ]

        parser = FakeParser(
            publications
        )

        original_import = (
            self.importer._import_publication
        )
        call_count = [0]

        def failing_import(
            publication: RawPublication,
        ) -> bool:
            call_count[0] += 1

            if call_count[0] == 2:
                raise ValueError(
                    "simulated import error"
                )

            return original_import(publication)

        self.importer._import_publication = (
            failing_import
        )

        report = await self.importer.import_from(
            parser
        )

        self.assertEqual(
            report.processed,
            3,
        )

        self.assertEqual(
            report.created,
            2,
        )

        self.assertEqual(
            report.failed,
            1,
        )

        self.assertEqual(
            report.skipped,
            0,
        )

        self.assertEqual(
            len(report.errors),
            1,
        )

        self.assertEqual(
            report.errors[0].url,
            "https://t.me/channel/2",
        )

        self.assertEqual(
            report.errors[0].error_type,
            "ValueError",
        )

        self.assertEqual(
            self.db._connection.execute(
                "SELECT COUNT(*) FROM publications"
            ).fetchone()[0],
            2,
        )

    async def test_single_import_can_be_atomic(
        self,
    ) -> None:
        publication = RawPublication(
            source_type_name="telegram",
            source_name="digital_jobster",
            text="hello",
            url="https://t.me/digital_jobster/1",
        )

        parser = FakeParser(
            [publication]
        )

        report = await self.importer.import_from(
            parser
        )

        self.assertEqual(
            report.created,
            1,
        )

        self.assertEqual(
            self.db._connection.execute(
                "SELECT COUNT(*) FROM source_types"
            ).fetchone()[0],
            1,
        )

        self.assertEqual(
            self.db._connection.execute(
                "SELECT COUNT(*) FROM sources"
            ).fetchone()[0],
            1,
        )

        self.assertEqual(
            self.db._connection.execute(
                "SELECT COUNT(*) FROM contents"
            ).fetchone()[0],
            1,
        )

        self.assertEqual(
            self.db._connection.execute(
                "SELECT COUNT(*) FROM publications"
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()