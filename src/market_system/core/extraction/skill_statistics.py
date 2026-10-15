"""One canonical skill per row; union documents across roles rather than sum counts."""

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path

CONTEXTUAL={'required','preferred','possessed','task'}


class SkillStatistics:
    def __init__(self, lexicon):
        self.skills={s['id']:s for s in lexicon['skills']}
        self.counts=Counter()
        self.roles=Counter()
        self.seen=set()

    def observe(self, text_hash, post_type, mentions):
        if text_hash in self.seen:raise ValueError(f'Duplicate text in statistics: {text_hash}')
        if post_type not in ('vacancy','resume'):raise ValueError('Unknown post type')
        self.seen.add(text_hash)
        pairs={(m['skill_id'],m['role']) for m in mentions}
        if any(skill not in self.skills for skill,_ in pairs):raise ValueError('Skill missing from run lexicon')
        for skill,role in pairs:self.roles[(post_type,role,skill)]+=1
        for skill in {s for s,_ in pairs}:
            roles={r for s,r in pairs if s==skill}
            self.counts[(skill,'unique_texts_total')]+=1
            self.counts[(skill,post_type+'_any_texts')]+=1
            if roles & CONTEXTUAL:
                self.counts[(skill,'contextual_texts_total')]+=1
                self.counts[(skill,post_type+'_contextual_texts')]+=1
            if post_type=='vacancy':
                for role in ['required','preferred','task']:
                    if role in roles:self.counts[(skill,'vacancy_'+role+'_texts')]+=1
                if roles & {'required','preferred'}:self.counts[(skill,'vacancy_demand_texts')]+=1
            elif 'possessed' in roles:self.counts[(skill,'resume_possessed_texts')]+=1

    def write(self, output):
        output=Path(output);output.mkdir(parents=True,exist_ok=True)
        metrics=['unique_texts_total','contextual_texts_total','vacancy_any_texts','resume_any_texts',
                 'vacancy_contextual_texts','resume_contextual_texts','vacancy_required_texts',
                 'vacancy_preferred_texts','vacancy_demand_texts','vacancy_task_texts','resume_possessed_texts']
        with (output/'skill_frequencies_summary.csv').open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=['skill_id','name','category',*metrics]);w.writeheader()
            for skill in sorted(self.skills,key=lambda k:(-self.counts[k,'unique_texts_total'],k)):
                item=self.skills[skill]
                w.writerow({'skill_id':skill,'name':item['name'],'category':item['category'],
                            **{m:self.counts[skill,m] for m in metrics}})
        with (output/'skill_frequencies.csv').open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.writer(f);w.writerow(['post_type','role','skill_id','name','document_count'])
            for (post_type,role,skill),count in sorted(self.roles.items(),key=lambda kv:(-kv[1],kv[0])):
                w.writerow([post_type,role,skill,self.skills[skill]['name'],count])


def rebuild(run_dir):
    run_dir=Path(run_dir)
    csv.field_size_limit(max(csv.field_size_limit(),10000000))
    stats=SkillStatistics(json.loads((run_dir/'skill_lexicon.json').read_text(encoding='utf-8')))
    digest=hashlib.sha256()
    with (run_dir/'skills_structured.csv').open(encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f):
            stats.observe(row['text_hash'],row['post_type'],json.loads(row['mentions_json']))
            digest.update(row['text_hash'].encode()+b'\n')
    stats.write(run_dir)
    (run_dir/'skill_statistics_metadata.json').write_text(json.dumps({
        'version':'role_frequencies_with_summary_v3','documents':len(stats.seen),'skills':len(stats.skills),
        'text_hash_sequence_sha256':digest.hexdigest(),
        'note':'skill_frequencies.csv: post type, role and skill; skill_frequencies_summary.csv: unique document unions.'
    },indent=2),encoding='utf-8')
    return len(stats.seen)


if __name__=='__main__':
    from market_system.runtime import enable_guard
    enable_guard()
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('run_dir',type=Path)

    print('Rebuilt statistics for documents:',rebuild(p.parse_args().run_dir))
