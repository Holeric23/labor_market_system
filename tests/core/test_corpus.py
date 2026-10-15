import csv
import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from market_system.core.classification.corpus import decide_label, export_snapshot, save_snapshot


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "test.db"
        with closing(sqlite3.connect(self.db)) as con:
            con.executescript("""
                CREATE TABLE contents(id INTEGER PRIMARY KEY,text_hash TEXT UNIQUE,text TEXT);
                CREATE TABLE sources(id INTEGER PRIMARY KEY,name TEXT);
                CREATE TABLE publications(id INTEGER PRIMARY KEY,content_id INTEGER,source_id INTEGER,published_at TEXT);
                INSERT INTO contents VALUES(1,'a','first'),(2,'b','second');
                INSERT INTO sources VALUES(1,'channel');
                INSERT INTO publications VALUES(1,1,1,'2026-01-01'),(2,1,1,'2026-01-02');
            """)
        self.meta = {"vacancy_threshold": .8, "resume_threshold": .8}
        self.rows = [('a', .9, .1), ('b', .1, .9)]

    def test_decision_boundaries(self):
        for pv, pr, label in [(0.8,.1,None),(.1,.8,None),(.9,.9,None),(.9,.95,None),(.79,.1,None),(.81,.1,'vacancy'),(.1,.81,'resume'),(.81,.8,None),(.8,.81,None),(.9,.3,None),(.3,.9,None),(.9,.299,'vacancy'),(.299,.9,'resume'),(.9,.44,None)]:
            self.assertEqual(decide_label(pv,pr),label)
        self.assertIsNone(decide_label(.89,.1,vacancy_threshold=.9))
        self.assertEqual(decide_label(.91,.1,vacancy_threshold=.9),'vacancy')
        for invalid in [float('nan'),float('inf'),-1,1.1]:
            with self.assertRaises(ValueError):
                decide_label(invalid,.1)
            with self.assertRaises(ValueError):
                decide_label(.9,.1,max_other_probability=invalid)
        with self.assertRaises(ValueError):
            decide_label(.9,.1,max_other_probability=.85)

    def test_custom_competing_ceiling_is_applied_and_saved(self):
        self.assertIsNone(decide_label(.9,.35))
        self.assertEqual(decide_label(.9,.35,max_other_probability=.4),'vacancy')
        self.assertIsNone(decide_label(.9,.4,max_other_probability=.4))
        metadata = {**self.meta, 'max_other_probability': .4}
        save_snapshot(self.db,[('a',.9,.35),('b',.4,.9)],'v1',metadata)
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT text_hash,final_label FROM labeled_contents ORDER BY text_hash').fetchall(), [('a','vacancy'),('b',None)])
            saved = json.loads(con.execute('SELECT metadata_json FROM classification_runs').fetchone()[0])
            self.assertEqual(saved['max_other_probability'],.4)

    def test_atomic_validation_and_versions(self):
        for rows in [self.rows[:1], self.rows + [self.rows[0]], [('x', .9, .1),self.rows[1]]]:
            with self.assertRaises(ValueError):
                save_snapshot(self.db, rows, 'v1', self.meta)
        self.assertEqual(save_snapshot(self.db,self.rows,'v1',self.meta),2)
        with self.assertRaises(sqlite3.IntegrityError):
            save_snapshot(self.db,self.rows,'v1',self.meta)
        save_snapshot(self.db,self.rows,'v2',self.meta)
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM labeled_contents').fetchone()[0],4)

    def test_exports_do_not_confuse_posts_with_texts(self):
        save_snapshot(self.db,self.rows,'v1',self.meta)
        export_snapshot(self.db,self.root,'v1')
        with (self.root/'tables/corpus_summary.csv').open(encoding='utf-8-sig') as f:
            rows=list(csv.DictReader(f))
        totals={r['final_label']:r for r in rows if r['dimension']=='corpus'}
        self.assertEqual(totals['vacancy']['unique_contents'],'1')
        self.assertEqual(totals['vacancy']['publications'],'2')
        self.assertEqual(totals['resume']['unique_contents'],'1')
        self.assertEqual(totals['resume']['publications'],'0')
        with (self.root/'unlabeled_contents.csv').open(encoding='utf-8-sig') as f:
            self.assertEqual(len(list(csv.reader(f))),1)

    def test_abstentions_are_sql_null_and_export_as_empty(self):
        save_snapshot(self.db,[('a',.8,.1),('b',.91,.92)],'v1',self.meta)
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM labeled_contents WHERE final_label IS NULL').fetchone()[0],2)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM labeled_contents WHERE final_label IS NOT NULL').fetchone()[0],0)
        export_snapshot(self.db,self.root,'v1')
        with (self.root/'unlabeled_contents.csv').open(encoding='utf-8-sig') as f:
            rows=list(csv.DictReader(f))
        self.assertEqual(len(rows),2)
        self.assertTrue(all(r['final_label']=='' for r in rows))
        self.assertFalse((self.root/'trash_final.csv').exists())


if __name__ == '__main__':
    unittest.main()
