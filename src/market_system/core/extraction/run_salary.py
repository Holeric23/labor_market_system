from market_system.sqlite import connect as sqlite_connect
from market_system.paths import local_path, DATABASE_PATH
"""Read a classifier snapshot, export salary candidates and unfilled audit sheets.

Run: python -m core.extraction.run_salary
No writes to the source database, no new classifier training.
"""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import uuid

from .salary import EXTRACTOR_VERSION, extract_salary


def _reservoir(bucket, row, seen, size, rng):
    if len(bucket) < size:
        bucket.append(row)
    else:
        index = rng.randrange(seen)
        if index < size:
            bucket[index] = row


def run(db_path, output_root, model_version=None, seed=42):
    """Create a new run folder, with 50 uniformly sampled audit posts per class.

    A separate development sample includes up to 10 examples per status/class.
    Audit values must be filled by a person, not copied from predictions.
    """
    db_path = local_path(db_path).resolve()
    con = sqlite_connect(db_path.as_uri() + '?mode=ro', uri=True)
    try:
        con.execute('BEGIN')
        if model_version is None:
            row = con.execute('SELECT model_version FROM classification_runs ORDER BY labeled_at DESC LIMIT 1').fetchone()
            if row is None:
                raise ValueError('No classifier snapshot found; run phase B first')
            model_version = row[0]
        source = con.execute('SELECT metadata_json FROM classification_runs WHERE model_version=?', (model_version,)).fetchone()
        if source is None:
            raise ValueError(f'Unknown classifier version: {model_version}')
        run_id = EXTRACTOR_VERSION + '_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid.uuid4().hex[:8]
        output = local_path(output_root) / run_id
        output.mkdir(parents=True, exist_ok=False)
        (output/'source').mkdir()
        for name in ['salary.py','run_salary.py']:
            (output/'source'/name).write_bytes(Path(__file__).with_name(name).read_bytes())
        totals, statuses = Counter(), defaultdict(Counter)
        audit, development, status_seen = defaultdict(list), defaultdict(list), Counter()
        rng, dev_rng = random.Random(seed), random.Random(seed + 1)
        digest = hashlib.sha256()
        columns = ['text_hash','post_type','classifier_version','extractor_version',
                   'status','salary_from','salary_to','currency','salary_period','tax_mode','mentions_json','text']
        audit_columns = columns + ['review_status','expected_mentions_json','comment']
        with (output/'salary_candidates.csv').open('w',encoding='utf-8-sig',newline='') as stream:
            writer = csv.DictWriter(stream,fieldnames=columns)
            writer.writeheader()
            query = '''SELECT c.text_hash,c.text,l.final_label FROM labeled_contents l
                       JOIN contents c USING(text_hash)
                       WHERE l.model_version=? AND l.final_label IN ('vacancy','resume')
                       ORDER BY l.text_hash'''
            for text_hash, text, label in con.execute(query,(model_version,)):
                result = extract_salary(text)
                record = {'text_hash':text_hash,'post_type':label,'classifier_version':model_version,
                          **{k:v for k,v in result.items() if k!='mentions'},
                          'mentions_json':json.dumps(result['mentions'],ensure_ascii=False),'text':text}
                writer.writerow(record)
                digest.update(json.dumps([text_hash,text,label],ensure_ascii=False).encode('utf-8')+b'\n')
                totals[label] += 1
                statuses[label][result['status']] += 1
                review = {**record,'review_status':'','expected_mentions_json':'','comment':''}
                _reservoir(audit[label],review,totals[label],50,rng)
                key = (label,result['status'])
                status_seen[key] += 1
                _reservoir(development[key],review,status_seen[key],10,dev_rng)
        for name, buckets in [('salary_audit.csv',audit),('salary_development.csv',development)]:
            with (output/name).open('w',encoding='utf-8-sig',newline='') as stream:
                writer = csv.DictWriter(stream,fieldnames=audit_columns)
                writer.writeheader()
                for key in sorted(buckets):
                    writer.writerows(sorted(buckets[key],key=lambda r:r['text_hash']))
        summary = {'run_id':run_id,'extractor_version':EXTRACTOR_VERSION,'classifier_version':model_version,
                   'created_at':datetime.now(timezone.utc).isoformat(),'seed':seed,
                   'source_db':str(db_path),'source_classifier_metadata':json.loads(source[0]),
                   'input_sha256':digest.hexdigest(),
                   'code_sha256':{name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                  for name in ['salary.py','run_salary.py']},
                   'counts':dict(totals),'status_counts':{k:dict(v) for k,v in statuses.items()},
                   'audit_sampling':'uniform_without_replacement_within_post_type_50_each',
                   'development_sampling':'up_to_10_per_post_type_and_extraction_status',
                   'quality_metrics':None,'audit_status':'not_reviewed',
                   'note':'Extraction coverage is not precision or recall. Not_found does not prove absence.'}
        (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        print(f'Extraction run: {output}')
        for label in sorted(totals):
            print(label,totals[label],dict(statuses[label]))
        return output
    finally:
        con.close()


if __name__ == '__main__':
    from market_system.runtime import enable_guard
    enable_guard()
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument('--db',type=Path,default=DATABASE_PATH)
    parser.add_argument('--output',type=Path,default=local_path('data/extraction'))
    parser.add_argument('--model-version')
    args=parser.parse_args()
    run(args.db,args.output,args.model_version)
