"""Metrics for two binary scores and the abstaining production decision policy."""
import numpy as np
from sklearn.metrics import log_loss, average_precision_score

from .model import HEADS, text_key


def evaluate(model, frame):
    texts, labels = frame['text'].tolist(), frame['label'].tolist()
    if not texts or not set(labels) <= {'vacancy', 'resume', 'trash'}:
        raise ValueError('Nonempty labeled evaluation frame required')
    scores = model.probabilities(texts)
    predictions = [model.label(a,b) for a,b in zip(scores['vacancy'],scores['resume'])]
    assigned = sum(p is not None for p in predictions)
    result = {'documents': len(texts), 'assigned': assigned, 'coverage': assigned/len(texts),
              'abstained': len(texts)-assigned, 'heads': {},
              'note': 'Abstention is not trash; metrics reflect this labeled sample, not the corpus.'}
    for name in HEADS:
        gold = np.array([int(y == name) for y in labels])
        pred = np.array([int(p == name) for p in predictions])
        tp = int(np.sum((gold==1)&(pred==1)))
        fp = int(np.sum((gold==0)&(pred==1)))
        fn = int(np.sum((gold==1)&(pred==0)))
        result['heads'][name] = {
            'tp':tp, 'fp':fp, 'fn':fn, 'support':int(gold.sum()),
            'precision':tp/(tp+fp) if tp+fp else None,
            'recall':tp/(tp+fn) if tp+fn else None,
            'f1':2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None,
            'log_loss':float(log_loss(gold,scores[name],labels=[0,1])),
            'average_precision':float(average_precision_score(gold,scores[name])) if gold.any() else None}
    return result


def compare(before, after, frame):
    import hashlib
    report = {'before':evaluate(before,frame),'after':evaluate(after,frame),
              'evaluation_sha256':hashlib.sha256('\n'.join(text_key(t)+':'+y for t,y in zip(frame['text'],frame['label'])).encode()).hexdigest(),
              'status':'candidate_requires_review', 'automatically_activated':False}
    report['deltas'] = {name:{metric:(report['after']['heads'][name][metric]-report['before']['heads'][name][metric])
                                     if report['after']['heads'][name][metric] is not None and report['before']['heads'][name][metric] is not None else None
                                  for metric in ('precision','recall','f1','log_loss','average_precision')}
                        for name in HEADS}
    return report
