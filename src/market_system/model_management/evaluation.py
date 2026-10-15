"""Small, fixed-parameter evaluations. Unknown field truth is never a negative."""
import json
import time
from .adapters import load

FIELDS = ('salary_from','salary_to','currency','salary_period','tax_mode',
          'work_format','schedule','employment_type','working_hours','hours_per_week')
LIMITATION = 'Малая целевая выборка указанных источников. Ноль ошибок validation не гарантирует качество. Исторический test повторно исследован и не является новым независимым аудитом.'


def counts(tp, fp, fn, total, tn=0):
    return {'tp':tp,'fp':fp,'fn':fn,'true_absence':tn,'evaluated':total,
            'precision':tp/(tp+fp) if tp+fp else None,'recall':tp/(tp+fn) if tp+fn else None}


def evaluate(spec, inputs, predictor=None):
    start = time.perf_counter()
    predictor = predictor or load(spec)
    predictions = predictor.predict([i['text'] for i in inputs])
    if len(predictions) != len(inputs):
        raise ValueError('Incomplete evaluation predictions')
    classes = {}
    for label in ('vacancy','resume','trash'):
        tp = sum(p['label']==label and i['label']==label for i,p in zip(inputs,predictions))
        fp = sum(p['label']==label and i['label']!=label for i,p in zip(inputs,predictions))
        fn = sum(p['label']!=label and i['label']==label for i,p in zip(inputs,predictions))
        classes[label] = counts(tp,fp,fn,len(inputs))
    fields = {}
    known_classes = {(x['namespace'],x['key']) for x in spec['class_space']}
    for field in (*FIELDS,'skills','occupations'):
        tp = fp = fn = tn = n = omitted = present = 0
        for item, prediction in zip(inputs,predictions):
            truth = item.get('fields')
            if truth is None or field not in truth:
                omitted += 1
                continue
            if field in ('skills','occupations'):
                gold = {tuple(x) for x in truth[field]}
                if any(x[:2] not in known_classes for x in gold):
                    omitted += 1
                    continue
                if field == 'skills':
                    namespace = 'skill:'+prediction['skill_lexicon_sha256']
                    roles = ('required','preferred') if prediction['label']=='vacancy' else ('possessed',) if prediction['label']=='resume' else ()
                    pred = {(namespace,k,role) for role in roles for k in prediction.get(role+'_skills',[])}
                else:
                    namespace = 'occupation:'+spec['legacy_versions']['occupation_dictionary_sha256']
                    pred = {(namespace,k) for k in prediction.get('occupation_ids',[])}
            else:
                gold = {json.dumps(truth[field],sort_keys=True)} if truth[field] is not None else set()
                value = prediction.get(field)
                pred = {json.dumps(value,sort_keys=True)} if value is not None else set()
            tp += len(gold & pred)
            fp += len(pred-gold)
            fn += len(gold-pred)
            tn += int(not gold and not pred)
            present += int(bool(pred))
            n += 1
        fields[field] = {**counts(tp,fp,fn,n,tn),'unknown_or_incompatible_truth':omitted,
                         'coverage':present/n if n else None}
    rejected = sum(p['label'] is None for p in predictions)
    errors = [{'content_id':i['content_id'],'truth':i['label'],'prediction':p['label']}
              for i,p in zip(inputs,predictions) if i['label']!=p['label']]
    return {'sample_size':len(inputs),'precision_recall':classes,'coverage':(len(inputs)-rejected)/len(inputs) if inputs else None,
            'refusals':rejected,'fields':fields,'errors':errors,'seconds':time.perf_counter()-start,
            'limitation':LIMITATION,'training_performed':False,'parameter_selection_performed':False}
