"""Shared proof for the private bot-to-parser login channel."""
import hashlib
import hmac


def shared_proof(database_url):
    if not database_url:
        raise ValueError('DATABASE_URL is required')
    return hmac.new(database_url.encode(),b'parser-login-v1',hashlib.sha256).hexdigest()
