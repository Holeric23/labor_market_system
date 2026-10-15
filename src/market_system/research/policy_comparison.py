"""Compare saved decisions under the user's false-positive priority, without refitting."""
import json
import pandas as pd
from market_system.research.benchmark import ROOT


def append_comparison(md, table, first_run, second_run):
    old = json.loads((first_run/'test_results.json').read_text(encoding='utf-8'))
    texts = pd.read_csv(first_run/'test_predictions.csv')
    selection = json.loads((second_run/'selection.json').read_text(encoding='utf-8'))['winner']
    strict = pd.read_csv(second_run/(selection['model']+'_diagnostic.csv'))
    assert set(texts.text_hash) == set(strict.text_hash)
    checked = texts[['text_hash','label']].merge(strict[['text_hash','label']], on='text_hash')
    assert (checked.label_x == checked.label_y).all()
    targets = ['resume','vacancy']
    total = int(texts.label.isin(targets).sum())
    rows = []
    for name, frame, column in [('MLP: обязательный выбор класса', texts, 'prediction'),
                                ('Бустинг: строгие пороги', strict, 'prediction')]:
        accepted = frame[column].isin(targets)
        tp = int((accepted & (frame[column]==frame.label)).sum())
        fp = int((accepted & (frame[column]!=frame.label)).sum())
        rows.append({'Режим': name, 'Правильно принято': tp, 'Ошибочно принято': fp,
                     'Precision целевых назначений': tp/(tp+fp), 'Полнота верных назначений': tp/total,
                     'Мусор в полезной базе': int((accepted & (frame.label=='trash')).sum()),
                     'Не принято в полезную базу': int((~accepted).sum())})
    policy = old['historical_production_policy']
    count = policy['accepted']
    tp = round(count*policy['accuracy_accepted'])
    rows.insert(1, {'Режим': 'LR + CatBoost: рабочие пороги, переобученная реплика',
                   'Правильно принято': tp, 'Ошибочно принято': count-tp,
                   'Precision целевых назначений': tp/count, 'Полнота верных назначений': tp/total,
                   'Мусор в полезной базе': 'не сохранено отдельно',
                   'Не принято в полезную базу': len(texts)-count})
    pd.DataFrame(rows).to_csv(second_run/'policy_comparison.csv',index=False)
    trash = strict[strict.label=='trash']
    trash_kept_out = int((~trash.prediction.isin(targets)).sum())
    trash_explicit = int((trash.prediction=='trash').sum())
    trash_uncertain = int((trash.prediction=='uncertain').sum())
    md('''### Прямое сравнение с исходным ансамблем

Приоритет пользователя: не включать пост в неправильную целевую категорию; пропуски допустимы.
Здесь FP — любое ошибочное присвоение resume/vacancy: попадание мусора либо путаница этих двух категорий.
Полнота верных назначений считает только правильно выбранную целевую категорию.
Все строки относятся к одним 668 постам с одинаковыми метками (621 целевой пост и 47 мусорных).
'''+table(pd.DataFrame(rows))+f'''

Для ансамбля применено рабочее правило: оценка целевого класса > 0.8 и конкурирующего < 0.3,
иначе отказ. Это сохранённые результаты заново обученной реплики LR + CatBoost из первого эксперимента,
а не независимая проверка старых сохранённых весов production. Старые веса могли видеть эти посты при обучении.
В реплике TF-IDF ограничен 40 000 признаками; поэтому она не является побитовой копией исходного обучения.
Числа верных и ложных целевых назначений восстановлены из сохранённых accepted и accuracy_accepted.
Поклассовые прогнозы этого режима не сохранялись, поэтому его отдельную долю пропущенного мусора не восстанавливаем догадкой.

MLP и историческая реплика обучены на 80% данных; строгий бустинг — усреднение трёх моделей,
обученных на подвыборках обучающих 60%. Это сравнение полученных систем, а не изолированная оценка эффекта алгоритма.

**Сравнивайте два столбца вместе:** число ложных назначений и полноту верных назначений.
Победа MLP по macro-F1 не означает победу по требованию чистоты полезной базы.

**Почему recall мусора может вводить в заблуждение:** он считает только явные ответы trash.
Ответ uncertain тоже не допускает мусор в базу. У выбранной строгой модели {trash_kept_out} из {len(trash)}
мусорных постов удержаны вне полезной базы: {trash_explicit} явно названы мусором,
{trash_uncertain} оставлены неопределёнными.
Повышать recall мусора ценой его precision допустимо при выбранном приоритете, но оценивать систему
лучше по ложным целевым назначениям и полноте полезных постов. Простая запись всех uncertain в trash
изменила бы название ответа, но не чистоту базы.
''')
