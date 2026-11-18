"""Explicit working conditions, with spans, negation and conditional evidence.

Workplace, schedule and employment are independent dimensions. No location,
full-time/office or hybrid inference is made from mere co-occurrence.
"""
import re

from .skills import URL

VERSION = 'work_conditions_rules_v1'
RULES = {
    'work_format': {
        'remote': r'\b(?:удал[её]н\w*|remote|дистанционн\w*\s+работ\w*|работ\w*\s+из\s+дома)\b',
        'hybrid': r'\b(?:гибрид\w*|hybrid)\b',
        'onsite': r'\b(?:в\s+офисе|из\s+офиса|офисн\w*\s+(?:работ\w*|формат)|офис|on[ -]?site|на\s+месте\s+работы)\b',
    },
    'schedule': {
        'flexible': r'\b(?:гибк\w*\s+(?:график\w*|расписани\w*|час\w*)|свободн\w*\s+график\w*|flexible\s+(?:hours|schedule))\b',
        'shift': r'\b(?:сменн\w*\s+(?:график\w*|работ\w*)|работ\w*\s+по\s+сменам|shift\s+work)\b',
        'night': r'\b(?:ночн\w*\s+(?:смен\w*|работ\w*)|night\s+shift)\b',
        'weekdays': r'\b(?:по\s+будням|пн\s*[-–—]\s*пт|понедельник\s*[-–—]\s*пятниц\w*)\b',
        'weekends': r'\b(?:работ\w*\s+по\s+выходным|смены\s+по\s+выходным)\b',
        'rotational': r'\b(?:вахт\w*\s+(?:метод\w*|график\w*)|работ\w*\s+вахтой)\b',
    },
    'employment_type': {
        'full_time': r'\b(?:полн\w*\s+(?:занятост\w*|рабоч\w*\s+день)|full[ -]?time|фул[ -]?тайм)\b',
        'part_time': r'\b(?:частичн\w*\s+занятост\w*|неполн\w*\s+рабоч\w*\s+день|part[ -]?time|парт[ -]?тайм|полставки|подработк\w*)\b',
        'project': r'\b(?:проектн\w*\s+(?:работ\w*|занятост\w*)|разов\w*\s+(?:задач\w*|проект\w*)|на\s+один\s+проект)\b',
        'freelance': r'\b(?:фриланс\w*|freelanc\w*)\b',
        'internship': r'\b(?:стажировк\w*|internship)\b',
        'temporary': r'\b(?:временн\w*\s+(?:работ\w*|занятост\w*)|срочн\w*\s+трудов\w*\s+договор)\b',
    },
}
PATTERNS = [(field,value,re.compile(pattern,re.I)) for field,values in RULES.items() for value,pattern in values.items()]
CYCLE = re.compile(r'(?<![\w/])(?P<on>[1-7])\s*/\s*(?P<off>[1-7])(?![\w/])')
HOURS = re.compile(r'\bс\s*(?P<h1>[01]?\d|2[0-3])(?::(?P<m1>[0-5]\d))?\s*(?:до|[-–—])\s*(?P<h2>[01]?\d|2[0-3])(?::(?P<m2>[0-5]\d))?(?![\d:])|(?<!\d)(?P<t1>(?:[01]?\d|2[0-3]):[0-5]\d)\s*[-–—]\s*(?P<t2>(?:[01]?\d|2[0-3]):[0-5]\d)(?!\d)',re.I)
WEEKLY = re.compile(r'(?<!\d)(\d{1,3})\s*(?:час(?:а|ов)?|ч\.)\s*(?:в|/)\s*недел\w*',re.I)
FIELDS = (*RULES, 'working_hours', 'hours_per_week')


def _context(text,start,end,field):
    line_start=text.rfind('\n',0,start)+1
    line_end=text.find('\n',end)
    if line_end<0:line_end=len(text)
    line=text[line_start:line_end]
    prefix=text[line_start:start]
    suffix=text[end:line_end]
    # Commas and conjunctions delimit separate choices / qualifications.
    left=max((m.end() for m in re.finditer(r'[;.!?,]|\b(?:но|зато)\b',prefix,re.I)),default=0)
    right=re.search(r'[;.!?,]|\b(?:но|зато)\b',suffix,re.I)
    local_prefix=prefix[left:]
    local_suffix=suffix[:right.start()] if right else suffix
    clause=local_prefix+' '+text[start:end]+' '+local_suffix
    normalized=re.sub(r'не\s+только','',local_prefix,flags=re.I)
    if re.search(r'\b(?:не|без)\s+(?:(?:рассматриваю|предлагаем|предусмотрен\w*|подходит|готов\w*|хочу|работаю|возможн\w*)\s+)?(?:к\s+|на\s+)?$',normalized,re.I) or re.match(r'^\s*(?:невозмож\w*|исключен\w*|не\s+(?:подходит|рассматрива\w*|предусмотрен\w*|предлага\w*|возмож\w*|будет))',local_suffix,re.I):
        return 'negated',line
    past=list(re.finditer(r'\b(?:раньше|ранее|работал[аи]?|предыдущ\w*|прошл\w*)\b',prefix,re.I))
    present=list(re.finditer(r'\b(?:ищу|ищем|сейчас|теперь|рассматриваю|предлагаем|готов\w*)\b',prefix,re.I))
    if past and (not present or past[-1].start()>present[-1].start()):return 'historical',line
    if re.search(r'после\s+(?:испытательн\w*|стажировк\w*)|через\s+\d+\s*(?:месяц\w*|недел\w*)|при\s+условии|по\s+согласованию|если\b',clause,re.I):
        return 'conditional',line
    if field=='work_format':
        if re.match(r'\s*(?:команд\w*|доступ\w*|подключени\w*|сотрудник\w*|клиент\w*)',suffix,re.I):return 'incidental',line
        if re.search(r'наш\s*$|(?:красивый|уютный|просторный)\s*$|адрес\s*:\s*$',local_prefix,re.I):return 'incidental',line
        if re.match(r'\s*(?:продаж|находится|компании|в\s+[А-ЯЁ])',suffix):return 'incidental',line
    if field=='employment_type' and re.search(r'команд\w*|сотруднич\w*\s+с\s*$|работ\w*\s+с\s*$',local_prefix,re.I):
        return 'incidental',line
    return 'affirmed',line


def extract_work_conditions(text,post_type):
    if not isinstance(text,str):raise TypeError('text must be a string')
    if post_type not in ('vacancy','resume'):raise ValueError('Expected vacancy or resume')
    mentions=[]
    excluded=[m.span() for m in URL.finditer(text)]

    def add(field,value,start,end,rule):
        if any(start<b and end>a for a,b in excluded):return
        role,context=_context(text,start,end,field)
        mentions.append({'field':field,'value':value,'raw':text[start:end],'start':start,'end':end,
                         'role':role,'rule_id':rule,'context':context})

    for field,value,pattern in PATTERNS:
        for match in pattern.finditer(text):add(field,value,*match.span(),f'{field}_{value}')
    for match in CYCLE.finditer(text):
        # Avoid dates: 05/02, 5/2/2026 and URLs are excluded by boundaries.
        add('schedule',f'{match["on"]}/{match["off"]}',*match.span(),'work_rest_cycle')
    for match in HOURS.finditer(text):
        if match['t1']:
            value=match['t1'].zfill(5)+'–'+match['t2'].zfill(5)
        else:
            value=f'{int(match["h1"]):02d}:{int(match["m1"] or 0):02d}–{int(match["h2"]):02d}:{int(match["m2"] or 0):02d}'
        # Explicit time syntax, never infer hours from salary numbers.
        add('working_hours',value,*match.span(),'clock_range')
    for match in WEEKLY.finditer(text):
        if 0<int(match[1])<=168:add('hours_per_week',int(match[1]),*match.span(),'weekly_hours')
    mentions.sort(key=lambda m:(m['start'],m['end'],m['field']))
    # Explicit hybrid composition, not "office OR remote". Keep component spans.
    hybrid_contexts={m['context'] for m in mentions if m['field']=='work_format' and m['value']=='hybrid'
                     and m['role']=='affirmed' and re.search(r'гибрид\w*\s*(?:формат\w*\s*)?[:(—–-]',m['context'],re.I)
                     and not re.search(r'\bили\b',m['context'],re.I)}
    for mention in mentions:
        if mention['field']=='work_format' and mention['value'] in ('remote','onsite') and mention['role']=='affirmed' and mention['context'] in hybrid_contexts:
            mention['role']='hybrid_component'
    result={'extractor_version':VERSION,'mentions':mentions}
    for field in FIELDS:
        evidence=[m for m in mentions if m['field']==field]
        values=sorted({m['value'] for m in evidence if m['role']=='affirmed'})
        negatives={m['value'] for m in evidence if m['role']=='negated'}
        conflict=bool(set(values)&negatives)
        status='needs_review' if conflict else 'single' if len(values)==1 else 'multiple' if values else 'needs_review' if evidence else 'not_found'
        result[field]={'values':values,'value':values[0] if status=='single' else None,
                       'status':status,'conflicting':conflict,
                       'conditional_values':sorted({m['value'] for m in evidence if m['role']=='conditional'})}
    return result
