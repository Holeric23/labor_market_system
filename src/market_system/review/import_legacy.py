"""One-time read-only import of review provenance and type-only training labels."""
from contextlib import closing
import csv
import hashlib
import json

from market_system.paths import local_path
from market_system.runtime import load_config
from market_system.sqlite import connect as sqlite_connect
from market_system.search.store import add_class, encode, now
from .projection import normalized, ensure_current, switch
from .store import owner


def sha(path):
    return hashlib.sha256(local_path(path).read_bytes()).hexdigest()


def register(con,identity,origin,manifest):
    encoded=encode(manifest)
    old=con.execute('SELECT manifest_json FROM review_datasets WHERE dataset_id=?',(identity,)).fetchone()
    if old:
        if old[0]!=encoded:
            raise ValueError('Imported source changed; preserve it as a separate dataset')
        return False
    con.execute('INSERT INTO review_datasets VALUES(?,?,?,?)',(identity,origin,encoded,now()))
    return True


def import_legacy(review,actor,paths=None,learning_files=None):
    owner(actor)
    paths=load_config() if paths is None else paths
    dataset_path=local_path(paths['dataset']); annotations_path=local_path(paths['annotation_database'])
    hashes={str(dataset_path):sha(dataset_path),str(annotations_path):sha(annotations_path)}
    dataset=json.loads(dataset_path.read_text(encoding='utf-8'))
    with closing(sqlite_connect(annotations_path.as_uri()+'?mode=ro',uri=True)) as source:
        source.execute('PRAGMA query_only=ON')
        revisions=source.execute('SELECT id,revision,status,annotation,saved_at FROM revisions WHERE dataset=? ORDER BY id,revision',(dataset['dataset_id'],)).fetchall()
    model=local_path(paths['model'])
    files=list(learning_files) if learning_files is not None else sorted(local_path('data/learning_data').glob('*.csv'))
    if (model/'labels.csv').is_file() and model/'labels.csv' not in files:
        files.append(model/'labels.csv')
    label_sources=[]
    for path in files:
        path=local_path(path)
        checksum=sha(path)
        with path.open(encoding='utf-8-sig',newline='') as file:
            rows=list(csv.DictReader(file))
        hashes[str(path)]=checksum
        label_sources.append(('labels:'+hashlib.sha256((str(path)+checksum).encode()).hexdigest(),str(path),{'sha256':checksum,'scope':'post_type_only'},rows,False))
    split=model/'split.csv'
    if split.exists():
        checksum=sha(split)
        with split.open(encoding='utf-8-sig',newline='') as file:
            rows=list(csv.DictReader(file))
        hashes[str(split)]=checksum
        label_sources.append(('split:'+hashlib.sha256((str(split)+checksum).encode()).hexdigest(),str(split),{'sha256':checksum,'scope':'post_type_only; original model split'},rows,True))
    with review.jobs.transaction() as con:
        identities={r['text_hash']:r['id'] for r in con.execute('SELECT id,text_hash FROM contents')}
        manifest={'dataset_sha256':hashes[str(dataset_path)],'annotations_sha256':hashes[str(annotations_path)],
                  'source':str(dataset_path),'annotations':str(annotations_path),'scope':dataset.get('scope'),
                  'classifier_version':dataset.get('classifier_version'),'original_author':'unknown'}
        if register(con,dataset['dataset_id'],str(dataset_path),manifest):
            definitions={}
            for kind,key in [('skill','skills'),('occupation','occupations')]:
                for term in dataset[key]:
                    definitions[(kind,term['id'])]=add_class(con,kind+':review-dataset:'+dataset['dataset_id'],term['id'],term['name'],dataset['dataset_id'])
            known={item['id'] for item in dataset['items']}
            for item in dataset['items']:
                con.execute('INSERT INTO review_memberships VALUES(?,?,?,?,?)',
                            (dataset['dataset_id'],item['id'],identities.get(item['id']),item['cohort'],str(item.get('group')) if item.get('group') is not None else None))
            for external,revision,status,encoded,saved_at in revisions:
                if external not in known or external not in identities:
                    raise ValueError('Review document is missing from the copied corpus')
                original=json.loads(encoded)
                value={'post_type':original['post_type'],
                       'occupations':[definitions[('occupation',key)] for key in original['occupations']],
                       'skills':[{'class_id':definitions[('skill',s['id'])],'role':s['role']} for s in original['skills']],
                       'salary':original['salary'],'conditions':original['conditions'],
                       'multiple_offers':False,'pending_terms':[],'comment':original.get('comment','')}
                value=normalized(con,value,status)
                cid=identities[external]
                number=con.execute('SELECT COALESCE(MAX(revision),0)+1 FROM review_revisions WHERE content_id=?',(cid,)).fetchone()[0]
                con.execute('INSERT INTO review_revisions VALUES(?,?,?,?,?,?,?)',
                            (cid,number,status,None,saved_at,encode(value),encode({'source':'legacy-review','dataset_id':dataset['dataset_id'],
                             'external_id':external,'original_revision':revision,'original_status':status,'original_annotation':original,'author_known':False})))
        for identity,path,manifest,rows,split_file in label_sources:
            if not register(con,identity,path,manifest):
                continue
            for row in rows:
                external=row['text_hash'];cid=identities.get(external)
                cohort=row['split'] if split_file else 'unspecified'
                if split_file and cohort not in ('train','development','test'):
                    raise ValueError('Unknown original split')
                con.execute('INSERT OR IGNORE INTO review_memberships VALUES(?,?,?,?,NULL)',(identity,external,cid,cohort))
                prior=con.execute('SELECT label FROM review_type_labels WHERE dataset_id=? AND external_id=?',(identity,external)).fetchone()
                if prior and prior[0]!=row['label']:
                    raise ValueError('Conflicting original type labels')
                con.execute('INSERT OR IGNORE INTO review_type_labels VALUES(?,?,?,?)',(identity,external,cid,row['label']))
        for path,expected in hashes.items():
            if sha(path)!=expected:
                raise ValueError('Source changed during import')
        active=con.execute('SELECT result_set_id FROM active_result_set WHERE singleton=1').fetchone()
        if active:
            switch(con,ensure_current(con,active[0]))
    return {'source_hashes':hashes,'review_dataset':dataset['dataset_id'],'source_revisions':len(revisions)}
