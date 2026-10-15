"""Keep all autonomous test artifacts inside the new project."""
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / '.artifacts/tmp'
TMP.mkdir(parents=True, exist_ok=True)
for name in ('TEMP', 'TMP', 'TMPDIR'):
    os.environ[name] = str(TMP)
tempfile.tempdir = str(TMP)
os.environ['MPLCONFIGDIR'] = str(ROOT / '.artifacts/mpl')
