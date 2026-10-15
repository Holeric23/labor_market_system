from market_system.sqlite import connect as sqlite_connect
from market_system.paths import local_path, DATABASE_PATH
"""Extract known skills and discover unknown phrases from a classifier snapshot."""

import argparse
from collections import Counter,defaultdict
import csv
from datetime import datetime,timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import random
import sqlite3
import uuid

from .skills import DEFAULT_LEXICON, VERSION, SkillExtractor
from .skill_discovery import propose
from .skill_statistics import SkillStatistics
from .run_salary import _reservoir


def run(db_path, output_root, model_version=None, lexicon_path=DEFAULT_LEXICON,
        min_documents=5,max_candidates=4000,limit=None,seed=42):
    extractor=SkillExtractor(lexicon_path)
    con=sqlite_connect(local_path(db_path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        con.execute('BEGIN')
        if model_version is None:
            row=con.execute('SELECT model_version FROM classification_runs ORDER BY labeled_at DESC LIMIT 1').fetchone()
            if not row:raise ValueError('Run classification first')
            model_version=row[0]
        if not con.execute('SELECT 1 FROM classification_runs WHERE model_version=?',(model_version,)).fetchone():
            raise ValueError('Unknown classifier version')
        run_id=VERSION+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8]
        output=local_path(output_root)/run_id
        output.mkdir(parents=True,exist_ok=False)
        (output/'source').mkdir()
        for filename in ['skills.py','run_skills.py','skill_discovery.py','skill_lexicon.py','skill_statistics.py']:
            (output/'source'/filename).write_bytes(Path(__file__).with_name(filename).read_bytes())
        (output/'skill_lexicon.json').write_bytes(Path(lexicon_path).read_bytes())
        counts,with_skills,statuses=Counter(),Counter(),Counter()
        skill_statistics=SkillStatistics(extractor.lexicon)
        profiles={}
        rng=random.Random(seed)
        audit_rng=random.Random(seed+1)
        audit=defaultdict(list)
        digest=hashlib.sha256()
        columns=['text_hash','post_type','classifier_version','extractor_version','lexicon_version',
                 'required_skills','preferred_skills','possessed_skills','task_skills',
                 'mentions_json','unknown_candidates_json','text']
        audit_fields=columns+['review_status','expected_skills_json','comment']
        query='''SELECT c.text_hash,c.text,l.final_label FROM labeled_contents l
                 JOIN contents c USING(text_hash) WHERE l.model_version=?
                 AND l.final_label IN ('vacancy','resume') ORDER BY l.text_hash'''
        params=[model_version]
        if limit is not None:
            if limit<=0:raise ValueError('limit must be positive')
            query+=' LIMIT ?';params.append(limit)
        with (output/'skills_structured.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=columns);writer.writeheader()
            for text_hash,text,post_type in con.execute(query,params):
                result=extractor.extract(text,post_type)
                skill_statistics.observe(text_hash,post_type,result['mentions'])
                counts[post_type]+=1
                digest.update(json.dumps([text_hash,text,post_type],ensure_ascii=False).encode()+b'\n')
                role_lists=result['skills_by_role']
                if any(role_lists[r] for r in ('required','preferred','possessed','task')):
                    with_skills[post_type]+=1
                row={'text_hash':text_hash,'post_type':post_type,'classifier_version':model_version,
                     'extractor_version':VERSION,'lexicon_version':extractor.lexicon['version'],
                     **{role+'_skills':json.dumps(role_lists[role],ensure_ascii=False)
                        for role in ('required','preferred','possessed','task')},
                     'mentions_json':json.dumps(result['mentions'],ensure_ascii=False),
                     'unknown_candidates_json':json.dumps(result['unknown_candidates'],ensure_ascii=False),'text':text}
                writer.writerow(row)
                review={**row,'review_status':'','expected_skills_json':'','comment':''}
                _reservoir(audit[post_type],review,counts[post_type],50,audit_rng)
                # One profile contribution per phrase/skill and text prevents repeated keywords inflating counts.
                contributions={}
                for mention in result['mentions']:
                    statuses[mention['role']]+=1
                    if mention['role'] in ('required','preferred','possessed','task'):
                        contributions.setdefault('skill:'+mention['skill_id'],mention)
                for candidate in result['unknown_candidates']:
                    contributions.setdefault('candidate:'+candidate['phrase'],candidate)
                for key,mention in contributions.items():
                    p=profiles.setdefault(key,{'document_count':0,'post_types':{},'examples':[]})
                    p['document_count']+=1
                    p['post_types'][post_type]=p['post_types'].get(post_type,0)+1
                    example={'text_hash':text_hash,'post_type':post_type,'raw':mention['raw'],
                             'start':mention['start'],'end':mention['end'],'role':mention['role'],'context':mention['context']}
                    _reservoir(p['examples'],example,p['document_count'],5,rng)
                if sum(counts.values())%10000==0:
                    print('Processed:',sum(counts.values()),'phrase profiles:',len(profiles),flush=True)
        with (output/'skills_audit.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=audit_fields);writer.writeheader()
            for label in sorted(audit):writer.writerows(sorted(audit[label],key=lambda r:r['text_hash']))
        skill_statistics.write(output)
        print('Training context similarity model...',flush=True)
        model_summary=propose(profiles,extractor,output,min_documents,max_candidates,seed)
        summary={'run_id':run_id,'classifier_version':model_version,'extractor_version':VERSION,
                 'lexicon_version':extractor.lexicon['version'],'lexicon_size':len(extractor.skills),
                 'counts':dict(counts),'texts_with_contextual_skills':dict(with_skills),
                 'mention_roles':dict(statuses),'phrase_profiles':len(profiles),'input_sha256':digest.hexdigest(),
                 'lexicon_sha256':hashlib.sha256(Path(lexicon_path).read_bytes()).hexdigest(),
                 'code_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (output/'source').iterdir()},
                 'packages':{p:importlib.metadata.version(p) for p in ['numpy','scipy','scikit-learn','pymorphy3','joblib']},
                 'seed':seed,'limit':limit,'similarity_model':model_summary,'quality_metrics':None,
                 'audit_status':'not_reviewed','candidate_decisions':'not_reviewed',
                 'created_at':datetime.now(timezone.utc).isoformat()}
        (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Results:',output,flush=True)
        print(json.dumps({'counts':dict(counts),'with_skills':dict(with_skills),'model':model_summary},ensure_ascii=False),flush=True)
        return output
    finally:
        con.close()


if __name__=='__main__':
    from market_system.runtime import enable_guard
    enable_guard()
    p=argparse.ArgumentParser(description=__doc__)

    p.add_argument('--db',type=Path,default=DATABASE_PATH)
    p.add_argument('--output',type=Path,default=local_path('data/extraction'))
    p.add_argument('--model-version')
    p.add_argument('--lexicon',type=Path,default=DEFAULT_LEXICON)
    p.add_argument('--min-documents',type=int,default=5)
    p.add_argument('--max-candidates',type=int,default=4000)
    p.add_argument('--limit',type=int)
    args=p.parse_args()
    run(args.db,args.output,args.model_version,args.lexicon,args.min_documents,args.max_candidates,args.limit)
