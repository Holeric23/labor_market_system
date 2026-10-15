"""Transactional producer. Called by the processing worker before its ready commit."""
import hashlib
import json
import uuid
from market_system.search.service import compile_page
from market_system.search.store import now
from .catalog import parse_filter


def enabled(con):
    return con.execute("SELECT 1 FROM sqlite_master WHERE name='telegram_outbox'").fetchone() is not None


def rebind(con, sub, model):
    if sub['model_version']=='legacy':
        return None
    value = json.loads(sub['filter_json'])
    if sub['model_version'] == model:
        con.execute("UPDATE telegram_subscriptions SET status='active',reason=NULL WHERE id=?",(sub['id'],))
        return value
    for key in ('skill_ids','occupation_ids'):
        mapped=[]
        for identity in value[key]:
            row=con.execute('SELECT namespace FROM classes WHERE class_id=?',(identity,)).fetchone()
            if row and ':human:' in row[0]:
                mapped.append(identity)
                continue
            row=con.execute('SELECT new_id FROM telegram_class_mappings WHERE old_id=? AND model_version=?',(identity,model)).fetchone()
            if row is None:
                con.execute("UPDATE telegram_subscriptions SET status='needs_update',reason=? WHERE id=?",
                            ('Нет подтверждённого соответствия класса '+str(identity)+' для модели '+model,sub['id']))
                return None
            mapped.append(row[0])
        value[key]=mapped
    value=parse_filter(value)
    con.execute("UPDATE telegram_subscriptions SET filter_json=?,model_version=?,status='active',reason=NULL WHERE id=?",
                (json.dumps(value,sort_keys=True,ensure_ascii=False),model,sub['id']))
    return value


def reconcile(con,rid):
    if not enabled(con):
        return
    model=con.execute('SELECT model_version FROM result_sets WHERE result_set_id=?',(rid,)).fetchone()[0]
    for sub in con.execute("SELECT * FROM telegram_subscriptions WHERE status<>'deleted'").fetchall():
        rebind(con,sub,model)


def produce(con,rid):
    if not enabled(con):
        return
    result=con.execute('SELECT * FROM result_sets WHERE result_set_id=?',(rid,)).fetchone()
    if result['state']!='ready':
        raise ValueError('Outbox requires ready data')
    if json.loads(result['coverage_json']).get('scope')=='sample':
        return
    # Observe every first ready text, even non-vacancies or unmatched texts.
    # Later edits/reclassification/subscriptions do not replay its notification.
    candidates=[]
    for row in con.execute('SELECT c.content_id,c.post_type,t.text FROM search_catalog c JOIN contents t ON t.id=c.content_id WHERE c.result_set_id=? AND NOT EXISTS(SELECT 1 FROM telegram_seen s WHERE s.content_id=c.content_id)',(rid,)):
        digest=hashlib.sha256(row['text'].encode()).hexdigest()
        inserted=con.execute('INSERT OR IGNORE INTO telegram_seen VALUES(?,?,?,?)',(digest,row['content_id'],rid,now())).rowcount
        if not inserted or row['post_type']!='vacancy':
            continue
        # A new revision at an old URL is an old-card edit, not a new vacancy.
        original=con.execute('SELECT 1 FROM result_publications rp JOIN publications p ON p.id=rp.publication_id WHERE rp.result_set_id=? AND rp.content_id=? AND p.content_id=rp.content_id LIMIT 1',(rid,row['content_id'])).fetchone()
        if original:
            candidates.append((row['content_id'],digest))
    if not candidates:
        return
    subscribers=[]
    for sub in con.execute("SELECT s.* FROM telegram_subscriptions s JOIN telegram_access a ON a.user_id=s.user_id WHERE s.status<>'deleted' AND a.enabled=1").fetchall():
        value=rebind(con,sub,result['model_version'])
        if value is not None:
            subscribers.append((sub,value))
    for cid,digest in candidates:
        matches=[]
        for sub,value in subscribers:
            sql,args=compile_page(value,rid,1)
            sql=sql.replace(' ORDER BY',' AND c.content_id=? ORDER BY',1)
            args.insert(-1,cid)
            if con.execute(sql,args).fetchone():
                matches.append(sub)
        if not matches:
            continue
        identity=str(uuid.uuid5(uuid.NAMESPACE_URL,'labor-market:new-vacancy:'+digest))
        con.execute('INSERT OR IGNORE INTO telegram_outbox VALUES(?,?,?,?,?,?)',
                    (identity,digest,cid,rid,'{"render_version":1}',now()))
        for sub in matches:
            con.execute("INSERT OR IGNORE INTO telegram_deliveries(event_id,user_id,state,next_attempt_at) VALUES(?,?,'queued',?)",(identity,sub['user_id'],now()))
            con.execute('INSERT OR IGNORE INTO telegram_delivery_subscriptions VALUES(?,?,?)',(identity,sub['user_id'],sub['id']))
