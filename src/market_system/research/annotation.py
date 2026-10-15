from market_system.sqlite import connect as sqlite_connect
from market_system.paths import local_path, DATABASE_PATH
from market_system.paths import PACKAGE_ROOT
"""Frozen review sampling, validation, revision storage and human evaluation."""
import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import HashingVectorizer
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.neighbors import radius_neighbors_graph
from scipy.sparse.csgraph import connected_components

from market_system.research.benchmark import ROOT, SEED, dump, key
from market_system.research.extraction import exact, prf, weak_context
from market_system.core.extraction.skills import SkillExtractor
from market_system.core.extraction.occupations import OccupationExtractor
from market_system.core.extraction.salary import extract_salary
from market_system.core.extraction.work_conditions import extract_work_conditions, FIELDS

REVIEW=ROOT/'research/review'
EXTRACTION=ROOT/'research/results/extraction_20260930'
SALARY_FIELDS=('salary_from','salary_to','currency','salary_period','tax_mode')
ROLES=('required','preferred','possessed','task','learning','negated','not_required','mentioned')


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def prepare():
    path=REVIEW/'dataset.json'
    if path.exists():return path
    REVIEW.mkdir(parents=True,exist_ok=True)
    protocol=json.loads((EXTRACTION/'extraction_protocol.json').read_text(encoding='utf-8'))
    pilot=json.loads((EXTRACTION/'pilot_reference.json').read_text(encoding='utf-8'))
    excluded=set(protocol['training_hashes'])|{r['text_hash'] for r in pilot}
    with closing(sqlite_connect((DATABASE_PATH).as_uri()+'?mode=ro',uri=True)) as con:
        version=con.execute('SELECT model_version FROM classification_runs ORDER BY labeled_at DESC LIMIT 1').fetchone()[0]
        candidates=pd.read_sql_query('''SELECT c.text_hash,c.text,l.final_label label FROM contents c
            JOIN labeled_contents l USING(text_hash) WHERE l.model_version=? AND l.final_label IS NOT NULL''',con,params=[version])
    candidates=candidates[~candidates.text_hash.isin(excluded)].copy()
    candidates['key']=candidates.text.map(key)
    candidates=candidates.drop_duplicates('key')
    # Reject close matches to any weak-training or pilot document before sampling.
    training=pd.concat([pd.read_csv(p) for p in sorted((ROOT/'data/learning_data').glob('*.csv'))])
    excluded_texts=training[training.text_hash.isin(excluded)].drop_duplicates('text_hash').text.tolist()
    h=HashingVectorizer(analyzer='char_wb',ngram_range=(4,5),n_features=2**18,alternate_sign=False)
    reference=h.transform(excluded_texts)
    shuffled=candidates.groupby('label',group_keys=False).sample(n=250,random_state=SEED)
    similarity=(h.transform(shuffled.text)@reference.T).toarray().max(axis=1)
    eligible=shuffled[similarity<.85]
    sampled=eligible.groupby('label',group_keys=False).head(160).reset_index(drop=True)
    if sampled.groupby('label').size().min()<160:raise ValueError('Not enough independent review documents')
    graph=radius_neighbors_graph(h.transform(sampled.text),radius=.15,metric='cosine',include_self=True,n_jobs=1)
    _,sampled['group']=connected_components(graph,directed=False)
    # 240 random documents: fixed test/development groups; 80 additional challenge documents.
    audit=sampled.groupby('label',group_keys=False).head(120).copy()
    a,b=next(StratifiedGroupKFold(2,shuffle=True,random_state=SEED).split(audit,audit.label,audit.group))
    audit['cohort']='development';audit.iloc[b,audit.columns.get_loc('cohort')]='test'
    challenge=sampled[~sampled.text_hash.isin(audit.text_hash)]
    challenge=challenge[~challenge.group.isin(audit.group)].copy();challenge['cohort']='challenge'
    sampled=pd.concat([audit,challenge],ignore_index=True)
    sk=SkillExtractor();oc=OccupationExtractor()
    students=joblib.load(EXTRACTION/'weak_students.joblib')
    sl=json.loads((PACKAGE_ROOT/'core/extraction/skill_lexicon.json').read_text(encoding='utf-8'))
    ol=json.loads((PACKAGE_ROOT/'core/extraction/occupation_lexicon.json').read_text(encoding='utf-8'))
    items=[]
    for row in sampled.itertuples():
        skills=sk.extract(row.text,row.label);occ=oc.extract(row.text,row.label)
        salary=extract_salary(row.text);work=extract_work_conditions(row.text,row.label)
        target=sorted({m['skill_id'] for m in skills['mentions'] if m['role'] in ROLES[:3]})
        student={}
        for name,model in students.items():
            mentions=skills['mentions'];mask=model.predict([weak_context(row.text,row.label,m) for m in mentions]) if mentions else []
            student[name]=sorted({m['skill_id'] for m,t in zip(mentions,mask) if t})
        draft={'post_type':row.label,'skills':[{'id':identity,'role':role} for identity,role in sorted({(m['skill_id'],m['role']) for m in skills['mentions']})],
            'occupations':occ['occupation_ids'],'salary':[{k:m[k] for k in SALARY_FIELDS} for m in salary['mentions']],
            'conditions':{f:work[f]['values'] for f in FIELDS},'comment':''}
        alternatives={'skills':{'context_rules':target,'exact_dictionary':sorted(exact(row.text,sl,'skills')),
                         'morphology_all_mentions':sorted({m['skill_id'] for m in skills['mentions']}),**student},
                      'occupations':{'context_rules':occ['occupation_ids'],'exact_dictionary':sorted(exact(row.text,ol,'occupations')),
                         'morphology_all_mentions':sorted({m['occupation_id'] for m in occ['mentions']})}}
        items.append({'id':row.text_hash,'text':row.text,'predicted_type':row.label,'cohort':row.cohort,'group':int(row.group),
            'draft':draft,'alternatives':alternatives,'mentions':skills['mentions'],'occupation_mentions':occ['mentions'],
            'salary_status':salary['status'],'occupation_status':occ['status'],
            'disagreement':len(set(target)^set(student['weak_lr']))})
    dataset={'schema':1,'created_at':datetime.now(timezone.utc).isoformat(),'classifier_version':version,
        'scope':'classified vacancy/resume texts, excluding extraction training/pilot near duplicates; not whole corpus',
        'seed':SEED,'items':items,'skills':sl['skills'],'occupations':ol['occupations'],
        'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (PACKAGE_ROOT/'core/extraction').glob('*.py')},
        'weak_model_sha256':hashlib.sha256((EXTRACTION/'weak_students.joblib').read_bytes()).hexdigest()}
    dataset['dataset_id']=digest(dataset)
    dump(path,dataset)
    Store(REVIEW/'annotations.sqlite',dataset)
    return path


def validate(value):
    if not isinstance(value,dict):raise ValueError('Ожидается объект разметки')
    if value.get('post_type') not in ('vacancy','resume','trash','uncertain'):raise ValueError('Некорректный тип поста')
    for field in ('skills','occupations','salary'):
        if not isinstance(value.get(field),list):raise ValueError('Поле '+field+' должно быть списком')
    for skill in value['skills']:
        if not isinstance(skill,dict) or not isinstance(skill.get('id'),str) or not skill['id'].strip() or skill.get('role') not in ROLES:
            raise ValueError('Нужны название/ID навыка и допустимая роль')
    if any(not isinstance(x,str) or not x.strip() for x in value['occupations']):raise ValueError('Некорректная специальность')
    for salary in value['salary']:
        if not isinstance(salary,dict) or set(salary)!=set(SALARY_FIELDS):raise ValueError('Неполная структура зарплаты')
        for field in SALARY_FIELDS[:2]:
            x=salary[field]
            if x is not None and (isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x<0):raise ValueError('Сумма должна быть неотрицательным числом')
        if salary['salary_from'] is None and salary['salary_to'] is None:raise ValueError('Укажите хотя бы одну границу суммы')
        if salary['salary_from'] is not None and salary['salary_to'] is not None and salary['salary_from']>salary['salary_to']:raise ValueError('Нижняя граница выше верхней')
        if salary['tax_mode'] not in (None,'net','gross'):raise ValueError('tax_mode: net, gross или пусто')
        for field in ('currency','salary_period'):
            if salary[field] is not None and not isinstance(salary[field],str):raise ValueError('Валюта и период должны быть строками')
    conditions=value.get('conditions')
    if not isinstance(conditions,dict) or set(conditions)!=set(FIELDS):raise ValueError('Нужны все пять полей условий работы')
    for field,values in conditions.items():
        if not isinstance(values,list):raise ValueError('Условия должны быть списками')
        for x in values:
            if field=='hours_per_week':
                if isinstance(x,bool) or not isinstance(x,(int,float)) or not 0<x<=168:raise ValueError('Часы в неделю: 1–168')
            elif not isinstance(x,str) or not x.strip():raise ValueError('Пустое/некорректное условие')
    if not isinstance(value.get('comment',''),str):raise ValueError('Комментарий должен быть строкой')
    return value


class Store:
    def __init__(self,path,dataset):
        self.path=local_path(path);self.dataset=dataset;self.items={i['id']:i for i in dataset['items']}
        if self.path==local_path('research/review/annotations.sqlite'):
            return  # Frozen imported archive; the unified OWNER workflow owns new edits.
        with self.connect() as con:
            con.execute('CREATE TABLE IF NOT EXISTS revisions (id TEXT, revision INTEGER, dataset TEXT, status TEXT, annotation TEXT, saved_at TEXT, PRIMARY KEY(id,revision))')

    @contextmanager
    def connect(self):
        frozen=self.path==local_path('research/review/annotations.sqlite')
        con=sqlite_connect(self.path.as_uri()+'?mode=ro' if frozen else self.path,uri=frozen,timeout=10)
        try:
            with con:
                yield con
        finally:
            con.close()

    def latest(self):
        with self.connect() as con:
            rows=con.execute('''SELECT r.id,r.revision,r.status,r.annotation,r.saved_at FROM revisions r
                JOIN (SELECT id,MAX(revision) rev FROM revisions WHERE dataset=? GROUP BY id) m
                ON r.id=m.id AND r.revision=m.rev WHERE r.dataset=?''',(self.dataset['dataset_id'],)*2).fetchall()
        return {r[0]:{'revision':r[1],'status':r[2],'annotation':json.loads(r[3]),'saved_at':r[4]} for r in rows}

    def save(self,identity,revision,status,annotation):
        if self.path==local_path('research/review/annotations.sqlite'):
            raise PermissionError('Imported annotations are frozen; use the unified OWNER review workflow')
        if identity not in self.items:raise ValueError('Неизвестный документ')
        if status not in ('confirmed','uncertain','draft'):raise ValueError('Некорректный статус')
        validate(annotation)
        if status=='confirmed' and annotation['post_type']=='uncertain':raise ValueError('Неуверенный тип: сохраните как «Сомневаюсь»')
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            current=con.execute('SELECT MAX(revision) FROM revisions WHERE id=?',(identity,)).fetchone()[0] or 0
            if revision!=current:raise ValueError('Запись уже изменена в другой вкладке. Обновите страницу.')
            now=datetime.now(timezone.utc).isoformat()
            con.execute('INSERT INTO revisions VALUES (?,?,?,?,?,?)',(identity,current+1,self.dataset['dataset_id'],status,json.dumps(annotation,ensure_ascii=False),now))
        return current+1


def sets(annotation):
    return {'skills':{x['id'] for x in annotation['skills'] if x['role'] in ROLES[:3]},
        'skill_roles':{(x['id'],x['role']) for x in annotation['skills']},'occupations':set(annotation['occupations']),
        'salary':{tuple(x[k] for k in SALARY_FIELDS) for x in annotation['salary']},
        **{f:set(annotation['conditions'][f]) for f in FIELDS}}


def evaluate():
    dataset=json.loads((REVIEW/'dataset.json').read_text(encoding='utf-8'))
    ids=[i['id'] for i in dataset['items']]
    if len(set(ids))!=len(ids):raise ValueError('Duplicate documents in review dataset')
    membership={}
    for item in dataset['items']:
        membership.setdefault(item['group'],set()).add(item['cohort'])
    if any(len(parts)>1 for parts in membership.values()):raise ValueError('Similarity group crosses review cohorts')
    store=Store(REVIEW/'annotations.sqlite',dataset);answers=store.latest()
    confirmed={k:v['annotation'] for k,v in answers.items() if v['status']=='confirmed'}
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');out=REVIEW/'evaluations'/stamp;out.mkdir(parents=True)
    dump(out/'annotations_snapshot.json',answers)
    # Actual human supervised models: development + challenge only. Never fit test.
    contexts=[];targets=[];occupation_contexts=[];occupation_targets=[]
    used=[]
    for item in dataset['items']:
        if item['cohort']=='test' or item['id'] not in confirmed:continue
        annotation=confirmed[item['id']]
        if annotation['post_type'] not in ('vacancy','resume'):continue
        gold=sets(annotation)['skills'];used.append(item['id'])
        for m in item['mentions']:
            contexts.append(weak_context(item['text'],annotation['post_type'],m));targets.append(int(m['skill_id'] in gold))
        for m in item.get('occupation_mentions',[]):
            occupation_contexts.append(weak_context(item['text'],annotation['post_type'],m))
            occupation_targets.append(int(m['occupation_id'] in sets(annotation)['occupations']))
    learned={}
    if len(used)>=20:
        from sklearn.pipeline import make_pipeline
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
        from sklearn.svm import LinearSVC
        for prefix,texts,labels in [('',contexts,targets),('occupation_',occupation_contexts,occupation_targets)]:
            if len(set(labels))!=2:continue
            for suffix,model in [('lr',LogisticRegression(class_weight='balanced',max_iter=1000,random_state=SEED)),
                                 ('svm',LinearSVC(class_weight='balanced',random_state=SEED))]:
                learned[prefix+'human_'+suffix]=make_pipeline(TfidfVectorizer(ngram_range=(1,2),min_df=1),model).fit(texts,labels)
    records={};record_groups={};details=[];type_rows=[]
    for item in dataset['items']:
        if item['id'] not in confirmed:continue
        human=confirmed[item['id']];gold=sets(human)
        type_rows.append({'id':item['id'],'cohort':item['cohort'],'prediction':item['predicted_type'],'reference':human['post_type']})
        if human['post_type']=='trash':continue
        preds={field:{'context_rules':value} for field,value in sets(item['draft']).items()}
        for task,methods in item['alternatives'].items():preds[task].update({k:set(v) for k,v in methods.items()})
        for name,model in learned.items():
            is_occupation=name.startswith('occupation_')
            mentions=item.get('occupation_mentions',[]) if is_occupation else item['mentions']
            mask=model.predict([weak_context(item['text'],item['predicted_type'],m) for m in mentions]) if mentions else []
            preds['occupations' if is_occupation else 'skills'][name]={m['occupation_id' if is_occupation else 'skill_id'] for m,t in zip(mentions,mask) if t}
        for field,methods in preds.items():
            for method,pred in methods.items():
                for stratum in ('all',human['post_type']):
                    record_key=(item['cohort'],field,method,stratum)
                    records.setdefault(record_key,[]).append((gold[field],pred))
                    record_groups.setdefault(record_key,[]).append(item['group'])
                if gold[field]!=pred:details.append({'id':item['id'],'cohort':item['cohort'],'task':field,'method':method,
                    'missing':list(gold[field]-pred),'extra':list(pred-gold[field])})
    table=[];rng=np.random.default_rng(SEED)
    for k,values in records.items():
        clusters={g:[i for i,v in enumerate(record_groups[k]) if v==g] for g in set(record_groups[k])}
        group_ids=list(clusters)
        intervals=[prf([values[i] for g in rng.choice(group_ids,len(group_ids),replace=True) for i in clusters[g]])['f1'] for _ in range(1000)]
        lo,hi=np.quantile(intervals,[.025,.975])
        table.append({'cohort':k[0],'task':k[1],'method':k[2],'post_type':k[3],**prf(values),'ci_low':lo,'ci_high':hi})
    pd.DataFrame(table,columns=['cohort','task','method','post_type','precision','recall','f1','tp','fp','fn','exact_match','documents','positive_documents','ci_low','ci_high']).to_csv(out/'metrics.csv',index=False)
    dump(out/'errors.json',details);dump(out/'classification_audit.json',type_rows)
    dump(out/'summary.json',{'dataset_id':dataset['dataset_id'],'confirmed':len(confirmed),'supervised_train_documents':len(used),
        'trained_models':list(learned),'test_confirmed':sum(i['cohort']=='test' and i['id'] in confirmed for i in dataset['items']),
        'training_document_ids':used,'training_mentions':len(contexts),'training_occupation_mentions':len(occupation_contexts),
        'excluded_true_trash':sum(r['reference']=='trash' for r in type_rows),
        'interval_method':'cluster bootstrap by frozen similarity group; overall is balanced audit sample, not corpus-weighted',
        'warning':'Repeated inspection of this fixed test makes later comparisons exploratory. Collect a fresh audit after changing rules or choosing models on these results.',
        'scope':dataset['scope'],'created_at':stamp,'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    if learned:joblib.dump(learned,out/'human_students.joblib')
    dump(REVIEW/'latest_evaluation.json',{'path':str(out.relative_to(ROOT))})
    return out
