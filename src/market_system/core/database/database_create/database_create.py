from market_system.sqlite import connect as sqlite_connect
from market_system.paths import local_path, DATABASE_PATH
import argparse
import sqlite3
from pathlib import Path


DEFAULT_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

from market_system.paths import DATABASE_PATH as DEFAULT_DATABASE_PATH

def initialize_database(
    database_path: str | Path,
    schema_path: str | Path = DEFAULT_SCHEMA_PATH,
) -> None:
    """Create the SQLite database schema if it does not already exist."""
    database_path = local_path(database_path)
    schema_path = Path(schema_path)

    database_path.parent.mkdir(parents=True, exist_ok=True)

    schema_sql = schema_path.read_text(encoding="utf-8")

    connection = sqlite_connect(database_path)
    try:
        with connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(schema_sql)
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Initialize the project SQLite database."
    )
    parser.add_argument(
        "--database",
        nargs="?",
        default=str(DEFAULT_DATABASE_PATH),
        help="Path to the SQLite database file",
    )
    parser.add_argument(
        "--schema",
        default=str(DEFAULT_SCHEMA_PATH),
        help="Path to schema.sql",
    )
    args = parser.parse_args()

    initialize_database(args.database, args.schema)
    print(f"Database initialized: {args.database}")


if __name__ == "__main__":
    from market_system.runtime import enable_guard
    enable_guard()
    main()
