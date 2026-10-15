from market_system.sqlite import connect as sqlite_connect
"""Explicit, non-destructive initial import. Never an update command."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sqlite3
import uuid

from .paths import PROJECT_ROOT, local_path


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def inventory(connection):
    schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    result = {'schema_sha256': hashlib.sha256(json.dumps(schema).encode()).hexdigest(), 'tables': {}}
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
        columns = connection.execute(f'PRAGMA table_info({quote(name)})').fetchall()
        order = ','.join(quote(c[1]) for c in sorted(columns, key=lambda c: c[5]) if c[5])
        if not order:
            order = ','.join(quote(c[1]) for c in columns)
        digest = hashlib.sha256()
        count = 0
        for row in connection.execute(f'SELECT * FROM {quote(name)} ORDER BY {order}'):
            encoded = json.dumps(row, ensure_ascii=False, separators=(',', ':'),
                                 default=lambda value: {'bytes_hex': value.hex()}).encode('utf-8')
            digest.update(len(encoded).to_bytes(8, 'big'))
            digest.update(encoded)
            count += 1
        result['tables'][name] = {'count': count, 'content_sha256': digest.hexdigest()}
    result['user_version'] = connection.execute('PRAGMA user_version').fetchone()[0]
    result['application_id'] = connection.execute('PRAGMA application_id').fetchone()[0]
    return result


def backup(source, destination):
    """Compare every row against the SAME read transaction used by backup()."""
    destination = local_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    with closing(sqlite_connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src:
        src.execute('PRAGMA query_only=ON')
        src.execute('BEGIN')
        src.execute('SELECT count(*) FROM sqlite_master').fetchone()
        with closing(sqlite_connect(destination)) as dst:
            src.backup(dst, pages=1024)
            expected = inventory(src)
            actual = inventory(dst)
            if expected != actual:
                raise ValueError('Source snapshot and backup differ')
            integrity = dst.execute('PRAGMA integrity_check').fetchall()
            foreign_keys = dst.execute('PRAGMA foreign_key_check').fetchall()
            if integrity != [('ok',)] or foreign_keys:
                raise ValueError(f'Invalid backup: {integrity}, {foreign_keys}')
        src.rollback()
    return {'source': str(source), 'snapshot': expected, 'integrity_check': 'ok',
            'foreign_key_check': [], 'backup_sha256': sha256(destination)}


def copy_checked(source, destination):
    destination = local_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    before = sha256(source)
    shutil.copyfile(source, destination)
    after = sha256(source)
    if before != after or sha256(destination) != before:
        raise ValueError(f'Artifact changed during copy: {source}')
    return {'source': str(source), 'sha256': before, 'bytes': destination.stat().st_size}


def initial_import(source, model_name, target=PROJECT_ROOT):
    target = local_path(target)
    app = target / 'data/app.db'
    # Deliberately before source access: repeat import preserves ALL working changes.
    if app.exists():
        raise FileExistsError('Initial import refused: data/app.db exists. Updating is a separate operation.')
    source = Path(source).resolve()
    if not (source/'data/data.db').is_file():
        raise FileNotFoundError('Source data/data.db is missing')
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError('Source and destination must be separate trees')
    if Path(model_name).name != model_name:
        raise ValueError('Model must be one directory name')
    destination_paths = ['data/app.db', 'research/review/annotations.sqlite',
                         'research/review/dataset.json', 'config/runtime.json', 'data/import_manifest.json']
    model = source / 'models' / model_name
    metadata = json.loads((model / 'metadata.json').read_text(encoding='utf-8'))
    artifact_names = set(metadata['artifacts_sha256']) | {'metadata.json'}
    if any(Path(name).name != name for name in artifact_names):
        raise ValueError('Unsafe artifact filename in model metadata')
    files = [(model/name, Path('models')/model_name/name) for name in sorted(artifact_names)]
    files += [(p, Path('data/learning_data')/p.name) for p in sorted((source/'data/learning_data').glob('*.csv'))]
    files += [(source/'core/extraction'/name, Path('data/dictionaries')/name)
              for name in ('skill_lexicon.json', 'occupation_lexicon.json')]
    files += [(source/'research/review/dataset.json', Path('research/review/dataset.json'))]
    # Keep the existing historical split test meaningful without copying experiment models.
    for relative in ('research/results/run_20260930_v2/split.csv', 'research/pilot_sample.json'):
        if (source/relative).exists():
            files.append((source/relative, Path(relative)))
    destination_paths += [relative.as_posix() for _, relative in files]
    for relative in destination_paths:
        if local_path(target/relative).exists():
            raise FileExistsError(f'Initial import target already exists: {relative}')
    target.mkdir(parents=True, exist_ok=True)
    lock = target / '.initial-import.lock'
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(descriptor)
    staging = target / '.artifacts' / ('import-' + uuid.uuid4().hex)
    staging.mkdir(parents=True)
    manifest = {'format_version': 1, 'operation': 'initial_import',
                'started_at_utc': datetime.now(timezone.utc).isoformat(), 'source_root': str(source),
                'target_root': str(target), 'sqlite_version': sqlite3.sqlite_version,
                'model_version': metadata['model_version'], 'model_packages': metadata['packages'],
                'runtime_packages': {p: importlib.metadata.version(p) for p in ('scikit-learn', 'catboost', 'numpy', 'pandas')},
                'artifacts': [], 'databases': {}}
    try:
        for original, relative in files:
            record = copy_checked(original, staging/relative)
            record['target'] = relative.as_posix()
            manifest['artifacts'].append(record)
        for name, expected in metadata['artifacts_sha256'].items():
            if sha256(staging/'models'/model_name/name) != expected:
                raise ValueError(f'Model metadata checksum mismatch: {name}')
        for name, expected in metadata.get('training_files_sha256', {}).items():
            if Path(name).name != name or sha256(staging/'data/learning_data'/name) != expected:
                raise ValueError(f'Training input checksum mismatch: {name}')
        dataset_source = source/'research/review/dataset.json'
        dataset_hash = sha256(staging/'research/review/dataset.json')
        manifest['databases']['annotations'] = backup(source/'research/review/annotations.sqlite', staging/'research/review/annotations.sqlite')
        if sha256(dataset_source) != dataset_hash:
            raise ValueError('Review dataset changed while backing up revisions; retry from a stable source')
        dataset = json.loads((staging/'research/review/dataset.json').read_text(encoding='utf-8'))
        with closing(sqlite_connect((staging/'research/review/annotations.sqlite').as_uri()+'?mode=ro', uri=True)) as con:
            revisions = con.execute('SELECT id,dataset,status FROM revisions').fetchall()
        ids = {item['id'] for item in dataset['items']}
        if any(identity not in ids or version != dataset['dataset_id'] for identity, version, _ in revisions):
            raise ValueError('Revision does not belong to the copied dataset')
        manifest['review'] = {'dataset_id': dataset['dataset_id'], 'documents': len(ids),
                             'cohorts': dict(Counter(item['cohort'] for item in dataset['items'])),
                             'revision_statuses': dict(Counter(status for _, _, status in revisions))}
        print('Backing up and comparing main database...', flush=True)
        manifest['databases']['app'] = backup(source/'data/data.db', staging/'data/app.db')
        with closing(sqlite_connect((staging/'data/app.db').as_uri()+'?mode=ro',uri=True)) as con:
            manifest['classification_versions'] = con.execute('SELECT model_version FROM classification_runs').fetchall()
            manifest['skills_versions'] = con.execute('SELECT run_id,model_version,extractor_version,lexicon_version FROM skill_extraction_runs').fetchall()
            manifest['profiles_versions'] = con.execute('SELECT run_id,model_version FROM profile_extraction_runs').fetchall()
        config = {'database': 'data/app.db', 'model': f'models/{model_name}',
                  'skill_lexicon': 'data/dictionaries/skill_lexicon.json',
                  'occupation_lexicon': 'data/dictionaries/occupation_lexicon.json',
                  'annotation_database': 'research/review/annotations.sqlite',
                  'dataset': 'research/review/dataset.json'}
        (staging/'config').mkdir()
        (staging/'config/runtime.json').write_text(json.dumps(config,indent=2),encoding='utf-8')
        manifest['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        (staging/'data/import_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        # Windows rename is no-clobber. Publish app.db LAST; a partial failure is explicit,
        # never silently merged or overwritten by a retry.
        for relative in dict.fromkeys([r for r in destination_paths if r != 'data/app.db'] + ['data/app.db']):
            dst = local_path(target/relative)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                raise FileExistsError(dst)
            (staging/relative).rename(dst)
        print(json.dumps({'status': 'imported', 'manifest': str(target/'data/import_manifest.json')},ensure_ascii=False))
        return manifest
    finally:
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description='Initial import only; refuses any existing working DB')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--source', type=Path)
    group.add_argument('--source-file', type=Path, help='UTF-8 JSON with source_root (avoids shell encoding issues)')
    parser.add_argument('--model', required=True)
    args = parser.parse_args()
    try:
        source = args.source if args.source is not None else Path(json.loads(local_path(args.source_file).read_text(encoding='utf-8'))['source_root'])
        initial_import(source, args.model)
    except (ValueError, FileExistsError) as error:
        parser.exit(2, str(error)+'\n')


if __name__ == '__main__':
    main()
