"""All application SQLite connections enable foreign keys, including readers."""
import sqlite3

def connect(*args, **kwargs):
    connection = sqlite3.connect(*args, **kwargs)
    try:
        connection.execute('PRAGMA foreign_keys=ON')
        if connection.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
            raise RuntimeError('SQLite foreign keys could not be enabled')
        return connection
    except Exception:
        connection.close()
        raise
