# Измерения поиска — этап 3

Дата: 2026-10-08. Все измерения выполнены на обычной Backup API копии новой `data/app.db`; старый проект не использовался.

## Данные и оборудование

- Набор `initial_saved_v1`: 168910 карточек, 239341 связей классов. Размер копии до нового индекса: 1641054208 байт.
- CPU: AMD Ryzen 5 5500U with Radeon Graphics, 12 логических процессоров. RAM: 16517849088 байт (15.38 GiB).
- ОС: Windows-11-10.0.26300-SP0; Python 3.12.14; SQLite 3.53.1. Файлы на локальном C:, модель накопителя через CIM недоступна в песочнице.
- Нагрузка одиночная, без параллельных тестов. Кеши ОС/SQLite прогреты; холодный старт и конкурентные читатели/писатели не измерены.

## Методика

Baseline — семантически эквивалентный SQL, написанный на этапе 3 поверх схемы этапа 2: коррелированный COUNT(DISTINCT class_id), исходные индексы. Это не замер старого Telegram-бота и не поиск по JSON. После: кандидаты IN для классов, EXISTS и явный catalog_salary для зарплатного минимума, дополнительный catalog_all_dates для сортировки без типа.

Для каждого запроса: 3 прогрева, 30 повторов; время execute + fetchall через perf_counter_ns. p95 — nearest-rank, 29-е значение из 30 после сортировки. Первая страница: 20 карточек + один служебный ключ. Фазы до/после последовательные; интервалы доверия не рассчитывались. Полная упорядоченная выдача, а не только первая страница, сравнена на равенство для каждого запроса.

Сначала измеряется только выбор компактных ключей. Отдельно после оптимизации — полный Catalog.search: новое read-only соединение, валидация, ключи, тексты, evidence, классы и все публикации выбранных карточек. После замера дополнительно ограничены ID диапазоном SQLite INTEGER; SQL и параметры окончательного кода сверены с записанными запросами без повторного замера. Время полной службы относится к версии с исходной проверкой положительного ID; это не оценка обработки некорректных входных данных.

## Результаты

Все времена в миллисекундах.

| Запрос | Совпадений | До: медиана | До: p95 | После: медиана | После: p95 | Служба: медиана | Служба: p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Без ограничений | 168910 | 67.613 | 83.521 | 0.247 | 0.291 | 4.263 | 4.582 |
| Один навык | 10909 | 457.038 | 468.349 | 34.014 | 36.445 | 41.422 | 45.124 |
| Два навыка ALL | 758 | 715.498 | 725.971 | 72.081 | 75.911 | 80.307 | 88.112 |
| Два навыка ANY | 15695 | 722.972 | 786.666 | 69.973 | 77.492 | 75.590 | 83.831 |
| Навыки ANY + тип + дата + зарплата | 37 | 26.025 | 26.511 | 4.120 | 4.359 | 11.396 | 11.940 |
| Тип + формат + занятость | 3574 | 0.335 | 0.360 | 0.324 | 0.342 | 6.203 | 7.537 |

Выбор страницы по классам ускорился на этом корпусе. Сочетание с зарплатой ускорилось после исправления выбора индекса. Для условий работы существенного выигрыша нет. Отношения времён нельзя переносить на другой корпус, холодный кеш, глубокую страницу или конкурентную нагрузку.

## Обнаруженные узкие места

1. Без post_type исходный catalog_date не обеспечивал общий порядок дат: SQLite строил временное B-tree для всей выборки. catalog_all_dates устранил сортировку для запроса без ограничений.
2. Коррелированный подсчёт классов для большого числа карточек заменён материализацией кандидатов из document_classes. Роли проверяются без чтения текстов и JSON.
3. Применение того же подхода к узкому зарплатному запросу ухудшило медиану примерно с 28 до 84 мс в разведочном запуске. Проверка классов через EXISTS убрала ухудшение; EXPLAIN затем показал выбор catalog_date вместо catalog_salary. Явный catalog_salary сократил окончательную медиану примерно до 4 мс. Принудительный forward-индекс для обычного ALL не дал выигрыша в разведочной проверке и не включён.

Разведочные результаты лежат в `.artifacts/stage3-benchmark` и `.artifacts/stage3-benchmark-final`; первый запуск частично пересекался с тестами, поэтому его времена не используются как итоговые доказательства. Окончательный независимый прогон: `.artifacts/stage3-benchmark-v2/results.json`.

## EXPLAIN QUERY PLAN

Ниже полный текст шагов плана для каждой пары. SQL, все параметры, фильтры, сырые времена и хеши измеренного кода сохранены в JSON результата. SQLite вправе выбрать другой план после изменения статистики, корпуса или своей версии.

### Без ограничений

Фильтр: `{"post_type":null,"occupation_ids":[],"skill_ids":[],"skills_mode":"ALL","skill_roles":["possessed","preferred","required"],"published_from":null,"published_until":null,"work_format":null,"schedule":null,"employment_type":null,"salary_min":null,"currency":null,"salary_period":null,"tax_mode":null,"unknown":{}}`.

До:

```text
SEARCH c USING COVERING INDEX catalog_date (result_set_id=?)
USE TEMP B-TREE FOR ORDER BY
```

После:

```text
SEARCH c USING COVERING INDEX catalog_all_dates (result_set_id=?)
```

### Один навык

Фильтр: `{"post_type":null,"occupation_ids":[],"skill_ids":[40],"skills_mode":"ALL","skill_roles":["possessed","preferred","required"],"published_from":null,"published_until":null,"work_format":null,"schedule":null,"employment_type":null,"salary_min":null,"currency":null,"salary_period":null,"tax_mode":null,"unknown":{}}`.

До:

```text
SEARCH c USING COVERING INDEX catalog_date (result_set_id=?)
CORRELATED SCALAR SUBQUERY 1
SEARCH s USING INDEX sqlite_autoindex_search_catalog_1 (result_set_id=? AND content_id=?)
SEARCH d USING COVERING INDEX document_classes_reverse (result_set_id=? AND content_id=? AND class_id=? AND role=?)
USE TEMP B-TREE FOR ORDER BY
```

После:

```text
SEARCH c USING COVERING INDEX catalog_all_dates (result_set_id=?)
LIST SUBQUERY 1
SEARCH d USING COVERING INDEX sqlite_autoindex_document_classes_1 (result_set_id=? AND class_id=?)
SEARCH s USING INDEX sqlite_autoindex_search_catalog_1 (result_set_id=? AND content_id=?)
CREATE BLOOM FILTER
```

### Два навыка ALL

Фильтр: `{"post_type":null,"occupation_ids":[],"skill_ids":[40,76],"skills_mode":"ALL","skill_roles":["possessed","preferred","required"],"published_from":null,"published_until":null,"work_format":null,"schedule":null,"employment_type":null,"salary_min":null,"currency":null,"salary_period":null,"tax_mode":null,"unknown":{}}`.

До:

```text
SEARCH c USING COVERING INDEX catalog_date (result_set_id=?)
CORRELATED SCALAR SUBQUERY 1
SEARCH s USING INDEX sqlite_autoindex_search_catalog_1 (result_set_id=? AND content_id=?)
SEARCH d USING COVERING INDEX sqlite_autoindex_document_classes_1 (result_set_id=? AND class_id=? AND content_id=? AND role=?)
USE TEMP B-TREE FOR ORDER BY
```

После:

```text
SEARCH c USING COVERING INDEX catalog_all_dates (result_set_id=?)
LIST SUBQUERY 1
SEARCH d USING COVERING INDEX document_classes_reverse (result_set_id=?)
SEARCH s USING INDEX sqlite_autoindex_search_catalog_1 (result_set_id=? AND content_id=?)
CREATE BLOOM FILTER
```

### Два навыка ANY

Фильтр: `{"post_type":null,"occupation_ids":[],"skill_ids":[40,76],"skills_mode":"ANY","skill_roles":["possessed","preferred","required"],"published_from":null,"published_until":null,"work_format":null,"schedule":null,"employment_type":null,"salary_min":null,"currency":null,"salary_period":null,"tax_mode":null,"unknown":{}}`.

До:

```text
SEARCH c USING COVERING INDEX catalog_date (result_set_id=?)
CORRELATED SCALAR SUBQUERY 1
SEARCH s USING INDEX sqlite_autoindex_search_catalog_1 (result_set_id=? AND content_id=?)
SEARCH d USING COVERING INDEX sqlite_autoindex_document_classes_1 (result_set_id=? AND class_id=? AND content_id=? AND role=?)
USE TEMP B-TREE FOR ORDER BY
```

После:

```text
SEARCH c USING COVERING INDEX catalog_all_dates (result_set_id=?)
LIST SUBQUERY 1
SEARCH d USING COVERING INDEX document_classes_forward (result_set_id=? AND class_id=? AND role=?)
SEARCH s USING INDEX sqlite_autoindex_search_catalog_1 (result_set_id=? AND content_id=?)
CREATE BLOOM FILTER
```

### Навыки ANY + тип + дата + зарплата

Фильтр: `{"post_type":"vacancy","occupation_ids":[],"skill_ids":[40,76],"skills_mode":"ANY","skill_roles":["possessed","preferred","required"],"published_from":"2020-01-01T00:00:00.000000Z","published_until":null,"work_format":null,"schedule":null,"employment_type":null,"salary_min":100000.0,"currency":"RUB","salary_period":"month","tax_mode":null,"unknown":{}}`.

До:

```text
SEARCH c USING INDEX catalog_date (result_set_id=? AND post_type=? AND published_at_utc>?)
CORRELATED SCALAR SUBQUERY 1
SEARCH s USING INDEX sqlite_autoindex_search_catalog_1 (result_set_id=? AND content_id=?)
SEARCH d USING COVERING INDEX sqlite_autoindex_document_classes_1 (result_set_id=? AND class_id=? AND content_id=? AND role=?)
```

После:

```text
SEARCH c USING INDEX catalog_salary (result_set_id=? AND currency=? AND salary_period=? AND salary_from>?)
CORRELATED SCALAR SUBQUERY 1
SEARCH s USING INDEX sqlite_autoindex_search_catalog_1 (result_set_id=? AND content_id=?)
SEARCH d USING COVERING INDEX document_classes_reverse (result_set_id=? AND content_id=? AND class_id=? AND role=?)
USE TEMP B-TREE FOR ORDER BY
```

### Тип + формат + занятость

Фильтр: `{"post_type":"vacancy","occupation_ids":[],"skill_ids":[],"skills_mode":"ALL","skill_roles":["possessed","preferred","required"],"published_from":null,"published_until":null,"work_format":"remote","schedule":null,"employment_type":"full_time","salary_min":null,"currency":null,"salary_period":null,"tax_mode":null,"unknown":{}}`.

До:

```text
SEARCH c USING INDEX catalog_date (result_set_id=? AND post_type=?)
```

После:

```text
SEARCH c USING INDEX catalog_date (result_set_id=? AND post_type=?)
```

## Проверка и воспроизведение

На копии: integrity_check=ok, foreign_key_check пуст. Сравнение полной выдачи прошло во всех шести случаях.

Из корня нового проекта, направив TEMP/TMP/TMPDIR в `.artifacts/tmp`:

```powershell
.venv/Scripts/python.exe scripts/benchmark_stage3.py --repeats 30 --output .artifacts/stage3-benchmark-repeat
```

Каталог вывода должен быть новым. Скрипт снимает копию текущей новой app.db, удаляет только на ней индекс/регистрацию миграции 2 для baseline, затем возвращает индекс вызовом optimize. Рабочая БД и активный набор при измерении не изменяются. Для итогового замера SQL прежняя проекция сохранена; алгоритмы обработки текста не запускались.
