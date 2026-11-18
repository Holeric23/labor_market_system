"""Explicit, repeatable migration: no training, collection, or sends."""
import hashlib
from pathlib import Path
from database.connection import connect


def migrate():
    with connect() as con:
        con.execute('SELECT pg_advisory_xact_lock(%s)', (174925601,))
        files = sorted(Path(__file__).parent.glob('[0-9][0-9][0-9]_*.sql'))
        for path in files:
            version = int(path.name[:3])
            text = path.read_text(encoding='utf-8')
            checksum = hashlib.sha256(text.encode()).hexdigest()
            exists = con.execute("SELECT to_regclass('public.schema_migrations') AS name").fetchone()['name']
            row = con.execute('SELECT sha256 FROM schema_migrations WHERE version=%s', (version,)).fetchone() if exists else None
            if row:
                if row['sha256'] != checksum:
                    raise RuntimeError('Unknown or incompatible database schema')
                continue
            if version != 1 and not exists:
                raise RuntimeError('Initial database schema is missing')
            con.execute(text)
            con.execute('INSERT INTO schema_migrations(version,sha256) VALUES (%s,%s)', (version,checksum))


if __name__ == '__main__':
    migrate()
    print('Database schema is ready.')
