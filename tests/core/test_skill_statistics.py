import csv
from pathlib import Path
import tempfile
import unittest
from market_system.core.extraction.skill_statistics import SkillStatistics


class SkillStatisticsTests(unittest.TestCase):
    def test_union_does_not_double_count_across_roles(self):
        stats=SkillStatistics({'skills':[{'id':'seo','name':'SEO','category':'marketing'}]})
        stats.observe('a','vacancy',[{'skill_id':'seo','role':r} for r in ['required','required','preferred','mentioned']])
        stats.observe('b','resume',[{'skill_id':'seo','role':'possessed'}])
        with tempfile.TemporaryDirectory() as tmp:
            stats.write(tmp)
            with (Path(tmp)/'skill_frequencies_summary.csv').open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['unique_texts_total'],'2')
            self.assertEqual(rows[0]['vacancy_demand_texts'],'1')
            self.assertEqual(rows[0]['vacancy_required_texts'],'1')
            self.assertEqual(rows[0]['vacancy_preferred_texts'],'1')
        with self.assertRaises(ValueError):stats.observe('a','vacancy',[])


if __name__=='__main__':unittest.main()
