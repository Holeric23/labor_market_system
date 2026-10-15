import tempfile
import unittest
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from market_system.core.classification.incremental import IncrementalLinear


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.texts = ['ищем дизайнера работа', 'нужен дизайнер работа',
                      'ищу работу дизайнер', 'мое резюме дизайнер',
                      'реклама курс скидка', 'курс дизайн обучение']
        self.labels = ['vacancy']*2 + ['resume']*2 + ['trash']*2
        self.vectorizer = TfidfVectorizer().fit(self.texts)
        x = self.vectorizer.transform(self.texts)
        self.vac = LogisticRegression().fit(x, [int(y=='vacancy') for y in self.labels])
        self.res = LogisticRegression().fit(x, [int(y=='resume') for y in self.labels])
        self.model = IncrementalLinear(self.vectorizer,self.vac,self.res,parent_version='fixture',
                                       holdout_texts=['дизайнер работа контроль'])

    def test_migration_update_and_persistence(self):
        before = self.vac.coef_.copy()
        np.testing.assert_allclose(self.model.predict_proba(self.texts)['vacancy'],
                                   self.vac.predict_proba(self.vectorizer.transform(self.texts))[:,1])
        vocab = self.model.vectorizer.vocabulary_.copy()
        self.model.update(self.texts,self.labels)
        self.assertFalse(np.array_equal(before,self.model.heads['vacancy'].coef_))
        np.testing.assert_array_equal(before,self.vac.coef_)
        self.assertEqual(vocab,self.model.vectorizer.vocabulary_)
        with tempfile.TemporaryDirectory() as tmp:
            loaded = IncrementalLinear.load(self.model.save(tmp))
            np.testing.assert_array_equal(loaded.predict_proba(self.texts)['vacancy'],
                                          self.model.predict_proba(self.texts)['vacancy'])
            loaded.update(['ищем дизайнера новая работа'],['vacancy'])
            self.assertEqual(len(loaded.history),2)

    def test_bad_batches_do_not_mutate(self):
        for texts,labels in [(['дизайнер'],['vacancy']),
                             ([self.texts[0]]*3,['vacancy','resume','trash']),
                             (['дизайнер работа контроль',self.texts[2],self.texts[4]],['vacancy','resume','trash']),
                             (['zzzz',self.texts[2],self.texts[4]],['vacancy','resume','trash'])]:
            with self.assertRaises(ValueError):self.model.update(texts,labels)
            self.assertFalse(self.model.migrated)
        self.model.update(self.texts,self.labels)
        with self.assertRaises(ValueError):self.model.update(self.texts,self.labels)


if __name__=='__main__':unittest.main()
