from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from market_system.initial_import import backup, initial_import, inventory, sha256
from market_system.paths import PROJECT_ROOT, local_path
from market_system.runtime import load_config
from market_system.telegram_bot.config import Settings


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=PROJECT_ROOT/'.artifacts/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_backup_includes_live_wal_and_preserves_all_values(self):
        original = self.root/'source.db'
        with closing(sqlite3.connect(original)) as source:
            source.execute('PRAGMA journal_mode=WAL')
            source.execute('CREATE TABLE records(id INTEGER PRIMARY KEY, text TEXT, score REAL, unknown TEXT)')
            source.execute('INSERT INTO records VALUES(7, ?, 0.125, NULL)', ('Исходный текст',))
            source.commit()
            self.assertTrue(Path(str(original)+'-wal').exists())
            result = backup(original, self.root/'copy.db')
            with closing(sqlite3.connect(self.root/'copy.db')) as copy:
                self.assertEqual(inventory(source), inventory(copy))
                copy.execute("UPDATE records SET text='owner edit'");copy.commit()
            before = sha256(self.root/'copy.db')
            with self.assertRaises(FileExistsError):
                backup(original, self.root/'copy.db')
            self.assertEqual(before, sha256(self.root/'copy.db'))
            self.assertEqual(result['integrity_check'], 'ok')

    def test_existing_working_db_refused_before_source_access(self):
        target = self.root/'target';(target/'data').mkdir(parents=True)
        with closing(sqlite3.connect(target/'data/app.db')) as con:
            con.execute('CREATE TABLE owner_changes(value TEXT)')
            con.execute("INSERT INTO owner_changes VALUES('keep')");con.commit()
        before = sha256(target/'data/app.db')
        with self.assertRaises(FileExistsError):
            initial_import(self.root/'nonexistent-source', 'absent-model', target)
        self.assertEqual(before, sha256(target/'data/app.db'))

    def test_complete_initial_import_and_repeat(self):
        source=self.root/'source';target=self.root/'new'
        for folder in ('data/learning_data','models/test-model','core/extraction','research/review'):
            (source/folder).mkdir(parents=True)
        with closing(sqlite3.connect(source/'data/data.db')) as con:
            con.executescript('CREATE TABLE contents(id INTEGER PRIMARY KEY,text TEXT);'
                             'INSERT INTO contents VALUES(1,"original");'
                             'CREATE TABLE classification_runs(model_version TEXT);'
                             'CREATE TABLE skill_extraction_runs(run_id TEXT,model_version TEXT,extractor_version TEXT,lexicon_version TEXT);'
                             'CREATE TABLE profile_extraction_runs(run_id TEXT,model_version TEXT);')
        dataset={'dataset_id':'d','items':[{'id':'1','cohort':'test'}]}
        (source/'research/review/dataset.json').write_text(json.dumps(dataset))
        with closing(sqlite3.connect(source/'research/review/annotations.sqlite')) as con:
            con.execute('CREATE TABLE revisions(id TEXT,dataset TEXT,status TEXT,annotation TEXT,revision INTEGER)')
            con.execute("INSERT INTO revisions VALUES('1','d','confirmed','human',1)");con.commit()
        for name in ('skill_lexicon.json','occupation_lexicon.json'):
            (source/'core/extraction'/name).write_text('{}')
        artifact=source/'models/test-model/weights.bin';artifact.write_bytes(b'test artifact, never loaded')
        csv=source/'data/learning_data/training.csv';csv.write_text('text,label\nx,vacancy\n')
        meta={'model_version':'test-model','packages':{},'artifacts_sha256':{'weights.bin':sha256(artifact)},
              'training_files_sha256':{'training.csv':sha256(csv)}}
        (source/'models/test-model/metadata.json').write_text(json.dumps(meta))
        result=initial_import(source,'test-model',target)
        self.assertEqual(result['review']['revision_statuses'],{'confirmed':1})
        self.assertEqual(sha256(target/'models/test-model/weights.bin'),sha256(artifact))
        with closing(sqlite3.connect(target/'data/app.db')) as con:
            con.execute("UPDATE contents SET text='user edit'");con.commit()
        before=sha256(target/'data/app.db')
        with self.assertRaises(FileExistsError):initial_import(source,'test-model',target)
        self.assertEqual(before,sha256(target/'data/app.db'))


class PathTests(unittest.TestCase):
    def test_runtime_guard_blocks_external_writes(self):
        outside = str(PROJECT_ROOT.parent/'stage1-forbidden-probe.txt')
        code = '''from market_system.runtime import enable_guard
import sqlite3, sys
enable_guard()
for action in (lambda: open(sys.argv[1], 'w'), lambda: sqlite3.connect(sys.argv[1])):
    try: action()
    except ValueError: pass
    else: raise AssertionError('external write was permitted')
print('blocked')
'''
        result = subprocess.run([sys.executable, '-c', code, outside], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('blocked', result.stdout)

    def test_configuration_rejects_outside_paths(self):
        outside=PROJECT_ROOT.parent/'forbidden.db'
        with self.assertRaises(ValueError):local_path(outside)
        with self.assertRaises(ValueError):Settings(token='',admins=frozenset(),bot_db_path=outside)
        with self.assertRaises(ValueError):Settings.load(outside)

    def test_runtime_config_is_local_and_old_db_default_is_gone(self):
        from market_system.paths import DATABASE_PATH
        self.assertEqual(DATABASE_PATH, PROJECT_ROOT/'data/app.db')
        if (PROJECT_ROOT/'config/runtime.json').exists():
            self.assertTrue(all(p.is_relative_to(PROJECT_ROOT) for p in load_config().values()))
