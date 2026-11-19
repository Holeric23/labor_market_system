"""Store a parser batch and its inference results in one PostgreSQL transaction."""
import hashlib
import json
import math
import uuid
from datetime import timezone
from psycopg.types.json import Jsonb
from database.connection import connect


def _number(value):
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _class(con, namespace, key, name, version):
    row = con.execute('''INSERT INTO classes(namespace,class_key,name,definition_version)
      VALUES (%s,%s,%s,%s) ON CONFLICT(namespace,class_key) DO NOTHING RETURNING *''',
                      (namespace,str(key),str(name),str(version))).fetchone()
    if not row:
        row = con.execute('SELECT * FROM classes WHERE namespace=%s AND class_key=%s', (namespace,str(key))).fetchone()
        if row['name'] != str(name) or row['definition_version'] != str(version):
            raise ValueError('Конфликт определения класса')
    return row['id']


def _insert_result(con, result_set, content_id, result, inference):
    if result['label'] not in (None, 'vacancy', 'resume'):
        raise ValueError('Модель вернула неизвестный тип публикации')
    evidence = {key:value for key,value in result.items() if key != 'text'}
    # A round trip rejects unsupported values and NaN instead of inventing data.
    evidence = json.loads(json.dumps(evidence,ensure_ascii=False,allow_nan=False))
    con.execute('''INSERT INTO document_results(result_set_id,content_id,post_type,salary_from,salary_to,
      currency,salary_period,tax_mode,work_format,schedule,employment_type,evidence)
      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
      (result_set,content_id,result['label'],_number(result.get('salary_from')),_number(result.get('salary_to')),
       result.get('currency'),result.get('salary_period'),result.get('tax_mode'),result.get('work_format'),result.get('schedule'),
       result.get('employment_type'),Jsonb(evidence)))
    skill_entries = inference.pipeline.extractor.skills
    for role in ('required','preferred','possessed','task','learning','negated','not_required','mentioned'):
        for key in result.get(role+'_skills',[]):
            entry = skill_entries[key]
            class_id = _class(con,'skill:'+result['skill_lexicon_sha256'],key,entry['name'],result['skill_lexicon_version'])
            con.execute('INSERT INTO document_classes(result_set_id,content_id,class_id,role) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING', (result_set,content_id,class_id,role))
    entries = inference.pipeline.occupations.entries
    for key in result.get('occupation_ids',[]):
        entry = entries[key]
        class_id = _class(con,'occupation:'+result['occupation_lexicon_sha256'],key,entry['name'],result['occupation_lexicon_version'])
        con.execute("INSERT INTO document_classes(result_set_id,content_id,class_id,role) VALUES (%s,%s,%s,'none') ON CONFLICT DO NOTHING", (result_set,content_id,class_id))


def store_batch(source, run_id, batch, inference):
    """Retain old ready sets and commit source cursor only with its data."""
    with connect() as con:
        con.execute('SELECT pg_advisory_xact_lock(%s)', (174925603,))
        current = con.execute('SELECT * FROM sources WHERE id=%s FOR UPDATE', (source['id'],)).fetchone()
        if not current or not current['enabled'] or current['deleted_at'] is not None or current['revision'] != source['revision']:
            raise ValueError('Настройки источника изменились во время запуска')
        if not batch.publications:
            con.execute('UPDATE sources SET cursor=%s WHERE id=%s', (Jsonb(batch.cursor['source']),source['id']))
            return 0
        existing_model = con.execute('SELECT manifest FROM model_versions WHERE id=%s', (inference.version,)).fetchone()
        if existing_model and existing_model['manifest'] != inference.manifest:
            raise ValueError('Для этого ID модели найден другой комплект артефактов')
        if not existing_model:
            con.execute('INSERT INTO model_versions(id,manifest) VALUES (%s,%s)', (inference.version,Jsonb(inference.manifest)))
        active = con.execute('''SELECT r.* FROM active_result_set a JOIN result_sets r ON r.id=a.result_set_id WHERE a.singleton=true FOR UPDATE OF a''').fetchone()
        if active and active['model_version'] != inference.version:
            raise ValueError('Смена модели требует отдельной полной обработки')
        result_set = uuid.uuid4()
        con.execute("INSERT INTO result_sets(id,model_version,state,provenance) VALUES (%s,%s,'building',%s)", (result_set,inference.version,Jsonb({'parser_run':str(run_id)})))
        if active:
            old = active['id']
            con.execute('''INSERT INTO document_results SELECT %s,content_id,post_type,salary_from,salary_to,currency,salary_period,tax_mode,work_format,schedule,employment_type,evidence FROM document_results WHERE result_set_id=%s''', (result_set,old))
            con.execute('''INSERT INTO document_classes SELECT %s,content_id,class_id,role,score FROM document_classes WHERE result_set_id=%s''', (result_set,old))
            con.execute('''INSERT INTO result_publications SELECT %s,publication_id,content_id,published_at FROM result_publications WHERE result_set_id=%s''', (result_set,old))
        created = 0
        for publication in batch.publications:
            text = publication.text
            digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
            row = con.execute('INSERT INTO contents(text_sha256,text) VALUES (%s,%s) ON CONFLICT(text_sha256) DO NOTHING RETURNING id', (digest,text)).fetchone()
            if row:
                created += 1
            else:
                row = con.execute('SELECT id,text FROM contents WHERE text_sha256=%s', (digest,)).fetchone()
                if row['text'] != text:
                    raise ValueError('Конфликт хеша исходного текста')
            content_id = row['id']
            if not con.execute('SELECT 1 FROM document_results WHERE result_set_id=%s AND content_id=%s', (result_set,content_id)).fetchone():
                _insert_result(con,result_set,content_id,inference.analyze(text),inference)
            published_at = publication.published_at
            if published_at and published_at.tzinfo is None:
                published_at = None
            elif published_at:
                published_at = published_at.astimezone(timezone.utc)
            prior = con.execute('SELECT * FROM publications WHERE source_id=%s AND external_key=%s FOR UPDATE', (source['id'],publication.url)).fetchone()
            if prior:
                publication_id = prior['id']
                changed = prior['current_content_id'] != content_id or prior['published_at'] != published_at
                con.execute('UPDATE publications SET current_content_id=%s,published_at=%s,last_observed_at=now() WHERE id=%s', (content_id,published_at,publication_id))
                revision = con.execute('SELECT COALESCE(MAX(revision),0)+1 AS next FROM publication_revisions WHERE publication_id=%s', (publication_id,)).fetchone()['next']
            else:
                publication_id = con.execute('''INSERT INTO publications(source_id,external_key,url,current_content_id,published_at)
                  VALUES (%s,%s,%s,%s,%s) RETURNING id''', (source['id'],publication.url,publication.url,content_id,published_at)).fetchone()['id']
                revision, changed = 1, True
            if changed:
                con.execute('INSERT INTO publication_revisions(publication_id,revision,content_id,parser_run_id) VALUES (%s,%s,%s,%s)', (publication_id,revision,content_id,run_id))
            con.execute('''INSERT INTO result_publications(result_set_id,publication_id,content_id,published_at) VALUES (%s,%s,%s,%s)
              ON CONFLICT(result_set_id,publication_id) DO UPDATE SET content_id=excluded.content_id,published_at=excluded.published_at''',
                        (result_set,publication_id,content_id,published_at))
        con.execute("UPDATE result_sets SET state='ready',ready_at=now() WHERE id=%s", (result_set,))
        con.execute('''INSERT INTO active_result_set(singleton,result_set_id) VALUES (true,%s)
          ON CONFLICT(singleton) DO UPDATE SET result_set_id=excluded.result_set_id''', (result_set,))
        con.execute('INSERT INTO result_set_switches(previous_id,result_set_id,actor) VALUES (%s,%s,%s)', (active['id'] if active else None,result_set,'parser:'+str(run_id)))
        con.execute('UPDATE sources SET cursor=%s WHERE id=%s', (Jsonb(batch.cursor['source']),source['id']))
        return created
