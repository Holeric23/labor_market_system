"""Verified directory snapshots, SQLite Backup API, never overwrite a restore target."""
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import uuid

from market_system.paths import local_path, PROJECT_ROOT
from market_system.sqlite import connect
from market_system.jobs.worker import worker_lock
from .diagnostics import migration_errors

FORMAT = 'labor-market-backup-v1'


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            h.update(block)
    return h.hexdigest()


def regular(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink!=1:
        raise ValueError('Only ordinary independent files can be archived')


def relative(path, home):
    return local_path(path).relative_to(home).as_posix()


def excluded(path):
    return (any(p in ('__pycache__','secrets') for p in path.parts) or path.name.startswith('.env')
            or '.session' in path.name or path.suffix in ('.env','.pyc') or path.name.endswith(('-wal','-shm','.lock')))


def create(config, target=None):
    home = config.home
    target = local_path(target or ('backups/'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]))
    if target.exists():
        raise ValueError('Backup target already exists')
    if not target.is_relative_to(home/'backups') and not target.is_relative_to(home/'.artifacts'):
        raise ValueError('Backup belongs in backups/ or .artifacts/')
    assets = {}
    def add(path):
        path = local_path(path)
        regular(path)
        if excluded(path):
            raise ValueError('Secret/session files are excluded from backups')
        assets[relative(path,home)] = path
    for folder in ('src/market_system','models','data/dictionaries','research/review','vendor','scripts','deploy','docs'):
        for path in (home/folder).rglob('*'):
            if path.is_file() and not excluded(path) and path.suffix not in ('.db','.sqlite'):
                add(path)
    for path in config.paths['model'].rglob('*'):
        if path.is_file() and not excluded(path):
            add(path)
    for name in ('pyproject.toml','requirements.lock.txt','requirements.windows.lock.txt','requirements.linux.lock.txt','README.md','AGENTS.md'):
        path = home/name
        if path.exists():
            add(path)
    for key in ('skill_lexicon','occupation_lexicon','dataset'):
        add(config.paths[key])
    dbs = {relative(config.paths[k],home):config.paths[k] for k in ('database','web_state','bot_state','annotation_database') if config.paths[k].exists()}
    if relative(config.paths['database'],home) not in dbs or relative(config.paths['annotation_database'],home) not in dbs:
        raise ValueError('Catalog and annotations are required')
    # All writers are held until every snapshot and artifact has been captured.
    # This includes web accounts/UI and review, not only the catalog WAL.
    with worker_lock(config.paths['database']), ExitStack() as stack:
        for db in sorted(dbs.values()):
            regular(db)
            writer = stack.enter_context(closing(connect(db,timeout=15)))
            writer.execute('BEGIN IMMEDIATE')
        catalog = stack.enter_context(closing(connect(config.paths['database'].as_uri()+'?mode=ro',uri=True)))
        problems = migration_errors(catalog)
        if problems:
            raise ValueError('Incompatible migrations: '+str(problems))
        for raw, in catalog.execute('SELECT spec_json FROM model_registry'):
            spec = json.loads(raw)
            for name in spec['files']:
                add(name)
        for raw, in catalog.execute('SELECT parameters_json FROM jobs'):
            spec = json.loads(raw)['request']['spec']
            for name in spec.get('files',{}):
                add(name)
        for raw, in catalog.execute('SELECT parameters_json FROM managed_sources'):
            csv = json.loads(raw).get('csv_path')
            if csv:
                add(csv)
        for raw, in catalog.execute('SELECT config_json FROM collection_jobs'):
            csv = json.loads(raw).get('parameters',{}).get('csv_path')
            if csv:
                add(csv)
        before = {name:sha256(path) for name,path in assets.items()}
        target.mkdir(parents=True)
        files = {}
        for name,path in assets.items():
            destination = target/name
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(path,destination)
            if sha256(destination)!=before[name] or sha256(path)!=before[name]:
                raise ValueError('Artifact changed during backup')
        for name,db in dbs.items():
            destination = target/name
            destination.parent.mkdir(parents=True,exist_ok=True)
            with closing(connect(db.as_uri()+'?mode=ro',uri=True)) as source, closing(connect(destination)) as out:
                source.backup(out,pages=1024)
                out.execute('PRAGMA journal_mode=DELETE')
                if out.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or out.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('Invalid SQLite snapshot')
        # Runtime paths are expressed relative to the snapshot. DB historical specs remain unchanged.
        (target/'config').mkdir(exist_ok=True)
        runtime = {k:relative(v,home) for k,v in config.paths.items() if k in ('database','model','skill_lexicon','occupation_lexicon','annotation_database','dataset')}
        (target/'config/runtime.json').write_text(json.dumps(runtime,indent=2),encoding='utf-8')
        operations = dict(config.values, runtime='config/runtime.json',web_state=relative(config.paths['web_state'],home),
                          bot_state=relative(config.paths['bot_state'],home),bot_env='state/secrets/telegram.env')
        (target/'config/operations.json').write_text(json.dumps(operations,indent=2),encoding='utf-8')
        origins = [str(home)]
        for origin in json.loads(os.environ.get('MARKET_PATH_ALIASES','{}')):
            if origin not in origins:
                origins.append(origin)
        for path in target.rglob('*'):
            if path.is_file():
                files[path.relative_to(target).as_posix()] = {'sha256':sha256(path),'size':path.stat().st_size}
        manifest = {'format':FORMAT,'created_at':datetime.now(timezone.utc).isoformat(), 'origins':origins,
                    'schema_versions':list(range(1,9)), 'sqlite_snapshots':list(dbs), 'runtime':runtime,
                    'files':files,'secrets_included':False,'sessions_require_reprovisioning':True}
        encoded = json.dumps(manifest,ensure_ascii=False,indent=2).encode()
        (target/'manifest.json').write_bytes(encoded)
        (target/'manifest.sha256').write_text(hashlib.sha256(encoded).hexdigest(),encoding='ascii')
    return target


def verify_bundle(bundle):
    bundle = local_path(bundle)
    regular(bundle/'manifest.json')
    if sha256(bundle/'manifest.json')!=(bundle/'manifest.sha256').read_text(encoding='ascii').strip():
        raise ValueError('Manifest checksum mismatch')
    manifest = json.loads((bundle/'manifest.json').read_text(encoding='utf-8'))
    if manifest['format']!=FORMAT or manifest['schema_versions']!=list(range(1,9)) or manifest['secrets_included']:
        raise ValueError('Incompatible backup format/schema')
    declared = set(manifest['files']) | {'manifest.json','manifest.sha256'}
    found = {p.relative_to(bundle).as_posix() for p in bundle.rglob('*') if p.is_file()}
    if found != declared:
        raise ValueError('Backup file inventory mismatch')
    for name,meta in manifest['files'].items():
        pure = PurePosixPath(name)
        if pure.is_absolute() or '..' in pure.parts or '\\' in name or ':' in name:
            raise ValueError('Unsafe backup filename')
        path = local_path(bundle/name)
        if not path.is_relative_to(bundle) or excluded(path):
            raise ValueError('Unsafe backup path')
        regular(path)
        if path.stat().st_size!=meta['size'] or sha256(path)!=meta['sha256']:
            raise ValueError('Backup checksum mismatch: '+name)
    for name in manifest['sqlite_snapshots']:
        if name not in manifest['files']:
            raise ValueError('Missing SQLite snapshot')
        with closing(connect((bundle/name).as_uri()+'?mode=ro&immutable=1',uri=True)) as con:
            if con.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or con.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('Snapshot integrity failed')
            if name==manifest['runtime']['database'] and migration_errors(con):
                raise ValueError('Snapshot migration compatibility failed')
    return manifest


def restore(bundle, target):
    bundle = local_path(bundle)
    target = local_path(target)
    if target.exists() or target==PROJECT_ROOT or target.is_relative_to(bundle):
        raise ValueError('Restore requires a new separate directory; overwrite is forbidden')
    manifest = verify_bundle(bundle)  # complete verification before creating the destination
    values = json.loads((bundle/'config/operations.json').read_text(encoding='utf-8'))
    runtime = json.loads((bundle/'config/runtime.json').read_text(encoding='utf-8'))
    for name in [*runtime.values(),values['web_state'],values['bot_state'],values['bot_env'],values['runtime']]:
        pure = PurePosixPath(name)
        if pure.is_absolute() or '..' in pure.parts or '\\' in name or ':' in name:
            raise ValueError('Restore configuration must use relative paths inside the destination')
        if not local_path(target/name).is_relative_to(target):
            raise ValueError('Restore configuration escapes destination')
    target.mkdir(parents=True)
    for name,meta in manifest['files'].items():
        destination = target/name
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(bundle/name,destination)
        if sha256(destination)!=meta['sha256']:
            raise ValueError('Restored checksum mismatch')
    shutil.copyfile(bundle/'manifest.json',target/'restore-manifest.json')
    shutil.copyfile(bundle/'manifest.sha256',target/'restore-manifest.sha256')
    (target/'config/relocation.json').write_text(json.dumps({'origins':manifest['origins']}),encoding='utf-8')
    filename = target/'config/operations.json'
    values.update(mode='local',host='127.0.0.1',allowed_hosts=['127.0.0.1','localhost','[::1]'],bot_enabled=False,delivery_enabled=False)
    filename.write_text(json.dumps(values,indent=2),encoding='utf-8')
    # Old browser sessions are deliberately invalidated; password hashes and users persist.
    web = target/values['web_state']
    if web.exists():
        with closing(connect(web)) as con:
            con.execute('DELETE FROM web_sessions')
            con.commit()
    return target
