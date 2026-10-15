from contextlib import closing, redirect_stdout
import csv
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from market_system.core.classification.corpus import save_snapshot
from market_system.core.extraction.run_salary import run


class SalaryRunTests(unittest.TestCase):
    def test_export_provenance_sampling_and_read_only_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            db=root/'data.db'
            with closing(sqlite3.connect(db)) as con:
                con.execute('CREATE TABLE contents(text_hash TEXT UNIQUE,text TEXT)')
                con.executemany('INSERT INTO contents VALUES(?,?)',[
                    ('a','Оплата от 100 тыс рублей в месяц'),
                    ('b','Ищу работу дизайнером'),
                    ('c','Оборот 1 млн рублей'),
                ])
                con.commit()
            save_snapshot(db,[('a',.9,.1),('b',.1,.9),('c',.1,.1)],'test',
                          {'vacancy_threshold':.8,'resume_threshold':.8})
            original=db.read_bytes()
            with redirect_stdout(io.StringIO()):
                output=run(db,root/'results')
            self.assertEqual(db.read_bytes(),original)
            summary=json.loads((output/'summary.json').read_text(encoding='utf-8'))
            self.assertEqual(summary['counts'],{'vacancy':1,'resume':1})
            self.assertIsNone(summary['quality_metrics'])
            self.assertTrue((output/'source/salary.py').is_file())
            with (output/'salary_audit.csv').open(encoding='utf-8-sig',newline='') as stream:
                rows=list(csv.DictReader(stream))
            self.assertEqual({row['text_hash'] for row in rows},{'a','b'})
            self.assertTrue(all(row['expected_mentions_json']=='' for row in rows))
            self.assertEqual({row['classifier_version'] for row in rows},{'test'})
            with self.assertRaises(ValueError):
                run(db,root/'results','nonexistent')


if __name__=='__main__':
    unittest.main()
