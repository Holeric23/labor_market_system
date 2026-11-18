"""Skill mentions, linguistic context and open-vocabulary candidate discovery.

No semantic similarity score is treated as a probability of synonymy.
Unapproved candidates never enter the canonical skill lists.
"""

from functools import lru_cache
import json
from pathlib import Path
import re

VERSION = 'skills_hybrid_v1'
DEFAULT_LEXICON = Path(__file__).with_name('skill_lexicon.json')
TOKEN = re.compile(r'\.net\b|[^\W_]+(?:[.-][^\W_]+)*(?:\+\+|#)?', re.UNICODE | re.I)
URL = re.compile(r'https?://\S+|www\.\S+|[\w.+-]+@[\w.-]+\.[a-z]+|@[\w]+', re.I)
STOP = set('и или а но в во на по с со к ко от до для из за у о об не ни без как что это все весь свой наш ваш ты мы вы он она они я работа опыт знание умение навык владение уверенный хороший отличный высокий уровень основной базовый профессиональный иметь быть год лет месяц день команда компания сотрудник кандидат вакансия резюме задача требование работающий работать разный любой большой новый готовый нужно нужен желание плюс мочь уметь знать владеть делать требуется приветствоваться желательно обязательно например т д др также только очень'.split())
GENERIC_SINGLE = set('понимание наличие текст проект образование преимущество создание портфолио сфера язык готовность результат инструмент информация речь способность программа процесс возможность решение принцип основа продукт направление участие специалист развитие пользователь клиент материал формат область опытный знание знание опыт опытный опытность'.split())
GENERIC_LEAD = set('понимание наличие готовность способность преимущество возможность требование'.split())
HEADINGS = [
    ('preferred', r'будет\s+(?:плюсом|преимуществом)|желательн\w*|приветствуется'),
    ('skills', r'навыки|компетенции|стек|инструменты|что\s+я\s+умею|умею'),
    ('required', r'требования|требуем|ожидаем|нам\s+важно|что\s+важно|от\s+вас|жд[её]м\s+от'),
    ('task', r'обязанности|задачи|чем\s+предстоит|что\s+нужно\s+делать'),
    ('other', r'условия|предлагаем|о\s+компании|контакты|отклик|зарплата|оплата|о\s+нас|портфолио|образование'),
]
POSITIVE = re.compile(r'\b(?:знани\w*|владени\w*|умени\w*|навык\w*|умею|умеем|знаю|владею|использую|занимаюсь|понимаю|работаю\s+(?:в|с)|опыт\s+работы\s+(?:в|с)|разбираюсь)\b', re.I)
PREFERRED = re.compile(r'желательн\w*|будет\s+(?:плюсом|преимуществом)|приветств\w*|как\s+плюс', re.I)
NEGATED = re.compile(r'\bне\s+(?:знаю|умею|владею|использую|работаю)|без\s+(?:знани\w*|опыт\w*)', re.I)
NOT_REQUIRED = re.compile(r'не\s+(?:требу\w*|обязательн\w*|нуж\w*)', re.I)
LEARNING = re.compile(r'обучим|научим|обучаем|научиться|изучаю|осваиваю|хочу\s+изучить', re.I)


class Normalizer:
    def __init__(self):
        import pymorphy3
        self.morph = pymorphy3.MorphAnalyzer()

    @lru_cache(maxsize=100000)
    def word(self, token):
        token = token.lower().replace('ё', 'е')
        if re.fullmatch('[а-я]+', token):
            return self.morph.parse(token)[0].normal_form.replace('ё', 'е')
        return token

    def tokens(self, text):
        return [(self.word(m.group()), m.start(), m.end()) for m in TOKEN.finditer(text)]

    def phrase(self, text):
        return ' '.join(t[0] for t in self.tokens(text))

    @lru_cache(maxsize=100000)
    def pos(self, word):
        return self.morph.parse(word)[0].tag.POS


def lines_with_sections(text):
    section = 'other'
    age = 0
    offset = 0
    for raw in text.splitlines(keepends=True):
        clean = raw.strip(' \t\r\n*_-•—–✅▪️🔹🔸:')
        found = None
        # Headings may be inline, but ordinary long sentences must not reset a section.
        heading = clean.split(':', 1)[0]
        if len(heading) < 85:
            for kind, pattern in HEADINGS:
                if re.search(r'^(?:[\W_]*)(?:' + pattern + ')', heading, re.I):
                    found = kind
                    break
        if found:
            section, age = found, 0
        else:
            age += 1
        if age > 14:
            section = 'other'
        yield offset, raw.rstrip('\r\n'), section
        offset += len(raw)


def context_role(line, start, end, section, post_type):
    # Commas delimit local qualifiers; shared heading context remains available.
    left = max(line.rfind(';',0,start),line.rfind('.',0,start),line.rfind(',',0,start)) + 1
    ends = [p for sep in [';', '.', ','] if (p := line.find(sep,end)) >= 0]
    right = min(ends) if ends else len(line)
    clause = line[left:right]
    # A shared verb may govern a comma-separated list: "владею Figma, Photoshop".
    # Do not inherit it across sentence/semicolon boundaries.
    sentence_left = max(line.rfind(';',0,start),line.rfind('.',0,start)) + 1
    shared = line[sentence_left:right]
    if not any(pattern.search(clause) for pattern in (POSITIVE,PREFERRED,NEGATED,NOT_REQUIRED,LEARNING)):
        clause = shared
    # "не только" is not a negation of competence.
    negative_context = re.sub(r'не\s+только', '', clause, flags=re.I)
    if NOT_REQUIRED.search(negative_context):
        return 'not_required' if post_type == 'vacancy' else 'mentioned'
    if NEGATED.search(negative_context):
        return 'negated'
    if LEARNING.search(clause):
        return 'learning'
    if post_type == 'vacancy' and (PREFERRED.search(clause) or section == 'preferred'):
        return 'preferred'
    if section == 'task':
        return 'task'
    if post_type == 'resume' and (section == 'skills' or POSITIVE.search(clause)):
        return 'possessed'
    if post_type == 'vacancy' and (section in ('skills','required') or POSITIVE.search(clause)
                                  or re.search(r'требуется|необходимо|обязательно|нужен\s+опыт', clause,re.I)):
        return 'required'
    return 'mentioned'


class SkillExtractor:
    def __init__(self, lexicon_path=DEFAULT_LEXICON):
        self.normalizer = Normalizer()
        self.lexicon_path = Path(lexicon_path)
        self.lexicon = json.loads(self.lexicon_path.read_text(encoding='utf-8'))
        self.skills = {}
        self.aliases = {}
        self.trie = {}
        for skill in self.lexicon['skills']:
            if skill['id'] in self.skills:
                raise ValueError(f"Duplicate skill id: {skill['id']}")
            self.skills[skill['id']] = skill
            for alias in [skill['name'], *skill['aliases']]:
                normalized = self.normalizer.phrase(alias)
                if not normalized:
                    raise ValueError('Empty alias')
                if normalized in self.aliases and self.aliases[normalized] != skill['id']:
                    raise ValueError(f'Ambiguous alias: {alias}')
                self.aliases[normalized] = skill['id']
                node = self.trie
                for token in normalized.split():
                    node = node.setdefault(token,{})
                node['$'] = skill['id']

    def extract(self, text, post_type):
        if post_type not in ('vacancy','resume'):
            raise ValueError('Expected vacancy or resume')
        mentions = []
        candidates = []
        for offset, line, section in lines_with_sections(text):
            excluded = [m.span() for m in URL.finditer(line)]
            tokens = self.normalizer.tokens(line)
            covered = set()
            i = 0
            while i < len(tokens):
                node, j, best = self.trie, i, None
                while j < len(tokens) and tokens[j][0] in node:
                    # A dictionary phrase must not jump a sentence/list delimiter.
                    if j > i and re.search(r'[,;.!?\n]',line[tokens[j-1][2]:tokens[j][1]]):
                        break
                    node = node[tokens[j][0]]
                    if '$' in node:
                        best = (j+1,node['$'])
                    j += 1
                if best:
                    j, skill_id = best
                    start, end = tokens[i][1], tokens[j-1][2]
                    if not any(start < b and end > a for a,b in excluded):
                        role = context_role(line,start,end,section,post_type)
                        mentions.append({'skill_id':skill_id,'name':self.skills[skill_id]['name'],
                                         'category':self.skills[skill_id]['category'],
                                         'raw':line[start:end], 'start':offset+start,'end':offset+end,
                                         'role':role,'context':line,'method':'lexicon_morphology',
                                         'lexicon_version':self.lexicon['version']})
                        covered.update(range(i,j))
                    i = j
                else:
                    i += 1
            # Unknown phrases only from competence-bearing context, not arbitrary post words.
            eligible = section in ('skills','required','preferred') or bool(POSITIVE.search(line))
            if not eligible or section == 'other' and re.search(r'присылайте|отклик|резюме\s+на|наши\s+клиенты',line,re.I):
                continue
            for i in range(len(tokens)):
                if i in covered or tokens[i][0] in STOP:
                    continue
                for size in range(1,5):
                    chunk = tokens[i:i+size]
                    if len(chunk)!=size or any(j in covered for j in range(i,i+size)):
                        break
                    words = [t[0] for t in chunk]
                    if words[-1] in STOP or any(w in STOP for w in words):
                        break
                    start,end = chunk[0][1],chunk[-1][2]
                    if re.search(r'[,;:()!?/\n]',line[start:end]) or any(start < b and end > a for a,b in excluded):
                        break
                    if any(w.isdigit() or len(w)<2 for w in words):
                        continue
                    if words[0] in GENERIC_LEAD or (size==1 and words[0] in GENERIC_SINGLE):
                        continue
                    if size==1 and re.fullmatch('[а-я]+',words[0]) and self.normalizer.pos(words[0]) != 'NOUN':
                        continue
                    # Inflected noun/adjective phrases and technical names; no generic verb fragments.
                    if any(re.fullmatch('[а-я]+',w) and self.normalizer.pos(w) not in ('NOUN','ADJF','ADJS','PRTF','INFN') for w in words):
                        continue
                    role = context_role(line,start,end,section,post_type)
                    if role not in ('required','preferred','possessed'):
                        continue
                    candidates.append({'phrase':' '.join(words),'raw':line[start:end],
                                       'start':offset+start,'end':offset+end,'role':role,'context':line})
        by_role = {role:sorted({m['skill_id'] for m in mentions if m['role']==role})
                   for role in ('required','preferred','possessed','task','learning','negated','not_required','mentioned')}
        return {'extractor_version':VERSION,'lexicon_version':self.lexicon['version'],
                'skills_by_role':by_role,'mentions':mentions,'unknown_candidates':candidates}
