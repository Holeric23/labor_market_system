from market_system.paths import local_path, DATABASE_PATH
"""Extract occupations from an existing skills snapshot without retraining classifiers."""

import argparse
from collections import Counter
import csv
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import uuid

from .occupations import OccupationExtractor,VERSION,DEFAULT_LEXICON


def run(skills_run,output_root,lexicon_path=DEFAULT_LEXICON,limit=None):
    skills_run=Path(skills_run).resolve()
    source=json.loads((skills_run/'summary.json').read_text(encoding='utf-8'))
    extractor=OccupationExtractor(lexicon_path)
    output=local_path(output_root)/(VERSION+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8])
    output.mkdir(parents=True,exist_ok=False)
    (output/'occupation_lexicon.json').write_bytes(Path(lexicon_path).read_bytes())
    (output/'source').mkdir()
    for name in ['occupations.py','run_occupations.py','skills.py']:
        (output/'source'/name).write_bytes(Path(__file__).with_name(name).read_bytes())
    csv.field_size_limit(max(csv.field_size_limit(),10000000))
    with (skills_run/'skills_audit.csv').open(encoding='utf-8-sig',newline='') as f:
        audit_sources={r['text_hash']:r for r in csv.DictReader(f)}
    fields=['text_hash','post_type','classifier_version','extractor_version','lexicon_version',
            'status','primary_occupation_id','occupation_ids_json','occupation_names_json','mentions_json','text']
    audit_fields=['text_hash','post_type','text','predicted_skills_json','predicted_occupations_json',
                  'skills_review_status','expected_skills_json','occupations_review_status','expected_occupations_json','comment']
    counts=Counter();statuses=Counter();frequencies=Counter();audits=[];digest=hashlib.sha256();seen=set()
    with (skills_run/'skills_structured.csv').open(encoding='utf-8-sig',newline='') as source_file, (output/'occupations_structured.csv').open('w',encoding='utf-8-sig',newline='') as target:
        w=csv.DictWriter(target,fieldnames=fields);w.writeheader()
        for i,row in enumerate(csv.DictReader(source_file)):
            if limit is not None and i>=limit:break
            if row['text_hash'] in seen:raise ValueError('Duplicate source text')
            if row['classifier_version']!=source['classifier_version']:raise ValueError('Mixed classifier snapshots')
            seen.add(row['text_hash'])
            result=extractor.extract(row['text'],row['post_type'])
            digest.update(json.dumps([row['text_hash'],row['text'],row['post_type']],ensure_ascii=False).encode()+b'\n')
            counts[row['post_type']]+=1;statuses[(row['post_type'],result['status'])]+=1
            for identity in result['occupation_ids']:frequencies[(row['post_type'],identity)]+=1
            w.writerow({**{key:row[key] for key in ['text_hash','post_type','classifier_version','text']},
                        'extractor_version':VERSION,'lexicon_version':extractor.lexicon['version'],
                        'status':result['status'],'primary_occupation_id':result['primary_occupation_id'],
                        'occupation_ids_json':json.dumps(result['occupation_ids'],ensure_ascii=False),
                        'occupation_names_json':json.dumps(result['occupation_names'],ensure_ascii=False),
                        'mentions_json':json.dumps(result['mentions'],ensure_ascii=False)})
            if row['text_hash'] in audit_sources:
                old=audit_sources[row['text_hash']]
                predictions=sorted({(m['skill_id'],m['role']) for m in json.loads(row['mentions_json'])
                                    if m['role'] in ('required','preferred','possessed','task')})
                audits.append({'text_hash':row['text_hash'],'post_type':row['post_type'],'text':row['text'],
                               'predicted_skills_json':json.dumps([{'skill_id':s,'role':r} for s,r in predictions],ensure_ascii=False),
                               'predicted_occupations_json':json.dumps(result['occupation_ids'],ensure_ascii=False),
                               # A previous all-role audit has a different scope. Preserve it in its source file.
                               'skills_review_status':'','expected_skills_json':'',
                               'occupations_review_status':'','expected_occupations_json':'','comment':''})
            if (i+1)%20000==0:print('Occupations processed:',i+1,flush=True)
    with (output/'extraction_audit.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=audit_fields);w.writeheader();w.writerows(audits)
    # An annotation-only copy permits reviewing texts without seeing predictions.
    blind_fields=[c for c in audit_fields if not c.startswith('predicted_')]
    with (output/'annotation_blind.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=blind_fields,extrasaction='ignore');w.writeheader();w.writerows(audits)
    with (output/'occupation_frequencies.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['occupation_id','name','group','vacancy_texts','resume_texts','total_texts'])
        for identity,entry in sorted(extractor.entries.items(),key=lambda item:(-sum(frequencies[t,item[0]] for t in ['vacancy','resume']),item[0])):
            vac,res=frequencies['vacancy',identity],frequencies['resume',identity]
            w.writerow([identity,entry['name'],entry['group'],vac,res,vac+res])
    summary={'run_id':output.name,'skills_run':str(skills_run),'classifier_version':source['classifier_version'],
             'extractor_version':VERSION,'lexicon_version':extractor.lexicon['version'],'lexicon_size':len(extractor.entries),
             'counts':dict(counts),'status_counts':{t:{s:statuses[t,s] for s in ['single','multiple','needs_review','not_found']} for t in counts},
             'input_sha256':digest.hexdigest(),'limit':limit,'audit_documents':len(audits),
             'quality_metrics':None,'source_skill_audit_has_annotations':any(r.get('expected_skills_json','').strip() for r in audit_sources.values()),
             'code_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (output/'source').iterdir()},
             'lexicon_sha256':hashlib.sha256(Path(lexicon_path).read_bytes()).hexdigest(),
             'created_at':datetime.now(timezone.utc).isoformat()}
    if limit is None and source.get('input_sha256')!=summary['input_sha256']:raise ValueError('Input snapshot hash mismatch')
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Occupation results:',output,flush=True);print(json.dumps(summary['status_counts']),flush=True)
    return output


if __name__=='__main__':
    from market_system.runtime import enable_guard
    enable_guard()
    p=argparse.ArgumentParser(description=__doc__)

    p.add_argument('skills_run',type=Path);p.add_argument('--output',type=Path,default=local_path('data/extraction'))
    p.add_argument('--lexicon',type=Path,default=DEFAULT_LEXICON);p.add_argument('--limit',type=int)
    args=p.parse_args();run(args.skills_run,args.output,args.lexicon,args.limit)
