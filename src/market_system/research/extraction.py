from market_system.paths import PACKAGE_ROOT
"""Real-post extraction pilot and weak supervision experiments; no gold claims."""
from pathlib import Path
import argparse
import html
import json
import re
import time
import joblib

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer, HashingVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.svm import LinearSVC
from sklearn.pipeline import make_pipeline

from market_system.core.extraction.skills import SkillExtractor
from market_system.core.extraction.occupations import OccupationExtractor
from market_system.core.extraction.salary import extract_salary
from market_system.core.extraction.work_conditions import extract_work_conditions
from market_system.research.benchmark import ROOT, SEED, dump, key
from market_system.research.pilot_annotations import ANNOTATIONS


def exact(text, lexicon, field):
    found = set()
    for entry in lexicon[field]:
        for alias in [entry['name'], *entry.get('aliases',[])]:
            if re.search(r'(?<!\w)'+re.escape(alias)+r'(?!\w)',text,re.I):
                found.add(entry['id']);break
    return found


def prf(records):
    tp=sum(len(g&p) for g,p in records); fp=sum(len(p-g) for g,p in records); fn=sum(len(g-p) for g,p in records)
    return {'precision':tp/(tp+fp) if tp+fp else 0., 'recall':tp/(tp+fn) if tp+fn else 0.,
            'f1':2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else 0.,'tp':tp,'fp':fp,'fn':fn,
            'exact_match':sum(g==p for g,p in records)/len(records), 'documents':len(records),
            'positive_documents':sum(bool(g) for g,p in records)}


def weak_context(text, post_type, mention):
    a,b=mention['start'],mention['end']
    # Include preceding section header; mask the entity to reduce memorization.
    return post_type+' '+text[max(0,a-180):a]+' ENTITY '+text[b:b+100]


def run(out):
    out.mkdir(parents=True,exist_ok=True)
    sample=json.loads((ROOT/'research/pilot_sample.json').read_text(encoding='utf-8'))
    assert len(sample)==len(ANNOTATIONS)==24
    annotation=[]
    for row,(skills,occupations,amounts,formats,gaps) in zip(sample,ANNOTATIONS):
        annotation.append({**row,'skills':skills.split(),'occupations':occupations.split(),
            'salary_amounts':amounts,'work_format':formats,'taxonomy_gaps':gaps,
            'annotation_provenance':'agent_read_original_text_before_predictions',
            'human_reviewed':False,'human_status':'','human_skills_json':'','human_occupations_json':'',
            'human_salary_amounts_json':'','human_work_format_json':''})
    dump(out/'pilot_reference.json',annotation)
    sk=SkillExtractor();oc=OccupationExtractor()
    sl=json.loads((PACKAGE_ROOT/'core/extraction/skill_lexicon.json').read_text(encoding='utf-8'))
    ol=json.loads((PACKAGE_ROOT/'core/extraction/occupation_lexicon.json').read_text(encoding='utf-8'))
    all_data=pd.concat([pd.read_csv(p) for p in sorted((ROOT/'data/learning_data').glob('*.csv'))],ignore_index=True)
    all_data=all_data[all_data.label.isin(['vacancy','resume'])].copy()
    all_data['key']=all_data.text.map(key)
    all_data=all_data.drop_duplicates('key')
    # Exclude all close matches to pilot posts from teacher-labeled training.
    h=HashingVectorizer(analyzer='char_wb',ngram_range=(4,5),alternate_sign=False,n_features=2**18)
    proximity=(h.transform(all_data.text) @ h.transform([r['text'] for r in sample]).T).toarray().max(axis=1)
    pool=all_data[proximity<.85].sample(frac=1,random_state=SEED)
    training=pool.iloc[:700]; queue=pool.iloc[700:820]
    contexts,targets=[],[]
    for row in training.itertuples():
        for mention in sk.extract(row.text,row.label)['mentions']:
            contexts.append(weak_context(row.text,row.label,mention))
            targets.append(int(mention['role'] in ('required','preferred','possessed')))
    students={}
    for name,model in [('weak_lr',LogisticRegression(class_weight='balanced',max_iter=1000,random_state=SEED)),
                       ('weak_svm',LinearSVC(class_weight='balanced',random_state=SEED))]:
        students[name]=make_pipeline(TfidfVectorizer(ngram_range=(1,2),min_df=2,max_features=20000),model).fit(contexts,targets)
    joblib.dump(students,out/'weak_students.joblib')
    predictions=[];timings=[]
    for index,row in enumerate(annotation):
        text,kind=row['text'],row['label'];start=time.perf_counter()
        skills=sk.extract(text,kind);occ=oc.extract(text,kind)
        salary=extract_salary(text);conditions=extract_work_conditions(text,kind)
        timings.append(time.perf_counter()-start)
        target=set().union(*(set(skills['skills_by_role'][r]) for r in ('required','preferred','possessed')))
        amounts={m[f] for m in salary['mentions'] for f in ('salary_from','salary_to') if m[f] is not None}
        naive_amounts={int(m.replace(' ','')) for m in re.findall(r'(\d[\d ]*)\s*(?:руб|₽)',text,re.I)}
        preds={
          'skills':{'exact_dictionary':exact(text,sl,'skills'),
                    'morphology_all_mentions':{m['skill_id'] for m in skills['mentions']},'context_rules':target},
          'occupations':{'exact_dictionary':exact(text,ol,'occupations'),
                         'morphology_all_mentions':{m['occupation_id'] for m in occ['mentions']},'context_rules':set(occ['occupation_ids'])},
          'salary_amounts':{'currency_regex':naive_amounts,'context_rules':amounts},
          'work_format':{'keyword':{value for value,pat in [('remote',r'удал[её]н|remote'),('onsite',r'офис'),('hybrid',r'гибрид')] if re.search(pat,text,re.I)},
                         'context_rules':set(conditions['work_format']['values'])}}
        for name,model in students.items():
            mentions=skills['mentions']
            accepted=model.predict([weak_context(text,kind,m) for m in mentions]) if mentions else []
            preds['skills'][name]={m['skill_id'] for m,t in zip(mentions,accepted) if t}
        for task,methods in preds.items():
            for method,pred in methods.items():
                predictions.append({'index':index,'text_hash':row['text_hash'],'post_type':kind,'task':task,
                    'method':method,'reference':sorted(set(row[task])),'prediction':sorted(pred)})
    dump(out/'pilot_predictions.json',predictions)
    rows=[];rng=np.random.default_rng(SEED)
    for task in ('skills','occupations','salary_amounts','work_format'):
        task_rows=[r for r in predictions if r['task']==task]
        for method in sorted({r['method'] for r in task_rows}):
            subset=[r for r in task_rows if r['method']==method]
            records=[(set(r['reference']),set(r['prediction'])) for r in subset]
            # Stratified document bootstrap preserves the 12/12 pilot design.
            intervals=[]
            for _ in range(1000):
                idx=np.r_[rng.choice(12,12,replace=True),rng.choice(np.arange(12,24),12,replace=True)]
                intervals.append(prf([records[i] for i in idx])['f1'])
            lo,hi=np.quantile(intervals,[.025,.975])
            rows.append({'task':task,'method':method,'scope':'agent_silver_short_posts',**prf(records),'ci_low':lo,'ci_high':hi})
            for kind in ('resume','vacancy'):
                part=[(set(r['reference']),set(r['prediction'])) for r in subset if r['post_type']==kind]
                rows.append({'task':task,'method':method,'scope':kind,**prf(part),'ci_low':None,'ci_high':None})
    pd.DataFrame(rows).to_csv(out/'extraction_metrics.csv',index=False)
    # A random review queue is kept separate from disagreement prioritization.
    queue_rows=[]
    for row in queue.itertuples():
        result=sk.extract(row.text,row.label)
        rule=set().union(*(set(result['skills_by_role'][r]) for r in ('required','preferred','possessed')))
        mentions=result['mentions']
        accepted=students['weak_lr'].predict([weak_context(row.text,row.label,m) for m in mentions]) if mentions else []
        learned={m['skill_id'] for m,t in zip(mentions,accepted) if t}
        queue_rows.append({'text_hash':row.text_hash,'post_type':row.label,'text':row.text,
            'rule_skills_json':json.dumps(sorted(rule),ensure_ascii=False),
            'student_skills_json':json.dumps(sorted(learned),ensure_ascii=False),
            'disagreement':len(rule^learned),'review_status':'','expected_skills_json':'',
            'expected_occupations_json':'','expected_salary_amounts_json':'','expected_work_format_json':''})
    pd.DataFrame(queue_rows).to_csv(out/'random_review_queue.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(queue_rows).sort_values('disagreement',ascending=False).to_csv(out/'priority_review_queue.csv',index=False,encoding='utf-8-sig')
    dump(out/'extraction_protocol.json',{'pilot_documents':24,'sample_seed':930,'length_range':[100,1600],
        'sampling':'12 per class from existing labeled CSVs; short posts only; not representative of corpus',
        'human_gold_documents':0,'training_documents':len(training),'weak_mentions':len(contexts),
        'weak_positive_mentions':int(sum(targets)),'close_pilot_matches_excluded':int((proximity>=.85).sum()),
        'training_hashes':training.text_hash.tolist(),'mean_rules_seconds':float(np.mean(timings)),
        'skill_scope':'closed 128-skill taxonomy; target requirements and possessed skills; duties alone excluded',
        'occupation_scope':'target roles, including explicit out-of-taxonomy unknown identifiers',
        'salary_scope':'amount detection only; NOT full currency/period/tax/role normalization',
        'limitation':'One agent drafted reference; no independent human confirmation. Bootstrap ignores annotation errors.'})
    create_review_page(annotation,predictions,out)
    print(pd.DataFrame(rows).query("scope == 'agent_silver_short_posts'")[['task','method','f1','ci_low','ci_high']].to_string(index=False),flush=True)


def create_review_page(reference,predictions,out):
    # Local file; editable JSON drafts with explicit confirmation; no network calls.
    safe=json.dumps(reference,ensure_ascii=False).replace('<','\\u003c')
    page='''<!doctype html><html lang="ru"><meta charset="utf-8"><title>Проверка разметки реальных постов</title>
<style>body{font:16px system-ui;max-width:1100px;margin:32px auto;background:#f5f6fa;color:#192234}article{background:white;padding:24px;margin:24px 0;border-radius:12px}pre{white-space:pre-wrap;line-height:1.5}textarea{width:98%;height:55px}button{padding:12px;background:#164ec2;color:white;border:0;border-radius:8px;cursor:pointer}label{display:block;margin:12px 0}h1{font-size:28px}</style>
<h1>Проверка 24 реальных постов</h1><p>Это черновая разметка ИИ. Проверьте весь текст, включая пропущенные сущности. Навыки: требования и желательные навыки вакансии, заявленные умения резюме. Обязанности вакансии сами по себе исключены. Суммы: все явно указанные суммы оплаты труда. Формат: только безусловно допустимые варианты. Нажмите «Подтверждено» только после проверки всех четырёх полей. [] означает отсутствие, пустая строка — нет ответа.</p>
<p>Файл работает локально. Данные никуда не отправляются. Перед закрытием скачайте JSON; автоматического сохранения нет.</p><button onclick="save()">Скачать проверенную разметку JSON</button><div id="posts"></div><script>
const data=DATA;const fields=['skills','occupations','salary_amounts','work_format'];
for(let i=0;i<data.length;i++){let a=document.createElement('article');let h=document.createElement('h2');h.textContent=(i+1)+'. '+data[i].label;a.append(h);let p=document.createElement('pre');p.textContent=data[i].text;a.append(p);for(const f of fields){let l=document.createElement('label');l.textContent=f;let t=document.createElement('textarea');t.value=JSON.stringify(data[i][f]);t.id=f+i;l.append(t);a.append(l)}let l=document.createElement('label');let c=document.createElement('input');c.type='checkbox';c.id='ok'+i;l.append(c,' Подтверждено человеком');a.append(l);document.getElementById('posts').append(a)}
function save(){try{let result=data.map((r,i)=>{let x={...r};for(const f of fields){let s=document.getElementById(f+i).value;x['human_'+f+'_json']=s;if(s.trim() && !Array.isArray(JSON.parse(s)))throw Error('Нужен JSON-массив: '+f+' пост '+(i+1));}x.human_status=document.getElementById('ok'+i).checked?'confirmed':'';return x});let a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:'application/json'}));a.download='human_review.json';a.click()}catch(e){alert(e.message)}}
</script></html>'''.replace('DATA',safe)
    (out/'review.html').write_text(page,encoding='utf-8')


def evaluate_human(path,out):
    human=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    reference=json.loads((out/'pilot_reference.json').read_text(encoding='utf-8'))
    original={r['text_hash']:r for r in reference}
    if len({r['text_hash'] for r in human})!=len(human):raise ValueError('Duplicate hashes')
    confirmed={}
    for r in human:
        base=original[r['text_hash']]
        if r['text']!=base['text'] or r['label']!=base['label']:raise ValueError('Text/type changed')
        if r['human_status']=='confirmed':
            parsed={}
            for field in ('skills','occupations','salary_amounts','work_format'):
                value=json.loads(r['human_'+field+'_json'])
                if not isinstance(value,list):raise ValueError('Expected list')
                parsed[field]=set(value)
            confirmed[r['text_hash']]=parsed
    if not confirmed:raise ValueError('No confirmed human annotations')
    pred=json.loads((out/'pilot_predictions.json').read_text(encoding='utf-8'))
    records={}
    for r in pred:
        if r['text_hash'] in confirmed:
            records.setdefault((r['task'],r['method']),[]).append((confirmed[r['text_hash']][r['task']],set(r['prediction'])))
    pd.DataFrame([{'task':k[0],'method':k[1],'scope':'human_reviewed_pilot',**prf(v)} for k,v in records.items()]).to_csv(out/'human_metrics.csv',index=False)


if __name__=='__main__':
    from market_system.runtime import enable_guard
    enable_guard()
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='research/results/extraction_20260930');parser.add_argument('--human')

    args=parser.parse_args();out=ROOT/args.output
    if args.human:evaluate_human(args.human,out)
    else:run(out)
