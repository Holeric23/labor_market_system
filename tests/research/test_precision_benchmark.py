import unittest
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.neighbors import KNeighborsClassifier
from catboost import CatBoostClassifier
from market_system.research.precision_benchmark import (WeightedArchitecture, fit_weighted, tune_thresholds,
    target_metrics, decide, qualifies, ARCHITECTURES, fresh)


class PrecisionTests(unittest.TestCase):
    def test_catboost_text_configuration_copies_without_sklearn_clone(self):
        original=CatBoostClassifier(text_features=['text'],iterations=2,verbose=False)
        copied=fresh(original)
        self.assertIsNot(copied,original)
        self.assertEqual(copied.get_params()['text_features'],['text'])

    def test_threshold_is_lowest_safe_cutoff_including_ties(self):
        y=np.array(['resume','resume','vacancy','trash'])
        values=np.array([[.8,.1],[.7,.2],[.1,.9],[.7,.8]])
        threshold=tune_thresholds(y,values)
        self.assertEqual(decide(values,threshold).tolist(),['resume','discard_uncertain','vacancy','discard_uncertain'])
        self.assertGreater(threshold[0],.7)
        self.assertGreater(threshold[1],.8)

    def test_precision_only_targets_and_nonempty_requirement(self):
        metrics=target_metrics(['resume','vacancy','trash'],['discard_uncertain']*3)
        self.assertFalse(qualifies(metrics))
        self.assertNotIn('trash_recall',metrics)
        self.assertEqual(metrics['target_macro_recall'],0)

    def test_conflict_retains_type_uncertainty(self):
        self.assertEqual(decide(np.array([[.99,.99],[.1,.1]]),[.9,.9]).tolist(),
                         ['useful_unspecified','discard_uncertain'])

    def test_all_architectures_weighted(self):
        y=np.array(['resume','vacancy','trash']*20)
        x=np.tile(np.eye(3),(20,1))
        for architecture in ARCHITECTURES:
            model=WeightedArchitecture(LogisticRegression(),architecture,5).fit(x,y)
            values=model.predict_support(x)
            self.assertEqual(values.shape,(60,2))
            self.assertTrue(np.isfinite(values).all())
            self.assertEqual(set(model.weight_methods),{'sample_weight'})

    def test_resampling_is_reproducible(self):
        x=np.arange(60).reshape(30,2);y=np.array([0,1]*15);w=np.where(y==0,5.,1.)
        a,method=fit_weighted(KNeighborsClassifier(3),x,y,w)
        b,_=fit_weighted(KNeighborsClassifier(3),x,y,w)
        self.assertEqual(method,'weighted_resampling')
        np.testing.assert_array_equal(a.predict(x),b.predict(x))


if __name__=='__main__':unittest.main()
