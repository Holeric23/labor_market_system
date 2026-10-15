from market_system.sqlite import connect as sqlite_connect
from market_system.paths import local_path, DATABASE_PATH
"""Import a complete saved skills run into SQLite atomically and idempotently."""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

ROLES = {'required','preferred','possessed','task','learning','negated','not_required','mentioned'}
SCHEMA = [
    '''CREATE TABLE IF NOT EXISTS skill_extraction_runs (
       run_id TEXT PRIMARY KEY, model_version TEXT NOT NULL REFERENCES classification_runs(model_version),
       extractor_version TEXT NOT NULL, lexicon_version TEXT NOT NULL, imported_at TEXT NOT NULL,
       artifact_sha256 TEXT NOT NULL, metadata_json TEXT NOT NULL,
       UNIQUE(run_id,model_version))''',
    '''CREATE TABLE IF NOT EXISTS extracted_skill_dictionary (
       run_id TEXT NOT NULL REFERENCES skill_extraction_runs(run_id), skill_id TEXT NOT NULL,
       name TEXT NOT NULL, category TEXT NOT NULL, PRIMARY KEY(run_id,skill_id))''',
    '''CREATE TABLE IF NOT EXISTS skill_extracted_contents (
       run_id TEXT NOT NULL, text_hash TEXT NOT NULL, model_version TEXT NOT NULL,
       post_type TEXT NOT NULL CHECK(post_type IN ('vacancy','resume')),
       mention_count INTEGER NOT NULL CHECK(mention_count>=0),
       PRIMARY KEY(run_id,text_hash),
       FOREIGN KEY(run_id,model_version) REFERENCES skill_extraction_runs(run_id,model_version),
       FOREIGN KEY(text_hash,model_version) REFERENCES labeled_contents(text_hash,model_version))''',
    '''CREATE TABLE IF NOT EXISTS extracted_skill_mentions (
       run_id TEXT NOT NULL, text_hash TEXT NOT NULL, mention_index INTEGER NOT NULL,
       skill_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN
       ('required','preferred','possessed','task','learning','negated','not_required','mentioned')),
       start INTEGER NOT NULL CHECK(start>=0), end INTEGER NOT NULL CHECK(end>start),
       raw TEXT NOT NULL, evidence_json TEXT NOT NULL,
       PRIMARY KEY(run_id,text_hash,mention_index),
       FOREIGN KEY(run_id,text_hash) REFERENCES skill_extracted_contents(run_id,text_hash),
       FOREIGN KEY(run_id,skill_id) REFERENCES extracted_skill_dictionary(run_id,skill_id))''',
    'CREATE INDEX IF NOT EXISTS idx_extracted_skills_text ON skill_extracted_contents(text_hash)',
    'CREATE INDEX IF NOT EXISTS idx_skill_mentions_lookup ON extracted_skill_mentions(skill_id,role)',
    '''CREATE VIEW IF NOT EXISTS content_skills AS
       SELECT m.run_id,m.text_hash,c.post_type,m.skill_id,d.name AS skill_name,d.category,
              m.role,COUNT(*) AS mention_count
       FROM extracted_skill_mentions m
       JOIN skill_extracted_contents c USING(run_id,text_hash)
       JOIN extracted_skill_dictionary d USING(run_id,skill_id)
       GROUP BY m.run_id,m.text_hash,c.post_type,m.skill_id,d.name,d.category,m.role''',
]


def file_hash(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def import_skills(db_path,run_dir):
    run_dir=Path(run_dir)
    meta=json.loads((run_dir/'summary.json').read_text(encoding='utf-8'))
    lexicon=json.loads((run_dir/'skill_lexicon.json').read_text(encoding='utf-8'))
    if meta.get('limit') is not None:raise ValueError('Only complete corpus runs can be imported')
    if file_hash(run_dir/'skill_lexicon.json')!=meta['lexicon_sha256']:
        raise ValueError('Lexicon checksum mismatch')
    skills={s['id']:s for s in lexicon['skills']}
    if len(skills)!=len(lexicon['skills']) or lexicon['version']!=meta['lexicon_version']:
        raise ValueError('Invalid lexicon')
    source=run_dir/'skills_structured.csv'
    fingerprint=hashlib.sha256((file_hash(source)+file_hash(run_dir/'summary.json')).encode()).hexdigest()
    run_id,version=meta['run_id'],meta['classifier_version']
    con=sqlite_connect(local_path(db_path).resolve().as_uri()+'?mode=rw',uri=True)
    try:
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('BEGIN IMMEDIATE')
        for statement in SCHEMA:con.execute(statement)
        existing=con.execute('SELECT artifact_sha256 FROM skill_extraction_runs WHERE run_id=?',(run_id,)).fetchone()
        if existing:
            if existing[0]!=fingerprint:raise ValueError('Run ID already imported with different artifacts')
            con.rollback()
            return {'run_id':run_id,'status':'already_imported'}
        expected=dict(con.execute('''SELECT text_hash,final_label FROM labeled_contents
                                     WHERE model_version=? AND final_label IN ('vacancy','resume')''',(version,)))
        if not expected:raise ValueError('No matching classified corpus')
        con.execute('INSERT INTO skill_extraction_runs VALUES(?,?,?,?,?,?,?)',
                    (run_id,version,meta['extractor_version'],meta['lexicon_version'],
                     datetime.now(timezone.utc).isoformat(),fingerprint,json.dumps(meta,ensure_ascii=False)))
        con.executemany('INSERT INTO extracted_skill_dictionary VALUES(?,?,?,?)',
                        [(run_id,s['id'],s['name'],s['category']) for s in skills.values()])
        csv.field_size_limit(max(csv.field_size_limit(),10_000_000))
        seen=set();counts=Counter();roles=Counter();digest=hashlib.sha256();total=0
        with source.open(encoding='utf-8-sig',newline='') as f:
            for row in csv.DictReader(f):
                key,kind,text=row['text_hash'],row['post_type'],row['text']
                if key in seen or expected.get(key)!=kind:raise ValueError('Duplicate, unknown or incorrectly labeled text')
                if any(row[k]!=meta[k] for k in ('classifier_version','extractor_version','lexicon_version')):
                    raise ValueError('Mixed extraction versions')
                stored=con.execute('SELECT text FROM contents WHERE text_hash=?',(key,)).fetchone()
                if not stored or stored[0]!=text:raise ValueError('Source text differs from database')
                mentions=json.loads(row['mentions_json'])
                if not isinstance(mentions,list):raise ValueError('Expected mention list')
                con.execute('INSERT INTO skill_extracted_contents VALUES(?,?,?,?,?)',(run_id,key,version,kind,len(mentions)))
                spans=set();values=[]
                for i,m in enumerate(mentions):
                    start,end=m['start'],m['end']
                    if type(start)!=int or type(end)!=int or not 0<=start<end<=len(text):
                        raise ValueError('Invalid span')
                    if (start,end) in spans or text[start:end]!=m['raw']:
                        raise ValueError('Duplicate or invalid mention evidence')
                    if m['skill_id'] not in skills or m['role'] not in ROLES:
                        raise ValueError('Unknown canonical skill or role')
                    spans.add((start,end));roles[m['role']]+=1
                    values.append((run_id,key,i,m['skill_id'],m['role'],start,end,m['raw'],json.dumps(m,ensure_ascii=False)))
                con.executemany('INSERT INTO extracted_skill_mentions VALUES(?,?,?,?,?,?,?,?,?)',values)
                seen.add(key);counts[kind]+=1;total+=len(mentions)
                digest.update(json.dumps([key,text,kind],ensure_ascii=False).encode()+b'\n')
        if seen!=set(expected) or dict(counts)!=meta['counts'] or digest.hexdigest()!=meta['input_sha256']:
            raise ValueError('Incomplete corpus or input checksum mismatch')
        if dict(roles)!=meta['mention_roles']:raise ValueError('Mention counts differ from manifest')
        con.commit()
        return {'run_id':run_id,'status':'imported','documents':len(seen),'mentions':total}
    except BaseException:
        con.rollback()
        raise
    finally:con.close()


if __name__=='__main__':
    from market_system.runtime import enable_guard
    enable_guard()
    p=argparse.ArgumentParser(description=__doc__)

    p.add_argument('--db',type=Path,required=True);p.add_argument('--run',type=Path,required=True)
    args=p.parse_args()
    print(json.dumps(import_skills(args.db,args.run),ensure_ascii=False))
