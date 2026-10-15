"""Additive, transactional search index migration; no result changes."""
import argparse
import hashlib
import sqlite3
from pathlib import Path
from market_system.paths import DATABASE_PATH
from .store import connect, migrate, now


def optimize(path=DATABASE_PATH):
    migrate(path)
    sql = Path(__file__).with_name('002_search.sql').read_text(encoding='utf-8-sig')
    checksum = hashlib.sha256(sql.encode()).hexdigest()
    with connect(path) as con:
        con.execute('BEGIN IMMEDIATE')
        try:
            row = con.execute('SELECT sha256 FROM schema_migrations WHERE version=2').fetchone()
            if row:
                if row[0] != checksum:
                    raise ValueError('Migration checksum changed')
            else:
                statement = ''
                for line in sql.splitlines(True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        con.execute(statement)
                        statement = ''
                con.execute('INSERT INTO schema_migrations VALUES(2,?,?)', (checksum, now()))
            con.commit()
        except Exception:
            con.rollback()
            raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=str(DATABASE_PATH))
    optimize(parser.parse_args().db)
