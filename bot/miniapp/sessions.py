"""Server-side Mini App sessions and one-time Telegram launch proofs."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from database.connection import connect


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def create_anonymous():
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    with connect() as con:
        con.execute('DELETE FROM admin_sessions WHERE expires_at <= now()')
        con.execute('INSERT INTO admin_sessions(token_hash,csrf,expires_at) VALUES (%s,%s,%s)', (_hash(token),csrf,datetime.now(timezone.utc)+timedelta(minutes=10)))
    return token, csrf


def get(token):
    if not token:
        return None
    with connect() as con:
        return con.execute('SELECT s.*,t.role,t.active FROM admin_sessions s LEFT JOIN staff t ON t.telegram_id=s.telegram_id WHERE s.token_hash=%s AND s.expires_at>now()', (_hash(token),)).fetchone()


def login(old_token, telegram_id, proof, proof_expires):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with connect() as con:
        owner = con.execute("SELECT 1 FROM staff WHERE telegram_id=%s AND role='owner' AND active", (telegram_id,)).fetchone()
        if not owner:
            raise PermissionError('Недостаточно прав')
        con.execute('DELETE FROM used_telegram_proofs WHERE expires_at <= now()')
        inserted = con.execute('INSERT INTO used_telegram_proofs(proof_hash,expires_at) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING proof_hash', (proof,datetime.fromtimestamp(proof_expires,timezone.utc))).fetchone()
        if not inserted:
            raise ValueError('Данные Telegram уже использованы')
        if old_token:
            con.execute('DELETE FROM admin_sessions WHERE token_hash=%s', (_hash(old_token),))
        con.execute('INSERT INTO admin_sessions(token_hash,telegram_id,csrf,expires_at) VALUES (%s,%s,%s,%s)', (_hash(token),telegram_id,csrf,datetime.now(timezone.utc)+timedelta(hours=1)))
    return token


def logout(token):
    with connect() as con:
        con.execute('DELETE FROM admin_sessions WHERE token_hash=%s', (_hash(token),))
