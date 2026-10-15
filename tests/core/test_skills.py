import csv
import json
from pathlib import Path
import tempfile
import unittest

from market_system.core.extraction.skills import SkillExtractor, DEFAULT_LEXICON
from market_system.core.extraction.skill_lexicon import apply_decisions
from market_system.core.extraction.skill_evaluation import evaluate


class SkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.extractor=SkillExtractor()

    def test_aliases_boundaries_and_offsets(self):
        text='Требования:\nВладение Фигмой и Adobe Photoshop. Знание C++, C#, .NET и MySQL.\nКонтакты: https://example.com/python и @figma'
        r=self.extractor.extract(text,'vacancy')
        ids={m['skill_id'] for m in r['mentions']}
        self.assertEqual(ids,{'figma','photoshop','cpp','csharp','dotnet','mysql'})
        self.assertNotIn('sql',ids)
        for m in r['mentions']:self.assertEqual(text[m['start']:m['end']],m['raw'])

    def test_roles_and_negation(self):
        text='Требования:\nЗнание Figma.\nPhotoshop будет плюсом.\nPython не требуется.\nОбучим Blender.\nУсловия:\nУ нас есть подписка на Canva.'
        r=self.extractor.extract(text,'vacancy')['skills_by_role']
        self.assertIn('figma',r['required'])
        self.assertIn('photoshop',r['preferred'])
        self.assertIn('python',r['not_required'])
        self.assertIn('blender',r['learning'])
        self.assertIn('canva',r['mentioned'])
        r=self.extractor.extract('Владею Figma, Photoshop','resume')['skills_by_role']
        self.assertEqual(set(r['possessed']),{'figma','photoshop'})
        r=self.extractor.extract('Навыки:\nВладею SQL.\nНе знаю Python.\nИзучаю Blender.','resume')['skills_by_role']
        self.assertIn('sql',r['possessed'])
        self.assertIn('python',r['negated'])
        self.assertIn('blender',r['learning'])

    def test_unknown_candidates_are_not_canonical_skills(self):
        r=self.extractor.extract('Требования:\nЗнание Fluxonium и композиции.\nЗарплата:\nСтабильная оплата.','vacancy')
        self.assertIn('fluxonium',{c['phrase'] for c in r['unknown_candidates']})
        self.assertNotIn('fluxonium',r['skills_by_role']['required'])
        self.assertFalse(any('оплата' in c['phrase'] for c in r['unknown_candidates']))

    def test_reviewed_alias_and_new_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'proposals.csv';output=root/'lexicon.json'
            fields=['candidate','decision','target_skill_id','new_skill_name','new_category']
            with p.open('w',encoding='utf-8-sig',newline='') as f:
                w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
                w.writerow(dict(zip(fields,['фигмочка','alias','figma','',''])))
                w.writerow(dict(zip(fields,['fluxonium','new','fluxonium','Fluxonium','tool'])))
            self.assertEqual(apply_decisions(p,DEFAULT_LEXICON,output,'test_v2'),2)
            r=SkillExtractor(output).extract('Навыки: фигмочка, Fluxonium','resume')
            self.assertEqual(set(r['skills_by_role']['possessed']),{'figma','fluxonium'})
            with self.assertRaises(FileExistsError):apply_decisions(p,DEFAULT_LEXICON,output,'test_v3')

    def test_quality_requires_gold_and_counts_missed_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'audit.csv'
            with p.open('w',encoding='utf-8',newline='') as f:
                w=csv.DictWriter(f,fieldnames=['review_status','expected_skills_json','mentions_json'])
                w.writeheader();w.writerow({'review_status':'incorrect',
                    'expected_skills_json':json.dumps([{'skill_id':'figma','role':'required'},{'skill_id':'sql','role':'required'}]),
                    'mentions_json':json.dumps([{'skill_id':'figma','role':'required'}])})
            result=evaluate(p)
            self.assertEqual(result['micro']['precision'],1)
            self.assertEqual(result['micro']['recall'],.5)

    def test_pending_decisions_do_not_change_lexicon(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'proposals.csv';output=root/'lexicon.json'
            with p.open('w',encoding='utf-8',newline='') as f:
                w=csv.DictWriter(f,fieldnames=['candidate','decision','target_skill_id','new_skill_name','new_category'])
                w.writeheader();w.writerow({'candidate':'unknown','decision':''})
            with self.assertRaises(ValueError):apply_decisions(p,DEFAULT_LEXICON,output,'v2')
            self.assertFalse(output.exists())

    def test_alias_collision_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'proposals.csv';output=root/'lexicon.json'
            with p.open('w',encoding='utf-8',newline='') as f:
                w=csv.DictWriter(f,fieldnames=['candidate','decision','target_skill_id','new_skill_name','new_category'])
                w.writeheader();w.writerow({'candidate':'Figma','decision':'alias','target_skill_id':'photoshop'})
            with self.assertRaises(ValueError):apply_decisions(p,DEFAULT_LEXICON,output,'v2')
            self.assertFalse(output.exists())


if __name__=='__main__':unittest.main()
