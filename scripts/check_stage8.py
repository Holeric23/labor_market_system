"""Apply outbox schema without enabling access, polling, or real delivery."""
import hashlib
import json
import os
from market_system.paths import DATABASE_PATH, local_path
from market_system.runtime import enable_guard
from market_system.search.store import connect
from market_system.telegram_bot.store import BotStore

enable_guard()

def snapshot():
    with connect(DATABASE_PATH) as con:
        active=con.execute('SELECT result_set_id FROM active_result_set').fetchone()[0]
        counts={name:con.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in
                ('contents','publications','result_sets','document_processing','document_classes',
                 'search_catalog','result_evidence','result_publications','review_revisions','review_memberships')}
        digest=hashlib.sha256()
        for row in con.execute('SELECT * FROM result_evidence ORDER BY result_set_id,content_id'):
            digest.update(json.dumps(tuple(row),ensure_ascii=False).encode())
        return {'active':active,'counts':counts,'machine_evidence_sha256':digest.hexdigest()}

before=snapshot()
store=BotStore(DATABASE_PATH)
store.initialize()
store.initialize()
after=snapshot()
assert before==after
with connect(DATABASE_PATH) as con:
    counts={name:con.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in
            ('telegram_access','telegram_subscriptions','telegram_seen','telegram_outbox','telegram_deliveries','telegram_attempts')}
    errors=con.execute('PRAGMA foreign_key_check').fetchall()
    migrations=[row[0] for row in con.execute('SELECT version FROM schema_migrations ORDER BY version')]
assert not errors
assert counts['telegram_outbox']==counts['telegram_deliveries']==0
report={'before':before,'after':after,'telegram_counts':counts,'migrations':migrations,
        'foreign_key_errors':len(errors),'live_telegram_tested':False,'real_sends':0,
        'bot_token_in_environment':bool(os.getenv('BOT_TOKEN')),
        'default_env_exists':local_path('.env').is_file(),
        'dedicated_bot_env_exists':local_path('config/telegram-bot.env').is_file(),
        'legacy_bot_state_exists':local_path('state/bot.db').is_file()}
local_path('.artifacts/stage8-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False,indent=2))
