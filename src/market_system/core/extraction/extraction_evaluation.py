"""Document-set extraction metrics with document bootstrap and explicit missing-gold state."""

import argparse
from collections import Counter
import csv
import json
import math
from pathlib import Path
import random

SKILL_ROLES={'required','preferred','possessed','task'}


def _sets(raw,task):
    data=json.loads(raw)
    if not isinstance(data,list):raise ValueError('Annotations must be JSON lists; [] means no entities')
    if task=='skills':
        if any(not isinstance(m,dict) or not isinstance(m.get('skill_id'),str) or not m['skill_id']
               or m.get('role') not in SKILL_ROLES for m in data):raise ValueError('Invalid skill/role annotation')
        return {(m['skill_id'],m['role']) for m in data}
    if any(not isinstance(item,str) or not item for item in data):raise ValueError('Expected occupation IDs')
    return set(data)


def _prf(tp,fp,fn):
    return {'precision':tp/(tp+fp) if tp+fp else None,
            'recall':tp/(tp+fn) if tp+fn else None,
            'f1':2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None}


def _quantile(values,p):
    values=sorted(values);index=(len(values)-1)*p;lo=int(index);hi=math.ceil(index)
    return values[lo]+(values[hi]-values[lo])*(index-lo)


def score_records(records,bootstrap=500,seed=42):
    if not records:return {'status':'awaiting_annotations','reviewed_documents':0,'micro':None,'macro_f1':None,'ci95':None,'per_entity':[]}
    entity_counts={};document_counts=[];matches=[];jaccards=[];nonempty=[]
    for gold,pred in records:
        tp,fp,fn=len(gold&pred),len(pred-gold),len(gold-pred)
        document_counts.append((tp,fp,fn));matches.append(gold==pred)
        jaccards.append(len(gold&pred)/len(gold|pred) if gold|pred else 1.0)
        if gold:nonempty.append(gold==pred)
        for entity in gold|pred:
            c=entity_counts.setdefault(entity,Counter())
            c['tp']+=int(entity in gold and entity in pred)
            c['fp']+=int(entity in pred and entity not in gold)
            c['fn']+=int(entity in gold and entity not in pred)
    total=[sum(row[j] for row in document_counts) for j in range(3)]
    per_entity=[]
    for entity,c in sorted(entity_counts.items(),key=lambda item:str(item[0])):
        per_entity.append({'entity':list(entity) if isinstance(entity,tuple) else entity,
                           **{key:c[key] for key in ['tp','fp','fn']},'support':c['tp']+c['fn'],
                           **_prf(c['tp'],c['fp'],c['fn'])})
    rng=random.Random(seed);draws={key:[] for key in ['precision','recall','f1','exact_match']}
    if len(records)>=2:
        for _ in range(bootstrap):
            indices=[rng.randrange(len(records)) for _ in records]
            b=[sum(document_counts[i][j] for i in indices) for j in range(3)]
            values={**_prf(*b),'exact_match':sum(matches[i] for i in indices)/len(indices)}
            for key,value in values.items():
                if value is not None:draws[key].append(value)
    ci={key:[_quantile(values,.025),_quantile(values,.975)] if values else None for key,values in draws.items()}
    return {'status':'evaluated','reviewed_documents':len(records),'tp':total[0],'fp':total[1],'fn':total[2],
            'micro':_prf(*total),'macro_f1':sum(r['f1'] for r in per_entity)/len(per_entity) if per_entity else None,
            'exact_match':sum(matches)/len(matches),'mean_jaccard':sum(jaccards)/len(jaccards),
            'gold_empty_documents':sum(not gold for gold,_ in records),
            'exact_match_nonempty_gold':sum(nonempty)/len(nonempty) if nonempty else None,
            'ci95':ci,'bootstrap_repetitions':bootstrap,'per_entity':per_entity}


def evaluate_audit(audit_path,blind_annotations=None,bootstrap=500):
    csv.field_size_limit(max(csv.field_size_limit(),10000000))
    with Path(audit_path).open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
    if len({r['text_hash'] for r in rows})!=len(rows):raise ValueError('Duplicate audit documents')
    if blind_annotations:
        with Path(blind_annotations).open(encoding='utf-8-sig',newline='') as f:annotations=list(csv.DictReader(f))
        if len({r['text_hash'] for r in annotations})!=len(annotations):raise ValueError('Duplicate annotation documents')
        mapping={r['text_hash']:r for r in rows}
        for row in annotations:
            if row['text_hash'] not in mapping:raise ValueError('Annotation hash outside audit')
            target=mapping[row['text_hash']]
            if row['text']!=target['text'] or row['post_type']!=target['post_type']:raise ValueError('Annotation text/type mismatch')
            for key in ['skills_review_status','expected_skills_json','occupations_review_status','expected_occupations_json']:
                target[key]=row[key]
    report={'audit_documents':len(rows),'unit':'unique entities per text; skills include role',
            'note':'Bootstrap is by document. Equal audit sample sizes by post type do not match corpus proportions; interpret per-type scores.',
            'tasks':{}}
    for task in ['skills','occupations']:
        records=[];by_type={'vacancy':[],'resume':[]};errors=[];invalid=0
        for row in rows:
            if row['post_type'] not in by_type:raise ValueError('Unknown post_type')
            status=row[task+'_review_status'].strip().lower()
            if status not in ('','correct','incorrect','uncertain'):raise ValueError('Invalid review status')
            raw=row['expected_'+task+'_json'].strip()
            if status not in ('correct','incorrect') or not raw:continue
            gold=_sets(raw,task);pred=_sets(row['predicted_'+task+'_json'],task)
            records.append((gold,pred));by_type[row['post_type']].append((gold,pred))
            if gold!=pred:
                errors.append({'text_hash':row['text_hash'],'post_type':row['post_type'],
                               'false_positive':sorted(pred-gold),'false_negative':sorted(gold-pred)})
        report['tasks'][task]={'pooled_balanced_audit':score_records(records,bootstrap),
                               'by_post_type':{k:score_records(v,bootstrap) for k,v in by_type.items()},
                               'unreviewed_documents':len(rows)-len(records),'errors':errors}
    return report


if __name__=='__main__':
    from market_system.runtime import enable_guard
    enable_guard()
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('audit',type=Path)

    p.add_argument('--annotations',type=Path);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();result=evaluate_audit(args.audit,args.annotations)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Quality report:',args.output)
