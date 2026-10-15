from market_system.paths import local_path, DATABASE_PATH
"""Explicit, versioned candidate updates with replay and holdout protection.

Text CatBoost is updated by additive residual trees trained against old logits.
Each residual model keeps its own text dictionary. Inference sums RAW logits,
then applies sigmoid once; probabilities are never added.
"""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import random
import uuid

import joblib
import numpy as np
import pandas as pd
from catboost import CatBoostClassifier, Pool
from sklearn.linear_model import SGDClassifier

from market_system.core.extraction.skills import DEFAULT_LEXICON
from market_system.core.extraction.occupations import DEFAULT_LEXICON as OCCUPATION_LEXICON
from .evaluation import compare
from .model import Ensemble, HEADS, clean, text_key, sha256

LABELS = ('vacancy','resume','trash')


def records(frame):
    if not isinstance(frame, pd.DataFrame) or not frame.columns.is_unique or not {'text','label'} <= set(frame.columns):
        raise ValueError('Expected DataFrame with unique columns text and label')
    result = []
    seen = set()
    for text,label in frame[['text','label']].itertuples(index=False, name=None):
        if not isinstance(text,str) or not clean(text).strip() or not isinstance(label,str) or label not in LABELS:
            raise ValueError('Expected nonempty text and human label vacancy/resume/trash')
        key = text_key(text)
        if key in seen:
            raise ValueError('Duplicate normalized text within the batch')
        seen.add(key)
        result.append({'text':text,'label':label,'key':key})
    if not result:
        raise ValueError('Empty labeled batch')
    return result


def balanced_buffer(items, size, seed):
    """Bounded stratified replay sample. Does not modify original training files."""
    rng = random.Random(seed)
    pools = {label:[r for r in items if r['label']==label] for label in LABELS}
    for pool in pools.values():
        rng.shuffle(pool)
    result = []
    while len(result) < size and any(pools.values()):
        for pool in pools.values():
            if pool and len(result)<size:
                result.append(pool.pop())
    return result


class ModelUpdater:
    def __init__(self, model, state):
        self.model, self.state = model, state

    @classmethod
    def load(cls, model_dir, *, replay_size=384, seed=42):
        if isinstance(replay_size,bool) or not isinstance(replay_size,int) or replay_size<3:
            raise ValueError('replay_size must be an integer >= 3')
        model = Ensemble.load(model_dir)
        folder = model.path
        if 'training_state.joblib' in model.metadata['artifacts_sha256']:
            return cls(model, joblib.load(folder/'training_state.joblib'))
        # Original notebook bundle has an explicit train/test split and labels.
        if not {'labels.csv','split.csv'} <= set(model.metadata['artifacts_sha256']):
            raise ValueError('Need verified training_state or original labels.csv and split.csv')
        labels = pd.read_csv(folder/'labels.csv',keep_default_na=False)
        split = pd.read_csv(folder/'split.csv',keep_default_na=False)
        if not labels['text_hash'].is_unique or not split['text_hash'].is_unique:
            raise ValueError('Duplicate source hashes')
        joined = labels.merge(split,on='text_hash',validate='one_to_one',suffixes=('','_split'))
        if len(joined)!=len(labels) or len(joined)!=len(split) or not (joined['label']==joined['label_split']).all():
            raise ValueError('Labels do not match the saved split')
        if set(joined['split']) != {'train','test'}:
            raise ValueError('Expected train/test split')
        # Normalized duplicates already present in the historical snapshot are
        # collapsed; cross-split duplicates are excluded from replay, never test.
        test_frame = joined[joined['split']=='test'][['text','label']].copy()
        test_frame['_key'] = test_frame['text'].map(text_key)
        if (test_frame.groupby('_key')['label'].nunique()>1).any():
            raise ValueError('Conflicting labels in the original holdout')
        test_frame = test_frame.drop_duplicates('_key')
        holdout = records(test_frame)
        protected = {r['key'] for r in holdout}
        train_frame = joined[joined['split']=='train'][['text','label']].copy()
        train_frame['_key'] = train_frame['text'].map(text_key)
        if (train_frame.groupby('_key')['label'].nunique()>1).any():
            raise ValueError('Conflicting labels in original training data')
        train_frame = train_frame.drop_duplicates('_key')
        overlap = int(train_frame['_key'].isin(protected).sum())
        train_frame = train_frame[~train_frame['_key'].isin(protected)]
        train = records(train_frame)
        if set(r['label'] for r in train)!=set(LABELS):
            raise ValueError('Original training set must contain all three labels')
        state = {'schema':1,'known':{r['key']:r['label'] for r in train},
                 'holdout':holdout,'replay':balanced_buffer(train,replay_size,seed),
                 'replay_size':replay_size,'seed':seed,'history':[],
                 'historical_cross_split_duplicates':overlap,
                 'evaluation_scope':'historical holdout; repeated use is development evaluation, not a new independent test'}
        return cls(model,state)

    def update(self, new_data, output_root, *, epochs=1, eta0=0.001, alpha=0.0001,
               cb_iterations=20, cb_learning_rate=0.03, update_catboost=True, thread_count=2):
        """Save a self-contained candidate; keep this updater and its parent intact.

        Re-load the returned folder for the next update. No automatic deployment.
        New labels must be human-reviewed. Correcting an already learned label is
        rejected: incremental updates cannot undo its historical influence.
        """
        for name,value in [('epochs',epochs),('cb_iterations',cb_iterations),('thread_count',thread_count)]:
            if isinstance(value,bool) or not isinstance(value,int) or value<1:
                raise ValueError(name+' must be a positive integer')
        if not np.isfinite(eta0) or eta0<=0 or not np.isfinite(alpha) or alpha<0 or not 0<cb_learning_rate<=1:
            raise ValueError('Invalid learning parameters')
        new = records(new_data)
        protected = {r['key'] for r in self.state['holdout']}
        for row in new:
            if row['key'] in protected:
                raise ValueError('Holdout leakage: update contains a protected evaluation text')
            if row['key'] in self.state['known']:
                if row['label'] != self.state['known'][row['key']]:
                    raise ValueError('Changed historical label: requires a planned rebuild, not incremental unlearning')
                raise ValueError('Text already used for training; submit only new reviewed examples')
        batch = new + deepcopy(self.state['replay'])
        random.Random(self.state['seed']+len(self.state['history'])).shuffle(batch)
        texts = [clean(r['text']) for r in batch]
        x = self.model.linear['vectorizer'].transform(texts)
        known_rows = np.asarray(x.getnnz(axis=1))>0
        if not known_rows.all():
            raise ValueError('Some texts have zero known TF-IDF features; rebuild vocabulary or review these examples')
        # Text CatBoost models must not be deep-copied/re-serialized here:
        # on the tested Windows build, re-saving a deserialized text model can
        # produce a native crash on its next load. Treat existing parts as
        # immutable and preserve their original .cbm bytes in the new bundle.
        candidate = Ensemble(deepcopy(self.model.linear),
                             {name:list(models) for name,models in self.model.boosts.items()},
                             self.model.metadata)
        candidate.skill_lexicon = self.model.skill_lexicon
        candidate.occupation_lexicon = self.model.occupation_lexicon
        feature_frame = pd.DataFrame({'clean_text':texts})
        for name,key in HEADS.items():
            y = np.array([int(r['label']==name) for r in batch])
            previous = candidate.linear[key]
            if isinstance(previous,SGDClassifier):
                previous.set_params(eta0=eta0,alpha=alpha)
                for _ in range(epochs):
                    previous.partial_fit(x,y)
            else:
                model = SGDClassifier(loss='log_loss',learning_rate='constant',eta0=eta0,alpha=alpha,
                                      random_state=self.state['seed'],max_iter=1,tol=None)
                model.fit(x,y,coef_init=previous.coef_.copy(),intercept_init=previous.intercept_.copy())
                for _ in range(epochs-1):
                    model.partial_fit(x,y)
                candidate.linear[key] = model
            if update_catboost:
                # baseline incorporates all previously learned trees, including
                # earlier residuals. The new model stores ONLY its new trees.
                baseline = self.model.raw_boost(name,feature_frame)
                pool = Pool(feature_frame,label=y,text_features=['clean_text'],baseline=baseline)
                residual = CatBoostClassifier(iterations=cb_iterations,learning_rate=cb_learning_rate,
                    depth=4,loss_function='Logloss',random_seed=self.state['seed'],verbose=False,
                    allow_writing_files=False,thread_count=thread_count,task_type='CPU',
                    text_processing={'tokenizers':[{'tokenizer_id':'Space','separator_type':'ByDelimiter','delimiter':' '}],
                                     'dictionaries':[{'dictionary_id':'Words','token_level_type':'Word','occurrence_lower_bound':'1'}],
                                     'feature_processing':{'default':[{'dictionaries_names':['Words'],'feature_calcers':['BoW'],'tokenizers_names':['Space']}]}})
                residual.fit(pool)
                candidate.boosts[name].append(residual)
        validation = pd.DataFrame(self.state['holdout'])[['text','label']]
        report = compare(self.model,candidate,validation)
        report['evaluation_scope'] = self.state['evaluation_scope']
        state = deepcopy(self.state)
        state['known'].update({r['key']:r['label'] for r in new})
        state['replay'] = balanced_buffer(state['replay']+new,state['replay_size'],state['seed']+len(state['history'])+1)
        entry = {'parent_version':self.model.version,'at':datetime.now(timezone.utc).isoformat(),
                 'new_documents':len(new),'replay_documents':len(batch)-len(new),
                 'new_text_keys':[r['key'] for r in new], 'epochs':epochs,'eta0':eta0,'alpha':alpha,
                 'cb_iterations':cb_iterations if update_catboost else 0,
                 'cb_learning_rate':cb_learning_rate,
                 'catboost_method':'additive_text_residual_with_baseline' if update_catboost else 'frozen',
                 'linear_method':'SGD_log_loss_frozen_tfidf',
                 'class_counts':{label:sum(r['label']==label for r in new) for label in LABELS}}
        state['history'].append(entry)
        return self._save(candidate,state,report,output_root)

    def _save(self,candidate,state,report,output_root):
        version = 'ensemble_update_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8]
        root = local_path(output_root).resolve()
        root.mkdir(parents=True,exist_ok=True)
        staging = root/('.'+version+'.partial')
        staging.mkdir(exist_ok=False)
        # metadata is written last; interrupted directories are not valid bundles.
        joblib.dump(candidate.linear,staging/'linear.joblib')
        joblib.dump(state,staging/'training_state.joblib')
        files = {}
        parent_files = self.model.metadata.get('catboost_files', {name:[name+'.cbm'] for name in HEADS})
        for name,models in candidate.boosts.items():
            files[name] = []
            for i,model in enumerate(models):
                filename = f'{name}_{i:03d}.cbm'
                if i < len(parent_files[name]):
                    source_name=parent_files[name][i]
                    (staging/filename).write_bytes((self.model.path/source_name).read_bytes())
                    if sha256(staging/filename)!=self.model.metadata['artifacts_sha256'][source_name]:
                        raise ValueError('Parent model file changed after loading')
                else:
                    model.save_model(str(staging/filename))
                files[name].append(filename)
        lexicon = candidate.skill_lexicon or DEFAULT_LEXICON
        (staging/'skill_lexicon.json').write_bytes(Path(lexicon).read_bytes())
        (staging/'occupation_lexicon.json').write_bytes(Path(candidate.occupation_lexicon or OCCUPATION_LEXICON).read_bytes())
        (staging/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
        metadata = deepcopy(candidate.metadata)
        # Avoid carrying old metrics / training hashes as if they describe this candidate.
        for key in ('artifacts_sha256','training_files_sha256','train_size','test_size'):
            metadata.pop(key,None)
        metadata.update(model_version=version,parent_version=self.model.version,bundle_schema=1,
                        library_version='0.2.0',
                        status='candidate_requires_review',catboost_files=files,
                        preprocessing={'operation':'re.sub','pattern':r'http\S+|www\S+|https\S+','replacement':'','flags':'MULTILINE'},
                        update=state['history'][-1],evaluation_scope=state['evaluation_scope'],
                        packages={name:importlib.metadata.version(name) for name in ('numpy','pandas','scikit-learn','catboost','joblib','pymorphy3')})
        metadata['library_source_sha256'] = {p.name:sha256(p) for p in Path(__file__).parent.glob('*.py')}
        metadata['artifacts_sha256'] = {p.name:sha256(p) for p in staging.iterdir() if p.is_file()}
        (staging/'metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
        destination = root/version
        staging.rename(destination)
        return destination
