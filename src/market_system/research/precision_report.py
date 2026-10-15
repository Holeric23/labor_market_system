"""Measured precision-constrained screening, tables and embedded explanatory plots."""
import base64
import json
from pathlib import Path
import uuid
import joblib
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import precision_recall_curve, roc_curve, roc_auc_score, average_precision_score
from market_system.research.benchmark import ROOT
from market_system.research.precision_benchmark import TARGETS, rank, qualifies


def render(run, md, figure, table, heading='## 13.'):
    read=lambda f:json.loads((run/f).read_text(encoding='utf-8'))
    protocol=read('protocol.json'); manifest=read('manifest.json')
    search=pd.read_csv(run/'search.csv');val=pd.read_csv(run/'validation_finalists.csv');test=pd.read_csv(run/'test_comparison.csv')
    selection=read('selection.json');old=pd.read_csv(run/'historical_comparison.csv')
    split=pd.read_csv(run/'split.csv')
    yv=split[split.split=='validation'].label.to_numpy();yt=split[split.split=='test'].label.to_numpy()
    eligible=test[test.validation_qualified & test.test_qualified].sort_values('target_macro_recall',ascending=False)
    md(f'''{heading} Единая цель обучения: precision 100% на validation, максимум recall

**Это актуальный эксперимент для выбора фаворитов.** Предыдущие эксперименты сохраняются как история.
Выполнено {manifest['configurations']} конфигураций для {manifest['families']} методов.

### Что именно оптимизируем

1. Precision на validation = 1.0 **отдельно для vacancy и resume**.
2. Минимум 20 назначений каждого целевого класса: отсутствие ответов не считается успехом.
3. Среди допустимых вариантов — максимум среднего recall двух целевых классов.
4. Метрики trash не участвуют ни в ограничениях, ни в выборе.
5. После фиксации параметров всех методов — сравнение на test. Условие precision ≥0.95 отдельно для двух целевых классов.

Средний recall — арифметическое среднее полноты вакансий и резюме, поэтому большой класс не вытесняет меньший.
Неуверенные ответы не включаются в recall назначенного типа. Если обе целевые шкалы проходят пороги,
результат useful_unspecified сохраняет неопределённость типа; если ни одна — discard_uncertain.
Этот эксперимент не оптимизирует отдельную очередь полезности: её качество не следует выводить из precision присвоенных типов.

### Как модель получает повышенный штраф

| Постановка | Какие обучающие примеры получают вес 1 / 5 / 20 |
|---|---|
| Две модели «класс против остальных» | Все отрицательные примеры соответствующей модели |
| Три класса | Мусорные посты; взаимная путаница целевых классов отдельно весом не штрафуется |
| Пары с мусором | Мусор в соответствующей паре |
| Пара «вакансия / резюме» | Одинаковые веса: обе ошибки важны симметрично |

Обычно используется sample_weight — вес примера в обучении. Средний вес нормирован к единице,
автоматическая балансировка классов отключена, чтобы не смешивать два способа перевзвешивания.
У KNN и ближайшего центроида нет поддержки sample_weight в fit: для них применяется воспроизводимая
перевыборка обучающих примеров. Это другой механизм; он отмечен в таблице параметров.
Увеличение веса отрицательных примеров усиливает наказание за ложные положительные ответы,
но само по себе не гарантирует нужный precision. За окончательное решение отвечают ещё и пороги.

### Одинаковый протокол для всех

- Сохранено групповое разбиение примерно 60/20/20. Похожие посты не пересекают части.
- TF-IDF, LSA и масштабирование обучаются только на train. Представление фиксировано внутри семейства,
  чтобы веса примеров меняли обучение классификатора, а не одновременно словарь и IDF.
- Четыре архитектуры, три веса и по два значения одного гиперпараметра; ближайший центроид — одно значение.
- MLP получает такой же объём подбора: 64 нейрона, 250 итераций максимум, alpha 0.01 или 1.0.
  Большой отдельный поиск по нейросетям и расписания штрафов на этом этапе не запускались.
- Модели обучаются на train; параметры и пороги подбираются на validation. После этого нет refit на train+validation:
  иначе поменялась бы шкала оценок, для которой выбраны пороги.
- Для каждой модели сохраняется один финалист до сравнения на test.

Порог класса — минимальное значение выше максимальной оценки неправильного ответа на validation.
Так получается максимальный recall при нуле наблюдаемых ошибок в семействе решений вида «оценка ≥ порога».
Для конечных оценок порог может оказаться выше всех ответов; тогда метод не проходит минимальный объём.

**Precision=100% на validation здесь построен подбором. Это не независимое подтверждение качества.**
Test уже рассматривался в прошлых исследованиях. Более того, выбор фаворитов по его результатам делает
его частью исследования. Следующий независимый тест для внедрения должен быть новым.
''')
    md('### Зафиксированные параметры финалистов\n\n'+table(val[['model','architecture','cost','parameters','weight_method',
        'threshold_resume','threshold_vacancy','resume_precision','vacancy_precision','target_macro_recall']]))
    md('### Сравнение на test\n\n'+table(test.sort_values(['validation_qualified','test_qualified','target_macro_recall'],ascending=False)[
        ['model','validation_qualified','test_qualified','resume_precision','vacancy_precision','resume_recall','vacancy_recall','target_macro_recall']])
       +'''\n\ntest_qualified требует ≥95% precision и минимум 20 назначений **каждого целевого класса**.
Строки, не прошедшие validation, показаны для полноты, но не являются допустимыми фаворитами.
Результаты с отсутствующими назначениями имеют неопределённый precision, а не 100%.
''')
    md('### Исходный ансамбль: до и после подбора порогов\n\n'+table(old)+'''

Обе строки основаны на одной заново обученной реплике LR + CatBoost, train60, веса смеси 20/80.
original_fixed использует прежние пороги >0.8 и <0.3; validation_tuned — новые пороги с той же целевой функцией.
Состав ансамбля и его обучение здесь не подбирались. Реплика использует TF-IDF максимум 40 000 признаков;
это не проверка старых сохранённых production-весов. Сравнение показывает отдельно эффект правила решения.
''')
    # Every numerical comparison has a companion plot; no test thresholds are selected visually.
    joined=val[['model','target_macro_recall']].merge(test[['model','target_macro_recall']],on='model',suffixes=('_validation','_test')).set_index('model')
    joined.plot.barh(figsize=(10,7));plt.xlabel('Средний recall вакансий и резюме');plt.xlim(0,1);plt.ylabel('')
    plt.legend(['Validation: после подбора','Test: параметры зафиксированы'])
    figure('recall_comparison','Полнота целевых классов на validation и test')
    test.set_index('model')[['resume_precision','vacancy_precision']].plot.barh(figsize=(10,7),xlim=(0,1.03))
    plt.axvline(.95,color='black',linestyle='--');plt.xlabel('Precision на test');plt.ylabel('');plt.legend(['Резюме','Вакансии'])
    figure('test_precision','Проверка порога precision отдельно для двух классов')
    for column,title,filename in [('cost','Вес отрицательных примеров','cost_heatmap'),('architecture','Архитектура','architecture_heatmap')]:
        valid_search=search[(search.resume_accepted>=20)&(search.vacancy_accepted>=20)&(search.resume_precision==1)&(search.vacancy_precision==1)]
        matrix=valid_search.pivot_table(index='model',columns=column,values='target_macro_recall',aggfunc='max').reindex(index=val.model)
        plt.figure(figsize=(9,7));cmap=plt.get_cmap('YlGnBu').copy();cmap.set_bad('lightgray')
        plt.imshow(matrix.to_numpy(),aspect='auto',vmin=0,vmax=1,cmap=cmap)
        plt.yticks(range(len(matrix)),matrix.index);plt.xticks(range(len(matrix.columns)),matrix.columns,rotation=20)
        plt.xlabel(title);plt.colorbar(label='Лучший допустимый recall на validation')
        figure(filename,title+' при строгом precision')
    # Curves on validation for a shortlist selected WITHOUT test rankings.
    shortlist=sorted(val.to_dict('records'),key=rank,reverse=True)[:4]
    auc_rows=[]
    for kind in ('roc','pr'):
        fig,axes=plt.subplots(1,2,figsize=(12,4.5))
        for j,c in enumerate(TARGETS):
            for row in shortlist:
                artifact=np.load(run/(row['model']+'_validation.npz'));values=artifact['values'][:,j]
                truth=yv==c
                if kind=='roc':
                    a,b,_=roc_curve(truth,values);metric=roc_auc_score(truth,values)
                else:
                    b,a,_=precision_recall_curve(truth,values);metric=average_precision_score(truth,values)
                axes[j].plot(a,b,label=f"{row['model']} ({metric:.3f})")
                auc_rows.append({'curve':kind,'class':c,'model':row['model'],'value':metric})
            axes[j].set_title(c);axes[j].legend(fontsize=8)
            axes[j].set_xlabel('FPR' if kind=='roc' else 'Recall');axes[j].set_ylabel('Recall' if kind=='roc' else 'Precision')
            if kind=='roc':axes[j].plot([0,1],[0,1],'k--',linewidth=.7)
            axes[j].grid(alpha=.2)
        figure(kind+'_curves','ROC-AUC на validation' if kind=='roc' else 'Precision–recall и Average Precision на validation')
    pd.DataFrame(auc_rows).to_csv(run/'curve_areas.csv',index=False)
    md('### Как читать ROC и precision–recall\n\n'+table(pd.DataFrame(auc_rows))+'''

Для каждой категории строится задача «этот класс против остальных». ROC показывает recall при разной доле
ложных положительных ответов среди отрицательных примеров. PR показывает precision и recall непосредственно.
Для нашего требования особенно важен край PR-кривой возле precision=1, а не только площадь всей кривой.
На PR указан Average Precision, а не трапецеидальная площадь. Большая ROC-AUC не гарантирует большой recall при precision=100%.
Кривые относятся к validation, использованной при подборе; test по этим графикам не настраивается.
''')
    leader=selection['validation_winner']['model'];artifact=np.load(run/(leader+'_validation.npz'))
    fig,axes=plt.subplots(1,2,figsize=(12,4.5))
    threshold_rows=[]
    for j,c in enumerate(TARGETS):
        precision,recall,threshold=precision_recall_curve(yv==c,artifact['values'][:,j])
        axes[j].plot(threshold,precision[:-1],label='Precision');axes[j].plot(threshold,recall[:-1],label='Recall')
        axes[j].axvline(artifact['thresholds'][j],color='black',linestyle='--',label='Выбранный порог')
        axes[j].set_title(leader+' / '+c);axes[j].set_xlabel('Оценка модели, не гарантированная вероятность');axes[j].legend()
        threshold_rows.extend({'class':c,'threshold':t,'precision':p,'recall':r} for t,p,r in zip(threshold,precision[:-1],recall[:-1]))
    pd.DataFrame(threshold_rows).to_csv(run/'threshold_curves.csv',index=False)
    figure('thresholds','Изменение precision и recall при выборе порога')
    times=search.groupby('model').seconds.sum().sort_values()
    times.plot.barh(figsize=(9,6));plt.xlabel('Суммарные секунды подбора (без общего преобразования текста)')
    figure('search_time','Стоимость выполненного поиска по семействам')
    md('### Стоимость и ограничения обучения\n\n'+table(val[['model','seconds','preprocessing_seconds']].merge(
        test[['model','inference_ms_per_post']],on='model'))+'''

seconds — обучение и проверка выбранной конфигурации; preprocessing_seconds — единожды построенное представление
для семейства; inference_ms_per_post — полный прогноз на test, включая преобразование текста. Это измерения данного запуска,
а не стабильный аппаратный бенчмарк. У методов разная внутренняя сложность; одинаковое число конфигураций не равно одинаковому CPU-бюджету.
Предупреждения сходимости сохранены в warnings.json. Ограниченный подбор не доказывает предел возможностей семейства.
''')
    warning_records=read('warnings.json')
    if any('not checkpointed' in r.get('message','') for r in warning_records):
        md('Запуск был продолжен после ошибки клонирования CatBoost. Числа и модели первых 14 семейств сохранены, '
           'но их журнал предупреждений до остановки не был записан. Поэтому отсутствие предупреждения '
           'в warnings.json не доказывает сходимость. Ниже дополнительно проверены счётчики итераций сохранённой MLP.')
    mlp=joblib.load(run/'mlp_lsa.joblib')['model']
    convergence=pd.DataFrame([{'head':str(name),'iterations':model.n_iter_,'max_iter':model.max_iter,
                               'reached_budget':model.n_iter_>=model.max_iter}
                              for name,model in mlp.models.items()])
    md('### Проверка бюджета MLP\n\n'+table(convergence)+'''

Достижение лимита итераций требует отдельной проверки сходимости на следующем этапе.
В текущем сравнении лимит зафиксирован заранее и не увеличивался специально для нейросети.
''')
    favorites=eligible.head(3)
    dump_data={'scope':'Exploratory shortlist selected from reused test; requires new independent confirmation',
               'models':favorites.model.tolist()}
    (run/'exploratory_favorites.json').write_text(json.dumps(dump_data,ensure_ascii=False,indent=2),encoding='utf-8')
    md('### Фавориты для следующего этапа\n\n'+(table(favorites[['model','architecture','cost','resume_precision',
        'vacancy_precision','resume_recall','vacancy_recall','target_macro_recall']]) if len(favorites) else
        'Ни один финалист одновременно не выполнил ограничения validation и test. Победитель не объявляется.')+f'''

На validation первым выбран **{leader}**. Таблица выше — отдельный исследовательский список по test;
он не выдаётся за результат независимого финального испытания. Не стоит продолжать бесконечно менять параметры,
пока именно этот test не начнёт устраивать: нужен новый отложенный материал.

### Воспроизведение

```powershell
.venv-library/Scripts/python.exe -m research.precision_benchmark
.venv-library/Scripts/python.exe -m research.precision_report
```

Каталог результатов: `{run.relative_to(ROOT).as_posix()}`. Полный поиск — search.csv; параметры до test — selection.json;
модели финалистов — *.joblib; прогнозы — *_test_predictions.csv; исходник запуска — source.py.
Обычный `python -m research.run` также добавляет итог в основной ноутбук. Production-модели не заменяются.

Источники: [подбор порогов](https://scikit-learn.org/stable/modules/classification_threshold.html),
[precision–recall](https://scikit-learn.org/stable/auto_examples/model_selection/plot_precision_recall.html),
[MLPClassifier и sample_weight](https://scikit-learn.org/stable/modules/generated/sklearn.neural_network.MLPClassifier.html).
''')


def append_report(md,figure,table):
    pointer=ROOT/'research/results/current_precision_benchmark.json'
    if pointer.exists():render(ROOT/json.loads(pointer.read_text(encoding='utf-8'))['path'],md,figure,table)


def build():
    from market_system.research.report import table
    run=ROOT/json.loads((ROOT/'research/results/current_precision_benchmark.json').read_text(encoding='utf-8'))['path']
    cells=[];figures=run/'figures';figures.mkdir(exist_ok=True)
    def md(text):cells.append({'cell_type':'markdown','id':uuid.uuid4().hex[:8],'metadata':{},'source':text})
    def figure(name,title):
        plt.tight_layout();path=figures/(name+'.png');plt.savefig(path,dpi=140,bbox_inches='tight');plt.close()
        cells.append({'cell_type':'markdown','id':uuid.uuid4().hex[:8],'metadata':{},'source':f'![{title}](attachment:{name}.png)',
                      'attachments':{name+'.png':{'image/png':base64.b64encode(path.read_bytes()).decode()}}})
    render(run,md,figure,table,heading='#')
    notebook={'cells':cells,'nbformat':4,'nbformat_minor':5,'metadata':{'kernelspec':{'display_name':'Python 3','language':'python','name':'python3'},
              'research':{'kind':'rendered_report','source_run':str(run.relative_to(ROOT))}}}
    path=ROOT/'notebooks/09_precision_first_comparison.ipynb'
    path.write_text(json.dumps(notebook,ensure_ascii=False,indent=1),encoding='utf-8');print(path,flush=True)


if __name__=='__main__':
    from market_system.runtime import enable_guard
    enable_guard()
    build()

