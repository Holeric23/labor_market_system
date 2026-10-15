"""OWNER-only review service, revisions, dictionary approval and review queues."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from market_system.collection.store import Sources
from market_system.paths import DATABASE_PATH
from market_system.search.store import connect, encode, now, add_class
from .projection import normalized, ensure_current, switch, SALARY, CONDITIONS


@dataclass(frozen=True)
class Actor:
    identity: str
    role: str


def owner(actor):
    if not isinstance(actor,Actor) or actor.role not in ('ADMIN', 'OWNER') or not actor.identity:
        raise PermissionError('Исправления доступны только админу.')


class Review:
    def __init__(self,path=DATABASE_PATH):
        self.sources=Sources(path)
        self.jobs=self.sources.jobs
        self.path=self.sources.path

    def initialize(self):
        self.sources.initialize()
        sql=Path(__file__).with_name('005_review.sql').read_text(encoding='utf-8-sig')
        checksum=hashlib.sha256(sql.encode()).hexdigest()
        with self.jobs.transaction() as con:
            row=con.execute('SELECT sha256 FROM schema_migrations WHERE version=5').fetchone()
            if row:
                if row[0]!=checksum:
                    raise ValueError('Migration checksum changed')
                return
            statement=''
            for line in sql.splitlines(True):
                statement+=line
                if sqlite3.complete_statement(statement):
                    con.execute(statement); statement=''
            for table in ('review_datasets','review_memberships','review_type_labels'):
                for operation in ('UPDATE','DELETE'):
                    con.execute(f"CREATE TRIGGER {table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'imported provenance is immutable'); END")
            for operation in ('INSERT','UPDATE','DELETE'):
                checks=[]
                if operation!='INSERT':
                    checks.append("(SELECT state FROM result_sets WHERE result_set_id=OLD.result_set_id) IS NOT 'building'")
                if operation!='DELETE':
                    checks.append("(SELECT state FROM result_sets WHERE result_set_id=NEW.result_set_id) IS NOT 'building'")
                con.execute(f"CREATE TRIGGER review_application_{operation.lower()} BEFORE {operation} ON result_review_applications WHEN {' OR '.join(checks)} BEGIN SELECT RAISE(ABORT,'result review snapshot is immutable'); END")
            con.execute('INSERT INTO schema_migrations VALUES(5,?,?)',(checksum,now()))

    def save(self,cid,expected_revision,status,annotation,actor,expected_active=None):
        owner(actor)
        if status not in ('draft','confirmed','uncertain') or type(expected_revision) is not int or expected_revision<0:
            raise ValueError('Некорректный статус или номер ревизии.')
        with self.jobs.transaction() as con:
            if not con.execute('SELECT 1 FROM contents WHERE id=?',(cid,)).fetchone():
                raise ValueError('Документ не найден.')
            current=con.execute('SELECT COALESCE(MAX(revision),0) FROM review_revisions WHERE content_id=?',(cid,)).fetchone()[0]
            if current!=expected_revision:
                raise ValueError('Ревизия уже изменилась. Обновите документ.')
            active=con.execute('SELECT result_set_id FROM active_result_set WHERE singleton=1').fetchone()
            if expected_active is not None and (active[0] if active else '')!=expected_active:
                raise ValueError('Рабочий каталог изменился. Обновите документ.')
            value=normalized(con,annotation,status)
            revision=current+1
            con.execute('INSERT INTO review_revisions VALUES(?,?,?,?,?,?,?)',
                        (cid,revision,status,actor.identity,now(),encode(value),encode({'source':'owner-review','machine_result_set':active[0] if active else None})))
            published=active[0] if active else None
            if status=='confirmed' and active:
                published=ensure_current(con,active[0])
                switch(con,published)
            return {'revision':revision,'result_set_id':published}

    def propose(self,cid,kind,code,name,definition,actor):
        owner(actor)
        if kind not in ('skill','occupation') or any(not isinstance(s,str) or not s.strip() or len(s)>2000 for s in (code,name,definition)):
            raise ValueError('Нужны тип, стабильный код, название и определение термина.')
        identity=uuid.uuid4().hex
        with self.jobs.transaction() as con:
            if not con.execute('SELECT 1 FROM contents WHERE id=?',(cid,)).fetchone():
                raise ValueError('Документ не найден.')
            con.execute('''INSERT INTO review_terms(term_id,content_id,kind,code,name,definition,proposed_by,proposed_at)
              VALUES(?,?,?,?,?,?,?,?)''',(identity,cid,kind,code.strip(),name.strip(),definition.strip(),actor.identity,now()))
        return identity

    def term_document(self,term_id,actor):
        owner(actor)
        with connect(self.path) as con:
            row=con.execute('SELECT content_id FROM review_terms WHERE term_id=?',(term_id,)).fetchone()
            if not row:
                raise ValueError('Термин не найден.')
            return row[0]

    def approve(self,term_id,actor):
        owner(actor)
        with self.jobs.transaction() as con:
            term=con.execute('SELECT * FROM review_terms WHERE term_id=?',(term_id,)).fetchone()
            if not term:
                raise ValueError('Термин не найден.')
            if term['class_id'] is not None:
                return term['class_id']
            # Identity is the explicitly proposed definition, never the displayed name.
            cid=add_class(con,term['kind']+':human:'+term_id,term['code'],term['name'],'human-v1')
            con.execute('UPDATE review_terms SET class_id=?,approved_by=?,approved_at=? WHERE term_id=?',(cid,actor.identity,now(),term_id))
            return cid

    def get(self,cid,actor):
        owner(actor)
        with connect(self.path) as con:
            con.execute('BEGIN')
            text=con.execute('SELECT * FROM contents WHERE id=?',(cid,)).fetchone()
            if not text:
                raise ValueError('Документ не найден.')
            active=con.execute('SELECT result_set_id FROM active_result_set WHERE singleton=1').fetchone()
            rid=active[0] if active else None
            card=con.execute('SELECT * FROM search_catalog WHERE result_set_id=? AND content_id=?',(rid,cid)).fetchone()
            history=[dict(r) for r in con.execute('SELECT * FROM review_revisions WHERE content_id=? ORDER BY revision DESC',(cid,))]
            value={'post_type':card['post_type'] if card else None,'occupations':[],'skills':[],
                   'salary':[], 'conditions':{k:[] for k in CONDITIONS},'multiple_offers':False,'pending_terms':[],'comment':''}
            if card:
                for row in con.execute('SELECT c.namespace,d.class_id,d.role FROM document_classes d JOIN classes c USING(class_id) WHERE result_set_id=? AND content_id=?',(rid,cid)):
                    if row['namespace'].startswith('occupation:'):
                        value['occupations'].append(row['class_id'])
                    elif row['namespace'].startswith('skill:'):
                        value['skills'].append({'class_id':row['class_id'],'role':row['role']})
                if card['salary_from'] is not None or card['salary_to'] is not None:
                    value['salary']=[{k:card[k] for k in SALARY}]
                value['conditions']={k:[card[k]] if card[k] is not None else [] for k in CONDITIONS}
            if history:
                value=json.loads(history[0]['annotation_json'])
            evidence=con.execute('SELECT * FROM result_evidence WHERE result_set_id=? AND content_id=?',(rid,cid)).fetchone()
            return {'content':dict(text),'active':rid,'revision':history[0]['revision'] if history else 0,
                    'annotation':value,'history':history,'machine':dict(evidence) if evidence else None,
                    'classes':[dict(r) for r in con.execute("SELECT * FROM classes WHERE namespace LIKE 'skill:%' OR namespace LIKE 'occupation:%' ORDER BY name,class_id")],
                    'terms':[dict(r) for r in con.execute('SELECT * FROM review_terms WHERE content_id=? ORDER BY proposed_at',(cid,))],
                    'memberships':[dict(r) for r in con.execute('SELECT * FROM review_memberships WHERE content_id=?',(cid,))],
                    'type_labels':[dict(r) for r in con.execute('SELECT * FROM review_type_labels WHERE content_id=?',(cid,))]}

    def audit(self,actor,limit=20):
        owner(actor)
        if type(limit) is not int or not 1<=limit<=100:
            raise ValueError('Размер случайного аудита: 1..100.')
        with self.jobs.transaction() as con:
            rows=con.execute('''SELECT c.id FROM contents c WHERE NOT EXISTS(SELECT 1 FROM review_audit_queue q WHERE q.content_id=c.id)
              AND NOT EXISTS(SELECT 1 FROM review_revisions r WHERE r.content_id=c.id AND r.status='confirmed') ORDER BY random() LIMIT ?''',(limit,)).fetchall()
            con.executemany('INSERT INTO review_audit_queue VALUES(?,?,?)',[(r[0],now(),actor.identity) for r in rows])
        return len(rows)

    def queue(self,reason,actor):
        owner(actor)
        clauses={
          'audit':"EXISTS(SELECT 1 FROM review_audit_queue q WHERE q.content_id=c.id) AND NOT EXISTS(SELECT 1 FROM review_revisions r WHERE r.content_id=c.id AND r.status='confirmed')",
          'uncertain':"(s.post_type IS NULL AND NOT EXISTS(SELECT 1 FROM review_revisions r WHERE r.content_id=c.id AND r.revision=a.revision AND json_extract(r.annotation_json,'$.post_type')='trash')) OR EXISTS(SELECT 1 FROM review_revisions r WHERE r.content_id=c.id AND r.revision=(SELECT max(x.revision) FROM review_revisions x WHERE x.content_id=c.id) AND r.status='uncertain')",
          'ambiguous':"s.review_status='needs_review' OR s.salary_scope='unattributed' OR (a.revision IS NULL AND EXISTS(SELECT 1 FROM result_evidence e,json_tree(e.profile_json) j WHERE e.result_set_id=s.result_set_id AND e.content_id=c.id AND (j.key='status' OR j.key LIKE '%_status') AND j.value IN ('needs_review','multiple')))",
          'terms':"EXISTS(SELECT 1 FROM review_terms t WHERE t.content_id=c.id AND t.class_id IS NULL)",
          'multiple':"a.multiple_offers=1 OR EXISTS(SELECT 1 FROM review_revisions r WHERE r.content_id=c.id AND r.revision=(SELECT max(x.revision) FROM review_revisions x WHERE x.content_id=c.id) AND json_extract(r.annotation_json,'$.multiple_offers')=1)"}
        if reason not in clauses:
            raise ValueError('Неизвестная очередь.')
        with connect(self.path) as con:
            return [dict(r) for r in con.execute('''SELECT c.id,substr(c.text,1,180) preview,s.post_type,s.review_status,
              (SELECT max(revision) FROM review_revisions r WHERE r.content_id=c.id) revision FROM contents c
              LEFT JOIN active_result_set active ON active.singleton=1
              LEFT JOIN search_catalog s ON s.result_set_id=active.result_set_id AND s.content_id=c.id
              LEFT JOIN result_review_applications a ON a.result_set_id=s.result_set_id AND a.content_id=c.id
              WHERE '''+clauses[reason]+' ORDER BY c.id LIMIT 100')]
