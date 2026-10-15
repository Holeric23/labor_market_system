"""Verify initial stage-2 projection; activation requires --activate."""
import argparse
import json
from pathlib import Path
from market_system.initial_import import inventory
from market_system.search.store import connect,validate,activate,migrate
from market_system.paths import PROJECT_ROOT,DATABASE_PATH

r=PROJECT_ROOT
identity='initial_saved_v1'
parser=argparse.ArgumentParser()
parser.add_argument('--activate',action='store_true')
args=parser.parse_args()
before=json.loads((r/'.artifacts/stage2-before.json').read_text(encoding='utf-8'))['snapshot']
migrate(DATABASE_PATH)
with connect() as con:
 con.execute('BEGIN')
 counts=validate(con,identity)
 assert con.execute('PRAGMA integrity_check').fetchall()[0][0]=='ok'
 assert not con.execute('PRAGMA foreign_key_check').fetchall()
 factory=con.row_factory
 con.row_factory=None
 actual=inventory(con)
 con.row_factory=factory
 assert all(actual['tables'][t]==v for t,v in before['tables'].items()),'Legacy rows changed'
 forward=[tuple(x) for x in con.execute('EXPLAIN QUERY PLAN SELECT content_id FROM document_classes WHERE result_set_id=? AND class_id=? AND role=?',(identity,1,'required'))]
 reverse=[tuple(x) for x in con.execute('EXPLAIN QUERY PLAN SELECT class_id,role FROM document_classes WHERE result_set_id=? AND content_id=?',(identity,1))]
 multiple=con.execute('SELECT content_id,count(DISTINCT class_id) n FROM document_classes WHERE result_set_id=? GROUP BY content_id HAVING n>1 ORDER BY n DESC LIMIT 1',(identity,)).fetchone()
 coverage=json.loads(con.execute('SELECT coverage_json FROM result_sets WHERE result_set_id=?',(identity,)).fetchone()[0])
 classes=con.execute('SELECT count(*) FROM classes').fetchone()[0]
 publication_count=con.execute('SELECT count(*) FROM result_publications WHERE result_set_id=?',(identity,)).fetchone()[0]
 ambiguous=con.execute("SELECT count(*) FROM search_catalog WHERE result_set_id=? AND salary_scope='unattributed'",(identity,)).fetchone()[0]
 con.rollback()
if args.activate:activate(DATABASE_PATH,identity)
with connect() as con:
 active=con.execute('SELECT result_set_id FROM active_result_set').fetchone()
 is_active=active is not None and active[0]==identity
 if args.activate:assert is_active
report={'result_set_id':identity,'state':'ready','active':is_active,'coverage':coverage,'classes':classes,'publications':publication_count,'unattributed_salary_cards':ambiguous,'multi_class_example':list(multiple),'integrity_check':'ok','foreign_key_check':[],'unchanged_original_tables':list(before['tables']),'forward_plan':forward,'reverse_plan':reverse,'database_bytes':DATABASE_PATH.stat().st_size}
(r/'.artifacts/stage2-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False,indent=2))
