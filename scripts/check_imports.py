"""Import every application module with writes and network denied by audit hook."""
import importlib
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
root = Path(__file__).resolve().parents[1]
os.environ['MPLCONFIGDIR'] = str(root / '.artifacts/mpl')
# Third-party imports may initialize their own cache; keep that warm-up local.
# The guard below applies to the application import phase.
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot
import catboost
import sklearn
import pandas
import aiogram
import telethon
import pymorphy3

def no_training(*args, **kwargs):
    raise RuntimeError('Training during import')
catboost.CatBoostClassifier.fit = no_training

def guard(event, args):
    if event in {'socket.connect', 'socket.bind', 'subprocess.Popen', 'os.system',
                 'os.mkdir', 'os.remove', 'os.rename', 'sqlite3.connect'}:
        raise RuntimeError(f'Import side effect: {event}')
    if event == 'open':
        _, mode, flags = args
        if (mode and any(c in mode for c in 'wax+')) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            raise RuntimeError(f'Import writes a file: {args[0]}')
sys.addaudithook(guard)
count = 0
for path in sorted((root / 'src/market_system').rglob('*.py')):
    if path.name == '__main__.py':
        continue  # Explicit command execution, not an import API.
    parts = list(path.relative_to(root / 'src').with_suffix('').parts)
    if parts[-1] == '__init__':
        parts.pop()
    importlib.import_module('.'.join(parts))
    count += 1
print(f'{count} application modules imported without writes, DB opens, network or subprocesses')
