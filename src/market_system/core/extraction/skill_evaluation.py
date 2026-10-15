"""Evaluate human-annotated document-level (skill_id, role) sets, not coverage."""

import argparse
import csv
import json
from pathlib import Path

ROLES={'required','preferred','possessed','task','learning','negated','not_required','mentioned'}


def evaluate(path):
    csv.field_size_limit(max(csv.field_size_limit(), 10_000_000))
    counts={role:{'tp':0,'fp':0,'fn':0} for role in ROLES}
    reviewed=skipped=0
    with Path(path).open(encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f):
            if row['review_status'] not in ('correct','incorrect') or not row['expected_skills_json'].strip():
                skipped+=1;continue
            expected=json.loads(row['expected_skills_json'])
            if not isinstance(expected,list):raise ValueError('Expected a list of skill/role objects')
            predicted=json.loads(row['mentions_json'])
            if any(m['role'] not in ROLES for m in expected+predicted):raise ValueError('Unknown role')
            reviewed+=1
            for role in ROLES:
                gold={m['skill_id'] for m in expected if m['role']==role}
                actual={m['skill_id'] for m in predicted if m['role']==role}
                counts[role]['tp']+=len(gold&actual)
                counts[role]['fp']+=len(actual-gold)
                counts[role]['fn']+=len(gold-actual)
    if not reviewed:raise ValueError('No reviewed rows with explicit gold annotations; cannot compute quality')
    def metrics(c):
        tp,fp,fn=c['tp'],c['fp'],c['fn']
        return {**c,'precision':tp/(tp+fp) if tp+fp else None,
                'recall':tp/(tp+fn) if tp+fn else None,
                'f1':2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None}
    total={k:sum(c[k] for c in counts.values()) for k in ['tp','fp','fn']}
    return {'reviewed_documents':reviewed,'skipped_documents':skipped,
            'unit':'unique skill_id + role per document','micro':metrics(total),
            'by_role':{r:metrics(c) for r,c in sorted(counts.items())}}


if __name__=='__main__':
    from market_system.runtime import enable_guard
    enable_guard()
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('audit',type=Path)

    args=p.parse_args();print(json.dumps(evaluate(args.audit),ensure_ascii=False,indent=2))
