"""Projection of pipeline rows, preserving machine evidence and confirmed revisions."""
from copy import deepcopy
import json
from market_system.search.store import add_class, encode

SALARY = ('salary_from','salary_to','currency','salary_period','tax_mode')
CONDITIONS = ('work_format','schedule','employment_type','working_hours','hours_per_week')


def project(con, job, item, machine):
    result_id, cid = job['result_set_id'], item['content_id']
    row = deepcopy(machine)
    spec = json.loads(job['parameters_json'])['request']['spec']
    if 'class_space' in spec:
        # A model cannot silently enlarge or redefine the submitted class space.
        allowed = {(x['namespace'],x['key']) for x in spec['class_space']}
        for kind, keys, checksum in [
            ('skill',set(machine.get('skills',[])) | set(machine.get('required_skills',[])) |
             set(machine.get('preferred_skills',[])) | set(machine.get('possessed_skills',[])),machine['skill_lexicon_sha256']),
            ('occupation',machine.get('occupation_ids',[]),spec['legacy_versions']['occupation_dictionary_sha256'])]:
            if any((kind+':'+checksum,key) not in allowed for key in keys):
                raise ValueError('Analyzer returned a class outside the frozen class space')
    correction = json.loads(item['correction_json']) if item['correction_json'] else None
    skill_names = dict(zip(row.get('skills',[]),row.get('skill_names',[])))
    occupation_names = dict(zip(row.get('occupation_ids',[]),row.get('occupation_names',[])))
    if correction:
        annotation = correction['annotation']
        row['label'] = annotation['post_type'] if annotation['post_type'] in ('vacancy','resume') else None
        row['occupation_ids'] = annotation['occupations']
        for role in ('required','preferred','possessed'):
            row[role+'_skills'] = [s['id'] for s in annotation['skills'] if s['role']==role]
        for field in SALARY:
            row[field] = annotation['salary'][0][field] if len(annotation['salary'])==1 else None
        row['salary_status'] = 'needs_review' if len(annotation['salary'])>1 else 'confirmed'
        for field in CONDITIONS:
            values = annotation['conditions'][field]
            row[field] = values[0] if len(values)==1 else None
        skill_names.update({s['id']:s['name'] for s in correction['skills']})
        occupation_names.update({o['id']:o['name'] for o in correction['occupations']})
    label = row.get('label')
    if label not in (None,'vacancy','resume'):
        raise ValueError('Invalid pipeline label')
    links = set()
    if label:
        links.add((add_class(con,'post_type:'+job['model_version'],label,label,job['model_version']),'none'))
        for role in (('required','preferred') if label=='vacancy' else ('possessed',)):
            for key in row.get(role+'_skills',[]):
                # Missing names must not silently create a different definition for an existing ID.
                namespace = 'skill:'+row['skill_lexicon_sha256']
                old = con.execute('SELECT name FROM classes WHERE namespace=? AND class_key=?',(namespace,key)).fetchone()
                name = skill_names.get(key) or (old[0] if old else key)
                links.add((add_class(con,namespace,key,name,row['skill_lexicon_version']),role))
        for key in row.get('occupation_ids',[]):
            namespace = 'occupation:'+spec.get('legacy_versions',{}).get('occupation_dictionary_sha256',row['occupation_lexicon_sha256'])
            old = con.execute('SELECT name FROM classes WHERE namespace=? AND class_key=?',(namespace,key)).fetchone()
            name = occupation_names.get(key) or (old[0] if old else key)
            links.add((add_class(con,namespace,key,name,row['occupation_lexicon_version']),'none'))
    # Replace the whole machine projection, including removed/negated links.
    for table in ('document_classes','search_catalog','result_evidence','result_publications','document_processing'):
        con.execute(f'DELETE FROM {table} WHERE result_set_id=? AND content_id=?',(result_id,cid))
    con.execute('INSERT INTO document_processing VALUES(?,?,?)',(result_id,cid,'processed' if links else 'no_classes'))
    con.executemany('INSERT INTO document_classes VALUES(?,?,?,?,NULL)',[(result_id,cl,cid,role) for cl,role in sorted(links)])
    pubs = con.execute('SELECT publication_id,published_at_utc FROM job_publications WHERE job_id=? AND content_id=?',
                       (job['job_id'],cid)).fetchall()
    con.executemany('INSERT INTO result_publications VALUES(?,?,?,?)',[(result_id,cid,p[0],p[1]) for p in pubs])
    dates = [p[1] for p in pubs if p[1]]
    ambiguous = len(row.get('occupation_ids',[]))>1 or row.get('salary_status')=='needs_review'
    values = [result_id,cid,label,max(dates) if dates else None,len(pubs),
              *[row.get(k) if not ambiguous and label else None for k in SALARY],
              *[row.get(k) if label else None for k in CONDITIONS],
              'needs_review' if ambiguous else 'unreviewed','unattributed' if ambiguous else 'document']
    con.execute('INSERT INTO search_catalog VALUES('+','.join('?' for _ in values)+')',values)
    con.execute('INSERT INTO result_evidence VALUES(?,?,?,?,?)',
                (result_id,cid,encode({'model_version':job['model_version'],'label':machine.get('label'),
                                      'prob_vac':machine.get('prob_vac'),'prob_res':machine.get('prob_res'),
                                      'classification_status':machine.get('classification_status')}),
                 encode({'machine':machine,'confirmed_correction':correction,'effective':row}),
                 encode(machine.get('skill_mentions',[]))))
