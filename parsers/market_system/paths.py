"""Confine parser sessions and model artifacts to this installation."""
import os
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get('APP_HOME', Path(__file__).resolve().parents[2])).resolve()
STATE_DIR = PROJECT_ROOT/'state'
DATABASE_PATH = PROJECT_ROOT/'data'/'legacy-unused.db'


def local_path(value):
    path = Path(value)
    resolved = (PROJECT_ROOT/path if not path.is_absolute() else path).resolve()
    if not resolved.is_relative_to(PROJECT_ROOT):
        raise ValueError('Path must remain inside the application directory')
    return resolved
