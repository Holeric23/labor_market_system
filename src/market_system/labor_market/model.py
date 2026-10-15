from market_system.paths import local_path, DATABASE_PATH
"""Self-contained model bundles, including additive CatBoost updates."""
from copy import deepcopy
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re

import joblib
import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from scipy.special import expit

from market_system.core.classification.corpus import decide_label, DECISION_POLICY

HEADS = {'vacancy': 'lr_vac', 'resume': 'lr_res'}


def clean(text):
    return re.sub(r'http\S+|www\S+|https\S+', '', text, flags=re.MULTILINE)


def text_key(text):
    # Catch whitespace and URL-only differences in learning/evaluation overlap.
    return hashlib.sha256(' '.join(clean(text).split()).encode('utf-8')).hexdigest()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


class Ensemble:
    def __init__(self, linear, boosts, metadata):
        self.linear, self.boosts, self.metadata = linear, boosts, deepcopy(metadata)
        self.version = metadata['model_version']
        self.path = None
        self.skill_lexicon = None
        self.occupation_lexicon = None
        preprocessing = metadata.get('preprocessing')
        expected = {'operation':'re.sub','pattern':r'http\S+|www\S+|https\S+',
                    'replacement':'','flags':'MULTILINE'}
        if preprocessing is not None and preprocessing != expected:
            raise ValueError('Unsupported text preprocessing; refusing to change model inputs')
        if metadata.get('decision_policy') != DECISION_POLICY:
            raise ValueError('Unsupported classification policy')
        weights = [metadata['weight_lr'], metadata['weight_cb']]
        if not all(np.isfinite(w) and w >= 0 for w in weights) or not np.isclose(sum(weights), 1):
            raise ValueError('Ensemble weights must be nonnegative and sum to 1')
        decide_label(0, 0, metadata['vacancy_threshold'], metadata['resume_threshold'], metadata['max_other_probability'])
        for name, key in HEADS.items():
            if not np.array_equal(linear[key].classes_, [0, 1]):
                raise ValueError('Expected binary linear classes [0, 1]')
            if linear[key].coef_.shape != (1,len(linear['vectorizer'].get_feature_names_out())):
                raise ValueError('Linear model and TF-IDF dimensions differ')
            if not boosts[name]:
                raise ValueError('Missing CatBoost head')
            for model in boosts[name]:
                if not np.array_equal(model.classes_, [0, 1]):
                    raise ValueError('Expected binary CatBoost classes [0, 1]')

    @classmethod
    def load(cls, folder):
        """Only load trusted local bundles: joblib may execute Python code."""
        folder = local_path(folder)
        meta = json.loads((folder / 'metadata.json').read_text(encoding='utf-8'))
        for package in ('scikit-learn', 'catboost'):
            expected = meta['packages'][package]
            installed = importlib.metadata.version(package)
            if expected != installed:
                raise RuntimeError(f'{package}=={expected} required by this artifact; installed {installed}')
        for filename, expected in meta['artifacts_sha256'].items():
            path = (folder / filename).resolve()
            if path.parent != folder or sha256(path) != expected:
                raise ValueError(f'Artifact integrity check failed: {filename}')
        files = meta.get('catboost_files', {name: [name + '.cbm'] for name in HEADS})
        required = {'linear.joblib'} | {f for paths in files.values() for f in paths}
        if not required <= set(meta['artifacts_sha256']):
            raise ValueError('Missing checksums for model files')
        boosts = {}
        for name in HEADS:
            boosts[name] = [CatBoostClassifier().load_model(str(folder / f)) for f in files[name]]
        result = cls(joblib.load(folder / 'linear.joblib'), boosts, meta)
        result.path = folder
        if 'skill_lexicon.json' in meta['artifacts_sha256']:
            result.skill_lexicon = folder / 'skill_lexicon.json'
        if 'occupation_lexicon.json' in meta['artifacts_sha256']:
            result.occupation_lexicon = folder / 'occupation_lexicon.json'
        return result

    def raw_boost(self, name, frame):
        return sum(np.asarray(model.predict(frame, prediction_type='RawFormulaVal'), dtype=float)
                   for model in self.boosts[name])

    def probabilities(self, texts):
        if not texts:
            return {name: np.empty(0) for name in HEADS}
        cleaned = [clean(t) for t in texts]
        x = self.linear['vectorizer'].transform(cleaned)
        frame = pd.DataFrame({'clean_text': cleaned})
        return {name: self.metadata['weight_lr'] * self.linear[key].predict_proba(x)[:, 1]
                      + self.metadata['weight_cb'] * expit(self.raw_boost(name, frame))
                for name, key in HEADS.items()}

    def label(self, pv, pr):
        return decide_label(float(pv), float(pr), self.metadata['vacancy_threshold'],
                            self.metadata['resume_threshold'], self.metadata['max_other_probability'])
