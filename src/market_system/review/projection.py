"""Validation and immutable projection of confirmed corrections. No ML is invoked."""
from copy import deepcopy
import json
import math
import uuid
from market_system.search.store import add_class, encode, now, validate

SALARY = ('salary_from','salary_to','currency','salary_period','tax_mode')
CONDITIONS = ('work_format','schedule','employment_type','working_hours','hours_per_week')
ROLES = ('required','preferred','possessed','task','learning','negated','not_required','mentioned')


def available(con):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE name='review_revisions'").fetchone())


def normalized(con, value, status):
    value = deepcopy(value)
    required = {'post_type','occupations','skills','salary','conditions','multiple_offers','pending_terms','comment'}
    if not isinstance(value,dict) or set(value)!=required:
        raise ValueError('Нужны тип, профессии, навыки, зарплата, условия, пометка нескольких предложений и комментарий.')
    if value['post_type'] not in (None,'vacancy','resume','trash','uncertain'):
        raise ValueError('Неизвестный тип документа.')
    if status=='confirmed' and value['post_type']=='uncertain':
        raise ValueError('Неуверенный тип нельзя подтвердить.')
    if type(value['multiple_offers']) is not bool or not isinstance(value['comment'],str) or len(value['comment'])>4000:
        raise ValueError('Некорректная пометка или комментарий.')
    for key in ('occupations','skills','salary','pending_terms'):
        if not isinstance(value[key],list) or len(value[key])>200:
            raise ValueError('Некорректный список: '+key)
    for term in value['pending_terms']:
        if not isinstance(term,dict) or set(term)!={'term_id','role'}:
            raise ValueError('Некорректный новый термин.')
        row=con.execute('SELECT * FROM review_terms WHERE term_id=?',(term['term_id'],)).fetchone()
        if not row:
            raise ValueError('Неизвестное предложение словаря.')
        if status=='confirmed':
            if row['class_id'] is None:
                raise ValueError('Сначала отдельно подтвердите новый термин в словаре.')
            if row['kind']=='skill':
                value['skills'].append({'class_id':row['class_id'],'role':term['role']})
            else:
                value['occupations'].append(row['class_id'])
    if status=='confirmed':
        value['pending_terms']=[]
    for kind,items in [('occupation',value['occupations']),('skill',[s.get('class_id') if isinstance(s,dict) else None for s in value['skills']])]:
        for identity in items:
            if type(identity) is not int:
                raise ValueError('Выберите класс из словаря по ID.')
            row=con.execute('SELECT namespace FROM classes WHERE class_id=?',(identity,)).fetchone()
            if not row or not row[0].startswith(kind+':'):
                raise ValueError('Неизвестный класс или неверный тип класса.')
    for skill in value['skills']:
        if set(skill)!={'class_id','role'} or skill['role'] not in ROLES:
            raise ValueError('Нужна допустимая роль навыка.')
    value['occupations']=sorted(set(value['occupations']))
    value['skills']=[{'class_id':cid,'role':role} for cid,role in sorted({(s['class_id'],s['role']) for s in value['skills']})]
    for salary in value['salary']:
        if not isinstance(salary,dict) or set(salary)!=set(SALARY):
            raise ValueError('Неполная структура зарплаты.')
        for field in SALARY[:2]:
            number=salary[field]
            if number is not None and (type(number) not in (int,float) or not math.isfinite(number) or number<0):
                raise ValueError('Сумма должна быть конечным неотрицательным числом.')
        low,high=salary['salary_from'],salary['salary_to']
        if low is None and high is None or low is not None and high is not None and low>high:
            raise ValueError('Проверьте границы зарплаты.')
        if salary['tax_mode'] not in (None,'gross','net'):
            raise ValueError('Некорректный режим налогов.')
        for key in ('currency','salary_period'):
            if salary[key] is not None and (not isinstance(salary[key],str) or not 0<len(salary[key])<=80):
                raise ValueError('Некорректная единица зарплаты.')
    if not isinstance(value['conditions'],dict) or set(value['conditions'])!=set(CONDITIONS):
        raise ValueError('Нужны все поля условий; неизвестные значения — пустые списки.')
    for field,items in value['conditions'].items():
        if not isinstance(items,list) or len(items)>20:
            raise ValueError('Условия должны быть списками.')
        for item in items:
            if field=='hours_per_week':
                if type(item) is not int or not 0<item<=168:
                    raise ValueError('Часов в неделю: целое число 1..168.')
            elif not isinstance(item,str) or not 0<len(item.strip())<=120:
                raise ValueError('Некорректное условие.')
    return value


def apply(con, identity, revision):
    cid=revision['content_id']
    value=json.loads(revision['annotation_json'])
    model=con.execute('SELECT model_version FROM result_sets WHERE result_set_id=?',(identity,)).fetchone()[0]
    label=value['post_type'] if value['post_type'] in ('vacancy','resume') else None
    links=set()
    if label:
        links.add((add_class(con,'post_type:'+model,label,label,model),'none'))
        links.update((i,'none') for i in value['occupations'])
        allowed=('required','preferred') if label=='vacancy' else ('possessed',)
        links.update((s['class_id'],s['role']) for s in value['skills'] if s['role'] in allowed)
    con.execute('DELETE FROM document_classes WHERE result_set_id=? AND content_id=?',(identity,cid))
    con.executemany('INSERT INTO document_classes VALUES(?,?,?,?,NULL)',[(identity,cl,cid,role) for cl,role in sorted(links)])
    con.execute('UPDATE document_processing SET status=? WHERE result_set_id=? AND content_id=?',('processed' if links else 'no_classes',identity,cid))
    ambiguous=value['multiple_offers'] or len(value['occupations'])>1 or len(value['salary'])>1
    fields_ambiguous=ambiguous or any(len(v)>1 for v in value['conditions'].values())
    salary=value['salary'][0] if label and not ambiguous and len(value['salary'])==1 else dict.fromkeys(SALARY)
    conditions={field:items[0] if label and len(items)==1 else None for field,items in value['conditions'].items()}
    con.execute('''UPDATE search_catalog SET post_type=?,salary_from=?,salary_to=?,currency=?,salary_period=?,tax_mode=?,
      work_format=?,schedule=?,employment_type=?,working_hours=?,hours_per_week=?,review_status=?,salary_scope=?
      WHERE result_set_id=? AND content_id=?''',
      (label,*[salary[k] for k in SALARY],*[conditions[k] for k in CONDITIONS],
       'needs_review' if fields_ambiguous else 'unreviewed','unattributed' if ambiguous else 'document',identity,cid))
    # Machine evidence is deliberately not changed, even in the derivative snapshot.
    con.execute('''INSERT INTO result_review_applications VALUES(?,?,?,?) ON CONFLICT(result_set_id,content_id)
      DO UPDATE SET revision=excluded.revision,multiple_offers=excluded.multiple_offers''',
      (identity,cid,revision['revision'],int(value['multiple_offers'])))


def pending(con,identity):
    if not available(con):
        return []
    return con.execute('''SELECT r.* FROM review_revisions r JOIN document_processing d ON d.content_id=r.content_id
      AND d.result_set_id=? LEFT JOIN result_review_applications a ON a.result_set_id=d.result_set_id AND a.content_id=r.content_id
      WHERE r.status='confirmed' AND r.revision=(SELECT max(x.revision) FROM review_revisions x WHERE x.content_id=r.content_id AND x.status='confirmed')
      AND (a.revision IS NULL OR a.revision!=r.revision)''',(identity,)).fetchall()


def apply_current(con,identity):
    for revision in pending(con,identity):
        apply(con,identity,revision)


def ensure_current(con,identity):
    revisions=pending(con,identity)
    if not revisions:
        return identity
    base=con.execute('SELECT * FROM result_sets WHERE result_set_id=?',(identity,)).fetchone()
    scope=json.loads(base['coverage_json']).get('scope')
    if scope=='sample' or con.execute("SELECT 1 FROM jobs WHERE result_set_id=? AND kind='sample'",(identity,)).fetchone():
        raise ValueError('sample cannot be activated')
    result='review_'+uuid.uuid4().hex
    provenance=json.loads(base['provenance_json'])
    provenance.update(review_base=identity,review_policy='latest confirmed revision; exact class IDs')
    con.execute('''INSERT INTO result_sets(result_set_id,model_version,skills_run_id,profiles_run_id,versions_json,
      provenance_json,coverage_json,state,created_at) VALUES(?,?,?,?,?,?,?,'building',?)''',
      (result,base['model_version'],base['skills_run_id'],base['profiles_run_id'],base['versions_json'],encode(provenance),base['coverage_json'],now()))
    for table in ('document_processing','document_classes','search_catalog','result_evidence','result_publications','result_review_applications'):
        columns=[r[1] for r in con.execute(f'PRAGMA table_info({table})') if r[1]!='result_set_id']
        con.execute(f"INSERT INTO {table} SELECT ?,{','.join(columns)} FROM {table} WHERE result_set_id=?",(result,identity))
    for revision in revisions:
        apply(con,result,revision)
    counts=validate(con,result)
    coverage=json.loads(base['coverage_json'])
    coverage.update(counts)
    coverage['no_classes']=con.execute("SELECT count(*) FROM document_processing WHERE result_set_id=? AND status='no_classes'",(result,)).fetchone()[0]
    con.execute("UPDATE result_sets SET state='ready',ready_at=?,coverage_json=? WHERE result_set_id=?",(now(),encode(coverage),result))
    return result


def switch(con,identity):
    from market_system.telegram_bot.outbox import reconcile
    reconcile(con,identity)
    old=con.execute('SELECT result_set_id FROM active_result_set WHERE singleton=1').fetchone()
    if old and old[0]==identity:
        return
    con.execute('INSERT INTO active_result_set VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET result_set_id=excluded.result_set_id',(identity,))
    con.execute('INSERT INTO result_set_switches(previous_id,result_set_id,switched_at) VALUES(?,?,?)',(old[0] if old else None,identity,now()))
