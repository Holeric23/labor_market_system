import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from market_system.core.database import DatabaseManager, initialize_database
from market_system.core.utils.dataclasses import RawPublication


class DatabaseManagerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temp_dir.name) / "test.sqlite3"
        )

        initialize_database(self.database_path)
        self.db = DatabaseManager(self.database_path)

    def tearDown(self) -> None:
        try:
            self.db.close()
        finally:
            self.temp_dir.cleanup()


class TestRawPublication(unittest.TestCase):
    def test_valid_publication(self) -> None:
        publication = RawPublication(
            source_type_name="telegram",
            source_name="digital_jobster",
            text="Vacancy text",
            url="https://example.com/1",
            published_at=datetime(
                2026,
                8,
                15,
                tzinfo=timezone.utc,
            ),
        )

        self.assertEqual(
            publication.source_type_name,
            "telegram",
        )

        self.assertEqual(
            publication.source_name,
            "digital_jobster",
        )

    def test_empty_required_field_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RawPublication(
                source_type_name="telegram",
                source_name="digital_jobster",
                text="   ",
                url="https://example.com/1",
            )

    def test_wrong_published_at_type_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            RawPublication(
                source_type_name="telegram",
                source_name="digital_jobster",
                text="text",
                url="https://example.com/1",
                published_at="2026-08-15",  # type: ignore[arg-type]
            )


class TestDatabaseManager(DatabaseManagerTestCase):
    def test_source_type_is_get_or_create(self) -> None:
        first_id = self.db.get_or_create_source_type(
            "telegram"
        )

        second_id = self.db.get_or_create_source_type(
            "telegram"
        )

        self.assertEqual(first_id, second_id)

        count = self.db._connection.execute(
            "SELECT COUNT(*) FROM source_types"
        ).fetchone()[0]

        self.assertEqual(count, 1)

    def test_source_is_unique_per_source_type(self) -> None:
        telegram_id = self.db.get_or_create_source_type(
            "telegram"
        )

        job_site_id = self.db.get_or_create_source_type(
            "job_website"
        )

        telegram_source_id = self.db.get_or_create_source(
            telegram_id,
            "example",
        )

        same_telegram_source_id = self.db.get_or_create_source(
            telegram_id,
            "example",
        )

        job_site_source_id = self.db.get_or_create_source(
            job_site_id,
            "example",
        )

        self.assertEqual(
            telegram_source_id,
            same_telegram_source_id,
        )

        self.assertNotEqual(
            telegram_source_id,
            job_site_source_id,
        )

        count = self.db._connection.execute(
            "SELECT COUNT(*) FROM sources"
        ).fetchone()[0]

        self.assertEqual(count, 2)

    def test_content_is_deduplicated_by_sha256(self) -> None:
        first_id = self.db.get_or_create_content(
            "same text"
        )

        second_id = self.db.get_or_create_content(
            "same text"
        )

        third_id = self.db.get_or_create_content(
            "same text with one difference"
        )

        self.assertEqual(first_id, second_id)
        self.assertNotEqual(first_id, third_id)

        row = self.db._connection.execute(
            """
            SELECT text, text_hash
            FROM contents
            WHERE id = ?
            """,
            (first_id,),
        ).fetchone()

        self.assertEqual(
            row["text_hash"],
            DatabaseManager.calculate_text_hash(
                "same text"
            ),
        )

        self.assertEqual(
            row["text"],
            "same text",
        )

    def test_empty_content_is_rejected_before_hashing(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            self.db.get_or_create_content(
                "   \n  "
            )

        count = self.db._connection.execute(
            "SELECT COUNT(*) FROM contents"
        ).fetchone()[0]

        self.assertEqual(count, 0)

    def test_publication_is_created_and_duplicate_url_is_skipped(
        self,
    ) -> None:
        source_type_id = (
            self.db.get_or_create_source_type(
                "telegram"
            )
        )

        source_id = self.db.get_or_create_source(
            source_type_id,
            "digital_jobster",
        )

        content_id = self.db.get_or_create_content(
            "publication text"
        )

        published_at = datetime(
            2026,
            8,
            15,
            12,
            30,
            tzinfo=timezone.utc,
        )

        created = self.db.create_publication(
            content_id,
            source_id,
            "https://example.com/post/1",
            published_at,
        )

        skipped = self.db.create_publication(
            content_id,
            source_id,
            "https://example.com/post/1",
            None,
        )

        self.assertTrue(created)
        self.assertFalse(skipped)

        row = self.db._connection.execute(
            """
            SELECT url, published_at
            FROM publications
            """
        ).fetchone()

        self.assertEqual(
            row["url"],
            "https://example.com/post/1",
        )

        self.assertEqual(
            row["published_at"],
            published_at.isoformat(),
        )

    def test_foreign_keys_are_enabled(self) -> None:
        foreign_keys = self.db._connection.execute(
            "PRAGMA foreign_keys"
        ).fetchone()[0]

        self.assertEqual(foreign_keys, 1)

        with self.assertRaises(
            sqlite3.IntegrityError
        ):
            self.db.create_publication(
                content_id=999,
                source_id=999,
                url="https://example.com/invalid",
                published_at=None,
            )

    def test_transaction_rolls_back_all_changes_on_error(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            with self.db.transaction():
                source_type_id = (
                    self.db.get_or_create_source_type(
                        "telegram"
                    )
                )

                source_id = self.db.get_or_create_source(
                    source_type_id,
                    "digital_jobster",
                )

                content_id = self.db.get_or_create_content(
                    "will be rolled back"
                )

                self.db.create_publication(
                    content_id,
                    source_id,
                    "   ",
                    None,
                )

        self.assertEqual(
            self.db._connection.execute(
                "SELECT COUNT(*) FROM source_types"
            ).fetchone()[0],
            0,
        )

        self.assertEqual(
            self.db._connection.execute(
                "SELECT COUNT(*) FROM sources"
            ).fetchone()[0],
            0,
        )

        self.assertEqual(
            self.db._connection.execute(
                "SELECT COUNT(*) FROM contents"
            ).fetchone()[0],
            0,
        )

        self.assertEqual(
            self.db._connection.execute(
                "SELECT COUNT(*) FROM publications"
            ).fetchone()[0],
            0,
        )

    def test_single_import_can_be_atomic(self) -> None:
        publication = RawPublication(
            source_type_name="telegram",
            source_name="digital_jobster",
            text="hello",
            url="https://example.com/hello",
            published_at=datetime(
                2026,
                8,
                15,
                tzinfo=timezone.utc,
            ),
        )

        with self.db.transaction():
            source_type_id = (
                self.db.get_or_create_source_type(
                    publication.source_type_name
                )
            )

            source_id = self.db.get_or_create_source(
                source_type_id,
                publication.source_name,
            )

            content_id = self.db.get_or_create_content(
                publication.text
            )

            created = self.db.create_publication(
                content_id,
                source_id,
                publication.url,
                publication.published_at,
            )

        self.assertTrue(created)

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