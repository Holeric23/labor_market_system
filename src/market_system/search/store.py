from market_system.sqlite import connect as sqlite_connect
"""Transactional migrations and immutable saved-result projection."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid
from market_system.paths import DATABASE_PATH, local_path


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def utc(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    except (ValueError, OverflowError):
        return None


@contextmanager
def connect(path=DATABASE_PATH):
    con = sqlite_connect(local_path(path), timeout=60)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    assert con.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    try:
        yield con
    finally:
        con.close()


def migrate(path=DATABASE_PATH):
    sql = Path(__file__).with_name('001_search.sql').read_text(encoding='utf-8-sig')
    base = Path(__file__).parents[1]/'core/database/database_create/schema.sql'
    checksum = hashlib.sha256(sql.encode()).hexdigest()
    with connect(path) as con:
        con.execute('BEGIN IMMEDIATE')
        try:
            con.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,sha256 TEXT NOT NULL,applied_at TEXT NOT NULL)')
            row=con.execute('SELECT sha256 FROM schema_migrations WHERE version=1').fetchone()
            if row:
                if row[0]!=checksum:raise ValueError('Migration checksum changed')
                con.commit();return
            script=base.read_text(encoding='utf-8-sig')+'\n'+sql
            # execute statements individually: executescript would implicitly commit.
            statement=''
            for line in script.splitlines(True):
                statement+=line
                if sqlite3.complete_statement(statement):
                    con.execute(statement);statement=''
            for table in ('document_processing','document_classes','search_catalog','result_evidence','result_publications'):
                for operation in ('INSERT','UPDATE','DELETE'):
                    check=[]
                    if operation!='INSERT':check.append("(SELECT state FROM result_sets WHERE result_set_id=OLD.result_set_id) IS NOT 'building'")
                    if operation!='DELETE':check.append("(SELECT state FROM result_sets WHERE result_set_id=NEW.result_set_id) IS NOT 'building'")
                    con.execute(f"CREATE TRIGGER {table}_{operation.lower()} BEFORE {operation} ON {table} WHEN {' OR '.join(check)} BEGIN SELECT RAISE(ABORT,'result data is immutable'); END")
            con.execute('INSERT INTO schema_migrations VALUES(1,?,?)',(checksum,now()))
            con.commit()
        except Exception:
            con.rollback();raise


def add_class(con, namespace, key, name, version):
    row=con.execute('SELECT * FROM classes WHERE namespace=? AND class_key=?',(namespace,key)).fetchone()
    if row:
        if (row['name'],row['definition_version'])!=(name,version):raise ValueError('Conflicting class definition')
        return row['class_id']
    return con.execute('INSERT INTO classes(namespace,class_key,name,definition_version) VALUES(?,?,?,?)',(namespace,key,name,version)).lastrowid


def validate(con, identity):
    r=con.execute('SELECT * FROM result_sets WHERE result_set_id=?',(identity,)).fetchone()
    if r is None:raise ValueError('Unknown result set')
    coverage=json.loads(r['coverage_json']);expected=coverage.get('expected',-1)
    counts={t:con.execute(f'SELECT count(*) FROM {t} WHERE result_set_id=?',(identity,)).fetchone()[0] for t in ('document_processing','search_catalog','result_evidence','document_classes')}
    if any(counts[t]!=expected for t in ('document_processing','search_catalog','result_evidence')):raise ValueError('Incomplete result set')
    bad=con.execute("""SELECT 1 FROM document_processing p WHERE result_set_id=? AND
      (status='no_classes') != (NOT EXISTS(SELECT 1 FROM document_classes d WHERE d.result_set_id=p.result_set_id AND d.content_id=p.content_id)) LIMIT 1""",(identity,)).fetchone()
    if bad or con.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('Invalid links or coverage')
    bad_publications=con.execute('''SELECT 1 FROM search_catalog c WHERE c.result_set_id=? AND c.publication_count !=
      (SELECT count(*) FROM result_publications p WHERE p.result_set_id=c.result_set_id AND p.content_id=c.content_id) LIMIT 1''',(identity,)).fetchone()
    historical = ''
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='publication_revisions'").fetchone():
        historical = ''' AND NOT EXISTS(SELECT 1 FROM publication_revisions v
          WHERE v.publication_id=rp.publication_id AND v.content_id=rp.content_id)'''
    wrong_owner=con.execute('''SELECT 1 FROM result_publications rp JOIN publications p ON p.id=rp.publication_id
      WHERE rp.result_set_id=? AND rp.content_id!=p.content_id'''+historical+' LIMIT 1',(identity,)).fetchone()
    if bad_publications or wrong_owner:raise ValueError('Publication grouping mismatch')
    return counts


def activate(path, identity):
    with connect(path) as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute('SELECT state FROM result_sets WHERE result_set_id=?',(identity,)).fetchone()
        if row is None or row[0]!='ready':raise ValueError('Only ready sets can be activated')
        from market_system.review.projection import ensure_current
        identity=ensure_current(con,identity)
        validate(con,identity)
        from market_system.telegram_bot.outbox import reconcile
        reconcile(con,identity)
        previous=con.execute('SELECT result_set_id FROM active_result_set WHERE singleton=1').fetchone()
        if previous and previous[0]==identity:
            con.commit()
            return
        con.execute('INSERT INTO active_result_set VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET result_set_id=excluded.result_set_id',(identity,))
        con.execute('INSERT INTO result_set_switches(previous_id,result_set_id,switched_at) VALUES(?,?,?)',(previous[0] if previous else None,identity,now()))
        con.commit()


def build(path, skills_run, profiles_run, identity=None):
    migrate(path)
    identity=identity or 'saved_'+uuid.uuid4().hex
    with connect(path) as con:
        con.execute("INSERT INTO result_sets(result_set_id,model_version,skills_run_id,profiles_run_id,versions_json,provenance_json,state,created_at) VALUES(?,?,?,?,?,?,'building',?)",(identity,'pending',skills_run,profiles_run,'{}','{}',now()));con.commit()
        try:
            con.execute('BEGIN IMMEDIATE')
            s=con.execute('SELECT * FROM skill_extraction_runs WHERE run_id=?',(skills_run,)).fetchone()
            p=con.execute('SELECT * FROM profile_extraction_runs WHERE run_id=?',(profiles_run,)).fetchone()
            if not s or not p or s['model_version']!=p['model_version']:raise ValueError('Incompatible saved runs')
            model=s['model_version'];sm=json.loads(s['metadata_json']);pm=json.loads(p['metadata_json'])
            c=con.execute('SELECT * FROM classification_runs WHERE model_version=?',(model,)).fetchone()
            if not c or sm.get('classifier_version',model)!=model:raise ValueError('Classification version mismatch')
            if not sm.get('input_sha256') or sm['input_sha256']!=pm.get('input_sha256'):raise ValueError('Different extraction input snapshots')
            expected=con.execute('SELECT count(*) FROM contents').fetchone()[0]
            labeled=con.execute('SELECT count(*) FROM labeled_contents WHERE model_version=?',(model,)).fetchone()[0]
            if expected!=c['content_count'] or expected!=labeled:raise ValueError('Incomplete classification snapshot')
            targets=con.execute("SELECT count(*) FROM labeled_contents WHERE model_version=? AND final_label IS NOT NULL",(model,)).fetchone()[0]
            for table,run in [('skill_extracted_contents',skills_run),('extracted_profiles',profiles_run)]:
                matched=con.execute(f'''SELECT count(*) FROM {table} e JOIN labeled_contents l ON l.text_hash=e.text_hash AND l.model_version=e.model_version AND l.final_label=e.post_type WHERE e.run_id=? AND e.model_version=?''',(run,model)).fetchone()[0]
                total=con.execute(f'SELECT count(*) FROM {table} WHERE run_id=?',(run,)).fetchone()[0]
                if matched!=targets or total!=targets:raise ValueError('Extraction coverage or label mismatch')
            if p['document_count']!=targets:raise ValueError('Profile declared coverage mismatch')
            lex=pm['occupation_lexicon'];occ_version=lex['version']
            versions={'skills_extractor':s['extractor_version'],'skills_dictionary':s['lexicon_version'],'skills_dictionary_sha256':sm['lexicon_sha256'],'occupation_extractor':pm['occupation_version'],'occupation_dictionary':occ_version,'occupation_dictionary_sha256':digest(lex),'salary_extractor':pm['salary_version'],'conditions_extractor':pm['conditions_version']}
            provenance={'classification':dict(c),'skills':dict(s),'profiles':dict(p),'projection':'saved_v1','date_policy':'aware UTC only; latest publication','roles':['required','preferred','possessed'],'no_inference':True}
            con.execute('UPDATE result_sets SET model_version=?,versions_json=?,provenance_json=?,coverage_json=? WHERE result_set_id=?',(model,encode(versions),encode(provenance),encode({'expected':expected}),identity))
            skills={r['skill_id']:add_class(con,'skill:'+sm['lexicon_sha256'],r['skill_id'],r['name'],s['lexicon_version']) for r in con.execute('SELECT * FROM extracted_skill_dictionary WHERE run_id=?',(skills_run,)).fetchall()}
            occupations={r['id']:add_class(con,'occupation:'+digest(lex),r['id'],r['name'],occ_version) for r in lex['occupations']}
            types={name:add_class(con,'post_type:'+model,name,name,model) for name in ('vacancy','resume')}
            # Stream per content; pregroup lightweight evidence, not original text.
            mentions={}
            for r in con.execute('SELECT * FROM extracted_skill_mentions WHERE run_id=? ORDER BY text_hash,mention_index',(skills_run,)):
                mentions.setdefault(r['text_hash'],[]).append(dict(r))
            profiles={r['text_hash']:dict(r) for r in con.execute('SELECT * FROM extracted_profiles WHERE run_id=?',(profiles_run,))}
            pubs={}
            for r in con.execute('SELECT id,content_id,published_at FROM publications ORDER BY id'):
                pubs.setdefault(r['content_id'],[]).append((r['id'],utc(r['published_at'])))
            for row in con.execute('SELECT c.id,l.* FROM contents c JOIN labeled_contents l ON l.text_hash=c.text_hash WHERE l.model_version=? ORDER BY c.id',(model,)).fetchall():
                cid=row['id'];label=row['final_label'];key=row['text_hash'];profile=profiles.get(key);ms=mentions.get(key,[])
                links=set();occ=[]
                if label:links.add((types[label],'none'))
                for mention in ms:
                    role=mention['role']
                    if (label=='vacancy' and role in ('required','preferred')) or (label=='resume' and role=='possessed'):
                        links.add((skills[mention['skill_id']],role))
                if profile:
                    occ=json.loads(profile['occupation_ids_json'])
                    for key in occ:links.add((occupations[key],'none'))
                con.execute('INSERT INTO document_processing VALUES(?,?,?)',(identity,cid,'processed' if links else 'no_classes'))
                con.executemany('INSERT INTO document_classes VALUES(?,?,?,?,NULL)',[(identity,cl,cid,role) for cl,role in sorted(links)])
                publications=pubs.get(cid,[]);dates=[date for _,date in publications if date]
                con.executemany('INSERT INTO result_publications VALUES(?,?,?,?)',[(identity,cid,pid,date) for pid,date in publications])
                ambiguous=len(occ)>1 or (profile is not None and profile['salary_status']=='needs_review')
                salary=[profile[k] if profile and not ambiguous else None for k in ('salary_from','salary_to','currency','salary_period','tax_mode')]
                conditions=[profile[k] if profile else None for k in ('work_format','schedule','employment_type','working_hours','hours_per_week')]
                values=[identity,cid,label,max(dates) if dates else None,len(publications),*salary,*conditions,'needs_review' if ambiguous else 'unreviewed','unattributed' if ambiguous else 'document']
                con.execute('INSERT INTO search_catalog VALUES('+','.join('?' for _ in values)+')',values)
                con.execute('INSERT INTO result_evidence VALUES(?,?,?,?,?)',(identity,cid,encode(dict(row)),encode(profile) if profile else None,encode(ms)))
            counts=validate(con,identity)
            zero=con.execute("SELECT count(*) FROM document_processing WHERE result_set_id=? AND status='no_classes'",(identity,)).fetchone()[0]
            con.execute("UPDATE result_sets SET state='ready',ready_at=?,coverage_json=? WHERE result_set_id=?",(now(),encode({'expected':expected,**counts,'no_classes':zero}),identity))
            from market_system.telegram_bot.outbox import produce
            produce(con,identity)
            con.commit()
        except Exception as error:
            con.rollback()
            con.execute("UPDATE result_sets SET state='failed',error=? WHERE result_set_id=?",(str(error),identity));con.commit()
            raise
    return identity


def main():
    from market_system.runtime import enable_guard
    enable_guard()
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['migrate','build','activate']);parser.add_argument('--db',default=DATABASE_PATH);parser.add_argument('--skills-run');parser.add_argument('--profiles-run');parser.add_argument('--id');args=parser.parse_args()
    if args.command=='migrate':migrate(args.db)
    elif args.command=='activate':activate(args.db,args.id)
    else:
        if not args.skills_run or not args.profiles_run:parser.error('Explicit skills and profiles run IDs required')
        print(build(args.db,args.skills_run,args.profiles_run,args.id))


if __name__=='__main__':main()
