"""Frozen analytics v1. Standard library only; also runs from an exported archive.

Usage: python engine.py inputs.json reports.json
The output is canonical JSON, suitable for comparing SHA-256 with manifest.json.
"""
import json
from collections import defaultdict

VERSION = 'analytics_v1'
TITLES = {
    'volume': 'Документы и публикации по датам и источникам',
    'frequency': 'Частота профессий и навыков',
    'occupation_skills': 'Навыки по профессиям',
    'conditions': 'Формат работы и занятость',
    'salary': 'Зарплатные распределения',
    'quality': 'Отсутствующие, неоднозначные и проверенные значения',
}
UNITS = {
    'volume': 'Документ — уникальный исходный текст; публикация — сохранённый ID публикации. В каждой группе документ считается один раз. Суммы документов по датам/источникам не являются числом уникальных документов корпуса.',
    'frequency': 'Число уникальных документов с точным ID профессии/навыка. Навык один раз на документ, независимо от роли и числа упоминаний. Вакансии: required/preferred; резюме: possessed.',
    'occupation_skills': 'Число уникальных документов с парой точных ID профессии и навыка; только однозначная единственная профессия.',
    'conditions': 'Число уникальных документов с эффективным значением каждого независимого поля. NULL — неизвестно, не отсутствие формата/занятости.',
    'salary': 'Число уникальных документов для каждой указанной нижней или верхней границы суммы. Валюта, период, gross/net и тип документа разделены. Это распределения границ, без усреднения вилок и конвертации.',
    'quality': 'Доля документов по каждому полю от всей выбранной выборки; missing/ambiguous/known взаимно исключаются. Verified — отдельная пересекающаяся доля применённых confirmed-исправлений, включая подтверждённое отсутствие.',
}
CONDITIONS = ('work_format', 'schedule', 'employment_type', 'working_hours', 'hours_per_week')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def profile(document):
    value = document['evidence']['profile'] or {}
    return value.get('machine', value)


def multiple(document):
    correction = document['correction']
    if correction:
        return correction['annotation']['multiple_offers']
    return bool(profile(document).get('multiple_offers', False))


def ambiguous(document, field):
    if multiple(document) and field != 'published_at_utc':
        return True
    correction = document['correction']
    if correction:
        value = correction['annotation']
        if field == 'occupation':
            return len(value['occupations']) > 1
        if field in ('salary', 'salary_from', 'salary_to', 'currency', 'salary_period', 'tax_mode'):
            return document['catalog']['salary_scope'] == 'unattributed'
        if field in CONDITIONS:
            return len(value['conditions'][field]) > 1
        return False
    value = profile(document)
    if field == 'occupation':
        return len(document['occupations']) > 1
    if field in ('salary', 'salary_from', 'salary_to', 'currency', 'salary_period', 'tax_mode'):
        return document['catalog']['salary_scope'] == 'unattributed' or value.get('salary_status') == 'needs_review'
    if field == 'post_type':
        return document['evidence']['classification'].get('classification_status') in ('uncertain', 'needs_review')
    raw = value.get('work_conditions_json') or value.get('work_conditions') or {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    status = (raw.get(field) or {}).get('status') if isinstance(raw.get(field), dict) else None
    return status in ('needs_review', 'multiple') or value.get(field + '_status') in ('needs_review', 'multiple')


def skills(document):
    kind = document['catalog']['post_type']
    roles = ('required', 'preferred') if kind == 'vacancy' else ('possessed',) if kind == 'resume' else ()
    return sorted({link['class_id'] for link in document['links'] if link['kind'] == 'skill' and link['role'] in roles})


def calculate(inputs):
    docs = inputs['documents']
    groups = {key: defaultdict(set) for key in TITLES}
    publication_counts = defaultdict(int)
    excluded = {key: set() for key in TITLES}
    def add(report, dimensions, cid):
        groups[report][canonical(dimensions)].add(cid)
    for d in docs:
        cid, c = d['content_id'], d['catalog']
        kind = c['post_type']
        for p in d['publications']:
            dimensions = {'date_utc': p['published_at_utc'][:10] if p['published_at_utc'] else None,
                          'source_id': p['source_id'], 'source': p['source_name'], 'post_type': kind}
            add('volume', dimensions, cid)
            publication_counts[canonical(dimensions)] += 1
        if not d['publications']:
            add('volume', {'date_utc': None, 'source_id': None, 'source': None, 'post_type': kind}, cid)
        if multiple(d):
            for key in ('frequency', 'occupation_skills', 'conditions', 'salary'):
                excluded[key].add(cid)
        else:
            for occ in d['occupations']:
                add('frequency', {'post_type': kind, 'kind': 'occupation', 'class_id': occ}, cid)
            for skill in skills(d):
                add('frequency', {'post_type': kind, 'kind': 'skill', 'class_id': skill}, cid)
            if len(d['occupations']) == 1 and kind in ('vacancy', 'resume'):
                for skill in skills(d):
                    add('occupation_skills', {'post_type': kind, 'occupation_id': d['occupations'][0], 'skill_id': skill}, cid)
            elif len(d['occupations']) > 1:
                excluded['occupation_skills'].add(cid)
            for field in CONDITIONS:
                if ambiguous(d, field):
                    excluded['conditions'].add(cid)
                else:
                    add('conditions', {'post_type': kind, 'field': field, 'value': c[field]}, cid)
            if ambiguous(d, 'salary'):
                excluded['salary'].add(cid)
            elif c['currency'] is not None and c['salary_period'] is not None:
                for field in ('salary_from', 'salary_to'):
                    if c[field] is not None:
                        add('salary', {'post_type': kind, 'currency': c['currency'], 'period': c['salary_period'],
                                       'tax_mode': c['tax_mode'], 'bound': field, 'amount': c[field]}, cid)
        for field in ('post_type', 'published_at_utc', 'occupation', 'skill', 'salary', 'salary_from', 'salary_to', 'currency', 'salary_period', 'tax_mode', *CONDITIONS):
            if field == 'occupation':
                missing = not d['occupations']
            elif field == 'skill':
                missing = not skills(d)
            elif field == 'salary':
                missing = c['salary_from'] is None and c['salary_to'] is None
            elif field == 'published_at_utc':
                missing = not any(p['published_at_utc'] for p in d['publications'])
            else:
                missing = c[field] is None
            status = 'ambiguous' if ambiguous(d, field) else 'missing' if missing else 'known'
            add('quality', {'field': field, 'status': status}, cid)
            if d['correction'] and field != 'published_at_utc':
                add('quality', {'field': field, 'status': 'verified'}, cid)
    # A zero count means an empty category, never an imputed numeric field value.
    for field in ('post_type', 'published_at_utc', 'occupation', 'skill', 'salary', 'salary_from', 'salary_to', 'currency', 'salary_period', 'tax_mode', *CONDITIONS):
        for status in ('missing', 'ambiguous', 'known', 'verified'):
            groups['quality'][canonical({'field': field, 'status': status})]
    reports = {}
    for key, values in groups.items():
        rows = []
        for dimensions, ids in sorted(values.items()):
            row = {'dimensions': json.loads(dimensions), 'count': len(ids), 'document_ids': sorted(ids)}
            if key == 'volume':
                row['publications'] = publication_counts[dimensions]
            if key == 'quality':
                row['share'] = len(ids) / len(docs) if docs else None
            rows.append(row)
        reports[key] = {'title': TITLES[key], 'unit': UNITS[key], 'sample_size': len(docs),
                        'excluded_ambiguous': len(excluded[key]), 'excluded_document_ids': sorted(excluded[key]), 'rows': rows}
    reports['salary']['unknown_units_document_ids'] = sorted(d['content_id'] for d in docs
        if not ambiguous(d, 'salary') and (d['catalog']['salary_from'] is not None or d['catalog']['salary_to'] is not None)
        and (d['catalog']['currency'] is None or d['catalog']['salary_period'] is None))
    reports['salary']['unknown_units'] = len(reports['salary']['unknown_units_document_ids'])
    reports['salary']['measured_documents'] = len({cid for r in reports['salary']['rows'] for cid in r['document_ids']})
    return reports


if __name__ == '__main__':
    import sys
    from pathlib import Path
    Path(sys.argv[2]).write_text(canonical(calculate(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')))), encoding='utf-8')
