"""Educational report for admission precision and two kinds of uncertainty."""
import json
import pandas as pd
import matplotlib.pyplot as plt
from market_system.research.benchmark import ROOT, LABELS
from market_system.research.architectures import decide as old_decide
from market_system.research.selection_pipeline import evaluate
import numpy as np


def append_report(md, figure, table):
    pointer=ROOT/'research/results/current_selection_pipeline.json'
    if not pointer.exists():
        return
    run=ROOT/json.loads(pointer.read_text(encoding='utf-8'))['path']
    read=lambda filename:json.loads((run/filename).read_text(encoding='utf-8'))
    selection=read('selection.json'); result=read('test_result.json')
    comparison=pd.read_csv(run/'comparison.csv')
    validation=pd.read_csv(run/'validation.csv')
    chosen=selection['winner']; metrics=result['metrics']
    previous=ROOT/read('protocol.json')['source']
    old_selection=json.loads((previous/'selection.json').read_text(encoding='utf-8'))['winner']
    saved=np.load(previous/(old_selection['model']+'__'+old_selection['architecture']+'.npz'))
    old_rows=[]
    for split in ('validation','test'):
        frame=pd.read_csv(run/(split+'_predictions.csv'))
        pred=old_decide(saved[split],saved['thresholds'])
        pred[~np.isin(pred,['resume','vacancy'])]='discard'
        old_rows.append({'system':'previous_strict_'+old_selection['model'],'split':split,**evaluate(frame.label,pred)})
    comparison=pd.concat([comparison,pd.DataFrame(old_rows)],ignore_index=True)
    comparison.to_csv(run/'comparison_with_previous.csv',index=False)
    md('''## 12. Отбор полезных постов: precision целевых классов и recall мусора

Этот раздел соответствует последнему согласованному критерию. Разделы 1–11 сохраняют предыдущие эксперименты.

| Результат | Решение |
|---|---|
| Уверенная вакансия | Сохранить как vacancy |
| Уверенное резюме | Сохранить как resume |
| Уверенно полезный пост, тип неясен | Сохранить как useful_unspecified |
| Полезность сомнительна | Отклонить: uncertain_usefulness |
| Высокая оценка мусора | Отклонить: trash_score_high |

Последние две причины относятся к действию discard. Поэтому recall мусора ниже — доля настоящего мусора,
которую система **не допустила в полезную базу**. Это метрика конечного отбора, а не только явных ответов trash.
Precision мусора не ограничивается. Полезные посты, отклонённые фильтром, учитываются как потери.
Мусор, сохранённый с неопределённым типом, учитывается как ошибка допуска, а не исчезает из оценки.

### Зафиксированный протокол

- Первый этап: 15 бинарных моделей useful против trash. Обучение по трём групповым фолдам; словари и SVD
  обучаются только внутри фолда. Это новые обученные модели, а не сумма старых вероятностей.
- Второй этап: для каждого метода проверяются четыре уже рассчитанные архитектуры. Их оценки resume/vacancy
  нормируются между собой. Это оценки для порогового решения, не откалиброванные вероятности.
- Пороги выбираются на OOF: нет наблюдаемых ошибок допуска; нет ошибок назначения типов среди допущенных;
  минимум 10 верно принятых примеров для включения соответствующего решения.
- Среди четырёх архитектур для каждой пары методов выбирается вариант на OOF. Всего 900 конфигураций.
- На validation сравниваются 225 выбранных вариантов. Требуется precision=100% отдельно у resume и vacancy,
  минимум 20 назначений каждого типа и отсутствие мусора во всей полезной очереди. Затем максимизируются
  recall мусора, число сохранённых полезных постов и число правильно назначенных типов.
- Выбор и пороги сохраняются до подсчёта тестовых метрик. На тесте проверяется precision ≥95% **отдельно
  у resume и vacancy**. Precision мусора не является условием. Минимум 20 ответов каждого типа защищает от пустого решения.

Обучающие функции потерь базовых алгоритмов сохранены; заданные приоритеты реализованы через подбор порогов
и правило выбора системы. Это не эксперимент со специальной асимметричной функцией потерь.
Общие проверочные посты сохраняют сравнимость. Разные обучающие подвыборки дают три модели каждого метода;
на validation/test их оценки усредняются. Перенос порогов с одиночных OOF-оценок на средние оценки является ограничением.

Старый тест уже изучен в предыдущих экспериментах, поэтому здесь он **ретроспективный**. Нет нового доказательства
precision на реальном потоке. Выбор из 225 вариантов также делает validation оптимистичной оценкой.
Разметка корпуса и доля мусора требуют отдельной проверки; исходные метки не исправлялись по результатам моделей.
''')
    md(f"### Выбранная система\n\nФильтр полезности: **{chosen['gate']}**. Определение типа: "
       f"**{chosen['type_model']} / {chosen['architecture']}**.\n\n"
       f"Условия на validation выполнены: **{selection['qualified_validation']}**. "
       f"Условие precision ≥95% для обоих целевых классов на старом тесте выполнено: "
       f"**{result['precision_requirement_met']}**.\n\n"
       +table(comparison[comparison.system=='selected_hierarchy'][['split','resume_precision','vacancy_precision',
                    'trash_recall','useful_recall','useful_unspecified','trash_admitted','discarded_useful']]))
    md('### Сравнение с существующим ансамблем и обычным ответом моделей\n\n'
       +table(comparison[comparison.split=='test'][['system','resume_precision','vacancy_precision','trash_recall',
               'useful_retained','useful_unspecified','trash_admitted','discarded_useful']])+'''

historical_refit_train60 — заново обученная исходная схема двух LR и двух CatBoost, веса 20/80,
рабочие пороги >0.8 и <0.3. Она обучена на train60; validation/test не участвуют в обучении.
Это реплика исходной архитектуры с ограничением TF-IDF в 40 000 признаков, а не старые production-веса,
которые могли видеть данные теста. В этом запуске сохранены обе шкалы и решения для каждого поста.
forced_fold_average — обязательный выбор класса по средним оценкам трёх моделей фолдов.
previous_strict — выбранная ранее строгая система; её uncertain и trash одинаково отклоняются.
Она не имеет отдельной очереди с неопределённым типом. Её показатели добавлены как контроль:
новый входной фильтр должен оправдывать потерю полезных постов, а не считаться улучшением по факту усложнения.
Разница в размерах обучающих наборов и порогах означает сравнение систем целиком, а не чистого эффекта алгоритма.
''')
    old_metrics=old_rows[1]
    md(f"**Цена отдельного фильтра относительно прежней строгой системы:** на старом тесте изменение "
       f"числа сохранённых полезных постов {metrics['useful_retained']-old_metrics['useful_retained']:+d}; "
       f"изменение числа допущенных мусорных постов {metrics['trash_admitted']-old_metrics['trash_admitted']:+d}. "
       "Выполнение порога качества и улучшение относительно предыдущего решения — разные выводы. "
       "Если фильтр не снижает попадание мусора, а полезных постов теряет больше, его не следует внедрять только ради новой архитектуры.")
    plot=comparison[comparison.split=='test'].set_index('system')
    plot=plot.rename(index={'selected_hierarchy':'Два этапа', 'historical_refit_train60':'Исходный ансамбль\n(переобучен)',
                            'mlp_lsa_forced_fold_average':'MLP\n(среднее фолдов)',
                            'gradient_boosting_lsa_forced_fold_average':'Бустинг\n(обязательный ответ)',
                            'previous_strict_gradient_boosting_lsa':'Прежний строгий\nбустинг'})
    plot[['resume_precision','vacancy_precision','trash_recall','useful_recall']].rename(columns={
        'resume_precision':'Precision резюме','vacancy_precision':'Precision вакансий',
        'trash_recall':'Отсев мусора','useful_recall':'Сохранение полезных'}).plot.bar(figsize=(11,5),ylim=(0,1.05))
    plt.axhline(.95,color='black',linestyle='--',linewidth=1,label='Требование к precision')
    plt.xticks(rotation=0);plt.xlabel('');plt.ylabel('Доля');plt.legend(loc='upper left',bbox_to_anchor=(1,1),fontsize=8)
    figure('useful_admission_comparison','Отбор полезных постов и точность их типов')
    # Pair by identity: totals can conceal complementary errors.
    audit=pd.read_csv(run/'test_predictions.csv').rename(columns={'prediction':'new_prediction'})
    old_predictions=pd.read_csv(previous/(old_selection['model']+'_diagnostic.csv'))
    audit=audit.merge(old_predictions[['text_hash','label','prediction']].rename(
        columns={'label':'old_label','prediction':'old_prediction'}),on='text_hash',validate='one_to_one')
    assert len(audit)==len(pd.read_csv(run/'test_predictions.csv'))
    assert (audit.label==audit.old_label).all()
    old_kept=audit.old_prediction.isin(['resume','vacancy'])
    new_kept=audit.new_prediction!='discard'
    audit['retention_group']=np.select([old_kept & new_kept,old_kept & ~new_kept,~old_kept & new_kept],
        ['both','old_only','new_only'],default='neither')
    corpus=pd.concat([pd.read_csv(p,keep_default_na=False,usecols=['text_hash','text'])
                      for p in (ROOT/'data/learning_data').glob('*.csv')]).drop_duplicates('text_hash')
    audit=audit.merge(corpus,on='text_hash',how='left',validate='one_to_one')
    audit.to_csv(run/'paired_retention_audit.csv',index=False)
    counts=pd.crosstab(audit.retention_group,audit.label).reindex(
        index=['both','old_only','new_only'],columns=LABELS,fill_value=0).fillna(0).astype(int)
    counts.index.name='Какая система сохранила'
    md('### Какие посты теряются и возвращаются\n\n'+table(counts.reset_index())+'''

`both` — сохранили обе системы; `old_only` — только прежняя строгая; `new_only` — только новая.
Полезный пост с неопределённым типом считается сохранённым. Посты, отклонённые обеими, также сохранены в CSV.
Это попарная диагностика старого теста. По ней не менялись пороги или выбранные модели.
Полные тексты, исходные метки, решения обеих систем и причины нового решения — в `paired_retention_audit.csv`.
''')
    unspecified=audit[audit.new_prediction=='useful_unspecified']
    md('### Что находится в очереди с неопределённым типом\n\n'
       +table(pd.crosstab(unspecified.retention_group,unspecified.label).reset_index())+'''

Очередь может содержать как вновь возвращённые посты, так и посты, которым прежняя модель уже назначала тип.
Поэтому её размер нельзя трактовать как число дополнительно найденных полезных постов.
''')
    examples=audit[audit.retention_group.isin(['old_only','new_only'])].groupby('retention_group',sort=True).head(3).copy()
    examples['Фрагмент текста']=examples.text.fillna('').str.replace(r'\s+',' ',regex=True).str.slice(0,240)
    md('### Примеры расхождений\n\n'+table(examples[['retention_group','label','old_prediction','new_prediction','reason','Фрагмент текста']])+'''

Показаны первые три поста каждой группы в сохранённом порядке, без отбора наиболее убедительных примеров.
Фрагмент нужен для ориентации; для проверки разметки следует читать полный текст в CSV.
''')
    qualifying=validation[(validation.resume_precision==1)&(validation.vacancy_precision==1)&
                          (validation.resume_accepted>=20)&(validation.vacancy_accepted>=20)&(validation.trash_admitted==0)]
    md(f"### Альтернативы на validation\n\nУсловия выполнили {len(qualifying)} из {len(validation)} вариантов. "
       "Ниже до десяти вариантов, упорядоченных по сохранению полезных постов и правильных типов.\n\n"
       +table(qualifying.sort_values(['useful_retained','typed_correct'],ascending=False).head(10)[
           ['gate','type_model','architecture','useful_retained','useful_unspecified','typed_correct']]))
    md(f'''### Практический смысл

На старом тесте выбранная система сохранила {metrics['useful_retained']} полезных постов;
мусора в сохранённых — {metrics['trash_admitted']}. Очередь с неопределённым типом содержит
{metrics['useful_unspecified']} постов. Полезных постов потеряно: {metrics['discarded_useful']}.
Именно эти числа показывают цену строгого допуска. Требование precision не достигается простым отказом от всех ответов.

Предсказания и причины решений сохранены в `test_predictions.csv`. Отдельные значения исходного ансамбля —
в `test_historical.csv`. Пороги и решение до проверки теста — в `selection.json`.
Каталог: `{run.relative_to(ROOT).as_posix()}`.

```powershell
.venv-library/Scripts/python.exe -m research.selection_pipeline
.venv-library/Scripts/python.exe -m research.run
```

Первая команда выполняет новый эксперимент, вторая включает последний завершённый результат в общий ноутбук.
Production-модели и исходная разметка не изменяются. Следующая проверка для внедрения — свежая случайная выборка
из всего потока, включая ранее отклонённые посты. Разметка только уже отобранных вакансий и резюме не заменяет её.
''')
