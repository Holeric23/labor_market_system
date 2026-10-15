from copy import deepcopy
import importlib.metadata
import json
from pathlib import Path
import tempfile
import unittest

import joblib
import numpy as np
import pandas as pd
from catboost import CatBoostClassifier, Pool
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

from market_system.core.classification.corpus import decide_label, DECISION_POLICY
from market_system.labor_market import LaborMarketPipeline
from market_system.labor_market.model import Ensemble, sha256, clean, text_key
from market_system.labor_market.training import ModelUpdater


class ControlledClassifier:
    version = 'controlled-test-fixture'
    def probabilities(self,texts):
        return {'vacancy':np.array([.95 if t.startswith('Вакансия') else .05 for t in texts]),
                'resume':np.array([.95 if t.startswith('Резюме') else .05 for t in texts])}
    def label(self,pv,pr):
        return decide_label(pv,pr)


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.pipeline=LaborMarketPipeline(ControlledClassifier())

    def test_scalar_roles_and_salary(self):
        text='Вакансия\nТребования: знание Figma.\nЗадачи: работать в Photoshop.\nЗарплата: от 100 до 150 тыс руб в месяц на руки'
        row=self.pipeline.analyze(text)
        self.assertEqual(row['label'],'vacancy')
        self.assertIn('figma',row['skills'])
        self.assertNotIn('photoshop',row['skills'])
        self.assertIn('photoshop',row['task_skills'])
        self.assertEqual(row['salary_from'],100000)
        self.assertEqual(row['salary_to'],150000)
        self.assertEqual(row['salary_period'],'month')
        self.assertEqual(row['tax_mode'],'net')
        for m in row['skill_mentions']+row['salary_mentions']:
            self.assertEqual(text[m['start']:m['end']],m['raw'])

    def test_series_duplicates_and_no_input_mutation(self):
        frame=pd.DataFrame({'text':['Резюме\nВладею Figma','Вакансия\nЗнание SQL'],
                            'label':['human_a','human_b']},index=['same','same'])
        original=frame.copy(deep=True)
        result=self.pipeline.transform(frame,batch_size=1)
        pd.testing.assert_frame_equal(frame,original)
        self.assertEqual(result.index.tolist(),['same','same'])
        self.assertEqual(result['lm_label'].tolist(),['resume','vacancy'])
        self.assertEqual(result['label'].tolist(),['human_a','human_b'])
        self.assertEqual(self.pipeline.transform(frame['text'])['lm_label'].tolist(),['resume','vacancy'])
        with self.assertRaises(ValueError):self.pipeline.transform(result)

    def test_null_empty_unknown_and_empty_frame(self):
        mixed=self.pipeline(pd.Series(['Вакансия\nЗнание Figma','Просто текст',None]))
        self.assertEqual(mixed['lm_label'].tolist(),['vacancy',None,None])
        result=self.pipeline.transform(pd.Series([None,pd.NA,42,'','  ','https://example.com','Просто текст']))
        self.assertEqual(result['lm_classification_status'].tolist(),['invalid_text']*6+['unlabeled'])
        self.assertTrue(all(v is None for v in result['lm_label']))
        self.assertTrue(all(v=='skipped' for v in result['lm_extraction_status']))
        empty=self.pipeline.transform(pd.DataFrame({'text':[]}))
        self.assertEqual(len(empty),0)
        self.assertIn('lm_salary_from',empty)
        with self.assertRaises(TypeError):self.pipeline.transform('text')
        with self.assertRaises(ValueError):self.pipeline.transform(pd.Series(['text']),batch_size=0)

    def test_occupations_and_conditions_are_connected(self):
        row=self.pipeline('Вакансия. Мы давно развиваем разные интересные проекты и сейчас ищем графического дизайнера. Удаленно. График 5/2, с 10 до 19. Полная занятость.')
        self.assertEqual(row['primary_occupation_id'],'graphic_designer')
        self.assertEqual(row['work_format'],'remote')
        self.assertEqual(row['schedule'],'5/2')
        self.assertEqual(row['employment_type'],'full_time')
        self.assertEqual(row['working_hours'],'10:00–19:00')
        skipped=self.pipeline('Неуверенный пост')
        self.assertEqual(skipped['occupation_status'],'skipped')
        self.assertEqual(skipped['work_format_status'],'skipped')


def make_bundle(folder):
    rows=[]
    templates={'vacancy':'ищем дизайнер работа зарплата требования figma',
               'resume':'ищу работу резюме опыт дизайнер figma',
               'trash':'реклама курс скидка обучение дизайн'}
    for label,template in templates.items():
        for i in range(8):rows.append({'text_hash':f'{label}-{i}','text':template+' '+str(i),
                                       'label':label,'split':'train' if i<6 else 'test'})
    data=pd.DataFrame(rows);train=data[data['split']=='train'];texts=train['text'].tolist()
    vec=TfidfVectorizer().fit(texts);x=vec.transform(texts);linear={'vectorizer':vec}
    text_processing={'tokenizers':[{'tokenizer_id':'Space','separator_type':'ByDelimiter','delimiter':' '}],
                     'dictionaries':[{'dictionary_id':'Words','token_level_type':'Word','occurrence_lower_bound':'1'}],
                     'feature_processing':{'default':[{'dictionaries_names':['Words'],'feature_calcers':['BoW'],'tokenizers_names':['Space']}]}}
    models={}
    for name,key in [('vacancy','lr_vac'),('resume','lr_res')]:
        y=(train['label']==name).astype(int)
        linear[key]=LogisticRegression().fit(x,y)
        model=CatBoostClassifier(iterations=3,depth=2,verbose=False,allow_writing_files=False,
                                text_features=['clean_text'],text_processing=text_processing,thread_count=2)
        model.fit(pd.DataFrame({'clean_text':texts}),y)
        model.save_model(str(folder/(name+'.cbm')))
        models[name]=model
    joblib.dump(linear,folder/'linear.joblib')
    data[['text_hash','text','label']].to_csv(folder/'labels.csv',index=False)
    data[['text_hash','label','split']].to_csv(folder/'split.csv',index=False)
    metadata={'model_version':'fixture','decision_policy':DECISION_POLICY,'weight_lr':.2,'weight_cb':.8,
              'vacancy_threshold':.8,'resume_threshold':.8,'max_other_probability':.3,
              'packages':{p:importlib.metadata.version(p) for p in ('scikit-learn','catboost')},
              'artifacts_sha256':{p.name:sha256(p) for p in folder.iterdir()}}
    (folder/'metadata.json').write_text(json.dumps(metadata),encoding='utf-8')
    return data,models


class UpdateTests(unittest.TestCase):
    def test_real_roundtrip_two_updates_and_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);parent=root/'parent';parent.mkdir()
            data,_=make_bundle(parent)
            before={p.name:sha256(p) for p in parent.iterdir()}
            updater=ModelUpdater.load(parent,replay_size=9)
            original=updater.model.probabilities(data['text'].tolist())
            batch=pd.DataFrame({'text':['ищем дизайнер работа новая вакансия'], 'label':['vacancy']})
            child=updater.update(batch,root/'candidates',cb_iterations=2)
            self.assertEqual(before,{p.name:sha256(p) for p in parent.iterdir()})
            self.assertEqual(sha256(parent/'vacancy.cbm'),sha256(child/'vacancy_000.cbm'))
            self.assertEqual(sha256(parent/'resume.cbm'),sha256(child/'resume_000.cbm'))
            np.testing.assert_array_equal(original['vacancy'],updater.model.probabilities(data['text'].tolist())['vacancy'])
            loaded=ModelUpdater.load(child)
            self.assertEqual(len(loaded.model.boosts['vacancy']),2)
            self.assertEqual(loaded.model.boosts['vacancy'][0].tree_count_,3)
            self.assertEqual(loaded.model.boosts['vacancy'][1].tree_count_,2)
            self.assertEqual(len(loaded.state['replay']),9)
            report=json.loads((child/'comparison.json').read_text())
            self.assertFalse(report['automatically_activated'])
            self.assertEqual(report['before']['documents'],6)
            with self.assertRaisesRegex(ValueError,'already used'):
                loaded.update(batch,root/'candidates')
            with self.assertRaisesRegex(ValueError,'Holdout leakage'):
                loaded.update(data[data['split']=='test'][['text','label']],root/'candidates')
            corrected=batch.copy();corrected['label']='trash'
            with self.assertRaisesRegex(ValueError,'Changed historical'):
                loaded.update(corrected,root/'candidates')
            later=loaded.update(pd.DataFrame({'text':['резюме дизайнер опыт ищу удаленную работу'],
                                             'label':['resume']}),root/'candidates',cb_iterations=1)
            result=LaborMarketPipeline.load(later).transform(data[['text']])
            self.assertEqual(len(result),len(data))
            self.assertTrue(np.isfinite(result['lm_prob_vac'].astype(float)).all())
            self.assertEqual(len(ModelUpdater.load(later).state['history']),2)
            # A copied child is self-contained: it does not resolve parent paths.
            import shutil
            standalone=root/'standalone'
            shutil.copytree(later,standalone)
            np.testing.assert_array_equal(Ensemble.load(standalone).probabilities(data['text'].tolist())['vacancy'],
                                          Ensemble.load(later).probabilities(data['text'].tolist())['vacancy'])

    def test_baseline_residual_predictions_are_not_double_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data,models=make_bundle(root)
            model=Ensemble.load(root);frame=pd.DataFrame({'clean_text':data['text']})
            p=model.probabilities(data['text'].tolist())
            x=model.linear['vectorizer'].transform(data['text'])
            for name,key in [('vacancy','lr_vac'),('resume','lr_res')]:
                expected=.2*model.linear[key].predict_proba(x)[:,1]+.8*models[name].predict_proba(frame)[:,1]
                np.testing.assert_allclose(p[name],expected,atol=1e-14)
            # Corruption fails before deserializing joblib.
            with (root/'linear.joblib').open('ab') as f:f.write(b'corrupt')
            with self.assertRaisesRegex(ValueError,'integrity'):
                Ensemble.load(root)


if __name__=='__main__':unittest.main()
