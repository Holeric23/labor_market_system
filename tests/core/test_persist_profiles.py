from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from market_system.core.extraction.persist_profiles import persist_profiles


class ProfilePersistenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / 'data.db'
        with closing(sqlite3.connect(self.db)) as con:
            con.executescript('''CREATE TABLE contents(text_hash TEXT PRIMARY KEY,text TEXT);
                CREATE TABLE classification_runs(model_version TEXT PRIMARY KEY);
                CREATE TABLE labeled_contents(text_hash TEXT,model_version TEXT,final_label TEXT,
                    PRIMARY KEY(text_hash,model_version));
                INSERT INTO classification_runs VALUES('test');''')
            con.executemany('INSERT INTO contents VALUES(?,?)', [
                ('a', 'Ищем дизайнера\nЗарплата 100000 рублей в месяц\nУдалённо, полная занятость, график 5/2'),
                ('b', 'Привет всем'), ('c', 'Ищем дизайнера')])
            con.executemany('INSERT INTO labeled_contents VALUES(?,?,?)',
                            [('a', 'test', 'vacancy'), ('b', 'test', 'resume'), ('c', 'test', None)])
            con.commit()

    def test_roundtrip_empty_and_idempotency(self):
        self.assertEqual(persist_profiles(self.db, 'test')['documents'], 2)
        self.assertEqual(persist_profiles(self.db, 'test')['status'], 'already_imported')
        with closing(sqlite3.connect(self.db)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute('SELECT * FROM extracted_profiles ORDER BY text_hash').fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['salary_from'], 100000)
            self.assertEqual(rows[0]['work_format'], 'remote')
            self.assertEqual(rows[0]['schedule'], '5/2')
            self.assertTrue(json.loads(rows[0]['occupation_ids_json']))
            self.assertEqual(json.loads(rows[0]['work_conditions_json'])['work_format']['status'], 'single')
            self.assertEqual(rows[1]['salary_status'], 'not_found')
            self.assertIsNone(rows[1]['salary_from'])
            self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_failure_rolls_back_whole_run(self):
        with patch('market_system.core.extraction.persist_profiles.extract_work_conditions', side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):
                persist_profiles(self.db, 'test')
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute("SELECT name FROM sqlite_master WHERE name='extracted_profiles'").fetchall(), [])
            self.assertEqual(con.execute('SELECT COUNT(*) FROM contents').fetchone()[0], 3)


if __name__ == '__main__':
    unittest.main()
