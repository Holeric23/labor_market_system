import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from market_system.core.database import DatabaseManager
from market_system.core.parsers import Parser
from market_system.core.utils.dataclasses import RawPublication


IMPORT_BATCH_SIZE = 100


@dataclass
class ImportError:
    url: str
    error_type: str
    error_message: str


@dataclass
class ImportReport:
    processed: int = 0
    created: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[ImportError] = field(default_factory=list)


class Importer:
    """Coordinates parser execution and publication import."""

    def __init__(
        self,
        database_manager: DatabaseManager,
    ) -> None:
        self._database_manager = database_manager

    async def import_from(
        self,
        parser: Parser,
    ) -> ImportReport:
        started_at = datetime.now()

        print(f"[{started_at.isoformat()}] Import started")

        publications = await parser.parse()

        report = ImportReport()

        for batch_start in range(0, len(publications), IMPORT_BATCH_SIZE):
            batch_end = min(batch_start + IMPORT_BATCH_SIZE, len(publications))
            batch = publications[batch_start:batch_end]

            self._import_batch(batch, report)

        finished_at = datetime.now()
        duration = finished_at - started_at

        print(f"[{finished_at.isoformat()}] Import finished")
        print(f"Duration: {duration}")
        print("STATISTICS")
        print("-" * 40)
        print(f"Publications processed: {report.processed}")
        print(f"Publications created:   {report.created}")
        print(f"Publications skipped:   {report.skipped}")
        print(f"Publications failed:    {report.failed}")

        if report.errors:
            print(f"Total errors: {len(report.errors)}")
        else:
            print("No errors.")

        return report

    def _import_batch(
        self,
        publications: list[RawPublication],
        report: ImportReport,
    ) -> None:
        """Import a batch of publications in a single transaction."""
        with self._database_manager.transaction():
            for publication in publications:
                report.processed += 1

                try:
                    created = self._import_publication(publication)

                    if created:
                        report.created += 1
                    else:
                        report.skipped += 1

                except sqlite3.OperationalError:
                    raise

                except sqlite3.DatabaseError as error:
                    report.failed += 1
                    self._add_error(report, publication, error)

                except Exception as error:
                    report.failed += 1
                    self._add_error(report, publication, error)

    def _import_publication(
        self,
        publication: RawPublication,
    ) -> bool:
        source_type_id = (
            self._database_manager
            .get_or_create_source_type(
                publication.source_type_name
            )
        )

        source_id = (
            self._database_manager
            .get_or_create_source(
                source_type_id,
                publication.source_name,
            )
        )

        content_id = (
            self._database_manager
            .get_or_create_content(
                publication.text
            )
        )

        return (
            self._database_manager
            .create_publication(
                content_id,
                source_id,
                publication.url,
                publication.published_at,
            )
        )

    @staticmethod
    def _add_error(
        report: ImportReport,
        publication: RawPublication,
        error: Exception,
    ) -> None:
        error_info = ImportError(
            url=publication.url,
            error_type=type(error).__name__,
            error_message=str(error),
        )

        report.errors.append(error_info)

        print(
            f"ERROR: Failed to import {publication.url} "
            f"[{type(error).__name__}]: {error}"
        )