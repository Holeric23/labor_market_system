from market_system.sqlite import connect as sqlite_connect
from market_system.paths import local_path, DATABASE_PATH
"""Extract salaries, occupations and working conditions into versioned SQLite rows."""
import argparse
from collections import Counter
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

from .salary import extract_salary, EXTRACTOR_VERSION
from .occupations import OccupationExtractor, VERSION as OCCUPATION_VERSION
from .work_conditions import extract_work_conditions, VERSION as CONDITIONS_VERSION, FIELDS


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def persist_profiles(db_path, model_version, progress=None):
    occupations = OccupationExtractor()
    root = Path(__file__).parent
    metadata = {
        'salary_version': EXTRACTOR_VERSION,
        'occupation_version': OCCUPATION_VERSION,
        'conditions_version': CONDITIONS_VERSION,
        'occupation_lexicon': occupations.lexicon,
        'packages': {p: version(p) for p in ('pymorphy3', 'pymorphy3-dicts-ru')},
        'code_sha256': {p: hashlib.sha256((root / p).read_bytes()).hexdigest()
                        for p in ('salary.py', 'occupations.py', 'work_conditions.py', 'skills.py', 'persist_profiles.py')},
    }
    con = sqlite_connect(local_path(db_path).resolve().as_uri() + '?mode=rw', uri=True)
    try:
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('BEGIN IMMEDIATE')
        rows = con.execute('''SELECT c.text_hash,c.text,l.final_label FROM contents c
            JOIN labeled_contents l USING(text_hash)
            WHERE l.model_version=? AND l.final_label IN ('vacancy','resume')
            ORDER BY c.text_hash''', (model_version,)).fetchall()
        if not rows:
            raise ValueError('No confident texts for this model version')
        digest = hashlib.sha256()
        for row in rows:
            digest.update(encoded(row).encode() + b'\n')
        metadata['input_sha256'] = digest.hexdigest()
        run_id = 'profiles_' + hashlib.sha256((model_version + encoded(metadata)).encode()).hexdigest()
        con.execute('''CREATE TABLE IF NOT EXISTS profile_extraction_runs (
            run_id TEXT PRIMARY KEY, model_version TEXT NOT NULL REFERENCES classification_runs(model_version),
            created_at TEXT NOT NULL, document_count INTEGER NOT NULL, metadata_json TEXT NOT NULL,
            UNIQUE(run_id,model_version))''')
        con.execute('''CREATE TABLE IF NOT EXISTS extracted_profiles (
            run_id TEXT NOT NULL, text_hash TEXT NOT NULL, model_version TEXT NOT NULL,
            post_type TEXT NOT NULL CHECK(post_type IN ('vacancy','resume')),
            salary_status TEXT NOT NULL, salary_from REAL, salary_to REAL,
            currency TEXT, salary_period TEXT, tax_mode TEXT,
            occupation_status TEXT NOT NULL, primary_occupation_id TEXT,
            occupation_ids_json TEXT NOT NULL, occupation_names_json TEXT NOT NULL,
            work_format TEXT, schedule TEXT, employment_type TEXT, working_hours TEXT, hours_per_week INTEGER,
            salary_json TEXT NOT NULL, occupation_json TEXT NOT NULL, work_conditions_json TEXT NOT NULL,
            PRIMARY KEY(run_id,text_hash),
            FOREIGN KEY(run_id,model_version) REFERENCES profile_extraction_runs(run_id,model_version),
            FOREIGN KEY(text_hash,model_version) REFERENCES labeled_contents(text_hash,model_version))''')
        con.execute('CREATE INDEX IF NOT EXISTS idx_profiles_text ON extracted_profiles(text_hash)')
        if con.execute('SELECT 1 FROM profile_extraction_runs WHERE run_id=?', (run_id,)).fetchone():
            if con.execute('SELECT COUNT(*) FROM extracted_profiles WHERE run_id=?', (run_id,)).fetchone()[0] != len(rows):
                raise ValueError('Existing run is incomplete')
            con.rollback()
            return {'run_id': run_id, 'status': 'already_imported', 'documents': len(rows)}
        con.execute('INSERT INTO profile_extraction_runs VALUES(?,?,?,?,?)',
                    (run_id, model_version, datetime.now(timezone.utc).isoformat(), len(rows), encoded(metadata)))
        counts = Counter()
        for index, (key, text, kind) in enumerate(rows, 1):
            salary = extract_salary(text)
            occupation = occupations.extract(text, kind)
            conditions = extract_work_conditions(text, kind)
            for result in (salary, occupation, conditions):
                for mention in result['mentions']:
                    if not 0 <= mention['start'] < mention['end'] <= len(text) or text[mention['start']:mention['end']] != mention['raw']:
                        raise ValueError('Invalid extraction evidence')
            values = [run_id, key, model_version, kind, salary['status'],
                      *(salary[k] for k in ('salary_from', 'salary_to', 'currency', 'salary_period', 'tax_mode')),
                      occupation['status'], occupation['primary_occupation_id'],
                      encoded(occupation['occupation_ids']), encoded(occupation['occupation_names']),
                      *(conditions[field]['value'] for field in FIELDS),
                      encoded(salary), encoded(occupation), encoded(conditions)]
            con.execute('INSERT INTO extracted_profiles VALUES(' + ','.join('?' for _ in values) + ')', values)
            counts['salary_' + salary['status']] += 1
            counts['occupation_' + occupation['status']] += 1
            for field in FIELDS:
                counts[field + '_' + conditions[field]['status']] += 1
            if progress and index % 5000 == 0:
                progress({'processed': index, 'total': len(rows)})
        con.commit()
        return {'run_id': run_id, 'status': 'imported', 'documents': len(rows), 'counts': dict(counts)}
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


if __name__ == '__main__':
    from market_system.runtime import enable_guard
    enable_guard()
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument('--db', required=True)
    parser.add_argument('--model-version', required=True)
    args = parser.parse_args()
    print(encoded(persist_profiles(args.db, args.model_version, lambda p: print(encoded(p), flush=True))))
