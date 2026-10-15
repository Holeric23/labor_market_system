from market_system.sqlite import connect as sqlite_connect
"""Verify migrated data and non-destructive repeat import without training."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from market_system.initial_import import inventory, sha256
from market_system.paths import PROJECT_ROOT

root = PROJECT_ROOT
manifest = json.loads((root/'data/import_manifest.json').read_text(encoding='utf-8'))
checks = {}
for name, relative in [('app', 'data/app.db'), ('annotations', 'research/review/annotations.sqlite')]:
    with closing(sqlite_connect((root/relative).as_uri()+'?mode=ro',uri=True)) as con:
        actual = inventory(con)
        assert actual == manifest['databases'][name]['snapshot'], name
        assert con.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert con.execute('PRAGMA foreign_key_check').fetchall() == []
    checks[name] = {'all_rows_and_schema_match_import_snapshot': True,
                    'integrity_check': 'ok', 'foreign_key_check': [],
                    'table_counts': {key:value['count'] for key,value in actual['tables'].items()}}
for artifact in manifest['artifacts']:
    path = root/artifact['target']
    assert sha256(path) == artifact['sha256'], artifact['target']
    assert not path.is_symlink() and path.stat().st_nlink == 1
checks['verified_artifacts'] = len(manifest['artifacts'])
before = sha256(root/'data/app.db')
repeat = subprocess.run([sys.executable,'-m','market_system.initial_import',
                         '--source-file','config/import-source.json','--model',manifest['model_version']],
                        cwd=root,capture_output=True,text=True)
assert repeat.returncode == 2, (repeat.returncode,repeat.stderr)
assert before == sha256(root/'data/app.db')
checks['repeat_import'] = {'exit_code': repeat.returncode, 'working_database_unchanged': True,
                           'message': repeat.stderr.strip()}
checks['verified_at_utc'] = datetime.now(timezone.utc).isoformat()
(root/'.artifacts/stage1-verification.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(checks,ensure_ascii=False,indent=2))
