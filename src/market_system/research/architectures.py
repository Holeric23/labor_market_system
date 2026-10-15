"""Controlled architecture screening; old test is retrospective, never used for selection."""
import argparse
from datetime import datetime, timezone
import hashlib
import itertools
import json
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.special import expit, softmax
from sklearn.base import clone
from sklearn.model_selection import StratifiedGroupKFold

from market_system.research.benchmark import ROOT, SEED, LABELS, candidates, load_data, split_data, dump, scores

ARCHITECTURES = ('multiclass', 'two_ovr', 'two_pairs', 'three_pairs')
TARGETS = ('resume', 'vacancy')


def support(model, texts):
    """Comparable bounded scores, deliberately NOT described as calibrated probabilities."""
    if hasattr(model, 'predict_proba'):
        values = model.predict_proba(texts)
    elif hasattr(model, 'decision_function'):
        values = model.decision_function(texts)
        values = np.column_stack((expit(-values), expit(values))) if values.ndim == 1 else softmax(values, axis=1)
    else:
        pred = model.predict(texts)
        values = np.column_stack([pred == c for c in model.classes_]).astype(float)
    if not np.isfinite(values).all():
        raise ValueError('Nonfinite model scores')
    return dict(zip(model.classes_, values.T))


class Architecture:
    def __init__(self, estimator, kind):
        self.estimator, self.kind = estimator, kind

    def fit(self, x, y):
        x, y = np.asarray(x), np.asarray(y)
        self.models = {}
        if self.kind == 'multiclass':
            self.models['all'] = clone(self.estimator).fit(x, y)
        elif self.kind == 'two_ovr':
            for label in TARGETS:
                self.models[label] = clone(self.estimator).fit(x, (y == label).astype(int))
        else:
            pairs = [('resume', 'trash'), ('vacancy', 'trash')]
            if self.kind == 'three_pairs':
                pairs.append(('resume', 'vacancy'))
            for a, b in pairs:
                mask = np.isin(y, [a, b])
                self.models[a, b] = clone(self.estimator).fit(x[mask], y[mask])
        return self

    def predict_support(self, x):
        if self.kind == 'multiclass':
            result = support(self.models['all'], x)
        elif self.kind == 'two_ovr':
            result = {c: support(self.models[c], x)[1] for c in TARGETS}
            result['trash'] = np.minimum(1-result['resume'], 1-result['vacancy'])
        else:
            evidence = {c: [] for c in LABELS}
            for pair, model in self.models.items():
                output = support(model, x)
                for c in pair:
                    evidence[c].append(output[c])
            # A class needs support from every available pair involving that class.
            result = {c: np.min(evidence[c], axis=0) for c in LABELS}
        return np.column_stack([result[c] for c in LABELS])


def thresholds(y, values, minimum=10):
    """Maximum observed recall with zero false positives, per class. Fitted on OOF only."""
    result = []
    for j, c in enumerate(LABELS):
        wrong = values[np.asarray(y) != c, j]
        threshold = max(.5, float(np.nextafter(wrong.max(), np.inf)))
        if np.sum((values[:, j] >= threshold) & (np.asarray(y) == c)) < minimum:
            threshold = 2.0  # Explicitly disable unsupported automatic decisions.
        result.append(threshold)
    return np.asarray(result)


def decide(values, limits):
    accepted = values >= limits
    pred = np.full(len(values), 'uncertain', dtype=object)
    unique = accepted.sum(axis=1) == 1
    pred[unique] = np.asarray(LABELS)[accepted[unique].argmax(axis=1)]
    return pred


def metrics(y, pred):
    y, pred = np.asarray(y), np.asarray(pred)
    result = {'coverage': float(np.mean(pred != 'uncertain'))}
    for c in LABELS:
        tp = int(np.sum((pred == c) & (y == c)))
        fp = int(np.sum((pred == c) & (y != c)))
        result.update({c+'_tp': tp, c+'_fp': fp, c+'_accepted': tp+fp,
                       c+'_precision': tp/(tp+fp) if tp+fp else None,
                       c+'_recall': tp/int(np.sum(y == c)),
                       c+'_fpr': fp/int(np.sum(y != c))})
    result['target_fp'] = sum(result[c+'_fp'] for c in TARGETS)
    result['target_tp'] = sum(result[c+'_tp'] for c in TARGETS)
    result['target_recall'] = result['target_tp']/int(np.sum(np.isin(y, TARGETS)))
    return result


def rank(row):
    # Never reward an all-abstain system. Eligibility is reported separately.
    eligible = all(row[c+'_accepted'] >= 20 for c in TARGETS)
    return (eligible, -row['target_fp'], row['target_tp'])


def run(out, names=None):
    out.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    df, audit, _ = load_data()
    train, valid, test = split_data(df)
    x, y = df.text_clean.to_numpy(), df.label.to_numpy()
    folds = list(StratifiedGroupKFold(3, shuffle=True, random_state=SEED+101).split(
        x[train], y[train], df.iloc[train].group))
    for a, b in folds:
        assert not set(df.iloc[train[a]].group) & set(df.iloc[train[b]].group)
    models = {k: v[0] for k, v in candidates().items() if k not in ('dummy', 'soft_voting')}
    if names:
        models = {k: models[k] for k in names}
    protocol = {'seed': SEED+101, 'data': audit, 'architectures': ARCHITECTURES,
        'parameters': {k: str(v) for k, v in models.items()},
        'selection': 'Architecture: training grouped OOF zero-FP thresholds and maximum target TP. Model: common validation, minimum 20 accepted per target, then minimum FP, maximum TP.',
        'scope': 'Retrospective reused corpus and old test. Fixed default hyperparameters; no new grid tuning. OOF threshold fit scores are optimistic, validation is model selection, test is diagnostic.',
        'aggregation': 'Pairwise minimum support (conservative conjunction), not sklearn majority OvO. Scores not calibrated. No refit after thresholds: fold models averaged at inference.',
        'test_used_for_selection': False}
    dump(out/'protocol.json', protocol)
    (out/'source.py').write_bytes(Path(__file__).read_bytes())
    parts = np.full(len(df), '', dtype=object)
    parts[train], parts[valid], parts[test] = 'train', 'validation', 'retrospective_test'
    df[['text_hash','group','label']].assign(split=parts).to_csv(out/'split.csv', index=False)
    rows, chosen, cache, warning_rows = [], [], {}, []
    for name, estimator in models.items():
        local = []
        for kind in ARCHITECTURES:
            print('Architecture', name, kind, flush=True)
            begin = time.perf_counter()
            oof = np.zeros((len(train), 3)); pv = []; pt = []
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                for a, b in folds:
                    model = Architecture(estimator, kind).fit(x[train[a]], y[train[a]])
                    oof[b] = model.predict_support(x[train[b]])
                    pv.append(model.predict_support(x[valid]))
                    pt.append(model.predict_support(x[test]))
            warning_rows.extend({'model': name, 'architecture': kind, 'message': str(w.message)} for w in caught)
            limits = thresholds(y[train], oof)
            val, diagnostic = np.mean(pv, axis=0), np.mean(pt, axis=0)
            row = {'model': name, 'architecture': kind, 'seconds': time.perf_counter()-begin,
                   'forced_macro_f1': scores(y[train], np.asarray(LABELS)[oof.argmax(axis=1)])['macro_f1'],
                   **metrics(y[train], decide(oof, limits))}
            local.append(row); rows.append(row)
            cache[name, kind] = (oof, val, diagnostic, limits)
            np.savez_compressed(out/(name+'__'+kind+'.npz'), oof=oof, validation=val,
                                test=diagnostic, thresholds=limits)
            pd.DataFrame(rows).to_csv(out/'architecture_selection.csv', index=False)
        selected = max(local, key=rank)
        kind = selected['architecture']
        oof, val, diagnostic, limits = cache[name, kind]
        chosen.append({'model': name, 'architecture': kind, **metrics(y[valid], decide(val, limits))})
        pd.DataFrame(chosen).to_csv(out/'model_selection.csv', index=False)
    winner = max(chosen, key=rank)
    dump(out/'selection.json', {'winner': winner, 'qualified_on_validation':
        winner['target_fp'] == 0 and all(winner[c+'_accepted'] >= 20 for c in TARGETS),
        'test_opened_for_selection': False})
    diagnostics = []
    for row in chosen:
        _, _, values, limits = cache[row['model'], row['architecture']]
        pred = decide(values, limits)
        diagnostics.append({'model': row['model'], 'architecture': row['architecture'], **metrics(y[test], pred)})
        df.iloc[test][['text_hash','group','label']].assign(prediction=pred).to_csv(out/(row['model']+'_diagnostic.csv'), index=False)
    pd.DataFrame(diagnostics).to_csv(out/'retrospective_test.csv', index=False)
    # User proposal: high-recall permissive gate followed by high-precision specialist.
    # All pair choices are compared on validation; test never chooses the pair.
    ensemble_rows = []
    for gate, specialist in itertools.product(chosen, repeat=2):
        go, gv, gt, _ = cache[gate['model'], gate['architecture']]
        so, sv, st, limits = cache[specialist['model'], specialist['architecture']]
        # Gate only rejects confidently trash-like posts; retain every observed OOF target.
        cutoff = float(np.nextafter(go[np.isin(y[train], TARGETS), 1].max(), np.inf))
        for split, g, s, truth in [('validation', gv, sv, y[valid]), ('test', gt, st, y[test])]:
            pred = decide(s, limits)
            reject = g[:, 1] >= cutoff
            pred[reject] = 'uncertain'  # Gate is not allowed to invent a trusted trash label.
            ensemble_rows.append({'gate': gate['model'], 'specialist': specialist['model'],
                                  'split': split, 'gate_cutoff': cutoff, **metrics(truth, pred)})
    pd.DataFrame(ensemble_rows).to_csv(out/'cascades.csv', index=False)
    best_cascade = max([r for r in ensemble_rows if r['split']=='validation'], key=rank)
    dump(out/'cascade_selection.json', best_cascade)
    dump(out/'warnings.json', warning_rows)
    dump(out/'manifest.json', {'complete': True, 'elapsed_seconds': time.perf_counter()-started,
         'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
         'n_methods': len(models), 'n_architectures': len(ARCHITECTURES)})
    dump(ROOT/'research/results/current_architectures.json', {'path': str(out.relative_to(ROOT))})
    print('Complete', out, flush=True)
    return out


if __name__ == '__main__':
    from market_system.runtime import enable_guard
    enable_guard()
    parser = argparse.ArgumentParser()

    parser.add_argument('--models', nargs='+')
    parser.add_argument('--output')
    args = parser.parse_args()
    run(ROOT/args.output if args.output else ROOT/'research/results'/('architectures_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')), args.models)
