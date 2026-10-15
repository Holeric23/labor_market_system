"""Apply reviewed alias/new-skill decisions to a NEW, validated lexicon version."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re

from .skills import SkillExtractor


def apply_decisions(proposals_path, base_path, output_path, version):
    csv.field_size_limit(max(csv.field_size_limit(), 10_000_000))
    proposals_path,base_path,output_path=map(Path,(proposals_path,base_path,output_path))
    if not version.strip():raise ValueError('A new lexicon version is required')
    if output_path.exists():raise FileExistsError(output_path)
    snapshot=proposals_path.parent/'skill_lexicon.json'
    if snapshot.exists() and snapshot.read_bytes()!=base_path.read_bytes():
        raise ValueError('Base lexicon differs from the proposal run; regenerate proposals or use its snapshot')
    extractor=SkillExtractor(base_path)
    lexicon=extractor.lexicon
    if version==lexicon['version']:raise ValueError('Version must differ from the base')
    ids=extractor.skills
    aliases=dict(extractor.aliases)
    changes=[]
    with proposals_path.open(encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f):
            decision=row['decision'].strip().lower()
            if decision in ('','reject'):continue
            if decision not in ('alias','new'):raise ValueError(f'Unknown decision: {decision}')
            phrase=row['candidate'].strip()
            target=row['target_skill_id'].strip()
            if not phrase or not re.fullmatch('[a-z][a-z0-9_]*',target):
                raise ValueError('A phrase and stable target_skill_id are required')
            normalized=extractor.normalizer.phrase(phrase)
            if normalized in aliases:
                if aliases[normalized]!=target:raise ValueError(f'Alias collision: {phrase}')
                continue
            if decision=='alias':
                if target not in ids:raise ValueError(f'Unknown target: {target}')
                ids[target]['aliases'].append(phrase)
            else:
                name=row['new_skill_name'].strip()
                category=row['new_category'].strip()
                if target in ids or not name or not category:
                    raise ValueError('New skill requires a unique id, name and category')
                normalized_name=extractor.normalizer.phrase(name)
                if normalized_name in aliases:raise ValueError(f'Canonical name collision: {name}')
                entry={'id':target,'name':name,'category':category,'aliases':[phrase]}
                lexicon['skills'].append(entry);ids[target]=entry
                aliases[normalized_name]=target
            aliases[normalized]=target
            changes.append({'decision':decision,'phrase':phrase,'skill_id':target})
    if not changes:raise ValueError('No accepted new decisions; nothing written')
    lexicon.update(version=version,parent_version=extractor.lexicon['version'],
                   parent_sha256=hashlib.sha256(base_path.read_bytes()).hexdigest(),
                   decisions_sha256=hashlib.sha256(proposals_path.read_bytes()).hexdigest(),changes=changes)
    # All collisions were checked before a new immutable file is created.
    output_path.parent.mkdir(parents=True,exist_ok=True)
    with output_path.open('x',encoding='utf-8') as f:
        json.dump(lexicon,f,ensure_ascii=False,indent=2)
    return len(changes)


if __name__=='__main__':
    from market_system.runtime import enable_guard
    enable_guard()
    p=argparse.ArgumentParser(description=__doc__)

    p.add_argument('--proposals',type=Path,required=True)
    p.add_argument('--base',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--version',required=True)
    a=p.parse_args()
    print('Accepted changes:',apply_decisions(a.proposals,a.base,a.output,a.version))
