from market_system.sqlite import connect as sqlite_connect
from market_system.paths import local_path, DATABASE_PATH
"""Run with python -m research.benchmark. Never writes to the source database."""
from pathlib import Path
import argparse
import hashlib
import importlib.metadata
import json
import re
import sqlite3
import time
import warnings
from contextlib import closing

import joblib
import numpy as np
import pandas as pd
from scipy.sparse.csgraph import connected_components
from sklearn.base import BaseEstimator, ClassifierMixin, clone
from sklearn.decomposition import TruncatedSVD
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier, HistGradientBoostingClassifier, VotingClassifier
from sklearn.feature_extraction.text import TfidfVectorizer, HashingVectorizer
from sklearn.linear_model import LogisticRegression, SGDClassifier, RidgeClassifier
from sklearn.metrics import f1_score, accuracy_score, classification_report, confusion_matrix
from sklearn.model_selection import StratifiedGroupKFold, GridSearchCV, train_test_split
from sklearn.naive_bayes import ComplementNB, MultinomialNB
from sklearn.neighbors import KNeighborsClassifier, NearestCentroid, radius_neighbors_graph
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import Pipeline, FeatureUnion
from sklearn.preprocessing import Normalizer, StandardScaler
from sklearn.svm import LinearSVC
from catboost import CatBoostClassifier

from market_system.paths import PROJECT_ROOT as ROOT, PACKAGE_ROOT
SEED = 20260930
LABELS = ['resume', 'trash', 'vacancy']


def clean(text):
    return re.sub(r'http\S+|www\S+|https\S+', '', text, flags=re.MULTILINE)


def key(text):
    return re.sub(r'\s+', ' ', clean(text).lower().replace('ё', 'е')).strip()


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False), encoding='utf-8')


def scores(y, pred):
    return {'macro_f1': f1_score(y, pred, labels=LABELS, average='macro', zero_division=0),
            'accuracy': accuracy_score(y, pred),
            **{f'f1_{k}': v for k, v in zip(LABELS, f1_score(y, pred, labels=LABELS, average=None, zero_division=0))}}


def load_data():
    paths = sorted((ROOT / 'data/learning_data').glob('*.csv'))
    if not paths:
        raise ValueError('No labeled data')
    frames = [pd.read_csv(p, keep_default_na=False).assign(origin=p.name) for p in paths]
    raw = pd.concat(frames, ignore_index=True)
    if not set(raw.label) <= set(LABELS) or not raw.text.map(lambda s: isinstance(s, str) and bool(key(s))).all():
        raise ValueError('Invalid labels or empty text')
    raw['normalized'] = raw.text.map(key)
    conflict = raw.groupby('normalized').label.nunique()
    bad = set(conflict[conflict > 1].index)
    rejected = raw[raw.normalized.isin(bad)]
    df = raw[~raw.normalized.isin(bad)].drop_duplicates('normalized').copy().reset_index(drop=True)
    df['text_clean'] = df.text.map(clean)
    # Stateless hashing: no vocabulary/IDF is fitted on the held-out documents.
    h = HashingVectorizer(analyzer='char_wb', ngram_range=(4, 5), n_features=2**18, alternate_sign=False)
    x = h.transform(df.normalized)
    graph = radius_neighbors_graph(x, radius=0.10, metric='cosine', include_self=True, n_jobs=2)
    _, df['group'] = connected_components(graph, directed=False)
    with closing(sqlite_connect((DATABASE_PATH).as_uri() + '?mode=ro', uri=True)) as con:
        meta = pd.read_sql_query('''SELECT c.text_hash, MIN(p.published_at) first_date,
            MIN(s.name) source, COUNT(DISTINCT s.id) source_count FROM contents c
            LEFT JOIN publications p ON p.content_id=c.id LEFT JOIN sources s ON s.id=p.source_id
            GROUP BY c.text_hash''', con)
    df = df.merge(meta, on='text_hash', how='left', validate='one_to_one')
    audit = {'input_rows': len(raw), 'retained_rows': len(df), 'conflicting_rows': len(rejected),
             'duplicate_rows_removed': len(raw)-len(rejected)-len(df), 'groups': int(df.group.nunique()),
             'near_duplicate_groups': int((df.groupby('group').size()>1).sum()),
             'largest_group': int(df.groupby('group').size().max()),
             'input_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}
    return df, audit, rejected


def split_data(df):
    # Approximate 60/20/20; whole similarity components stay together.
    outer = StratifiedGroupKFold(5, shuffle=True, random_state=SEED)
    dev, test = next(outer.split(df, df.label, df.group))
    inner = StratifiedGroupKFold(4, shuffle=True, random_state=SEED+1)
    tr, va = next(inner.split(df.iloc[dev], df.iloc[dev].label, df.iloc[dev].group))
    train, valid = dev[tr], dev[va]
    sets = [set(df.iloc[idx].group) for idx in (train, valid, test)]
    assert not (sets[0]&sets[1] or sets[0]&sets[2] or sets[1]&sets[2])
    return train, valid, test


class TextCatBoost(ClassifierMixin, BaseEstimator):
    def __init__(self, iterations=180, depth=4):
        self.iterations, self.depth = iterations, depth

    def fit(self, X, y):
        self.model_ = CatBoostClassifier(iterations=self.iterations, depth=self.depth,
            learning_rate=0.1, auto_class_weights='Balanced', random_seed=SEED,
            thread_count=2, verbose=False, allow_writing_files=False,
            text_features=['text'])
        self.model_.fit(pd.DataFrame({'text': list(X)}), y)
        self.classes_ = self.model_.classes_
        return self

    def predict(self, X):
        return self.model_.predict(pd.DataFrame({'text': list(X)})).ravel()

    def predict_proba(self, X):
        return self.model_.predict_proba(pd.DataFrame({'text': list(X)}))


class HistoricalEnsemble(ClassifierMixin, BaseEstimator):
    """Fresh refit of the notebook's two heads and fixed 0.2/0.8 blend."""
    def fit(self, X, y):
        self.vectorizer_ = word()
        matrix = self.vectorizer_.fit_transform(X)
        self.linear_, self.boost_ = {}, {}
        for label in ('vacancy', 'resume'):
            target = (np.asarray(y) == label).astype(int)
            self.linear_[label] = LogisticRegression(class_weight='balanced', max_iter=1000, random_state=42).fit(matrix, target)
            self.boost_[label] = CatBoostClassifier(iterations=500, learning_rate=0.1,
                auto_class_weights='Balanced', random_seed=42, verbose=False,
                thread_count=2, allow_writing_files=False, text_features=['text'])
            self.boost_[label].fit(pd.DataFrame({'text': list(X)}), target)
        self.classes_ = np.array(LABELS)
        return self

    def heads(self, X):
        matrix = self.vectorizer_.transform(X)
        return {k: .2*self.linear_[k].predict_proba(matrix)[:, 1] +
                .8*self.boost_[k].predict_proba(pd.DataFrame({'text': list(X)}))[:, 1] for k in self.linear_}

    def predict(self, X):
        p = self.heads(X)
        return np.where((p['vacancy']>=.8)&(p['vacancy']>p['resume']), 'vacancy',
               np.where((p['resume']>=.8)&(p['resume']>p['vacancy']), 'resume', 'trash'))


def word():
    return TfidfVectorizer(ngram_range=(1, 2), min_df=2, max_df=.9, sublinear_tf=True, max_features=40000)


def char():
    return TfidfVectorizer(analyzer='char_wb', ngram_range=(3, 5), min_df=2, sublinear_tf=True, max_features=50000)


def pipe(estimator, features=None, dense=False):
    steps = [('features', features if features is not None else word())]
    if dense:
        steps += [('svd', TruncatedSVD(64, random_state=SEED)), ('scale', StandardScaler())]
    return Pipeline(steps+[('model', estimator)])


def candidates():
    lr = LogisticRegression(max_iter=1200, class_weight='balanced', random_state=SEED)
    return {
        'dummy': (pipe(DummyClassifier(strategy='most_frequent')), {}),
        'word_lr': (pipe(lr), {'model__C': [.3, 3.]}),
        'char_lr': (pipe(clone(lr), char()), {'model__C': [.3, 3.]}),
        'word_char_lr': (pipe(clone(lr), FeatureUnion([('word', word()), ('char', char())])), {'model__C': [.3, 3.]}),
        'word_svm': (pipe(LinearSVC(class_weight='balanced', random_state=SEED)), {'model__C': [.3, 3.]}),
        'char_svm': (pipe(LinearSVC(class_weight='balanced', random_state=SEED), char()), {'model__C': [.3, 3.]}),
        'complement_nb': (pipe(ComplementNB()), {'model__alpha': [.1, 1.]}),
        'multinomial_nb': (pipe(MultinomialNB()), {'model__alpha': [.1, 1.]}),
        'ridge': (pipe(RidgeClassifier(class_weight='balanced')), {'model__alpha': [1., 10.]}),
        'sgd_logistic': (pipe(SGDClassifier(loss='log_loss', class_weight='balanced', max_iter=1500, random_state=SEED)), {'model__alpha': [1e-5, 1e-4]}),
        'nearest_centroid': (pipe(NearestCentroid()), {}),
        'knn_cosine': (pipe(KNeighborsClassifier(metric='cosine', algorithm='brute', weights='distance')), {'model__n_neighbors': [5, 15]}),
        'random_forest_lsa': (pipe(RandomForestClassifier(n_estimators=180, class_weight='balanced', n_jobs=2, random_state=SEED), dense=True), {'model__min_samples_leaf': [1, 4]}),
        'gradient_boosting_lsa': (pipe(HistGradientBoostingClassifier(max_iter=120, random_state=SEED), dense=True), {'model__max_leaf_nodes': [7, 15]}),
        'mlp_lsa': (pipe(MLPClassifier(hidden_layer_sizes=(64,), max_iter=250, random_state=SEED), dense=True), {'model__alpha': [.01, 1.]}),
        'catboost_text': (TextCatBoost(), {'depth': [4, 6]}),
        'soft_voting': (VotingClassifier(estimators=[('word', pipe(clone(lr))), ('char', pipe(clone(lr), char())),
                          ('nb', pipe(ComplementNB(alpha=.1)))], voting='soft'), {'weights': [(1, 1, 1), (2, 2, 1)]})}


def bootstrap(y, pred, groups, other=None, n=1500):
    rng = np.random.default_rng(SEED)
    unique = np.unique(groups)
    indices = {g: np.flatnonzero(groups == g) for g in unique}
    result = []
    for _ in range(n):
        idx = np.concatenate([indices[g] for g in rng.choice(unique, len(unique), replace=True)])
        value = scores(y[idx], pred[idx])['macro_f1']
        if other is not None:
            value -= scores(y[idx], other[idx])['macro_f1']
        result.append(value)
    return np.quantile(result, [.025, .975]).tolist()


def incremental(df, train, valid, out):
    # All updates use train only; validation is diagnostic, never used as new labels.
    splitter = StratifiedGroupKFold(3, shuffle=True, random_state=SEED+5)
    a, b = next(splitter.split(df.iloc[train], df.iloc[train].label, df.iloc[train].group))
    old, new = df.iloc[train[a]], df.iloc[train[b]]
    val = df.iloc[valid]
    vectorizer = word()
    xo = vectorizer.fit_transform(old.text_clean)
    xn, xv = vectorizer.transform(new.text_clean), vectorizer.transform(val.text_clean)
    from scipy.sparse import vstack
    import copy
    rows = []
    def record(name, model, x, elapsed):
        rows.append({'method': name, 'seconds': elapsed, **scores(val.label, model.predict(x))})
    for name, model in [('sgd', SGDClassifier(loss='log_loss', random_state=SEED, alpha=1e-4, max_iter=1000)),
                        ('naive_bayes', MultinomialNB(alpha=.1))]:
        start = time.perf_counter(); model.fit(xo, old.label)
        record(name+'_frozen', model, xv, time.perf_counter()-start)
        for replay in (False, True):
            updated = copy.deepcopy(model)
            start = time.perf_counter()
            xr = vstack([xn, xo]) if replay else xn
            yr = np.r_[new.label, old.label] if replay else new.label
            # NB counts cannot replay already counted observations without reweighting.
            if name == 'naive_bayes' and replay:
                continue
            for _ in range(5 if name == 'sgd' else 1):
                updated.partial_fit(xr, yr)
            record(name+('_update_replay' if replay else '_update_new'), updated, xv, time.perf_counter()-start)
        start = time.perf_counter()
        rebuilt = clone(model).fit(vstack([xo, xn]), np.r_[old.label, new.label])
        record(name+'_refit_fixed_vocabulary', rebuilt, xv, time.perf_counter()-start)
    lr = LogisticRegression(max_iter=1000, warm_start=True, random_state=SEED).fit(xo, old.label)
    record('lr_frozen', lr, xv, 0.)
    for scope, x, y in [('new_only', xn, new.label), ('all_seen', vstack([xo,xn]), np.r_[old.label,new.label])]:
        start = time.perf_counter(); updated = copy.deepcopy(lr).fit(x,y)
        record('lr_warm_start_'+scope, updated, xv, time.perf_counter()-start)
    start=time.perf_counter()
    fresh=pipe(LogisticRegression(max_iter=1000,random_state=SEED)).fit(df.iloc[train].text_clean,df.iloc[train].label)
    record('lr_refit_new_vocabulary',fresh,val.text_clean,time.perf_counter()-start)
    # A separate numeric-feature CatBoost experiment: init_model is not assumed
    # to be compatible with the historical text-feature model.
    svd=TruncatedSVD(64,random_state=SEED)
    zo=svd.fit_transform(xo); zn=svd.transform(xn); zv=svd.transform(xv)
    params=dict(iterations=100,depth=4,learning_rate=.1,verbose=False,thread_count=2,
                random_seed=SEED,allow_writing_files=False)
    start=time.perf_counter(); base=CatBoostClassifier(**params).fit(zo,old.label)
    record('catboost_lsa_frozen_100',base,zv,time.perf_counter()-start)
    for replay in (False,True):
        start=time.perf_counter()
        model=CatBoostClassifier(**{**params,'iterations':50}).fit(
            np.vstack([zo,zn]) if replay else zn, np.r_[old.label,new.label] if replay else new.label,init_model=base)
        record('catboost_lsa_plus50_'+('replay' if replay else 'new'),model,zv,time.perf_counter()-start)
    start=time.perf_counter()
    model=CatBoostClassifier(**{**params,'iterations':150}).fit(np.vstack([zo,zn]),np.r_[old.label,new.label])
    record('catboost_lsa_refit150',model,zv,time.perf_counter()-start)
    pd.DataFrame(rows).to_csv(out/'incremental.csv',index=False)
    dump(out/'incremental_protocol.json',{'old':len(old),'new':len(new),'validation':len(val),
        'split':'random similarity groups; simulation, not observed temporal drift',
        'sgd_update_epochs':5,'catboost_additional_trees':50,'representation':'fit on old only except explicit new-vocabulary baseline'})


def run(out):
    out.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    df, audit, rejected = load_data()
    rejected.to_csv(out/'label_conflicts.csv', index=False)
    train, valid, test = split_data(df)
    df['split'] = ''
    for name, idx in [('train',train),('validation',valid),('test',test)]:
        df.loc[idx,'split'] = name
    df.drop(columns=['normalized','text','text_clean']).to_csv(out/'split.csv',index=False)
    dump(out/'data_audit.json',audit)
    print('Data',audit,flush=True)
    cv = list(StratifiedGroupKFold(3,shuffle=True,random_state=SEED+2).split(df.iloc[train],df.iloc[train].label,df.iloc[train].group))
    for a,b in cv:
        assert not set(df.iloc[train[a]].group)&set(df.iloc[train[b]].group)
    rows, fitted, failures, warning_rows = [], {}, [], []
    for name,(estimator, grid) in candidates().items():
        print('Fitting',name,flush=True); start=time.perf_counter()
        search=GridSearchCV(estimator,grid,cv=cv,scoring='f1_macro',n_jobs=1,error_score='raise',return_train_score=True)
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                search.fit(df.iloc[train].text_clean,df.iloc[train].label)
            warning_rows.extend({'model':name,'message':str(w.message),'category':w.category.__name__} for w in caught)
            fitted[name]=search.best_estimator_
            pred=search.predict(df.iloc[valid].text_clean)
            best=search.best_index_
            rows.append({'model':name,'cv_f1':search.best_score_,'cv_std':search.cv_results_['std_test_score'][best],
                'train_f1':search.cv_results_['mean_train_score'][best], 'fit_seconds':time.perf_counter()-start,
                'parameters':json.dumps(search.best_params_),**scores(df.iloc[valid].label,pred)})
            pd.DataFrame(search.cv_results_).to_csv(out/(name+'_cv.csv'),index=False)
            pd.DataFrame(rows).to_csv(out/'leaderboard.csv',index=False)
        except Exception as exc:
            failures.append({'model':name,'error':repr(exc)})
            print('FAILED',name,repr(exc),flush=True)
    dump(out/'failures.json',failures);dump(out/'warnings.json',warning_rows)
    if not rows: raise RuntimeError('All models failed')
    board=pd.DataFrame(rows).sort_values(['macro_f1','cv_f1'],ascending=False)
    winner=board.iloc[0]['model']
    # Lock the choice before examining test labels or predictions.
    dump(out/'selection.json',{'winner':winner,'criterion':'validation macro-F1, then train CV macro-F1',
        'test_opened':False,'seed':SEED,'protocol':'train60 grouped 3-fold tuning; validation20 family selection; test20 final evaluation',
        'scope':'retrospective, previously used purposefully sampled labels; not a new independent corpus test'})
    print('Selected',winner,flush=True)
    incremental(df,train,valid,out)
    learning=[]
    for folds in (5,3,2):
        _,sub=next(StratifiedGroupKFold(folds,shuffle=True,random_state=SEED).split(df.iloc[train],df.iloc[train].label,df.iloc[train].group))
        idx=train[sub]; model=clone(fitted[winner]).fit(df.iloc[idx].text_clean,df.iloc[idx].label)
        learning.append({'n_train':len(idx),'train_f1':scores(df.iloc[idx].label,model.predict(df.iloc[idx].text_clean))['macro_f1'],
                         'validation_f1':scores(df.iloc[valid].label,model.predict(df.iloc[valid].text_clean))['macro_f1']})
    learning.append({'n_train':len(train),'train_f1':scores(df.iloc[train].label,fitted[winner].predict(df.iloc[train].text_clean))['macro_f1'],
                     'validation_f1':float(board.iloc[0].macro_f1)})
    pd.DataFrame(learning).to_csv(out/'learning_curve.csv',index=False)
    # Chronological stress check only on development data. Remove crossing groups.
    dev=np.r_[train,valid]; dated=df.iloc[dev].dropna(subset=['first_date']).sort_values('first_date')
    cut=int(.8*len(dated)); temporal_train,temporal_val=dated.iloc[:cut],dated.iloc[cut:]
    temporal_train=temporal_train[~temporal_train.group.isin(temporal_val.group)]
    if len(temporal_train) and set(temporal_train.label)==set(LABELS) and set(temporal_val.label)==set(LABELS):
        model=clone(fitted[winner]).fit(temporal_train.text_clean,temporal_train.label)
        dump(out/'temporal.json',{'train':len(temporal_train),'validation':len(temporal_val),
            'cutoff':str(temporal_val.first_date.min()),**scores(temporal_val.label,model.predict(temporal_val.text_clean)),
            'scope':'development sensitivity; parameters already chosen using development, not independent temporal test'})
    print('Refitting winner and historical comparator',flush=True)
    final=clone(fitted[winner]).fit(df.iloc[dev].text_clean,df.iloc[dev].label)
    baseline=HistoricalEnsemble().fit(df.iloc[dev].text_clean,df.iloc[dev].label)
    truth=df.iloc[test].label.to_numpy(); groups=df.iloc[test].group.to_numpy()
    pred=final.predict(df.iloc[test].text_clean); old=baseline.predict(df.iloc[test].text_clean)
    result={'winner':winner,'winner_metrics':scores(truth,pred),'historical_metrics':scores(truth,old),
            'winner_ci95':bootstrap(truth,pred,groups),'difference_ci95':bootstrap(truth,pred,groups,old),
            'report':classification_report(truth,pred,labels=LABELS,output_dict=True,zero_division=0),
            'confusion':confusion_matrix(truth,pred,labels=LABELS).tolist()}
    heads=baseline.heads(df.iloc[test].text_clean)
    accepted=((heads['vacancy']>.8)&(heads['resume']<.3))|((heads['resume']>.8)&(heads['vacancy']<.3))
    result['historical_production_policy']={'coverage':float(accepted.mean()),'accepted':int(accepted.sum()),
        'accuracy_accepted':float(accuracy_score(truth[accepted],old[accepted])) if accepted.any() else None,
        'note':'abstention is not the trash class'}
    test_rows=df.iloc[test][['text_hash','label','group','origin','source','text']].copy()
    test_rows['prediction']=pred;test_rows['historical_prediction']=old
    test_rows.to_csv(out/'test_predictions.csv',index=False)
    test_rows[test_rows.label!=test_rows.prediction].to_csv(out/'test_errors.csv',index=False)
    # Predeclared descriptive subgroups, never used to change winner.
    sub=[]
    for dimension in ('origin','source'):
        for value,frame in test_rows.groupby(dimension):
            sub.append({'dimension':dimension,'value':value,'n':len(frame),**scores(frame.label,frame.prediction)})
    pd.DataFrame(sub).to_csv(out/'test_subgroups.csv',index=False)
    dump(out/'test_results.json',result)
    joblib.dump(final,out/'selected_model.joblib')
    dump(out/'manifest.json',{'seed':SEED,'elapsed_seconds':time.perf_counter()-started,
        'packages':{p:importlib.metadata.version(p) for p in ['numpy','pandas','scipy','scikit-learn','catboost','matplotlib','joblib']},
        'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (PACKAGE_ROOT/'research').glob('*.py')},
        'complete':True})
    print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':
    from market_system.runtime import enable_guard
    enable_guard()
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='research/results/run_20260930')

    args=parser.parse_args();run(ROOT/args.output)
