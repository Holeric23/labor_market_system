"""Shared cost-sensitive screening: tune on validation, compare frozen finalists on test."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import time
import warnings

import joblib
import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from sklearn.base import clone
from sklearn.model_selection import ParameterGrid
from threadpoolctl import threadpool_limits

from market_system.research.benchmark import ROOT, SEED, LABELS, candidates, load_data, split_data, dump, HistoricalEnsemble
from market_system.research.architectures import support

TARGETS=('resume','vacancy')
OBJECTIVE={'validation_precision':1.0,'test_precision':.95,'minimum_predictions_per_target':20,
           'maximize':'mean(recall_resume, recall_vacancy)', 'trash_metrics_used_for_selection':False}
COSTS=(1.,5.,20.)
ARCHITECTURES=('multiclass','two_ovr','two_pairs','three_pairs')


def fresh(estimator):
    # CatBoost mutates list-valued text_features, violating sklearn clone's identity check.
    if isinstance(estimator,CatBoostClassifier):
        return CatBoostClassifier(**copy.deepcopy(estimator.get_params()))
    return clone(estimator)


def take(x, indices):
    return x.iloc[indices] if isinstance(x,pd.DataFrame) else x[indices]


def fit_weighted(estimator, x, y, weight):
    model=fresh(estimator)
    if 'sample_weight' in inspect.signature(model.fit).parameters:
        model.fit(x,y,sample_weight=weight)
        return model,'sample_weight'
    if np.all(weight==weight[0]):
        return model.fit(x,y),'unweighted'
    # Methods without sample weights get reproducible, class-stratified weighted resampling.
    # Fit the representation before this step so duplicated texts do not change IDF.
    rng=np.random.default_rng(SEED+301)
    labels=np.unique(y)
    masses=np.array([weight[y==c].sum() for c in labels]); masses=masses/masses.sum()
    counts=np.maximum(2,np.floor(masses*len(y)).astype(int))
    counts[np.argmax(masses)]+=len(y)-counts.sum()
    idx=np.concatenate([rng.choice(np.flatnonzero(y==c),int(n),replace=True) for c,n in zip(labels,counts)])
    rng.shuffle(idx)
    return model.fit(take(x,idx),y[idx]),'weighted_resampling'


class WeightedArchitecture:
    def __init__(self, estimator, architecture, cost):
        self.estimator,self.architecture,self.cost=estimator,architecture,cost

    def fit(self,x,y):
        y=np.asarray(y); self.models={}; self.weight_methods=[]
        if self.architecture=='multiclass':
            tasks=[('all',np.ones(len(y),bool),y,y=='trash')]
        elif self.architecture=='two_ovr':
            tasks=[(c,np.ones(len(y),bool),(y==c).astype(int),y!=c) for c in TARGETS]
        else:
            pairs=[('resume','trash'),('vacancy','trash')]
            if self.architecture=='three_pairs': pairs.append(('resume','vacancy'))
            tasks=[(pair,np.isin(y,pair),y,y=='trash') for pair in pairs]
        for name,mask,target,negative in tasks:
            weight=np.where(negative[mask],self.cost,1.).astype(float)
            weight/=weight.mean()
            model,method=fit_weighted(self.estimator,take(x,np.flatnonzero(mask)),target[mask],weight)
            self.models[name]=model; self.weight_methods.append(method)
        return self

    def predict_support(self,x):
        if self.architecture=='multiclass':
            result=support(self.models['all'],x)
        elif self.architecture=='two_ovr':
            result={c:support(self.models[c],x)[1] for c in TARGETS}
            result['trash']=np.minimum(1-result['resume'],1-result['vacancy'])
        else:
            evidence={c:[] for c in LABELS}
            for pair,model in self.models.items():
                values=support(model,x)
                for c in pair: evidence[c].append(values[c])
            result={c:np.min(evidence[c],axis=0) for c in LABELS}
        return np.column_stack([result[c] for c in TARGETS])


def tune_thresholds(y, values):
    """Lowest score cutoff excluding every observed false assignment, independently per target."""
    y=np.asarray(y)
    return np.array([np.nextafter(values[y!=c,j].max(),np.inf) for j,c in enumerate(TARGETS)])


def decide(values, thresholds):
    accepted=values>=thresholds
    result=np.full(len(values),'discard_uncertain',dtype=object)
    result[accepted.sum(axis=1)==2]='useful_unspecified'
    unique=accepted.sum(axis=1)==1
    result[unique]=np.asarray(TARGETS)[accepted[unique].argmax(axis=1)]
    return result


def target_metrics(y, pred):
    y,pred=np.asarray(y),np.asarray(pred)
    result={}
    for c in TARGETS:
        accepted=pred==c; count=int(accepted.sum()); tp=int(np.sum(accepted & (y==c)))
        result.update({c+'_accepted':count,c+'_tp':tp,c+'_fp':count-tp,
                       c+'_precision':tp/count if count else None,c+'_recall':tp/int(np.sum(y==c))})
    result['target_macro_recall']=float(np.mean([result[c+'_recall'] for c in TARGETS]))
    result['useful_unspecified']=int(np.sum(pred=='useful_unspecified'))
    result['discard_uncertain']=int(np.sum(pred=='discard_uncertain'))
    return result


def qualifies(row, precision=1.):
    return all(row[c+'_accepted']>=OBJECTIVE['minimum_predictions_per_target'] and
               row[c+'_precision'] is not None and row[c+'_precision']>=precision for c in TARGETS)


def rank(row):
    return (qualifies(row),row['target_macro_recall'],min(row['resume_recall'],row['vacancy_recall']))


def train_family(name, original, grid, xtrain,ytrain,xvalid,yvalid,out):
    preprocessing_begin=time.perf_counter()
    if name=='catboost_text':
        preprocessor=None
        train=pd.DataFrame({'text':xtrain}); valid=pd.DataFrame({'text':xvalid})
        base=CatBoostClassifier(iterations=180,depth=4,learning_rate=.1,random_seed=SEED,
            thread_count=2,verbose=False,allow_writing_files=False,text_features=['text'])
    else:
        preprocessor=clone(original[:-1]); train=preprocessor.fit_transform(xtrain,ytrain)
        valid=preprocessor.transform(xvalid); base=clone(original[-1])
        if 'class_weight' in base.get_params(): base.set_params(class_weight=None)
    preprocessing_seconds=time.perf_counter()-preprocessing_begin
    rows=[]; best=None; caught_rows=[]
    for parameter_index,parameters in enumerate(ParameterGrid(grid)):
        params={k.removeprefix('model__'):v for k,v in parameters.items()}
        for architecture in ARCHITECTURES:
            for cost in COSTS:
                begin=time.perf_counter()
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter('always')
                    model=WeightedArchitecture(fresh(base).set_params(**params),architecture,cost).fit(train,ytrain)
                    values=model.predict_support(valid)
                thresholds=tune_thresholds(yvalid,values); pred=decide(values,thresholds)
                row={'model':name,'parameter_index':parameter_index,'parameters':json.dumps(parameters),
                     'architecture':architecture,'cost':cost,'seconds':time.perf_counter()-begin,
                     'preprocessing_seconds':preprocessing_seconds,'weight_method':','.join(sorted(set(model.weight_methods))),
                     'threshold_resume':thresholds[0],'threshold_vacancy':thresholds[1],**target_metrics(yvalid,pred)}
                rows.append(row)
                caught_rows.extend({'model':name,'architecture':architecture,'cost':cost,'message':str(w.message)} for w in caught)
                if best is None or rank(row)>rank(best):
                    best=row
                    joblib.dump({'preprocessor':preprocessor,'model':model,'thresholds':thresholds},out/(name+'.joblib'))
                    np.savez_compressed(out/(name+'_validation.npz'),values=values,thresholds=thresholds)
        print('Parameters complete',name,parameter_index,flush=True)
    return rows,best,caught_rows


def run(out,resume=False):
    if resume:
        if (out/'manifest.json').exists():raise ValueError('Completed runs cannot be resumed')
        prior_protocol=json.loads((out/'protocol.json').read_text(encoding='utf-8'))
    else:
        out.mkdir(parents=True,exist_ok=False)
    begin=time.perf_counter()
    df,audit,_=load_data(); train,valid,test=split_data(df)
    x=df.text_clean.to_numpy();y=df.label.to_numpy()
    if resume:
        if prior_protocol['data']!=audit or prior_protocol['objective']!=OBJECTIVE:
            raise ValueError('Data/objective changed; create a new run')
    split=np.full(len(df),'',dtype=object);split[train]='train';split[valid]='validation';split[test]='test'
    df[['text_hash','label','group']].assign(split=split).to_csv(out/'split.csv',index=False)
    dump(out/'protocol.json',{'objective':OBJECTIVE,'costs':COSTS,'architectures':ARCHITECTURES,'seed':SEED,'data':audit,
        'split':'Same grouped approximately 60/20/20. Fit and preprocessing on train only. Tune parameters, costs, architecture, thresholds on validation. Frozen per-family finalists compared on reused test.',
        'scope':'One common small grid per method; two values of one main hyperparameter (centroid has one). MLP same budget, no curriculum/architecture-size sweep.',
        'weights':'Normalized to mean one. No automatic balanced class weights. Two-OvR penalizes all negatives of each target. Multiclass and pairs penalize trash examples; mutual target confusion handled by thresholds. No trash metric in selection.',
        'thresholds':'Independently lowest cutoffs above every validation negative. Zero observed false assignments. Both pass = useful_unspecified, neither = discard_uncertain. No lower 0.5 cutoff.',
        'warning':'Validation precision=1 is constructed by tuning, not independent evidence. Test comparisons/favorite selection are exploratory: this corpus was already inspected. Test precision>=.95 is a per-target check, not retuning.'})
    all_rows=[];finalists=[];warning_rows=[]
    if resume:
        def records(path):
            frame=pd.read_csv(path).astype(object)
            return frame.where(pd.notnull(frame),None).to_dict('records')
        all_rows=records(out/'search.csv');finalists=records(out/'validation_finalists.csv')
        warning_rows=(json.loads((out/'warnings.json').read_text(encoding='utf-8')) if (out/'warnings.json').exists() else
                      [{'message':'Earlier families completed before resume support; their warning log was not checkpointed. Do not infer convergence from absence of warnings.'}])
    completed={row['model'] for row in finalists}
    for name,(estimator,grid) in candidates().items():
        if name in ('dummy','soft_voting'):continue
        if name in completed:continue
        print('Family',name,flush=True)
        rows,best,warnings_for_model=train_family(name,estimator,grid,x[train],y[train],x[valid],y[valid],out)
        all_rows.extend(rows);finalists.append(best);warning_rows.extend(warnings_for_model)
        pd.DataFrame(all_rows).to_csv(out/'search.csv',index=False)
        pd.DataFrame(finalists).to_csv(out/'validation_finalists.csv',index=False)
        dump(out/'warnings.json',warning_rows)
    # Every family's architecture, hyperparameters, cost, and thresholds are frozen BEFORE test.
    dump(out/'selection.json',{'validation_winner':max(finalists,key=rank),
                             'finalists':finalists,'test_used_for_parameter_selection':False})
    test_rows=[]
    for row in finalists:
        name=row['model'];bundle=joblib.load(out/(name+'.joblib'))
        begin_prediction=time.perf_counter()
        transformed=(bundle['preprocessor'].transform(x[test]) if bundle['preprocessor'] is not None else pd.DataFrame({'text':x[test]}))
        values=bundle['model'].predict_support(transformed);pred=decide(values,bundle['thresholds'])
        result={'model':name,'architecture':row['architecture'],'cost':row['cost'],
                'validation_qualified':qualifies(row),**target_metrics(y[test],pred),
                'inference_ms_per_post':(time.perf_counter()-begin_prediction)/len(test)*1000}
        result['test_qualified']=qualifies(result,OBJECTIVE['test_precision']);test_rows.append(result)
        np.savez_compressed(out/(name+'_test.npz'),values=values,thresholds=bundle['thresholds'])
        df.iloc[test][['text_hash','label','group','text']].assign(prediction=pred).to_csv(out/(name+'_test_predictions.csv'),index=False)
    pd.DataFrame(test_rows).to_csv(out/'test_comparison.csv',index=False)
    # Existing ensemble: fixed production policy and the same validation-tuned target policy.
    print('Historical reference',flush=True)
    historical=HistoricalEnsemble().fit(x[train],y[train]); hv=historical.heads(x[valid]);ht=historical.heads(x[test])
    v=np.column_stack([hv[c] for c in TARGETS]);t=np.column_stack([ht[c] for c in TARGETS]);limits=tune_thresholds(y[valid],v)
    baseline=[]
    for part,truth,values in [('validation',y[valid],v),('test',y[test],t)]:
        fixed=np.full(len(values),'discard_uncertain',dtype=object)
        fixed[(values[:,0]>.8)&(values[:,1]<.3)]='resume';fixed[(values[:,1]>.8)&(values[:,0]<.3)]='vacancy'
        for policy,pred in [('original_fixed',fixed),('validation_tuned',decide(values,limits))]:
            baseline.append({'policy':policy,'split':part,**target_metrics(truth,pred)})
    pd.DataFrame(baseline).to_csv(out/'historical_comparison.csv',index=False)
    np.savez_compressed(out/'historical_scores.npz',validation=v,test=t,thresholds=limits)
    dump(out/'warnings.json',warning_rows)
    (out/'source.py').write_bytes(Path(__file__).read_bytes())
    dump(out/'manifest.json',{'complete':True,'seconds_current_invocation':time.perf_counter()-begin,'resumed':resume,'configurations':len(all_rows),
                             'families':len(finalists),'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    dump(ROOT/'research/results/current_precision_benchmark.json',{'path':str(out.relative_to(ROOT))})
    print('Complete',out,flush=True)


if __name__=='__main__':
    from market_system.runtime import enable_guard
    enable_guard()
    # Keep joblib artifacts importable when the runner was launched with python -m.

    import sys
    sys.modules['market_system.research.precision_benchmark']=sys.modules[__name__]
    WeightedArchitecture.__module__='market_system.research.precision_benchmark'
    parser=argparse.ArgumentParser();parser.add_argument('--output');parser.add_argument('--resume',action='store_true');args=parser.parse_args()
    if args.resume and not args.output:parser.error('--resume requires --output')
    with threadpool_limits(limits=2):
        run(ROOT/args.output if args.output else ROOT/'research/results'/('precision_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')),args.resume)
