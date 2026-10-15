from market_system.paths import local_path, DATABASE_PATH
"""Experimental incremental linear branch; does not replace the deployed ensemble."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

import joblib
import numpy as np
import sklearn
from sklearn.linear_model import SGDClassifier


def clean_text(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Expected a nonempty text string')
    return re.sub(r'http\S+|www\S+|https\S+', '', text, flags=re.MULTILINE)


class IncrementalLinear:
    """Two binary heads, frozen TF-IDF, explicit LR -> SGD migration on first update.

    Inputs are sequences, e.g. df['text'].tolist(), df['label'].tolist().
    First update must contain vacancy, resume and trash; later batches may be
    single-class. No automatic class balancing or threshold recalibration.
    """

    def __init__(self, vectorizer, lr_vac, lr_res, *, parent_version,
                 eta0=0.001, alpha=0.0001, seed=42, holdout_texts=()):
        if not np.isfinite(eta0) or eta0 <= 0 or not np.isfinite(alpha) or alpha < 0:
            raise ValueError('Invalid SGD learning rate or regularization')
        self.vectorizer = deepcopy(vectorizer)
        self.heads = {'vacancy': deepcopy(lr_vac), 'resume': deepcopy(lr_res)}
        for head in self.heads.values():
            if not np.array_equal(head.classes_, [0, 1]):
                raise ValueError('Expected binary heads with classes [0, 1]')
            if head.coef_.shape != (1, len(vectorizer.get_feature_names_out())):
                raise ValueError('Model and vectorizer dimensions differ')
        self.parent_version = parent_version
        self.params = dict(eta0=eta0, alpha=alpha, seed=seed)
        self.holdout = {self._hash(t) for t in holdout_texts}
        self.seen = set()
        self.history = []
        self.migrated = False

    @staticmethod
    def _hash(text):
        return hashlib.sha256(clean_text(text).encode('utf-8')).hexdigest()

    def predict_proba(self, texts):
        if isinstance(texts, str):
            raise TypeError('Pass a list of texts, not a single string')
        x = self.vectorizer.transform([clean_text(t) for t in texts])
        return {name: head.predict_proba(x)[:, 1] for name, head in self.heads.items()}

    def update(self, texts, labels, *, epochs=1):
        if isinstance(texts, str) or isinstance(labels, str):
            raise TypeError('Pass sequences of texts and labels')
        texts, labels = list(texts), list(labels)
        if not texts or len(texts) != len(labels):
            raise ValueError('Texts and labels must be nonempty and aligned')
        if isinstance(epochs, bool) or not isinstance(epochs, int) or epochs < 1:
            raise ValueError('epochs must be a positive integer')
        if not set(labels) <= {'vacancy', 'resume', 'trash'}:
            raise ValueError('Only human labels vacancy/resume/trash are accepted')
        if not self.migrated and set(labels) != {'vacancy', 'resume', 'trash'}:
            raise ValueError('First migration batch must contain all three classes')
        hashes = [self._hash(t) for t in texts]
        if len(set(hashes)) != len(hashes):
            raise ValueError('Duplicate texts in batch')
        if set(hashes) & self.holdout:
            raise ValueError('Holdout leakage: evaluation text in update')
        if set(hashes) & self.seen:
            raise ValueError('Batch contains previously updated texts')
        x = self.vectorizer.transform([clean_text(t) for t in texts])
        if np.any(np.asarray(x.getnnz(axis=1)) == 0):
            raise ValueError('Text has no known TF-IDF features; inspect vocabulary coverage')
        # Transactional in memory: a failed second head never leaves a half-update.
        heads = deepcopy(self.heads)
        for name, previous in heads.items():
            y = np.array([int(label == name) for label in labels])
            if not self.migrated:
                model = SGDClassifier(loss='log_loss', learning_rate='constant',
                                      eta0=self.params['eta0'], alpha=self.params['alpha'],
                                      random_state=self.params['seed'], max_iter=1, tol=None)
                model.fit(x, y, coef_init=previous.coef_.copy(),
                          intercept_init=previous.intercept_.copy())
                remaining = epochs - 1
            else:
                model, remaining = previous, epochs
            for _ in range(remaining):
                model.partial_fit(x, y)
            heads[name] = model
        self.heads = heads
        self.migrated = True
        self.seen.update(hashes)
        self.history.append({'at': datetime.now(timezone.utc).isoformat(),
                             'documents': len(texts), 'epochs': epochs,
                             'class_counts': {k: labels.count(k) for k in ('vacancy','resume','trash')},
                             'batch_sha256': hashlib.sha256(json.dumps(list(zip(hashes, labels))).encode()).hexdigest()})
        return self

    def save(self, output_root):
        version = 'linear_online_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid.uuid4().hex[:8]
        folder = local_path(output_root) / version
        folder.mkdir(parents=True, exist_ok=False)
        joblib.dump(self, folder / 'model.joblib')
        metadata = {'version': version, 'parent_version': self.parent_version,
                    'sklearn_version': sklearn.__version__, 'params': self.params,
                    'migrated_to_sgd': self.migrated, 'history': self.history,
                    'status': 'experimental_not_evaluated', 'tfidf': 'frozen',
                    'sha256': hashlib.sha256((folder / 'model.joblib').read_bytes()).hexdigest()}
        (folder / 'metadata.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
        return folder

    @classmethod
    def load(cls, folder):
        """Load trusted local artifacts only; joblib is not safe for untrusted input."""
        folder = Path(folder)
        metadata = json.loads((folder / 'metadata.json').read_text(encoding='utf-8'))
        if metadata['sklearn_version'] != sklearn.__version__:
            raise ValueError('Use the same scikit-learn version as the saved model')
        if hashlib.sha256((folder / 'model.joblib').read_bytes()).hexdigest() != metadata['sha256']:
            raise ValueError('Artifact checksum mismatch')
        result = joblib.load(folder / 'model.joblib')
        if not isinstance(result, cls):
            raise ValueError('Unexpected artifact type')
        return result
