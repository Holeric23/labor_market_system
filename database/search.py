"""Validated PostgreSQL search against a pinned, immutable result set."""
import math
from datetime import datetime
from database.connection import connect
from psycopg.types.json import Jsonb

FIELDS = {'post_type','occupation_ids','skill_ids','skills_mode','skill_roles',
          'published_from','published_until','work_format','schedule','employment_type',
          'salary_min','currency','salary_period','tax_mode','unknown'}


def normalize(value):
    if not isinstance(value,dict) or set(value)-FIELDS:
        raise ValueError('Укажите корректный JSON-фильтр.')
    result = {'post_type':'vacancy','occupation_ids':[],'skill_ids':[],'skills_mode':'ALL',
              'skill_roles':['required','preferred','possessed'],'published_from':None,'published_until':None,
              'work_format':None,'schedule':None,'employment_type':None,'salary_min':None,
              'currency':None,'salary_period':None,'tax_mode':None,'unknown':{}}
    result.update(value)
    if result['post_type'] != 'vacancy':
        raise ValueError('Поиск доступен только по вакансиям.')
    for name in ('occupation_ids','skill_ids'):
        ids = result[name]
        if not isinstance(ids,list) or len(ids)>100 or any(type(v) is not int or not 0<v<2**63 for v in ids):
            raise ValueError('Некорректные идентификаторы классов.')
        result[name] = sorted(set(ids))
    if result['skills_mode'] not in ('ALL','ANY'):
        raise ValueError('skills_mode должен быть ALL или ANY.')
    roles = result['skill_roles']
    if not isinstance(roles,list) or not roles or any(v not in ('required','preferred','possessed') for v in roles):
        raise ValueError('Некорректные роли навыков.')
    result['skill_roles'] = sorted(set(roles))
    for name in ('work_format','schedule','employment_type','tax_mode','currency','salary_period'):
        item = result[name]
        if item is not None and (not isinstance(item,str) or not item.strip()):
            raise ValueError('Некорректное значение '+name+'.')
    if result['tax_mode'] not in (None,'gross','net'):
        raise ValueError('Некорректный tax_mode.')
    salary = result['salary_min']
    if salary is not None:
        if type(salary) not in (int,float) or not math.isfinite(salary) or salary<0 or not result['currency'] or not result['salary_period']:
            raise ValueError('Для зарплаты укажите неотрицательную сумму, валюту и период.')
    elif result['currency'] is not None or result['salary_period'] is not None:
        raise ValueError('Валюта и период требуют минимальной зарплаты.')
    for name in ('published_from','published_until'):
        item = result[name]
        if item is not None:
            try:
                stamp = datetime.fromisoformat(item.replace('Z','+00:00'))
                if stamp.tzinfo is None:
                    raise ValueError()
            except (AttributeError,TypeError,ValueError):
                raise ValueError('Для даты укажите часовой пояс.') from None
            result[name] = stamp.isoformat()
    if result['published_from'] and result['published_until'] and datetime.fromisoformat(result['published_from']) >= datetime.fromisoformat(result['published_until']):
        raise ValueError('Интервал дат пуст.')
    policies = result['unknown']
    allowed = {'published_at_utc','work_format','schedule','employment_type','tax_mode','salary'}
    if not isinstance(policies,dict) or any(k not in allowed or v not in ('exclude','include','only') for k,v in policies.items()):
        raise ValueError('Некорректные правила неизвестных значений.')
    return result


def _query(con, result_set, filters, offset, page_size, *, exclude_user=None):
    for name, namespace in (('occupation_ids','occupation:'),('skill_ids','skill:')):
        ids = filters[name]
        if ids:
            rows = con.execute('SELECT id FROM classes WHERE id=ANY(%s) AND namespace LIKE %s', (ids,namespace+'%')).fetchall()
            if {row['id'] for row in rows} != set(ids):
                raise ValueError('Неизвестный идентификатор класса.')
    where = ["r.result_set_id=%s", "r.post_type='vacancy'",
             """(p.published_at IS NULL OR p.has_unknown OR NOT (SELECT retention_enabled FROM app_settings WHERE singleton=true)
                  OR p.published_at >= now() - ((SELECT retention_days FROM app_settings WHERE singleton=true) * interval '1 day'))"""]
    args = [result_set,result_set]
    if exclude_user is not None:
        where.append('''NOT EXISTS (SELECT 1 FROM client_vacancies cv WHERE cv.telegram_id=%s AND cv.content_id=c.id AND cv.hidden)
          AND NOT EXISTS (SELECT 1 FROM selection_items si JOIN selections s ON s.id=si.selection_id
          WHERE s.telegram_id=%s AND s.status IN ('delivered','pending','uncertain') AND si.content_id=c.id)''')
        args.extend([exclude_user,exclude_user])
    for name,column in (('work_format','r.work_format'),('schedule','r.schedule'),
                        ('employment_type','r.employment_type'),('tax_mode','r.tax_mode'),
                        ('published_at_utc','p.published_at')):
        policy = filters['unknown'].get(name,'exclude')
        terms, values = [], []
        if name == 'published_at_utc':
            for key,op in (('published_from','>='),('published_until','<')):
                if filters[key] is not None:
                    terms.append(f'{column}{op}%s'); values.append(filters[key])
        elif filters[name] is not None:
            terms.append(f'{column}=%s'); values.append(filters[name])
        if policy == 'only':
            where.append(f'{column} IS NULL')
        elif terms:
            expression = '('+' AND '.join(terms)+')'
            where.append(f'({expression} OR {column} IS NULL)' if policy == 'include' else expression)
            args.extend(values)
    if filters['salary_min'] is not None:
        condition = '(COALESCE(r.salary_to,r.salary_from) >= %s AND r.currency=%s AND r.salary_period=%s)'
        policy = filters['unknown'].get('salary','exclude')
        if policy == 'include':
            condition = '('+condition+' OR (r.salary_from IS NULL AND r.salary_to IS NULL))'
        elif policy == 'only':
            condition = '(r.salary_from IS NULL AND r.salary_to IS NULL)'
        where.append(condition)
        if policy != 'only':
            args.extend([filters['salary_min'],filters['currency'],filters['salary_period']])
    for name, roles, all_required in (('occupation_ids',['none'],False),
                                      ('skill_ids',filters['skill_roles'],filters['skills_mode']=='ALL')):
        ids = filters[name]
        if not ids:
            continue
        role_clause = " AND d.role IN ('required','preferred')" if name == 'skill_ids' else ''
        having = ' HAVING count(DISTINCT d.class_id)=%s' if all_required else ''
        where.append('''r.content_id IN (SELECT d.content_id FROM document_classes d
          WHERE d.result_set_id=r.result_set_id AND d.class_id=ANY(%s) AND d.role=ANY(%s)'''+role_clause+' GROUP BY d.content_id'+having+')')
        args.extend([ids,roles])
        if all_required:
            args.append(len(ids))
    args.extend([page_size+1,offset])
    sql = '''WITH p AS (SELECT rp.content_id,max(rp.published_at) AS published_at,bool_or(rp.published_at IS NULL) AS has_unknown,min(pub.url) AS source_url
      FROM result_publications rp JOIN publications pub ON pub.id=rp.publication_id
      WHERE rp.result_set_id=%s GROUP BY rp.content_id)
      SELECT c.id,c.text,r.salary_from,r.salary_to,r.currency,r.salary_period,r.work_format,p.published_at,p.source_url
      FROM document_results r JOIN contents c ON c.id=r.content_id JOIN p ON p.content_id=r.content_id WHERE ''' + ' AND '.join(where) + '''
      ORDER BY p.published_at DESC NULLS LAST,c.id DESC LIMIT %s OFFSET %s'''
    rows = con.execute(sql,args).fetchall()
    return rows[:page_size], len(rows)>page_size


def page(telegram_id, filters=None, *, next_page=False, page_size=3):
    if type(telegram_id) is not int or not 0<telegram_id<2**63 or not 1<=page_size<=30:
        raise ValueError('Некорректные параметры поиска.')
    with connect() as con:
        active = con.execute('SELECT result_set_id FROM active_result_set WHERE singleton=true').fetchone()
        if not active:
            return [],False
        if next_page:
            session = con.execute('SELECT * FROM search_sessions WHERE telegram_id=%s FOR UPDATE', (telegram_id,)).fetchone()
            if not session or not session['has_more']:
                raise ValueError('Следующей страницы нет. Начните /search.')
            if session['result_set_id'] != active['result_set_id']:
                raise ValueError('Результаты обновились. Начните /search заново.')
            value, offset = normalize(session['filter_json']), session['next_offset']
        else:
            value, offset = normalize(filters if filters is not None else {}), 0
        items, more = _query(con,active['result_set_id'],value,offset,page_size)
        con.execute('''INSERT INTO search_sessions(telegram_id,filter_json,result_set_id,next_offset,has_more)
          VALUES (%s,%s,%s,%s,%s) ON CONFLICT(telegram_id) DO UPDATE SET
          filter_json=excluded.filter_json,result_set_id=excluded.result_set_id,next_offset=excluded.next_offset,
          has_more=excluded.has_more,updated_at=now()''', (telegram_id,Jsonb(value),active['result_set_id'],offset+len(items),more))
        return items, more
