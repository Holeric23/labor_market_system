import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from market_system.search.store import connect,migrate,build,activate,add_class,utc
from market_system.paths import PROJECT_ROOT

class SearchSchemaTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=PROJECT_ROOT/'.artifacts/tmp');self.addCleanup(self.tmp.cleanup)
  self.path=Path(self.tmp.name)/'test.db';migrate(self.path)
  with connect(self.path) as c:
   c.executescript('''
   CREATE TABLE classification_runs(model_version TEXT,content_count INTEGER,metadata_json TEXT);
   CREATE TABLE skill_extraction_runs(run_id TEXT,model_version TEXT,extractor_version TEXT,lexicon_version TEXT,metadata_json TEXT);
   CREATE TABLE profile_extraction_runs(run_id TEXT,model_version TEXT,document_count INTEGER,metadata_json TEXT);
   CREATE TABLE labeled_contents(text_hash TEXT,model_version TEXT,final_label TEXT,prob_vac REAL,prob_res REAL);
   CREATE TABLE skill_extracted_contents(run_id TEXT,text_hash TEXT,model_version TEXT,post_type TEXT);
   CREATE TABLE extracted_skill_dictionary(run_id TEXT,skill_id TEXT,name TEXT);
   CREATE TABLE extracted_skill_mentions(run_id TEXT,text_hash TEXT,mention_index INTEGER,skill_id TEXT,role TEXT);
   CREATE TABLE extracted_profiles(run_id TEXT,text_hash TEXT,model_version TEXT,post_type TEXT,
    occupation_ids_json TEXT,salary_status TEXT,salary_from REAL,salary_to REAL,currency TEXT,salary_period TEXT,tax_mode TEXT,
    work_format TEXT,schedule TEXT,employment_type TEXT,working_hours TEXT,hours_per_week INTEGER);
   INSERT INTO contents VALUES(1,'vacancy','h1'),(2,'resume','h2'),(3,'unknown','h3');
   INSERT INTO source_types VALUES(1,'telegram'); INSERT INTO sources VALUES(1,1,'fixture');
   INSERT INTO publications VALUES(1,1,1,'https://test/1','2026-01-01T01:00:00+03:00'),(2,1,1,'https://test/2','2026-01-02T01:00:00Z');
   INSERT INTO labeled_contents VALUES('h1','m','vacancy',.9,.1),('h2','m','resume',.1,.9),('h3','m',NULL,.1,.1);
   INSERT INTO skill_extracted_contents VALUES('s','h1','m','vacancy'),('s','h2','m','resume');
   INSERT INTO extracted_skill_dictionary VALUES('s','a','Same name'),('s','b','Same name');
   INSERT INTO extracted_skill_mentions VALUES('s','h1',0,'a','required'),('s','h1',1,'b','preferred'),('s','h1',2,'b','task'),('s','h2',0,'b','possessed'),('s','h2',1,'a','required');
   ''')
   c.execute('INSERT INTO classification_runs VALUES(?,?,?)',('m',3,'{}'))
   sm={'input_sha256':'input','lexicon_sha256':'dictionary','classifier_version':'m'}
   pm={'input_sha256':'input','occupation_lexicon':{'version':'v','occupations':[{'id':'x','name':'Same name'},{'id':'y','name':'Same name'}]},'occupation_version':'o','salary_version':'s','conditions_version':'c'}
   c.execute('INSERT INTO skill_extraction_runs VALUES(?,?,?,?,?)',('s','m','e','v',json.dumps(sm)))
   c.execute('INSERT INTO profile_extraction_runs VALUES(?,?,?,?)',('p','m',2,json.dumps(pm)))
   for h,label,occ in [('h1','vacancy',['x','y']),('h2','resume',[])]:
    c.execute('INSERT INTO extracted_profiles VALUES('+','.join('?'*16)+')',('p',h,'m',label,json.dumps(occ),'extracted',100,200,'RUB','month',None,None,None,None,None,None))
   c.commit()
 def test_clean_and_repeated_migration(self):
  migrate(self.path)
  with connect(self.path) as c:
   self.assertEqual(c.execute('PRAGMA foreign_keys').fetchone()[0],1)
   self.assertEqual(c.execute('SELECT count(*) FROM schema_migrations').fetchone()[0],1)
   with self.assertRaises(sqlite3.IntegrityError):c.execute("INSERT INTO contents VALUES(4,'other','h1')")
 def test_links_roles_coverage_dates_evidence_and_immutability(self):
  build(self.path,'s','p','one')
  with connect(self.path) as c:
   self.assertEqual(c.execute("SELECT status FROM document_processing WHERE content_id=3").fetchone()[0],'no_classes')
   roles=[r[0] for r in c.execute("SELECT d.role FROM document_classes d JOIN classes c USING(class_id) WHERE namespace LIKE 'skill:%' ORDER BY role")]
   self.assertEqual(roles,['possessed','preferred','required'])
   card=c.execute('SELECT * FROM search_catalog WHERE content_id=1').fetchone()
   self.assertEqual(card['publication_count'],2);self.assertEqual(card['published_at_utc'],'2026-01-02T01:00:00.000000Z')
   self.assertIsNone(card['salary_from']);self.assertEqual(card['salary_scope'],'unattributed')
   self.assertIsNotNone(c.execute('SELECT profile_json FROM result_evidence WHERE content_id=1').fetchone()[0])
   with self.assertRaises(sqlite3.IntegrityError):c.execute("DELETE FROM document_classes WHERE result_set_id='one'")
 def test_unique_link_new_class_without_schema_change(self):
  with connect(self.path) as c:
   c.execute("INSERT INTO result_sets VALUES('b','m','s','p','{}','{}','{}','building','now',NULL,NULL)")
   c.execute("INSERT INTO document_processing VALUES('b',1,'processed')")
   before=c.execute('SELECT count(*) FROM sqlite_master').fetchone()[0]
   a=add_class(c,'n','a','name','v');b=add_class(c,'n','b','name','v');self.assertNotEqual(a,b)
   other=add_class(c,'n:v2','a','name','v2');self.assertNotEqual(a,other)
   with self.assertRaises(ValueError):add_class(c,'n','a','name','v2')
   c.execute("INSERT INTO document_classes VALUES('b',?,1,'none',NULL)",(a,))
   c.execute("INSERT INTO document_classes VALUES('b',?,1,'none',NULL)",(b,))
   with self.assertRaises(sqlite3.IntegrityError):c.execute("INSERT INTO document_classes VALUES('b',?,1,'none',NULL)",(a,))
   with self.assertRaises(sqlite3.IntegrityError):c.execute("INSERT INTO document_classes VALUES('b',?,1,NULL,NULL)",(a,))
   self.assertEqual(before,c.execute('SELECT count(*) FROM sqlite_master').fetchone()[0])
 def test_switch_rollback_and_failed_build_preserve_active(self):
  build(self.path,'s','p','one');activate(self.path,'one')
  build(self.path,'s','p','two');activate(self.path,'two');activate(self.path,'one')
  with connect(self.path) as c:
   c.execute("UPDATE profile_extraction_runs SET model_version='incompatible'");c.commit()
  with self.assertRaises(ValueError):build(self.path,'s','p','bad')
  with self.assertRaises(ValueError):activate(self.path,'bad')
  with connect(self.path) as c:
   self.assertEqual(c.execute('SELECT result_set_id FROM active_result_set').fetchone()[0],'one')
   self.assertEqual(c.execute("SELECT state FROM result_sets WHERE result_set_id='bad'").fetchone()[0],'failed')
   self.assertEqual(c.execute('SELECT count(*) FROM result_set_switches').fetchone()[0],3)
 def test_partial_coverage_refused(self):
  with connect(self.path) as c:c.execute("DELETE FROM extracted_profiles WHERE text_hash='h2'");c.commit()
  with self.assertRaises(ValueError):build(self.path,'s','p','bad')
 def test_naive_date_stays_unknown(self):
  self.assertIsNone(utc('2026-01-01'));self.assertIsNone(utc('invalid'))
  self.assertEqual(utc('2026-01-01T01:00:00+03:00'),'2025-12-31T22:00:00.000000Z')
 def test_same_model_different_input_rejected(self):
  with connect(self.path) as c:
   value=json.loads(c.execute('SELECT metadata_json FROM profile_extraction_runs').fetchone()[0]);value['input_sha256']='different'
   c.execute('UPDATE profile_extraction_runs SET metadata_json=?',(json.dumps(value),));c.commit()
  with self.assertRaises(ValueError):build(self.path,'s','p','mismatch')
 def test_foreign_keys_and_unready_pointer(self):
  with connect(self.path) as c:
   with self.assertRaises(sqlite3.IntegrityError):c.execute("INSERT INTO document_processing VALUES('missing',1,'no_classes')")
   c.execute("INSERT INTO result_sets VALUES('b','m','s','p','{}','{}','{}','building','now',NULL,NULL)")
   with self.assertRaises(sqlite3.IntegrityError):c.execute("INSERT INTO active_result_set VALUES(1,'b')")
   with self.assertRaises(sqlite3.IntegrityError):c.execute("UPDATE result_sets SET state='ready' WHERE result_set_id='b'")
 def test_migration_failure_rolls_back(self):
  other=Path(self.tmp.name)/'collision.db'
  with connect(other) as c:c.execute('CREATE TABLE classes(existing TEXT)');c.commit()
  with self.assertRaises(sqlite3.OperationalError):migrate(other)
  with connect(other) as c:
   self.assertEqual([r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")],['classes'])
