import unittest
from market_system.core.extraction.extraction_evaluation import score_records, _sets


class EvaluationTests(unittest.TestCase):
    def test_missing_gold_is_not_perfect_quality(self):
        self.assertEqual(score_records([])['status'], 'awaiting_annotations')
        self.assertIsNone(score_records([])['micro'])

    def test_wrong_missing_and_extra_entities(self):
        result = score_records([({'a', 'b'}, {'a', 'c'}), (set(), {'d'})])
        self.assertEqual((result['tp'], result['fp'], result['fn']), (1, 2, 1))
        self.assertAlmostEqual(result['micro']['precision'], 1/3)
        self.assertEqual(result['micro']['recall'], .5)
        self.assertAlmostEqual(result['micro']['f1'], .4)
        self.assertEqual(result['exact_match'], 0)

    def test_empty_documents_and_deterministic_intervals(self):
        records = [(set(), set()), ({'a'}, {'a'})]
        result = score_records(records)
        self.assertEqual(result['exact_match'], 1)
        self.assertEqual(result['gold_empty_documents'], 1)
        self.assertEqual(result['ci95'], score_records(records)['ci95'])
        self.assertIsNone(score_records([(set(), set())])['micro']['recall'])

    def test_skill_roles_are_part_of_entity(self):
        gold = _sets('[{"skill_id":"python","role":"required"}]', 'skills')
        pred = _sets('[{"skill_id":"python","role":"preferred"}]', 'skills')
        self.assertEqual(score_records([(gold, pred)])['micro']['f1'], 0)
        with self.assertRaises(ValueError):
            _sets('[{"skill_id":"python","role":"invented"}]', 'skills')


if __name__ == '__main__':
    unittest.main()
