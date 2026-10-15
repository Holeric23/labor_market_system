"""Local runtime configuration and write boundary for command execution."""
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import unquote, urlsplit, parse_qs
from .paths import PROJECT_ROOT, MODEL_CONFIG, local_path

_guard_installed = False


def enable_guard():
    """Application guard, not an OS sandbox for untrusted native code."""
    global _guard_installed
    if _guard_installed:
        return
    # Imports may otherwise write bytecode into the external Python runtime.
    # Keep the write boundary intact for both this process and its children.
    sys.dont_write_bytecode = True
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    tmp = local_path('.artifacts/tmp')
    tmp.mkdir(parents=True, exist_ok=True)
    for key in ('TEMP', 'TMP', 'TMPDIR'):
        os.environ[key] = str(tmp)
    os.environ['MPLCONFIGDIR'] = str(local_path('.artifacts/mpl'))
    os.environ['PIP_CACHE_DIR'] = str(local_path('.artifacts/pip-cache'))
    os.environ['XDG_CACHE_HOME'] = str(local_path('.artifacts/cache'))
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(local_path('.artifacts/playwright'))
    tempfile.tempdir = str(tmp)

    def check(value):
        if isinstance(value, (str, bytes, os.PathLike)):
            if os.fsdecode(value) == os.devnull:
                return  # discarded process input/output is not a filesystem write
            # Audit paths are relative to process cwd, not to project root.
            local_path(Path(os.fsdecode(value)).absolute(), relocate=False)

    def audit(event, args):
        if event == 'open':
            path, mode, flags = args
            if (mode and any(c in mode for c in 'wax+')) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                check(path)
        elif event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.utime', 'os.truncate'):
            check(args[0])
        elif event in ('os.rename', 'os.link', 'os.symlink'):
            check(args[0]); check(args[1])
        elif event == 'sqlite3.connect':
            value = str(args[0])
            if value == ':memory:':
                return
            if value.startswith('file:'):
                uri = urlsplit(value)
                if parse_qs(uri.query).get('mode') == ['ro']:
                    return
                value = unquote(uri.path)
                if os.name == 'nt' and value.startswith('/'):
                    value = value[1:]
            check(value)
    sys.addaudithook(audit)
    _guard_installed = True


def load_config(path=None):
    path = path or os.environ.get('MARKET_RUNTIME_CONFIG', MODEL_CONFIG)
    values = json.loads(local_path(path).read_text(encoding='utf-8'))
    required = {'database', 'model', 'skill_lexicon', 'occupation_lexicon', 'annotation_database', 'dataset'}
    if set(values) != required:
        raise ValueError('Unexpected runtime configuration keys')
    return {key: local_path(value) for key, value in values.items()}
