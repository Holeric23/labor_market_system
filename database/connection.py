"""Connection setup: callers own transactions, connections never escape requests."""
import os
import psycopg
from psycopg.rows import dict_row


def connect():
    dsn = os.environ.get('DATABASE_URL', '')
    if not dsn.strip():
        raise RuntimeError('DATABASE_URL is required')
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)
