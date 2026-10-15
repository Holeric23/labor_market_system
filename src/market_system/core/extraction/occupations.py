"""Dictionary-based target occupation extraction with explicit textual evidence."""

import json
from pathlib import Path
import re
from functools import lru_cache

from .skills import Normalizer, URL

VERSION='occupations_rules_v3'
DEFAULT_LEXICON=Path(__file__).with_name('occupation_lexicon.json')
RECRUIT=re.compile(r'\b(?:ищем|ищет|ищу|требуется|требуются|нужен|нужна|нужны|ваканси[яюи]|приглашаем|разыскиваем|в\s+поиске|открыта\s+позиция)\b',re.I)
SEEK=re.compile(r'ищу\s+(?:работу|вакансию|позицию|проекты|должность)|рассматриваю\s+(?:позици\w*|ваканси\w*|работу)|хочу\s+работать|желаемая\s+должность|позици[яю]\s*:',re.I)
SELF=re.compile(r'\bя\s+(?:(?:опытный|начинающий|ведущий|junior|middle|senior)\s+)?$|работаю\s+(?:как\s+)?$|специализируюсь\s+как\s*$',re.I)
HISTORY=re.compile(r'раньше|ранее|бывш\w*|работал[аи]?|предыдущ\w*|прошл\w*|начинал\w*',re.I)
COLLEAGUE=re.compile(r'(?:работ\w*|взаимодейств\w*|сотруднич\w*|коммуникаци\w*)\s+с\s+(?:командой\s+)?$|команд[аеу]\s+|для\s+команды\s+|от\s*$',re.I)
SECTION_OTHER=re.compile(r'^(?:опыт\s+работы|обязанности|задачи|что\s+делать|чем\s+предстоит|мы\s+предлагаем|что\s+предлагаем|требования|навыки|о\s+компании|условия|образование|контакты)\b',re.I)


class OccupationNormalizer(Normalizer):
    @lru_cache(maxsize=100000)
    def word(self, token):
        # Inflected compound titles, without changing the skills pipeline.
        return '-'.join(super(OccupationNormalizer, self).word(part) for part in token.split('-'))


class OccupationExtractor:
    def __init__(self,lexicon_path=DEFAULT_LEXICON):
        self.normalizer=OccupationNormalizer()
        self.lexicon_path=Path(lexicon_path)
        self.lexicon=json.loads(self.lexicon_path.read_text(encoding='utf-8'))
        self.entries={}
        self.trie={}
        for entry in self.lexicon['occupations']:
            if entry['id'] in self.entries:raise ValueError('Duplicate occupation ID')
            self.entries[entry['id']]=entry
            for alias in [entry['name'],*entry['aliases']]:
                tokens=self.normalizer.phrase(alias).split()
                if not tokens:raise ValueError('Empty occupation alias')
                node=self.trie
                for token in tokens:node=node.setdefault(token,{})
                if '$' in node and node['$']!=entry['id']:raise ValueError(f'Ambiguous alias: {alias}')
                node['$']=entry['id']
        for entry in self.entries.values():
            parent=entry.get('parent_id')
            if parent and parent not in self.entries:raise ValueError('Unknown parent occupation')

    def _role(self,line,start,end,line_number,post_type,section):
        prefix=line[:start]
        # Analyze the governing sentence, not a fixed 100-character window.
        left=max((m.end() for m in re.finditer(r'[.!?;]|\b(?:но|зато)\b',prefix,re.I)),default=0)
        boundary=re.search(r'[.!?;]|\b(?:но|зато)\b',line[end:],re.I)
        right=end+boundary.start() if boundary else len(line)
        local=line[left:start]
        suffix=line[end:right]
        clause_prefix=local.rsplit(',',1)[-1]
        clause_suffix=suffix.split(',',1)[0]
        # Explicit exclusion and historical/other-person roles take priority.
        recruitment=list(RECRUIT.finditer(local))
        last_cue=recruitment[-1] if recruitment else None
        negated_cue=last_cue and re.search(r'\bне\s*$',local[:last_cue.start()],re.I)
        if negated_cue or re.search(r'\bне\s*$',clause_prefix,re.I) or re.match(r'^\s*(?:нам\s+|мы\s+)?не\s+(?:нуж\w*|требу\w*|ищем|ищу)',clause_suffix,re.I):
            return 'negated','explicit_negation'
        history=list(HISTORY.finditer(local))
        current=list((SEEK if post_type=='resume' else RECRUIT).finditer(local))
        if (history and (not current or history[-1].start()>current[-1].start())) or (section=='history' and not current):
            return 'historical','past_experience'
        governed=local[last_cue.end():] if last_cue else local
        if COLLEAGUE.search(governed[-75:]):return 'other_person','collaboration_context'
        if re.search(r'опыт\w*\s+(?:работы\s+)?(?:в\s+качестве\s+|на\s+позиции\s+)?$|(?:помогать|помощь|взаимодействовать|руководить)\s+(?:команде\s+)?$|(?:в\s+команде|у\s+нас\s+работают)\s+\d*\s*$',governed,re.I):
            return 'other_person','experience_or_team_context'
        if re.search(r'находить\s+исполнителей|подбор\s+исполнителей|нанимать|вам\s+будут\s+нужны|для\s+реализации\s+целей',governed,re.I):
            return 'other_person','staffing_duty'
        if post_type=='vacancy' and RECRUIT.search(local):
            return 'target','recruitment_cue'
        if post_type=='vacancy' and re.match(r'^\s*(?:нам\s+|в\s+команду\s+)?(?:нужен|нужна|нужны|требуется|требуются)\b',suffix,re.I):
            return 'target','recruitment_after_title'
        if post_type=='resume' and SEEK.search(local):
            return 'target','job_search_cue'
        if post_type=='resume' and SELF.search(local[-60:]):
            return 'self_description','self_identification'
        if re.search(r'(?:на\s+(?:позицию|должность)|должность|специальность|позиция)\s*[:—–-]?\s*$',local,re.I):
            return 'target','explicit_position_label'
        if post_type=='resume' and RECRUIT.search(local) and not SEEK.search(local):
            return 'other_person','recruiting_in_resume'
        if post_type=='resume' and re.search(r'(?:по\s+профессии|моя\s+специальность|моя\s+профессия)\s*[:—–-]?\s*$',local,re.I):
            return 'self_description','explicit_profession'
        if section=='target' and len(self.normalizer.tokens(local))<=6:
            return 'target','list_after_target_heading'
        # Do not promote occupation words buried in requirements or duties.
        prefix_words=[t[0] for t in self.normalizer.tokens(prefix)]
        prefix_has_verb=any(re.fullmatch('[а-я]+',w) and self.normalizer.pos(w) in ('VERB','INFN') for w in prefix_words)
        short_title=(line_number<=6 and section=='header' and len(prefix_words)<=6 and not prefix_has_verb
                     and not re.search(r'опыт|навык|знани|владени|владею|использую|умею|взаимодейств|сотруднич|работа\s+с|курсы|обучени|приглаш[её]н|команд|компани|\d',prefix,re.I))
        if short_title:return 'target','short_header'
        return 'incidental','no_target_evidence'

    def extract(self,text,post_type):
        if not isinstance(text,str):raise TypeError('text must be a string')
        if post_type not in ('vacancy','resume'):raise ValueError('Expected vacancy or resume')
        mentions=[];offset=0;line_number=0;section='header'
        for raw in text.splitlines(keepends=True):
            line=raw.rstrip('\r\n')
            if line.strip():line_number+=1
            clean=re.sub(r'^[\W_]+','',line)
            if re.match(r'опыт\s+работы|предыдущ\w*\s+работ',clean,re.I):section='history'
            elif SECTION_OTHER.search(clean):section='body'
            elif SEEK.search(clean) or RECRUIT.match(clean):section='header'
            if re.search(r'(?:ищем|требуются|желаемая\s+должность|ищу\s+работу)\s*:\s*$',clean,re.I):
                section='target'
            elif not line.strip() and section=='target':section='body'
            tokens=self.normalizer.tokens(line)
            excluded=[m.span() for m in URL.finditer(line)]
            i=0
            while i<len(tokens):
                node=self.trie;j=i;best=None
                while j<len(tokens) and tokens[j][0] in node:
                    if j>i and re.search(r'[,;.!?]',line[tokens[j-1][2]:tokens[j][1]]):break
                    node=node[tokens[j][0]]
                    if '$' in node:best=(j+1,node['$'])
                    j+=1
                if best:
                    j,identity=best;start,end=tokens[i][1],tokens[j-1][2]
                    if not any(start<b and end>a for a,b in excluded):
                        role,rule=self._role(line,start,end,line_number,post_type,section)
                        entry=self.entries[identity]
                        mentions.append({'occupation_id':identity,'name':entry['name'],'group':entry['group'],
                                         'raw':text[offset+start:offset+end],'start':offset+start,'end':offset+end,
                                         'role':role,'rule_id':rule,'context':line})
                    i=j
                else:i+=1
            offset+=len(raw)
        accepted={m['occupation_id'] for m in mentions if m['role'] in ('target','self_description')}
        # A more specific target replaces its generic parent in the canonical list.
        parents=set()
        for identity in accepted:
            parent=self.entries[identity].get('parent_id')
            chain={identity}
            while parent:
                if parent in chain:raise ValueError('Cyclic occupation hierarchy')
                chain.add(parent);parents.add(parent)
                parent=self.entries[parent].get('parent_id')
        chosen=sorted(accepted-parents)
        for m in mentions:
            if m['occupation_id'] in parents and m['role'] in ('target','self_description'):
                m['role']='broader_target'
        status='single' if len(chosen)==1 else 'multiple' if chosen else 'needs_review' if mentions else 'not_found'
        return {'extractor_version':VERSION,'lexicon_version':self.lexicon['version'],
                'occupation_ids':chosen,'occupation_names':[self.entries[i]['name'] for i in chosen],
                'primary_occupation_id':chosen[0] if len(chosen)==1 else None,
                'status':status,'mentions':mentions}
