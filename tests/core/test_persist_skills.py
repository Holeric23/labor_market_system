import csv
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from market_system.core.extraction.persist_skills import file_hash, import_skills


class PersistSkillsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'data.db'
        with closing(sqlite3.connect(self.db)) as con:
            con.executescript('''
                CREATE TABLE contents(text_hash TEXT PRIMARY KEY,text TEXT);
                CREATE TABLE classification_runs(model_version TEXT PRIMARY KEY);
                CREATE TABLE labeled_contents(text_hash TEXT,model_version TEXT,final_label TEXT,
                    PRIMARY KEY(text_hash,model_version));
                INSERT INTO classification_runs VALUES('model');
                INSERT INTO contents VALUES('a','Python Python'),('b','Hello'),('c','Unknown');
                INSERT INTO labeled_contents VALUES('a','model','vacancy'),
                    ('b','model','resume'),('c','model',NULL);
            ''')
        lex = {'version': 'v1', 'skills': [{'id': 'python', 'name': 'Python', 'category': 'tool'}]}
        (self.root / 'skill_lexicon.json').write_text(json.dumps(lex), encoding='utf-8')
        self.rows = [
            dict(text_hash='a', text='Python Python', post_type='vacancy',
                 mentions_json=json.dumps([dict(skill_id='python', role='required', start=s,
                                                end=s+6, raw='Python') for s in (0, 7)])),
            dict(text_hash='b', text='Hello', post_type='resume', mentions_json='[]'),
        ]
        for row in self.rows:
            row.update(classifier_version='model', extractor_version='extractor', lexicon_version='v1')
        digest = hashlib.sha256()
        for row in self.rows:
            digest.update(json.dumps([row['text_hash'], row['text'], row['post_type']],
                                     ensure_ascii=False).encode() + b'\n')
        self.meta = dict(run_id='run', classifier_version='model', extractor_version='extractor',
                         lexicon_version='v1', limit=None, counts={'vacancy': 1, 'resume': 1},
                         mention_roles={'required': 2}, input_sha256=digest.hexdigest(),
                         lexicon_sha256=file_hash(self.root / 'skill_lexicon.json'))
        (self.root / 'summary.json').write_text(json.dumps(self.meta), encoding='utf-8')
        self.write_rows()

    def write_rows(self):
        with (self.root / 'skills_structured.csv').open('w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=self.rows[0].keys())
            writer.writeheader()
            writer.writerows(self.rows)

    def test_import_and_idempotency(self):
        self.assertEqual(import_skills(self.db, self.root)['mentions'], 2)
        self.assertEqual(import_skills(self.db, self.root)['status'], 'already_imported')
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM skill_extracted_contents').fetchone()[0], 2)
            self.assertEqual(con.execute("SELECT mention_count FROM skill_extracted_contents WHERE text_hash='b'").fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT skill_name,role,mention_count FROM content_skills').fetchall(), [('Python', 'required', 2)])
            self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(con.execute('SELECT COUNT(*) FROM contents').fetchone()[0], 3)
            self.assertIsNone(con.execute("SELECT final_label FROM labeled_contents WHERE text_hash='c'").fetchone()[0])

    def test_late_error_rolls_back(self):
        self.rows[1]['text'] = 'Changed'
        self.write_rows()
        with self.assertRaisesRegex(ValueError, 'Source text'):
            import_skills(self.db, self.root)
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute("SELECT name FROM sqlite_master WHERE name='skill_extraction_runs'").fetchall(), [])

    def test_missing_text_rejected(self):
        self.rows.pop()
        self.write_rows()
        with self.assertRaisesRegex(ValueError, 'Incomplete corpus'):
            import_skills(self.db, self.root)

    def test_changed_artifact_cannot_overwrite_run(self):
        import_skills(self.db, self.root)
        self.rows[0]['mentions_json'] = '[]'
        self.write_rows()
        with self.assertRaisesRegex(ValueError, 'different artifacts'):
            import_skills(self.db, self.root)
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM extracted_skill_mentions').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()

