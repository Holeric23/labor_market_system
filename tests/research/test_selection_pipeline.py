import unittest
import numpy as np
from market_system.research.selection_pipeline import decide, evaluate, qualified, fit_policy, zero_error_threshold, rank


class SelectionTests(unittest.TestCase):
    def test_two_different_uncertainties(self):
        pred, reason = decide(np.array([.99,.7,.2,.99]),
                              np.array([[.5,.5],[.99,.01],[.1,.9],[.99,.01]]),
                              {'admission':.95,'types':[.9,.9]})
        self.assertEqual(pred.tolist(),['useful_unspecified','discard','discard','resume'])
        self.assertEqual(reason.tolist(),['uncertain_type','uncertain_usefulness','trash_score_high','typed'])

    def test_untyped_trash_is_still_an_error(self):
        metrics=evaluate(['resume','vacancy','trash'],['resume','useful_unspecified','useful_unspecified'])
        self.assertEqual(metrics['trash_admitted'],1)
        self.assertAlmostEqual(metrics['admission_precision'],2/3)
        self.assertEqual(metrics['useful_recall'],1)
        self.assertEqual(metrics['trash_recall'],0)

    def test_empty_selection_does_not_qualify(self):
        metrics=evaluate(['resume','vacancy','trash'],['discard']*3)
        self.assertFalse(qualified(metrics,.95))
        self.assertEqual(metrics['trash_recall'],1)
        self.assertIsNone(metrics['resume_precision'])

    def test_precision_requirement_separate_for_each_target(self):
        row={'resume_accepted':100,'vacancy_accepted':20,'resume_precision':1.,'vacancy_precision':.9}
        self.assertFalse(qualified(row,.95))
        row['vacancy_precision']=.95
        self.assertTrue(qualified(row,.95))

    def test_thresholds_never_use_rejected_type_examples(self):
        y=np.array(['resume']*12+['vacancy']*12+['trash']*12)
        gate=np.array([.99]*24+[.8]*12)
        types=np.array([[.99,.01]]*12+[[.01,.99]]*12+[[1.,0.]]*12)
        policy=fit_policy(y,gate,types)
        pred,_=decide(gate,types,policy)
        self.assertEqual(pred.tolist(),['resume']*12+['vacancy']*12+['discard']*12)
        self.assertEqual(zero_error_threshold(np.array([.9]*12),np.ones(12,dtype=bool)),2.)


if __name__=='__main__':
    unittest.main()
