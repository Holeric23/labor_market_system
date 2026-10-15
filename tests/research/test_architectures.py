import unittest
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from market_system.research.architectures import Architecture, ARCHITECTURES, thresholds, decide, metrics


class ArchitectureTests(unittest.TestCase):
    def test_uncertainty_is_not_trash_and_conflict_abstains(self):
        values = np.array([[.8,.1,.8], [.1,.9,.1], [.2,.2,.2]])
        self.assertEqual(decide(values, [.7,.7,.7]).tolist(), ['uncertain','trash','uncertain'])

    def test_threshold_blocks_false_positives_and_empty_acceptance(self):
        y = np.array(['resume','trash','vacancy']*12)
        values = np.tile([[.9,.1,.1],[.8,.9,.1],[.1,.1,.9]], (12,1))
        limits = thresholds(y, values)
        report = metrics(y, decide(values, limits))
        self.assertEqual(report['target_fp'], 0)
        self.assertEqual(report['target_tp'], 24)
        self.assertTrue(np.all(thresholds(y, np.ones_like(values)) == 2))

    def test_training_membership_and_shapes(self):
        x = ['resume experience developer','trash weather rain','vacancy hiring developer']*12
        y = np.array(['resume','trash','vacancy']*12)
        base = make_pipeline(TfidfVectorizer(), LogisticRegression())
        for kind in ARCHITECTURES:
            model = Architecture(base, kind).fit(x,y)
            values = model.predict_support(x)
            self.assertEqual(values.shape,(36,3))
            self.assertTrue(np.isfinite(values).all())
            if kind == 'two_ovr':
                self.assertEqual(set(model.models['resume'].classes_), {0,1})
            if kind in ('two_pairs','three_pairs'):
                self.assertEqual(set(model.models['resume','trash'].classes_), {'resume','trash'})
                self.assertEqual(len(model.models), 3 if kind=='three_pairs' else 2)


if __name__ == '__main__':
    unittest.main()
