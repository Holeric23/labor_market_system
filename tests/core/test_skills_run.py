from contextlib import closing,redirect_stdout
import csv
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from market_system.core.classification.corpus import save_snapshot
from market_system.core.extraction.run_skills import run


class SkillsRunTests(unittest.TestCase):
    def test_end_to_end_discovery_keeps_proposals_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);db=root/'data.db'
            posts=[
                ('a','Требования: знание Figma и Fluxonium. Дизайн интерфейсов.'),
                ('b','Требования: знание Photoshop и Fluxonium. Ретушь фотографий.'),
                ('c','Навыки: владею Python и SQL. Работаю с Quuxbase.'),
                ('d','Требования: знание SQL и Quuxbase. Аналитика данных.'),
                ('e','Требования: знание Figma и композиции.'),
                ('f','Требования: знание Photoshop и композиции.'),
                ('g','Текст без уверенного класса. Python'),
            ]
            with closing(sqlite3.connect(db)) as con:
                con.execute('CREATE TABLE contents(text_hash TEXT UNIQUE,text TEXT)')
                con.executemany('INSERT INTO contents VALUES(?,?)',posts);con.commit()
            predictions=[(key,.1,.9) if key=='c' else (key,.1,.1) if key=='g' else (key,.9,.1) for key,_ in posts]
            save_snapshot(db,predictions,'test',{'vacancy_threshold':.8,'resume_threshold':.8})
            original=db.read_bytes()
            with redirect_stdout(io.StringIO()):
                output=run(db,root/'results',min_documents=1,max_candidates=30)
            self.assertEqual(db.read_bytes(),original)
            summary=json.loads((output/'summary.json').read_text(encoding='utf-8'))
            self.assertEqual(sum(summary['counts'].values()),6)
            self.assertEqual(summary['similarity_model']['status'],'trained')
            self.assertEqual(summary['similarity_model']['automatically_accepted'],0)
            self.assertTrue((output/'context_model.joblib').is_file())
            with (output/'skill_proposals.csv').open(encoding='utf-8-sig',newline='') as f:
                proposals=list(csv.DictReader(f))
            self.assertTrue(any(p['candidate']=='fluxonium' for p in proposals))
            self.assertTrue(all(p['decision']=='' for p in proposals))
            with (output/'skills_structured.csv').open(encoding='utf-8-sig',newline='') as f:
                rows=list(csv.DictReader(f))
            self.assertNotIn('g',{r['text_hash'] for r in rows})
            for row in rows:
                self.assertNotIn('fluxonium',json.loads(row['required_skills']))
                for mention in json.loads(row['mentions_json']):
                    self.assertEqual(row['text'][mention['start']:mention['end']],mention['raw'])


if __name__=='__main__':unittest.main()
