"""Capture one read transaction; freeze inputs, outputs and executable algorithm."""
import csv
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import uuid
import zipfile
from datetime import date

from market_system.paths import DATABASE_PATH
from market_system.review.store import owner
from market_system.search.store import connect, now
from market_system.telegram_bot.store import BotStore
from . import engine_v1

LIMITATION = 'Выводы относятся только к выбранным документам и источникам. Выборка не представляет весь рынок труда. Точные репосты объединены по исходному тексту; близкие тексты не объединяются, документы не равны установленному числу вакансий.'


def sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def filters(start=None, until=None, sources=(), post_type=None):
    if post_type not in (None, 'vacancy', 'resume'):
        raise ValueError('Неизвестный тип документа.')
    bounds = [date.fromisoformat(v).isoformat() if v else None for v in (start, until)]
    if all(bounds) and bounds[0] >= bounds[1]:
        raise ValueError('Начало периода должно быть раньше исключительной верхней границы.')
    sources = tuple(sorted(set(int(v) for v in sources)))
    if len(sources) > 100 or any(v <= 0 or v > 2**63-1 for v in sources):
        raise ValueError('Некорректный источник.')
    return {'start': bounds[0], 'until': bounds[1], 'sources': list(sources), 'post_type': post_type}


class Analytics:
    def __init__(self, path=DATABASE_PATH):
        self.bot = BotStore(path)
        self.path = self.bot.path

    def initialize(self):
        self.bot.initialize()
        sql = Path(__file__).with_name('007_analytics.sql').read_text(encoding='utf-8')
        checksum = sha(sql)
        with self.bot.transaction() as con:
            prior = con.execute('SELECT sha256 FROM schema_migrations WHERE version=7').fetchone()
            if prior:
                if prior[0] != checksum:
                    raise ValueError('Migration checksum changed')
                return
            statement = ''
            for line in sql.splitlines(True):
                statement += line
                if sqlite3.complete_statement(statement):
                    con.execute(statement)
                    statement = ''
            con.execute('INSERT INTO schema_migrations VALUES(7,?,?)', (checksum, now()))

    def options(self, actor):
        owner(actor)
        with connect(self.path) as con:
            return {'sources': [dict(r) for r in con.execute('SELECT id,name FROM sources ORDER BY id')],
                    'slices': [dict(r) for r in con.execute('SELECT slice_id,created_at,author FROM scientific_slices ORDER BY created_at DESC LIMIT 100')]}

    def _capture(self, con, conditions, result_set_id):
        conditions = filters(**conditions)
        if not result_set_id:
            active = con.execute('SELECT result_set_id FROM active_result_set WHERE singleton=1').fetchone()
            result_set_id = active[0] if active else None
        result = con.execute("SELECT * FROM result_sets WHERE result_set_id=? AND state='ready'", (result_set_id,)).fetchone()
        if not result:
            raise ValueError('Нет выбранного готового каталога.')
        known = {r[0] for r in con.execute('SELECT id FROM sources')}
        if set(conditions['sources']) - known:
            raise ValueError('Источник не найден.')
        predicates, params = ['rp.result_set_id=?'], [result_set_id]
        for field, op in (('start', '>='), ('until', '<')):
            if conditions[field]:
                predicates.append('rp.published_at_utc ' + op + ' ?')
                params.append(conditions[field] + 'T00:00:00.000000Z')
        if conditions['sources']:
            predicates.append('p.source_id IN (' + ','.join('?' for _ in conditions['sources']) + ')')
            params.extend(conditions['sources'])
        restricted = any(conditions[k] for k in ('start', 'until', 'sources'))
        chosen_where, chosen_params = ['sc.result_set_id=?'], [result_set_id]
        if restricted:
            chosen_where.append('sc.content_id IN (SELECT rp.content_id FROM result_publications rp JOIN publications p ON p.id=rp.publication_id WHERE ' + ' AND '.join(predicates) + ')')
            chosen_params.extend(params)
        if conditions['post_type']:
            chosen_where.append('sc.post_type=?')
            chosen_params.append(conditions['post_type'])
        chosen = 'WITH chosen AS (SELECT sc.content_id FROM search_catalog sc WHERE ' + ' AND '.join(chosen_where) + ') '
        pubs = {}
        for row in con.execute('''SELECT rp.content_id,rp.publication_id,rp.published_at_utc,p.source_id,p.url,
                p.published_at AS original_published_at,s.name AS source_name,t.name AS source_type
                FROM result_publications rp JOIN publications p ON p.id=rp.publication_id
                JOIN sources s ON s.id=p.source_id JOIN source_types t ON t.id=s.source_type_id
                WHERE ''' + ' AND '.join(predicates) + ' ORDER BY rp.publication_id', params):
            pubs.setdefault(row['content_id'], []).append(dict(row))
        links = {}
        definitions = {}
        for row in con.execute(chosen + '''SELECT d.content_id,d.class_id,d.role,d.score,c.namespace,c.class_key,c.name,c.definition_version
                FROM document_classes d JOIN classes c USING(class_id) JOIN chosen x ON x.content_id=d.content_id
                WHERE result_set_id=? ORDER BY d.content_id,d.class_id,d.role''', [*chosen_params,result_set_id]):
            value = dict(row)
            kind = value['namespace'].split(':', 1)[0]
            links.setdefault(value['content_id'], []).append({'class_id': value['class_id'], 'role': value['role'], 'score': value['score'], 'kind': kind})
            definitions[value['class_id']] = {k: value[k] for k in ('class_id','namespace','class_key','name','definition_version')}
        corrections = {r['content_id']: dict(r) for r in con.execute(chosen + '''SELECT r.*,a.multiple_offers FROM result_review_applications a
            JOIN chosen x ON x.content_id=a.content_id JOIN review_revisions r ON r.content_id=a.content_id AND r.revision=a.revision
            WHERE a.result_set_id=?''', [*chosen_params,result_set_id])}
        docs = []
        for row in con.execute(chosen + '''SELECT c.*,t.text,t.text_hash,e.classification_json,e.profile_json,e.skills_json
                FROM search_catalog c JOIN chosen x ON x.content_id=c.content_id JOIN contents t ON t.id=c.content_id
                JOIN result_evidence e ON e.result_set_id=c.result_set_id AND e.content_id=c.content_id
                WHERE c.result_set_id=? ORDER BY c.content_id''', [*chosen_params,result_set_id]):
            cid = row['content_id']
            correction = corrections.get(cid)
            if correction:
                correction = {**correction, 'annotation': json.loads(correction['annotation_json'])}
            doc_links = links.get(cid, [])
            raw_profile = json.loads(row['profile_json']) if row['profile_json'] else {}
            machine = raw_profile.get('machine', raw_profile)
            conditions_value = machine.get('work_conditions_json') or machine.get('work_conditions') or {}
            if isinstance(conditions_value,str):
                conditions_value=json.loads(conditions_value)
            compact_profile = {k: machine.get(k) for k in ('salary_status','multiple_offers',*[f+'_status' for f in engine_v1.CONDITIONS])}
            compact_profile['work_conditions'] = {f: {'status': (conditions_value.get(f) or {}).get('status')}
                                                  for f in engine_v1.CONDITIONS if isinstance(conditions_value.get(f),dict)}
            evidence = {'classification': json.loads(row['classification_json']), 'profile': compact_profile,
                        'original_profile_sha256': sha(row['profile_json']) if row['profile_json'] else None,
                        'original_skills_sha256': sha(row['skills_json'])}
            docs.append({'content_id': cid, 'text_sha256': sha(row['text']), 'original_text_hash': row['text_hash'],
                         'catalog': {k: row[k] for k in row.keys() if k not in ('text','text_hash','classification_json','profile_json','skills_json')},
                         'publications': pubs.get(cid, []), 'links': doc_links,
                         'occupations': sorted({v['class_id'] for v in doc_links if v['kind']=='occupation'}),
                         'correction': correction, 'evidence': evidence})
        used = {link['class_id'] for d in docs for link in d['links']}
        registry = None
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='model_registry'").fetchone():
            row = con.execute('SELECT adapter,manifest_json,class_space_json,evaluation_scope,dependencies_json FROM model_registry WHERE model_version=?',
                              (result['model_version'],)).fetchone()
            if row:
                registry = {k:json.loads(row[k]) if k.endswith('_json') else row[k] for k in row.keys()}
            for r in con.execute(chosen + '''SELECT DISTINCT m.class_id,m.definition_json FROM model_class_definitions m
                JOIN document_classes d USING(class_id) JOIN chosen x ON x.content_id=d.content_id WHERE d.result_set_id=?''',
                [*chosen_params,result_set_id]):
                definitions[r['class_id']]['definition'] = json.loads(r['definition_json'])
        terms = [dict(r) for r in con.execute('SELECT * FROM review_terms WHERE class_id IS NOT NULL ORDER BY class_id') if r['class_id'] in used]
        return {'schema': 'scientific_inputs_v1', 'filters': conditions, 'result_set': dict(result),
                'class_definitions': [definitions[k] for k in sorted(used)], 'human_definitions': terms,
                'documents': docs, 'limitations': LIMITATION, **({'model_registry':registry} if registry else {})}

    @staticmethod
    def _result(inputs):
        reports = engine_v1.calculate(inputs)
        engine = Path(engine_v1.__file__).read_text(encoding='utf-8')
        manifest = {'schema': 'scientific_manifest_v1', 'engine_version': engine_v1.VERSION,
                    'filters': inputs['filters'], 'result_set': inputs['result_set'], 'limitations': LIMITATION,
                    'sample_size': len(inputs['documents']), 'publication_sample_size': sum(len(d['publications']) for d in inputs['documents']),
                    'date_policy': 'UTC; start inclusive, until exclusive; select matching publications then distinct documents; undated excluded by a date filter',
                    'class_definitions': inputs['class_definitions'],
                    'human_definitions_sha256': sha(engine_v1.canonical(inputs['human_definitions'])),
                    'checksums': {'inputs': sha(engine_v1.canonical(inputs)), 'reports': sha(engine_v1.canonical(reports)), 'engine': sha(engine)},
                    'documents': [{'content_id': d['content_id'], 'sha256': d['text_sha256']} for d in inputs['documents']]}
        return {'inputs': inputs, 'reports': reports, 'manifest': manifest, 'engine': engine, 'slice_id': None}

    def report(self, conditions, actor, result_set_id=None):
        owner(actor)
        with connect(self.path) as con:
            con.execute('BEGIN')
            return self._result(self._capture(con, conditions, result_set_id))

    def save(self, conditions, actor, result_set_id):
        owner(actor)
        if not result_set_id:
            raise ValueError('Сначала откройте отчёт и зафиксируйте версию каталога.')
        with self.bot.transaction() as con:
            result = self._result(self._capture(con, conditions, result_set_id))
            identity = uuid.uuid4().hex
            result['manifest'].update(slice_id=identity, created_at=now(), author=actor.identity)
            con.execute('INSERT INTO scientific_slices VALUES(?,?,?,?,?,?,?)', (identity, result['manifest']['created_at'], actor.identity,
                        engine_v1.canonical(result['inputs']), engine_v1.canonical(result['reports']),
                        engine_v1.canonical(result['manifest']), result['engine']))
            con.executemany('INSERT INTO scientific_documents VALUES(?,?,?)', [(identity, d['content_id'], d['text_sha256']) for d in result['inputs']['documents']])
            return identity

    def load(self, identity, actor):
        owner(actor)
        with connect(self.path) as con:
            con.execute('BEGIN')
            row = con.execute('SELECT * FROM scientific_slices WHERE slice_id=?', (identity,)).fetchone()
            if row is None:
                raise ValueError('Научный срез не найден.')
            result = {'slice_id': identity, 'inputs': json.loads(row['inputs_json']), 'reports': json.loads(row['reports_json']),
                      'manifest': json.loads(row['manifest_json']), 'engine': row['engine_text']}
            for key in ('inputs','reports','engine'):
                value = result[key] if key == 'engine' else engine_v1.canonical(result[key])
                if sha(value) != result['manifest']['checksums'][key]:
                    raise ValueError('Контрольная сумма среза не совпала: ' + key)
            return result

    def document(self, result, cid, actor):
        owner(actor)
        doc = next((d for d in result['inputs']['documents'] if d['content_id']==cid), None)
        if doc is None:
            raise ValueError('Документ не входит в срез.')
        with connect(self.path) as con:
            text = con.execute('SELECT text FROM contents WHERE id=?', (cid,)).fetchone()
            if text is None or sha(text[0]) != doc['text_sha256']:
                raise ValueError('Контрольная сумма исходного текста не совпала.')
            return {**doc, 'text': text[0]}

    def export(self, result, actor, report=None):
        owner(actor)
        if report is not None and report not in result['reports']:
            raise ValueError('Отчёт не найден.')
        files = {}
        selected = [report] if report else list(result['reports'])
        for key in selected:
            output = io.StringIO(newline='')
            writer = csv.writer(output)
            writer.writerow(['dimensions_json','documents','publications','share','document_ids_json'])
            for row in result['reports'][key]['rows']:
                writer.writerow([engine_v1.canonical(row['dimensions']),row['count'],row.get('publications',''),
                                 row.get('share',''),engine_v1.canonical(row['document_ids'])])
            files[key + '.csv'] = output.getvalue().encode('utf-8-sig')
        if report is None:
            # Operational storage keeps the original text once. Portable archives include
            # the exact text so they can be replayed without any application database.
            inputs = json.loads(engine_v1.canonical(result['inputs']))
            with connect(self.path) as con:
                con.execute('BEGIN')
                for d in inputs['documents']:
                    text = con.execute('SELECT text FROM contents WHERE id=?', (d['content_id'],)).fetchone()
                    if text is None or sha(text[0]) != d['text_sha256']:
                        raise ValueError('Контрольная сумма исходного текста не совпала.')
                    d['text'] = text[0]
            files['inputs.json'] = engine_v1.canonical(inputs).encode('utf-8')
            files['reports.json'] = engine_v1.canonical(result['reports']).encode('utf-8')
            files['engine.py'] = result['engine'].encode('utf-8')
        manifest = {**result['manifest'], 'reports': {key: {k: v for k,v in result['reports'][key].items() if k!='rows'} for key in selected},
                    'files_sha256': {name: hashlib.sha256(data).hexdigest() for name,data in files.items()},
                    'replay': 'python engine.py inputs.json replay.json; compare SHA-256 replay.json with files_sha256[reports.json]' if report is None else None}
        files['manifest.json'] = engine_v1.canonical(manifest).encode('utf-8')
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name,data in files.items():
                archive.writestr(name,data)
        return output.getvalue()
