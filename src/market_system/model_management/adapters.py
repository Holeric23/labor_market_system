"""Allow-listed formats. Research score arrays are not executable model bundles."""
import hashlib
import importlib.metadata
import json
import math
import re
from pathlib import Path

from market_system.paths import local_path
from market_system.search.store import digest

ADAPTER = 'ensemble-v1'


def sha256(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            checksum.update(block)
    return checksum.hexdigest()


class IncompatibleArtifact(ValueError):
    pass


def verify(spec):
    if spec.get('adapter', ADAPTER) != ADAPTER:
        raise IncompatibleArtifact('Неподдерживаемый адаптер. Проверен только ensemble-v1; research joblib/npz не загружаются.')
    try:
        meta = spec['model_manifest']
        required = {'model_version','weight_lr','weight_cb','vacancy_threshold','resume_threshold',
                    'max_other_probability','decision_policy','packages','artifacts_sha256'}
        if not required <= meta.keys() or meta.get('ensemble') != 'arithmetic':
            raise IncompatibleArtifact('Непроверенный формат Ensemble: отсутствуют обязательные метаданные.')
        if meta['model_version'] != spec['model_version']:
            raise IncompatibleArtifact('Версия модели не совпадает с manifest.')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}',meta['model_version']):
            raise IncompatibleArtifact('Некорректный ID версии модели.')
        if meta['decision_policy'] != 'strict_target_threshold_and_low_other_else_null':
            raise IncompatibleArtifact('Непроверенная политика принятия решений Ensemble.')
        weights = [meta['weight_lr'],meta['weight_cb']]
        if any(type(w) not in (int,float) or not math.isfinite(w) or w<0 for w in weights) or not math.isclose(sum(weights),1):
            raise IncompatibleArtifact('Некорректные веса Ensemble.')
        for field in ('vacancy_threshold','resume_threshold','max_other_probability'):
            if type(meta[field]) not in (int,float) or not math.isfinite(meta[field]) or not 0<=meta[field]<=1:
                raise IncompatibleArtifact('Некорректный порог Ensemble: '+field)
        if meta.get('preprocessing') not in (None,{'operation':'re.sub','pattern':r'http\S+|www\S+|https\S+',
                                                  'replacement':'','flags':'MULTILINE'}):
            raise IncompatibleArtifact('Непроверенный препроцессинг Ensemble.')
        model = local_path(spec['paths']['model'])
        if json.loads((model/'metadata.json').read_text(encoding='utf-8')) != meta:
            raise IncompatibleArtifact('Метаданные модели изменились после регистрации.')
        heads = meta.get('catboost_files', {'vacancy':['vacancy.cbm'], 'resume':['resume.cbm']})
        if set(heads) != {'vacancy','resume'} or any(not isinstance(v,list) or not v for v in heads.values()):
            raise IncompatibleArtifact('Непроверенный формат голов Ensemble.')
        needed = {'linear.joblib', *[name for names in heads.values() for name in names]}
        if not needed <= meta['artifacts_sha256'].keys():
            raise IncompatibleArtifact('Нет контрольных сумм файлов модели.')
        for name, expected in meta['artifacts_sha256'].items():
            path = local_path(model/name)
            if path.parent != model or spec['files'].get(str(path)) != expected:
                raise IncompatibleArtifact('Некорректный путь/контрольная сумма артефакта: '+name)
        for name, expected in spec['files'].items():
            if sha256(local_path(name)) != expected:
                raise IncompatibleArtifact('Изменился фиксированный артефакт/словарь/код: '+name)
        for package in ('numpy','pandas','scikit-learn','catboost','joblib'):
            expected = meta['packages'][package]
            actual = importlib.metadata.version(package)
            if actual != expected:
                raise IncompatibleArtifact(f'Несовместимая зависимость {package}: требуется {expected}, установлена {actual}.')
        if spec['fingerprint'] != digest(spec['files']):
            raise IncompatibleArtifact('Повреждён fingerprint модели.')
    except (KeyError, TypeError, OSError, AttributeError, importlib.metadata.PackageNotFoundError) as exc:
        raise IncompatibleArtifact('Неполный или несовместимый локальный комплект Ensemble: '+str(exc)) from exc


def class_space(spec):
    result = [{'namespace':'post_type:'+spec['model_version'], 'key':key, 'name':key,
               'version':spec['model_version'], 'definition':{'meaning':meaning}}
              for key, meaning in [('vacancy','Объявление о найме'),('resume','Предложение труда соискателем')]]
    for kind, field, items in [('skill','skill_lexicon','skills'),('occupation','occupation_lexicon','occupations')]:
        path = local_path(spec['paths'][field])
        value = json.loads(path.read_text(encoding='utf-8'))
        checksum = spec['files'][str(path)] if kind == 'skill' else digest(value)
        seen = set()
        for item in value[items]:
            if not isinstance(item.get('id'),str) or not item['id'] or item['id'] in seen or not item.get('name'):
                raise IncompatibleArtifact('Некорректные определения классов в словаре.')
            seen.add(item['id'])
            result.append({'namespace':kind+':'+checksum,'key':item['id'],'name':item['name'],
                           'version':value['version'],'definition':item})
    return result


class Predictor:
    def __init__(self, pipeline):
        self.pipeline = pipeline

    def predict(self, texts):
        import pandas as pd
        return self.pipeline.transform(pd.Series(list(texts),dtype=object),prefix='',batch_size=max(1,len(texts))).drop(columns=['text']).to_dict('records')


def load(spec):
    verify(spec)
    from market_system.labor_market import LaborMarketPipeline
    try:
        paths = spec['paths']
        pipeline = LaborMarketPipeline.load(paths['model'],skill_lexicon=paths['skill_lexicon'],
                                            occupation_lexicon=paths['occupation_lexicon'])
    except Exception as exc:
        raise IncompatibleArtifact('Артефакт Ensemble не прошёл загрузку: '+str(exc)) from exc
    return Predictor(pipeline)
