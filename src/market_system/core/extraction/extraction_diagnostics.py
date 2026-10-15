from market_system.sqlite import connect as sqlite_connect
from market_system.paths import local_path, DATABASE_PATH
"""Observed extraction coverage charts and, only with gold, quality charts."""

import argparse
from collections import Counter,defaultdict
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .extraction_evaluation import evaluate_audit

TYPE_NAMES={'vacancy':'Вакансии','resume':'Резюме'}
STATUS_NAMES={'single':'Одна специальность','multiple':'Несколько','needs_review':'Только косвенные упоминания','not_found':'Не найдено упоминаний'}
ROLE_NAMES={'required':'Требуется','preferred':'Желательно','possessed':'Заявлен в резюме','task':'В задачах',
            'mentioned':'Просто упоминание','learning':'Обучение','not_required':'Не требуется','negated':'Отрицание'}


def _read(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))


def _table(path,rows,columns):
    with Path(path).open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=columns);writer.writeheader();writer.writerows(rows)


def build(occupation_run,db_path=None,annotations=None):
    config=Path(occupation_run).resolve()/'diagnostics'/'.mplconfig'
    config.mkdir(parents=True,exist_ok=True)
    os.environ.setdefault('MPLCONFIGDIR',str(config))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np

    csv.field_size_limit(max(csv.field_size_limit(),10000000))
    occupation_run=Path(occupation_run).resolve()
    manifest=json.loads((occupation_run/'summary.json').read_text(encoding='utf-8'))
    skills_run=Path(manifest['skills_run'])
    out=occupation_run/'diagnostics';figures=out/'figures';tables=out/'tables'
    for d in [figures,figures/'pdf',tables]:d.mkdir(parents=True,exist_ok=True)
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,'axes.spines.right':False})
    charts=[]
    def save(fig,name,caption):
        fig.text(.01,.01,caption,fontsize=8,color='#555555')
        fig.tight_layout(rect=(0,.06,1,1))
        fig.savefig(figures/(name+'.png'),dpi=150,bbox_inches='tight')
        fig.savefig(figures/'pdf'/(name+'.pdf'),bbox_inches='tight')
        plt.close(fig);charts.append(name)

    counts=Counter();skill_found=Counter();occupation_found=Counter();cardinality=Counter();flags={}
    with (skills_run/'skills_structured.csv').open(encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f):
            identity=row['text_hash'];post_type=row['post_type']
            if identity in flags:raise ValueError('Duplicate skill text')
            ids=set().union(*(set(json.loads(row[r+'_skills'])) for r in ['required','preferred','possessed','task']))
            counts[post_type]+=1;skill_found[post_type]+=bool(ids)
            bucket=str(len(ids)) if len(ids)<=5 else '6–10' if len(ids)<=10 else '11+'
            cardinality[post_type,bucket]+=1
            flags[identity]={'type':post_type,'skills':bool(ids),'occupations':False}
    occupation_seen=set()
    with (occupation_run/'occupations_structured.csv').open(encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f):
            identity=row['text_hash']
            if identity not in flags or identity in occupation_seen:raise ValueError('Incompatible extraction snapshots')
            if flags[identity]['type']!=row['post_type']:raise ValueError('Post type mismatch')
            occupation_seen.add(identity)
            found=bool(json.loads(row['occupation_ids_json']))
            occupation_found[row['post_type']]+=found;flags[identity]['occupations']=found
    if occupation_seen!=set(flags):raise ValueError('Coverage charts require matching complete snapshots')
    caption=f"Источник: сохранённые извлечения, {sum(counts.values()):,} уникальных текстов. Покрытие ≠ точность."
    coverage=[{'post_type':t,'documents':counts[t],'skills_found':skill_found[t],'occupations_found':occupation_found[t],
               'skill_coverage':skill_found[t]/counts[t] if counts[t] else None,
               'occupation_coverage':occupation_found[t]/counts[t] if counts[t] else None} for t in TYPE_NAMES if counts[t]]
    _table(tables/'coverage.csv',coverage,['post_type','documents','skills_found','occupations_found','skill_coverage','occupation_coverage'])
    fig,ax=plt.subplots(figsize=(8,5));x=np.arange(len(coverage))
    for shift,key,color,label in [(-.19,'skill_coverage','#3979a5','Навыки с контекстом'),(.19,'occupation_coverage','#dc9144','Целевая специальность')]:
        bars=ax.bar(x+shift,[100*r[key] for r in coverage],width=.36,color=color,label=label)
        ax.bar_label(bars,fmt='%.1f%%',padding=4)
    ax.set(xticks=x,xticklabels=[TYPE_NAMES[r['post_type']]+f"\nn={r['documents']:,}" for r in coverage],
           ylim=(0,110),ylabel='Доля уникальных текстов, %',xlabel='Тип поста',title='Покрытие извлечения — не оценка качества')
    ax.legend();save(fig,'coverage',caption)

    status_rows=[{'post_type':t,'status':s,'documents':n,'share':n/counts[t]} for t,items in manifest['status_counts'].items() for s,n in items.items()]
    _table(tables/'occupation_status.csv',status_rows,['post_type','status','documents','share'])
    fig,ax=plt.subplots(figsize=(10,5));bottom=np.zeros(len(coverage))
    for status,color in zip(STATUS_NAMES,['#3979a5','#78a5bd','#dc9144','#d6dadd']):
        values=np.array([100*manifest['status_counts'][r['post_type']][status]/r['documents'] for r in coverage])
        ax.bar(x,values,bottom=bottom,label=STATUS_NAMES[status],color=color)
        bottom+=values
    ax.set(xticks=x,xticklabels=[TYPE_NAMES[r['post_type']] for r in coverage],ylabel='Доля текстов, %',xlabel='Тип поста',title='Результаты определения специальности')
    ax.legend(bbox_to_anchor=(1,1),loc='upper left');save(fig,'occupation_status',caption)

    buckets=['0','1','2','3','4','5','6–10','11+']
    rows=[{'post_type':t,'unique_skills':b,'documents':cardinality[t,b],'share':cardinality[t,b]/counts[t]} for t in counts for b in buckets]
    _table(tables/'skills_per_post.csv',rows,['post_type','unique_skills','documents','share'])
    fig,ax=plt.subplots(figsize=(10,5));x=np.arange(len(buckets))
    for shift,t,color in [(-.18,'vacancy','#3979a5'),(.18,'resume','#dc9144')]:
        if counts[t]:ax.bar(x+shift,[100*cardinality[t,b]/counts[t] for b in buckets],width=.35,label=TYPE_NAMES[t],color=color)
    ax.set(xticks=x,xticklabels=buckets,xlabel='Различных навыков с контекстом в одном тексте',ylabel='Доля текстов данного типа, %',title='Сколько навыков извлекается из поста')
    ax.legend();save(fig,'skills_per_post',caption)

    skills=_read(skills_run/'skill_frequencies_summary.csv')
    if skills and 'vacancy_demand_texts' not in skills[0]:raise ValueError('Rebuild skill statistics before diagnostics')
    fig,axes=plt.subplots(1,2,figsize=(15,7));top_rows=[]
    for ax,t,key,title in zip(axes,['vacancy','resume'],['vacancy_demand_texts','resume_possessed_texts'],['Требуемые / желательные навыки','Заявленные навыки соискателей']):
        top=sorted((r for r in skills if int(r[key])>0),key=lambda r:int(r[key]),reverse=True)[:15][::-1]
        ax.barh([r['name'] for r in top],[100*int(r[key])/counts[t] for r in top],color='#3979a5')
        ax.set(title=title,xlabel=f'Доля всех {"вакансий" if t=="vacancy" else "резюме"}, %',ylabel='Навык')
        top_rows.extend({'post_type':t,'skill_id':r['skill_id'],'name':r['name'],'documents':int(r[key]),'share':int(r[key])/counts[t]} for r in top)
    _table(tables/'top_skills.csv',top_rows,['post_type','skill_id','name','documents','share'])
    save(fig,'top_skills',caption+' В одном тексте навык учитывается один раз.')

    occupations=_read(occupation_run/'occupation_frequencies.csv');top_rows=[]
    fig,axes=plt.subplots(1,2,figsize=(15,7))
    for ax,t in zip(axes,['vacancy','resume']):
        key=t+'_texts';top=sorted((r for r in occupations if int(r[key])>0),key=lambda r:int(r[key]),reverse=True)[:15][::-1]
        ax.barh([r['name'] for r in top],[100*int(r[key])/counts[t] for r in top],color='#dc9144')
        ax.set(title=TYPE_NAMES[t],xlabel='Доля всех текстов данного типа, %',ylabel='Специальность')
        top_rows.extend({'post_type':t,'occupation_id':r['occupation_id'],'name':r['name'],'documents':int(r[key]),'share':int(r[key])/counts[t]} for r in top)
    fig.suptitle('Часто извлекаемые целевые специальности')
    _table(tables/'top_occupations.csv',top_rows,['post_type','occupation_id','name','documents','share'])
    save(fig,'top_occupations',caption+' В посте могут быть несколько специальностей.')

    roles=_read(skills_run/'skill_frequencies.csv')
    top=sorted((r for r in skills if int(r['unique_texts_total'])>0),key=lambda r:int(r['unique_texts_total']),reverse=True)[:15]
    role_counts=Counter()
    for r in roles:role_counts[r['skill_id'],r['role']]+=int(r['document_count'])
    matrix=np.array([[100*role_counts[r['skill_id'],role]/int(r['unique_texts_total']) for role in ROLE_NAMES] for r in top])
    fig,ax=plt.subplots(figsize=(12,8));im=ax.imshow(matrix,aspect='auto',vmin=0,vmax=100,cmap='Blues')
    ax.set(yticks=range(len(top)),yticklabels=[r['name'] for r in top],xticks=range(len(ROLE_NAMES)),
           xticklabels=list(ROLE_NAMES.values()),xlabel='Контекстная роль',ylabel='Навык',title='В каких контекстах упоминаются навыки')
    plt.setp(ax.get_xticklabels(),rotation=30,ha='right');fig.colorbar(im,ax=ax,label='% текстов с данным навыком')
    for i in range(len(top)):
        for j in range(len(ROLE_NAMES)):ax.text(j,i,f'{matrix[i,j]:.0f}',ha='center',va='center',fontsize=8,color='white' if matrix[i,j]>55 else 'black')
    role_rows=[{'skill_id':r['skill_id'],'role':role,'documents':role_counts[r['skill_id'],role],
                'skill_documents':int(r['unique_texts_total']),'share':matrix[i,j]/100} for i,r in enumerate(top) for j,role in enumerate(ROLE_NAMES)]
    _table(tables/'skill_roles.csv',role_rows,['skill_id','role','documents','skill_documents','share'])
    save(fig,'skill_roles','Источник: извлечения навыков. Знаменатель — тексты с данным навыком; роли могут пересекаться.')

    if db_path:
        con=sqlite_connect(local_path(db_path).resolve().as_uri()+'?mode=ro',uri=True)
        source_counts=defaultdict(Counter);source_names={}
        try:
            query='''SELECT DISTINCT c.text_hash,s.id,s.name FROM contents c JOIN publications p ON p.content_id=c.id
                     JOIN sources s ON s.id=p.source_id JOIN labeled_contents l ON l.text_hash=c.text_hash
                     WHERE l.model_version=? AND l.final_label IS NOT NULL'''
            for identity,source_id,name in con.execute(query,(manifest['classifier_version'],)):
                if identity not in flags:continue
                source_names[source_id]=name;source_counts[source_id]['documents']+=1
                for key in ['skills','occupations']:source_counts[source_id][key]+=flags[identity][key]
        finally:con.close()
        source_rows=[{'source_id':i,'source':source_names[i],**{k:c[k] for k in ['documents','skills','occupations']}} for i,c in source_counts.items()]
        source_rows.sort(key=lambda r:r['documents'])
        if source_rows:
            _table(tables/'coverage_by_source.csv',source_rows,['source_id','source','documents','skills','occupations'])
            fig,ax=plt.subplots(figsize=(12,8));y=np.arange(len(source_rows))
            for shift,key,color,label in [(-.18,'skills','#3979a5','Навыки'),(.18,'occupations','#dc9144','Специальности')]:
                ax.barh(y+shift,[100*r[key]/r['documents'] for r in source_rows],height=.34,color=color,label=label)
            ax.set(yticks=y,yticklabels=[str(r['source'])[:45]+f" (n={r['documents']})" for r in source_rows],
                   xlabel='Покрытие среди классифицированных текстов источника, %',ylabel='Источник',title='Где извлечение чаще оставляет пустые результаты')
            ax.legend();save(fig,'coverage_by_source','Источник: БД и сохранённые извлечения. Текст может входить в несколько источников. Покрытие ≠ точность.')

    quality=evaluate_audit(occupation_run/'extraction_audit.csv',annotations)
    (out/'quality_report.json').write_text(json.dumps(quality,ensure_ascii=False,indent=2),encoding='utf-8')
    quality_rows=[]
    for task,report in quality['tasks'].items():
        for post_type,score in report['by_post_type'].items():
            if score['status']!='evaluated':continue
            quality_rows.append({'task':task,'post_type':post_type,'reviewed_documents':score['reviewed_documents'],
                                 **score['micro'],'macro_f1':score['macro_f1'],'exact_match':score['exact_match'],'mean_jaccard':score['mean_jaccard']})
    if quality_rows:
        _table(tables/'quality_metrics.csv',quality_rows,['task','post_type','reviewed_documents','precision','recall','f1','macro_f1','exact_match','mean_jaccard'])
        fig,axes=plt.subplots(1,len(quality_rows),figsize=(5*len(quality_rows),5),squeeze=False)
        for ax,row in zip(axes[0],quality_rows):
            score=quality['tasks'][row['task']]['by_post_type'][row['post_type']]
            keys=[k for k in ['precision','recall','f1'] if row[k] is not None]
            vals=[row[k] for k in keys];ax.bar(keys,vals,color='#3979a5')
            for i,k in enumerate(keys):
                ci=score['ci95'][k]
                if ci:ax.vlines(i,ci[0],ci[1],color='black',linewidth=2)
            ax.set(ylim=(0,1.05),ylabel='Значение метрики [0–1]',xlabel='Метрика',title=f"{row['task']} / {TYPE_NAMES[row['post_type']]}\nn={row['reviewed_documents']}")
        save(fig,'quality_metrics','Источник: проверенные человеком тексты. Чёрные отрезки — 95% bootstrap-интервалы по документам.')
        errors=[]
        for task,report in quality['tasks'].items():
            for t,score in report['by_post_type'].items():
                for row in score['per_entity']:
                    errors.append({'task':task,'post_type':t,'entity':json.dumps(row['entity'],ensure_ascii=False),
                                   'tp':row['tp'],'fp':row['fp'],'fn':row['fn'],'support':row['support']})
        _table(tables/'entity_errors.csv',errors,['task','post_type','entity','tp','fp','fn','support'])
        top=sorted((r for r in errors if r['fp']+r['fn']>0),key=lambda r:r['fp']+r['fn'],reverse=True)[:15][::-1]
        if top:
            fig,ax=plt.subplots(figsize=(12,7));y=np.arange(len(top))
            ax.barh(y,[r['fp'] for r in top],label='Лишние (FP)',color='#dc9144')
            ax.barh(y,[r['fn'] for r in top],left=[r['fp'] for r in top],label='Пропущенные (FN)',color='#3979a5')
            ax.set(yticks=y,yticklabels=[r['post_type']+': '+r['entity'] for r in top],xlabel='Ошибок, шт.',ylabel='Сущность / роль',title='Что исправлять в экстракторах в первую очередь')
            ax.legend();save(fig,'entity_errors','Источник: проверенные тексты. Повторные упоминания в одном тексте не увеличивают счётчик.')
    (out/'report_manifest.json').write_text(json.dumps({'occupation_run':str(occupation_run),'skills_run':str(skills_run),
        'classifier_version':manifest['classifier_version'],'charts':charts,'quality_available':bool(quality_rows),
        'annotation_source':str(annotations) if annotations else str(occupation_run/'extraction_audit.csv'),
        'diagnostics_code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},ensure_ascii=False,indent=2),encoding='utf-8')
    print('Diagnostics:',out,'charts:',len(charts),'human quality available:',bool(quality_rows),flush=True)
    return out


if __name__=='__main__':
    from market_system.runtime import enable_guard
    enable_guard()
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('occupation_run',type=Path)

    p.add_argument('--db',type=Path);p.add_argument('--annotations',type=Path)
    args=p.parse_args();build(args.occupation_run,args.db,args.annotations)
