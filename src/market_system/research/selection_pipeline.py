"""Useful-post admission and type assignment have separate decisions and metrics."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import warnings

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.model_selection import StratifiedGroupKFold
from market_system.research.benchmark import ROOT, SEED, LABELS, candidates, load_data, split_data, dump, HistoricalEnsemble
from market_system.research.architectures import support

TARGETS = ('resume', 'vacancy')


def zero_error_threshold(score, positive, minimum=10):
    score, positive = np.asarray(score), np.asarray(positive, dtype=bool)
    if not (~positive).any():
        return 2.0  # Cannot establish a boundary without negatives.
    threshold = max(.5, float(np.nextafter(score[~positive].max(), np.inf)))
    return threshold if np.sum(positive & (score >= threshold)) >= minimum else 2.0


def type_support(values):
    pair = values[:, [0, 2]]
    denominator = pair.sum(axis=1, keepdims=True)
    return np.divide(pair, denominator, out=np.full_like(pair, .5), where=denominator > 0)


def fit_policy(y, gate, types):
    useful = np.isin(y, TARGETS)
    admission = zero_error_threshold(gate, useful)
    admitted = gate >= admission
    limits = [zero_error_threshold(types[admitted, j], y[admitted] == c)
              for j, c in enumerate(TARGETS)]
    return {'admission': admission, 'types': limits}


def decide(gate, types, policy):
    admitted = gate >= policy['admission']
    pred = np.full(len(gate), 'discard', dtype=object)
    pred[admitted] = 'useful_unspecified'
    passes = types >= np.asarray(policy['types'])
    unambiguous = admitted & (passes.sum(axis=1) == 1)
    pred[unambiguous] = np.asarray(TARGETS)[passes[unambiguous].argmax(axis=1)]
    reason = np.where(admitted, 'uncertain_type', np.where(gate <= .5, 'trash_score_high', 'uncertain_usefulness'))
    reason[unambiguous] = 'typed'
    return pred, reason


def evaluate(y, pred):
    y, pred = np.asarray(y), np.asarray(pred)
    useful, admitted = np.isin(y, TARGETS), pred != 'discard'
    tp = int(np.sum(useful & admitted)); fp = int(np.sum(~useful & admitted))
    result = {'useful_retained': tp, 'trash_admitted': fp,
              'admission_precision': tp/(tp+fp) if tp+fp else None,
              'useful_recall': tp/int(useful.sum()),
              'trash_recall': float(np.mean(pred[~useful]=='discard')),
              'useful_unspecified': int(np.sum(pred=='useful_unspecified')),
              'discarded_useful': int(np.sum(useful & ~admitted))}
    for c in TARGETS:
        chosen = pred == c
        correct = int(np.sum(chosen & (y==c)))
        count = int(chosen.sum())
        result.update({c+'_accepted': count, c+'_fp': count-correct,
                       c+'_precision': correct/count if count else None,
                       c+'_recall': correct/int(np.sum(y==c))})
    result['typed_correct'] = sum(result[c+'_accepted']-result[c+'_fp'] for c in TARGETS)
    return result


def qualified(row, precision=1.0):
    return all(row[c+'_accepted'] >= 20 and row[c+'_precision'] is not None
               and row[c+'_precision'] >= precision for c in TARGETS)


def rank(row):
    # Protect the untyped saved queue too; never hide trash in useful_unspecified.
    good = qualified(row) and row['trash_admitted']==0
    return (good, row['trash_recall'], row['useful_retained'], row['typed_correct'])


def run(out):
    out.mkdir(parents=True, exist_ok=False)
    start = time.perf_counter()
    previous = ROOT/json.loads((ROOT/'research/results/current_architectures.json').read_text(encoding='utf-8'))['path']
    df, audit, _ = load_data()
    train, valid, test = split_data(df)
    saved = pd.read_csv(previous/'split.csv')
    assert df.text_hash.tolist() == saved.text_hash.tolist()
    assert df.label.tolist() == saved.label.tolist()
    assert np.array_equal(df.group, saved.group)
    for name, idx in [('train',train),('validation',valid),('retrospective_test',test)]:
        assert np.array_equal(idx, np.flatnonzero(saved.split.to_numpy()==name))
    x, y = df.text_clean.to_numpy(), df.label.to_numpy()
    folds = list(StratifiedGroupKFold(3, shuffle=True, random_state=SEED+101).split(x[train],y[train],df.iloc[train].group))
    for a,b in folds:
        assert not set(df.iloc[train[a]].group) & set(df.iloc[train[b]].group)
    estimators = {k:v[0] for k,v in candidates().items() if k not in ('dummy','soft_voting')}
    cache = {file.stem:dict(np.load(file)) for file in previous.glob('*.npz')}
    assert len(cache)==60
    dump(out/'protocol.json', {'source': str(previous.relative_to(ROOT)), 'data':audit,
        'seed':SEED+101, 'validation_precision_target':1.0, 'test_precision_requirement':.95,
        'minimum_accepted_per_target':20,
        'selection':'Within gate/type family choose architecture on OOF; then choose pipeline on validation. Zero trash admission, precision=1 separately for resume/vacancy; maximise trash recall then useful retention then typed correct.',
        'thresholds':'OOF zero observed errors, at least 10 positives. Gate trained useful vs trash. Type scores normalized resume/vacancy supports, not calibrated probabilities.',
        'test':'Previously inspected retrospective test. No threshold adjustment after test; no production guarantees.',
        'rejected':'discard includes uncertain_usefulness; useful_unspecified is saved, counted in admission precision.'})
    warning_rows, gates = [], {}
    for name, estimator in estimators.items():
        print('Useful gate', name, flush=True)
        oof=np.zeros(len(train)); pv=[]; pt=[]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            for a,b in folds:
                model=clone(estimator).fit(x[train[a]],np.isin(y[train[a]],TARGETS).astype(int))
                oof[b]=support(model,x[train[b]])[1]
                pv.append(support(model,x[valid])[1]); pt.append(support(model,x[test])[1])
        warning_rows.extend({'model':name,'message':str(w.message)} for w in caught)
        gates[name]={'oof':oof,'validation':np.mean(pv,axis=0),'test':np.mean(pt,axis=0)}
        np.savez_compressed(out/(name+'_gate.npz'),**gates[name])
    rows, architecture_rows, policies = [], [], {}
    for gate_name, gate in gates.items():
        for type_name in estimators:
            choices=[]
            for kind in ('multiclass','two_ovr','two_pairs','three_pairs'):
                name=type_name+'__'+kind
                policy=fit_policy(y[train],gate['oof'],type_support(cache[name]['oof']))
                pred,_=decide(gate['oof'],type_support(cache[name]['oof']),policy)
                row={'gate':gate_name,'type_model':type_name,'architecture':kind,**evaluate(y[train],pred)}
                choices.append(row); architecture_rows.append(row)
                policies[gate_name,name]=policy
            choice=max(choices,key=rank); name=type_name+'__'+choice['architecture']
            pred,_=decide(gate['validation'],type_support(cache[name]['validation']),policies[gate_name,name])
            rows.append({'gate':gate_name,'type_model':type_name,'architecture':choice['architecture'],**evaluate(y[valid],pred)})
    pd.DataFrame(architecture_rows).to_csv(out/'architecture_selection.csv',index=False)
    pd.DataFrame(rows).to_csv(out/'validation.csv',index=False)
    winner=max(rows,key=rank); name=winner['type_model']+'__'+winner['architecture']
    policy=policies[winner['gate'],name]
    dump(out/'selection.json', {'winner':winner,'policy':policy,'qualified_validation':qualified(winner) and winner['trash_admitted']==0,
                               'test_used_for_selection':False})
    # Test metrics only AFTER the choice and thresholds are saved.
    comparisons=[]
    for split,idx in [('validation',valid),('test',test)]:
        pred,reason=decide(gates[winner['gate']][split],type_support(cache[name][split]),policy)
        metrics=evaluate(y[idx],pred)
        comparisons.append({'system':'selected_hierarchy','split':split,**metrics})
        df.iloc[idx][['text_hash','group','label']].assign(prediction=pred,reason=reason).to_csv(out/(split+'_predictions.csv'),index=False)
        if split=='test':
            dump(out/'test_result.json',{'metrics':metrics,'precision_requirement_met':qualified(metrics,.95),
                 'note':'Minimum 20 accepted per target is also required; retrospective test, not production certification.'})
    # Refit original architecture on train only; fixed original thresholds, no selection on test.
    print('Refit historical ensemble',flush=True)
    historical=HistoricalEnsemble().fit(x[train],y[train])
    for split,idx in [('validation',valid),('test',test)]:
        heads=historical.heads(x[idx]); pred=np.full(len(idx),'discard',dtype=object)
        pred[(heads['resume']>.8)&(heads['vacancy']<.3)]='resume'
        pred[(heads['vacancy']>.8)&(heads['resume']<.3)]='vacancy'
        comparisons.append({'system':'historical_refit_train60','split':split,**evaluate(y[idx],pred)})
        df.iloc[idx][['text_hash','group','label']].assign(prediction=pred,resume_score=heads['resume'],vacancy_score=heads['vacancy']).to_csv(out/(split+'_historical.csv'),index=False)
    # Common-policy diagnostic controls from already computed classifiers.
    for model_name in ['mlp_lsa','gradient_boosting_lsa']:
        for split,idx in [('validation',valid),('test',test)]:
            values=cache[model_name+'__multiclass'][split]
            pred=np.asarray(LABELS)[values.argmax(axis=1)].astype(object)
            pred[pred=='trash']='discard'
            comparisons.append({'system':model_name+'_forced_fold_average','split':split,**evaluate(y[idx],pred)})
    pd.DataFrame(comparisons).to_csv(out/'comparison.csv',index=False)
    dump(out/'warnings.json',warning_rows)
    (out/'source.py').write_bytes(Path(__file__).read_bytes())
    dump(out/'manifest.json', {'complete':True,'elapsed_seconds':time.perf_counter()-start,
         'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
         'gate_methods':15,'screened_configurations':900,'validation_candidates':225})
    dump(ROOT/'research/results/current_selection_pipeline.json',{'path':str(out.relative_to(ROOT))})
    print(json.dumps(winner,ensure_ascii=False),flush=True)


if __name__=='__main__':
    from market_system.runtime import enable_guard
    enable_guard()
    parser=argparse.ArgumentParser(); parser.add_argument('--output'); args=parser.parse_args()

    run(ROOT/args.output if args.output else ROOT/'research/results'/('selection_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')))
