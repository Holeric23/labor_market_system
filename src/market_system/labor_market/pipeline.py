"""Classification followed by existing skill and salary extractors."""
from copy import deepcopy
import hashlib

import numpy as np
import pandas as pd

from market_system.core.extraction.skills import SkillExtractor, VERSION as SKILLS_VERSION
from market_system.core.extraction.salary import extract_salary, EXTRACTOR_VERSION as SALARY_VERSION
from market_system.core.extraction.occupations import OccupationExtractor, VERSION as OCCUPATION_VERSION
from market_system.core.extraction.work_conditions import extract_work_conditions, VERSION as CONDITIONS_VERSION, FIELDS as CONDITION_FIELDS
from .model import Ensemble, clean

ROLES = ('required', 'preferred', 'possessed', 'task', 'learning', 'negated', 'not_required', 'mentioned')
FIELDS = ('label', 'prob_vac', 'prob_res', 'classification_status', 'model_version',
          'extraction_status', 'skills', 'skill_names', 'skill_mentions', 'skill_extractor_version',
          'skill_lexicon_version', 'skill_lexicon_sha256',
          *(role + '_skills' for role in ROLES), 'salary_status', 'salary_from', 'salary_to',
          'currency', 'salary_period', 'tax_mode', 'salary_mentions', 'salary_extractor_version',
          'occupation_ids', 'occupation_names', 'primary_occupation_id', 'occupation_status', 'occupation_mentions',
          'occupation_extractor_version', 'occupation_lexicon_version', 'occupation_lexicon_sha256',
          'work_conditions', 'work_condition_mentions', 'work_conditions_extractor_version',
          *(name for field in CONDITION_FIELDS for name in (field,field+'_values',field+'_status')))


class LaborMarketPipeline:
    """Load once, reuse. Does not train, write to DB, or select an interpreter."""
    def __init__(self, classifier, skill_lexicon=None, occupation_lexicon=None):
        self.classifier = classifier
        kwargs = {'lexicon_path': skill_lexicon} if skill_lexicon is not None else {}
        self.extractor = SkillExtractor(**kwargs)
        self.lexicon_hash = hashlib.sha256(self.extractor.lexicon_path.read_bytes()).hexdigest()
        self.occupations = OccupationExtractor(**({'lexicon_path':occupation_lexicon} if occupation_lexicon else {}))
        self.occupation_hash = hashlib.sha256(self.occupations.lexicon_path.read_bytes()).hexdigest()

    @classmethod
    def load(cls, model_dir, *, skill_lexicon=None, occupation_lexicon=None):
        classifier = Ensemble.load(model_dir)
        return cls(classifier, skill_lexicon or classifier.skill_lexicon,
                   occupation_lexicon or classifier.occupation_lexicon)

    def _blank(self, status):
        row = dict.fromkeys(FIELDS)
        row.update(classification_status=status, extraction_status='skipped',
                   model_version=self.classifier.version, salary_status='skipped',
                   skills=[], skill_names=[], skill_mentions=[], salary_mentions=[],
                   skill_extractor_version=SKILLS_VERSION, salary_extractor_version=SALARY_VERSION,
                   skill_lexicon_version=self.extractor.lexicon['version'],
                   skill_lexicon_sha256=self.lexicon_hash)
        row.update(occupation_ids=[],occupation_names=[],occupation_status='skipped',occupation_mentions=[],
                   occupation_extractor_version=OCCUPATION_VERSION,
                   occupation_lexicon_version=self.occupations.lexicon['version'],occupation_lexicon_sha256=self.occupation_hash,
                   work_conditions={},work_condition_mentions=[],work_conditions_extractor_version=CONDITIONS_VERSION)
        for field in CONDITION_FIELDS:
            row[field+'_values']=[]
            row[field+'_status']='skipped'
        for role in ROLES:
            row[role + '_skills'] = []
        return row

    def _rows(self, values, batch_size):
        rows = [self._blank('invalid_text') for _ in values]
        valid = [i for i, text in enumerate(values) if isinstance(text, str) and clean(text).strip()]
        # Positional assignment preserves duplicated and MultiIndex row labels.
        for start in range(0, len(valid), batch_size):
            positions = valid[start:start + batch_size]
            texts = [values[i] for i in positions]
            probs = self.classifier.probabilities(texts)
            if any(np.asarray(probs[name]).shape != (len(texts),) for name in ('vacancy','resume')):
                raise ValueError('Classifier returned invalid batch dimensions')
            for j, i in enumerate(positions):
                pv, pr = float(probs['vacancy'][j]), float(probs['resume'][j])
                label = self.classifier.label(pv, pr)
                row = rows[i]
                row.update(label=label, prob_vac=pv, prob_res=pr,
                           classification_status='confident' if label else 'unlabeled')
                if label is None:
                    continue
                skills = self.extractor.extract(values[i], label)
                for role, ids in skills['skills_by_role'].items():
                    row[role + '_skills'] = ids
                # Tasks remain separate; demand is an OR union, never a sum.
                ids = sorted(set(row['required_skills']) | set(row['preferred_skills'])) if label == 'vacancy' else row['possessed_skills'][:]
                row.update(skills=ids, skill_names=[self.extractor.skills[s]['name'] for s in ids],
                           skill_mentions=skills['mentions'], extraction_status='processed')
                salary = extract_salary(values[i])
                row.update({k: salary[k] for k in ('salary_from','salary_to','currency','salary_period','tax_mode')})
                row.update(salary_status=salary['status'], salary_mentions=salary['mentions'])
                occupation=self.occupations.extract(values[i],label)
                for key in ('occupation_ids','occupation_names','primary_occupation_id'):
                    row[key]=occupation[key]
                row.update(occupation_status=occupation['status'],occupation_mentions=occupation['mentions'])
                conditions=extract_work_conditions(values[i],label)
                row.update(work_conditions={field:conditions[field] for field in CONDITION_FIELDS},
                           work_condition_mentions=conditions['mentions'])
                for field in CONDITION_FIELDS:
                    row[field]=conditions[field]['value']
                    row[field+'_values']=conditions[field]['values']
                    row[field+'_status']=conditions[field]['status']
        return rows

    def analyze(self, text):
        """One string (including df.loc[row, 'text']) -> dict with original text."""
        return {'text': text, **self._rows([text], 1)[0]}

    def __call__(self, data, **kwargs):
        """Convenient single entry point: string -> dict; Series/DataFrame -> frame."""
        if isinstance(data, str):
            if kwargs:
                raise TypeError('Batch options apply only to Series/DataFrame')
            return self.analyze(data)
        return self.transform(data, **kwargs)

    def transform(self, data, *, text_column='text', prefix='lm_', batch_size=256):
        """DataFrame or text Series -> a new DataFrame, preserving row order/index.

        Prefix avoids overwriting human labels. Existing output columns cause an
        error; choose another prefix explicitly. Series always means many texts,
        never an implicitly guessed DataFrame row.
        """
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError('batch_size must be a positive integer')
        if not isinstance(prefix, str):
            raise TypeError('prefix must be a string')
        if isinstance(data, pd.Series):
            frame = data.to_frame(name=text_column).copy()
        elif isinstance(data, pd.DataFrame):
            frame = data.copy(deep=True)
        else:
            raise TypeError('Expected DataFrame or Series; use analyze(text) for a scalar')
        if not frame.columns.is_unique or text_column not in frame.columns:
            raise ValueError('Unique columns and the text column are required')
        names = [prefix + name for name in FIELDS]
        if set(names) & set(frame.columns):
            raise ValueError('Output columns already exist; choose a different prefix')
        rows = self._rows(frame[text_column].tolist(), batch_size)
        for field, name in zip(FIELDS, names):
            # object arrays retain None and nested lists even for empty batches.
            column = np.empty(len(rows), dtype=object)
            for i, row in enumerate(rows):
                column[i] = deepcopy(row[field])
            frame[name] = pd.Series(column, index=frame.index, dtype=object)
        return frame
