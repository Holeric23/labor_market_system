"""Prove local smoke succeeds while old-project I/O is explicitly denied."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit

root = Path(__file__).resolve().parents[1]
old = Path(json.loads((root/'config/import-source.json').read_text(encoding='utf-8'))['source_root']).resolve()
attempts = []

def audit(event, args):
    if event not in ('open', 'sqlite3.connect', 'os.listdir', 'os.scandir'):
        return
    value = args[0]
    if not isinstance(value, (str, bytes, os.PathLike)):
        return
    value = os.fsdecode(value)
    if value.startswith('file:'):
        value = unquote(urlsplit(value).path)
        if os.name == 'nt' and value.startswith('/'):
            value = value[1:]
    path = Path(value).resolve()
    if path.is_relative_to(old):
        attempts.append(str(path))
        raise PermissionError('Old project access denied by isolation verification')

sys.dont_write_bytecode = True
sys.addaudithook(audit)
from market_system.__main__ import smoke
result = smoke()
assert not attempts, attempts
result['old_project_access_attempts'] = attempts
result['old_project_access_denied_during_run'] = True
(root/'.artifacts/stage1-isolation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print('Local startup/model/3 real texts: passed with old-project I/O denied')
