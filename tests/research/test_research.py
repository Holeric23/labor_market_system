"""Checks for evaluation leakage, revision safety and extraction scoring."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from market_system.research.annotation import Store, validate, sets, evaluate, FIELDS
from market_system.research.benchmark import ROOT
from market_system.research.extraction import prf


def annotation():
    return {'post_type':'vacancy','skills':[{'id':'figma','role':'required'}],
            'occupations':['designer'],'salary':[], 'conditions':{f:[] for f in FIELDS},'comment':''}


def item(identity,cohort):
    return {'id':identity,'cohort':cohort,'text':'Нужен дизайнер, Figma','predicted_type':'vacancy',
            'draft':annotation(),'group':identity,'mentions':[],'occupation_mentions':[],
            'alternatives':{'skills':{'exact_dictionary':['figma']},'occupations':{'exact_dictionary':['designer']}}}


class ResearchTests(unittest.TestCase):
    def test_revision_preserves_history_and_rejects_stale_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset={'dataset_id':'d','items':[item('a','test')]}
            store=Store(Path(tmp)/'a.sqlite',dataset)
            self.assertEqual(store.save('a',0,'draft',annotation()),1)
            with self.assertRaises(ValueError):store.save('a',0,'confirmed',annotation())
            changed=annotation();changed['skills']=[]
            store.save('a',1,'confirmed',changed)
            self.assertEqual(store.latest()['a']['annotation']['skills'],[])
            with store.connect() as con:self.assertEqual(con.execute('SELECT COUNT(*) FROM revisions').fetchone()[0],2)

    def test_invalid_salary_and_unknown_status_rejected(self):
        value=annotation();value['salary']=[{'salary_from':100,'salary_to':50,'currency':'RUB','salary_period':'month','tax_mode':None}]
        with self.assertRaises(ValueError):validate(value)
        value['salary'][0]['salary_to']=float('nan')
        with self.assertRaises(ValueError):validate(value)

    def test_roles_and_false_negatives(self):
        value=annotation();value['skills']+=[{'id':'sql','role':'negated'}]
        self.assertEqual(sets(value)['skills'],{'figma'})
        report=prf([({'figma','sql'},{'figma'}),(set(),{'python'})])
        self.assertEqual((report['tp'],report['fp'],report['fn']),(1,1,1))
        self.assertEqual(report['f1'],.5)

    def test_only_confirmed_and_test_not_training(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            dataset={'dataset_id':'x','scope':'fixture','items':[item('a','development'),item('b','test'),item('c','test')]}
            (root/'dataset.json').write_text(json.dumps(dataset),encoding='utf-8')
            store=Store(root/'annotations.sqlite',dataset)
            store.save('a',0,'confirmed',annotation());store.save('b',0,'confirmed',annotation());store.save('c',0,'draft',annotation())
            with patch('market_system.research.annotation.REVIEW',root),patch('market_system.research.annotation.ROOT',root):
                output=evaluate()
            result=json.loads((output/'summary.json').read_text())
            self.assertEqual(result['confirmed'],2)
            self.assertEqual(result['test_confirmed'],1)
            self.assertEqual(result['supervised_train_documents'],1)
            self.assertEqual(result['trained_models'],[])

    @unittest.skipUnless((ROOT/'research/results/run_20260930_v2/split.csv').exists() and (ROOT/'research/review/dataset.json').exists(), 'Historical artifacts intentionally not migrated in stage 0')
    def test_actual_frozen_splits_are_disjoint(self):
        split=pd.read_csv(ROOT/'research/results/run_20260930_v2/split.csv')
        self.assertTrue(split.text_hash.is_unique)
        self.assertEqual(split.groupby('group')['split'].nunique().max(),1)
        dataset=json.loads((ROOT/'research/review/dataset.json').read_text(encoding='utf-8'))
        frame=pd.DataFrame(dataset['items'])
        self.assertEqual(frame.groupby('group').cohort.nunique().max(),1)
        self.assertTrue(frame.id.is_unique)

    def test_supervised_students_use_development_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            items=[item(str(i),'development' if i<20 else 'test') for i in range(21)]
            for row in items:
                row['text']='Требуется Figma. SQL не нужен. Ищем дизайнера, не продавца.'
                row['mentions']=[{'skill_id':'figma','start':10,'end':15},{'skill_id':'sql','start':17,'end':20}]
                row['occupation_mentions']=[{'occupation_id':'designer','start':36,'end':45},{'occupation_id':'sales_manager','start':50,'end':58}]
            dataset={'dataset_id':'trained-fixture','scope':'test fixture','items':items}
            (root/'dataset.json').write_text(json.dumps(dataset),encoding='utf-8')
            store=Store(root/'annotations.sqlite',dataset)
            for row in items:store.save(row['id'],0,'confirmed',annotation())
            with patch('market_system.research.annotation.REVIEW',root),patch('market_system.research.annotation.ROOT',root):output=evaluate()
            summary=json.loads((output/'summary.json').read_text())
            self.assertEqual(len(summary['trained_models']),4)
            self.assertNotIn('20',summary['training_document_ids'])
            metrics=pd.read_csv(output/'metrics.csv')
            self.assertTrue({'human_lr','human_svm','occupation_human_lr','occupation_human_svm'}<=set(metrics[metrics.cohort=='test'].method))

    def test_cross_cohort_group_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);a=item('a','development');b=item('b','test');b['group']=a['group']
            (root/'dataset.json').write_text(json.dumps({'items':[a,b]}),encoding='utf-8')
            with patch('market_system.research.annotation.REVIEW',root),self.assertRaises(ValueError):evaluate()


if __name__=='__main__':unittest.main()
