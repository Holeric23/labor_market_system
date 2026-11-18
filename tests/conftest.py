import os
from urllib.parse import urlsplit
import pytest


@pytest.fixture(scope='session',autouse=True)
def test_database():
    dsn = os.environ.get('TEST_DATABASE_URL','')
    parsed = urlsplit(dsn)
    if not dsn:
        pytest.skip('TEST_DATABASE_URL is not configured')
    if parsed.hostname not in ('127.0.0.1','localhost') or not parsed.path.lstrip('/').startswith('test_'):
        raise RuntimeError('Tests require an explicitly named local test_ database')
    os.environ['DATABASE_URL'] = dsn
    from database.migrate import migrate
    migrate()
