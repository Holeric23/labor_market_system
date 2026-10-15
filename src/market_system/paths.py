"""Canonical project-local paths; no directories are created on import."""
from pathlib import Path
import json
import os
from pathlib import PureWindowsPath

SOURCE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(os.environ.get('MARKET_HOME', SOURCE_ROOT)).resolve()
OLD_PROJECT = r'C:\Study\Анализ данных\Итоговый проект\Python_research_project'
PACKAGE_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
STATE_DIR = PROJECT_ROOT / "state"
DATABASE_PATH = DATA_DIR / "app.db"
MODEL_CONFIG = PROJECT_ROOT / "config/runtime.json"

def local_path(value, *, relocate=True):
    # Deny the read-only research tree even if somebody selects it as MARKET_HOME.
    raw = str(value)
    if 'python_research_project' in raw.casefold() or 'python_research_project' in str(PROJECT_ROOT).casefold():
        raise ValueError('The old research project is read-only and cannot be a working path')
    already_local = Path(raw).is_absolute() and Path(raw).resolve().is_relative_to(PROJECT_ROOT)
    if relocate and not already_local:
        aliases = json.loads(os.environ.get('MARKET_PATH_ALIASES', '{}'))
        for origin, target in aliases.items():
            # Windows snapshot paths must also resolve on Linux.
            kind = PureWindowsPath if PureWindowsPath(origin).drive else Path
            try:
                suffix = kind(raw).relative_to(kind(origin))
            except ValueError:
                continue
            raw = str(Path(target).joinpath(*suffix.parts))
            break
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    resolved = path.resolve()
    if 'python_research_project' in str(resolved).casefold():
        raise ValueError('The old research project is read-only')
    if not resolved.is_relative_to(PROJECT_ROOT):
        raise ValueError(f"Working path must stay inside project: {value}")
    if resolved.exists() and resolved.is_file() and resolved.stat().st_nlink != 1:
        raise ValueError(f"Hard-linked working file is forbidden: {value}")
    return resolved
