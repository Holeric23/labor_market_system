$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force .artifacts/tmp,.artifacts/mpl | Out-Null
$env:TEMP = "$PWD\.artifacts\tmp"
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
$env:MPLCONFIGDIR = "$PWD\.artifacts\mpl"
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
& .venv/Scripts/python.exe -m pip check
if ($LASTEXITCODE) { throw 'Dependency check failed' }
& .venv/Scripts/python.exe scripts/check_imports.py
if ($LASTEXITCODE) { throw 'Import check failed' }
& .venv/Scripts/python.exe -m pytest --basetemp=.artifacts/pytest-tmp -q
if ($LASTEXITCODE) { throw 'Tests failed' }
& .venv/Scripts/python.exe scripts/check_stage0.py
if ($LASTEXITCODE) { throw 'Source isolation check failed' }
