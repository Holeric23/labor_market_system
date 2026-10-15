# Labor Market System

Локальная информационная система поиска вакансий и научной аналитики. Этапы 0–12 прошли автономную приёмку на Windows: сбор, настоящий локальный ML, общий веб/Telegram-поиск, имитируемая доставка, исправления, смена модели, научные срезы и восстановление. Матрица требований и ограничения: [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md). Живые Telegram/DesignHunters и Linux не объявляются проверенными. Состояние: [docs/IMPLEMENTATION_STATE.md](docs/IMPLEMENTATION_STATE.md). Правила: [AGENTS.md](AGENTS.md). Контракты: [docs/CONTRACTS.md](docs/CONTRACTS.md).

Для уже установленного проекта выполните из его корня:

```powershell
Set-Location 'C:\Study\MatchArt\labor_market_system'
.venv/Scripts/python.exe -m market_system.operations diagnose --full
.venv/Scripts/python.exe -m market_system.operations create-admin admin
.venv/Scripts/python.exe -m market_system.operations launch
```

`create-admin` нужен один раз, если вашего аккаунта ещё нет. Пароль вводится скрыто; паролей по умолчанию нет. Откройте [http://127.0.0.1:8000](http://127.0.0.1:8000); Ctrl+C останавливает комплект. Для последующих запусков достаточно `launch`. Polling и реальные отправки изначально выключены; их обязательная настройка описана ниже. При обычной работе повторный импорт старого корпуса не нужен.

Итоговые автономные проверки (БД/копии создаются внутри `.artifacts`, внешний сбор и отправки не выполняются):

```powershell
powershell -ExecutionPolicy Bypass -File scripts/verify.ps1
.venv/Scripts/python.exe scripts/check_stage12.py
```

`verify.ps1` проверяет окружение, импорты, полный набор тестов и структуру без доступа к старому проекту. `check_stage12.py` создаёт новую независимую контрольную установку и сохраняет `.artifacts/stage12-acceptance.json` и `.artifacts/stage12-run.json`; использует существующие локальные модели и один текст из новой БД. Для отдельной проверки сохранности старого дерева: `.venv/Scripts/python.exe scripts/check_stage12_preservation.py before --name my-check`, затем проверки, затем та же команда с `after --name my-check`; для каждой новой пары выберите новое имя, начальный manifest не перезаписывается. Историческая сверка этапа 0 доступна явно: `check_stage0.py --verify-originals`; она сравнивает с прежней инвентаризацией и может выявить изменения, предшествующие текущей приёмке.

Пределы первой версии: только точные повторы; Telegram проверяет правки в ограниченном окне; DesignHunters читает заданный CSV, не обнаруживает новые URL; несколько предложений автоматически не выделяются. Подтверждение исправления копирует полный набор результатов и требует места/времени. Готовый набор владелец активирует отдельно. Отправка Telegram имеет окно возможного дубля после ответа API до записи квитанции. Научные выводы ограничены выбранными источниками. Скорость поиска подтверждена только сохранённым прогретым одиночным измерением [этапа 3](docs/STAGE3_BENCHMARK.md); холодный/конкурентный режим и скорость полной обработки не измерены.

## Этап 11: регулярная работа, копии и восстановление

Текущий порядок эксплуатации описан в этом разделе; ниже сохранены инструкции отдельных этапов. Рабочая система автономна: старый `Python_research_project`, его окружение, БД и секреты для исполнения не нужны. Настоящие ADMIN/USER-пароли, Telegram-токен, допуск друзей, Telethon-сессия и HTTPS **настраиваются пользователем отдельно**. Этап 11 не выполняет эти обязательные пользовательские настройки и не размещает приложение.

Единая конфигурация — `config/operations.json`: local/linux, runtime-файл, отдельные БД веба/бота, файл секретов, разрешённые HTTP Host, порт, включение polling/доставки, лимиты логов, время остановки и порог свежести. `config/runtime.json` задаёт БД, модель, два словаря, архив разметки и dataset. Все пути относительны `MARKET_HOME` (по умолчанию корень нового проекта); переменная задаётся **до запуска Python**. Не выбирайте старый исследовательский проект: конфигурация и guard отвергают старые пути, выход за рабочий корень и связанные файлы. Это защита приложения, не OS-песочница для произвольного native-кода. Восстановленная установка получает ограниченные aliases прежнего рабочего корня для исторических спецификаций; guard проверяет фактическое место записи без aliases.

Все рабочие каталоги постоянные: `data/`, `models/`, `research/review/`, `state/` (аккаунты, сессии и UI), `logs/`, `backups/`. Ничего из них не храните в ephemeral-каталоге службы. Временные файлы и кеши находятся в `.artifacts/`. JSON-логи разделены по компонентам; по умолчанию каждый файл до 5 MiB плюс четыре ротации (до 25 MiB на компонент). Логи не содержат HTTP query strings, исходных текстов, токенов и raw traceback; ошибки хранят класс исключения. Ротация не удаляет аудит БД, исходные тексты или истории заданий. Их объём растёт с данными: контролируйте свободное место и переносите проверенные копии на отдельный носитель; автоматического удаления резервных копий нет.

### Чистая установка Windows

Нужен Python 3.12 x64. Исходники копируются обычными файлами в новый рабочий каталог; `.venv`, `.env` и секреты не копируются. Для установки с текущими данными нужен проверенный backup v1: один исходный код не содержит БД и модели. Проверка этапа 11 использует отдельное новое окружение и реальное восстановление, без старого исследовательского проекта. Lock-файлы содержат SHA-256 выбранных wheel; `vendor/pyaes-1.6.1-py3-none-any.whl` — проверенный pure-Python wheel из закреплённого pyaes. `requirements.lock.txt` сохранён как прежний список версий; для новой установки используйте Windows lock.

```powershell
Set-Location 'C:\Study\MatchArt\labor_market_system'
$env:MARKET_HOME = "$PWD"
$env:TEMP = "$PWD\.artifacts\tmp"
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
$env:PIP_CACHE_DIR = "$PWD\.artifacts\pip-cache"
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
New-Item -ItemType Directory -Force $env:TEMP,$env:PIP_CACHE_DIR | Out-Null
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.windows.lock.txt
.venv/Scripts/python.exe -m pip install --no-deps --no-build-isolation -e .
.venv/Scripts/python.exe -m pip check
# После размещения проверенного комплекта данных и модели:
.venv/Scripts/python.exe -m market_system.operations init
.venv/Scripts/python.exe -m market_system.operations diagnose --full
```

Не запускайте `initial_import` для регулярной работы/восстановления. Если нет данных, получите собственный backup отдельно. Для пустого нового каталога восстановление можно вызвать из уже установленной новой системы (пример ниже), затем выполнить чистую установку внутри восстановленного каталога. Окружение для Linux создаётся заново на Linux.

### Обязательные аккаунты и секреты

Создайте собственный веб-ADMIN один раз, если ещё нет вашего активного аккаунта; пароль вводится скрыто и дважды, можно оставить пустым, нажав Enter дважды. Создайте отдельные USER для друзей. Паролей по умолчанию нет. CLI — интерфейс локального владельца с доступом ОС к файлам; не предоставляйте USER shell-доступ и права чтения БД/backup. Веб-аккаунты и Telegram ID независимы.

```powershell
.venv/Scripts/python.exe -m market_system.operations create-admin admin
.venv/Scripts/python.exe -m market_system.operations create-user friend
```

Для необязательного Telegram создайте **свой** `state/secrets/telegram.env`: `BOT_TOKEN` от BotFather, `ADMIN_USER_IDS` с вашими положительными Telegram ID, `REPOSITORY_BACKEND=sqlite`, `EVENTS_ENABLED=false`. Пример без секретов — `deploy/linux/telegram.env.example`. Ограничьте ACL этого файла вашим Windows-пользователем; на Linux — владелец службы, `chmod 600`, каталог `chmod 700`. При изменении путей БД укажите соответствующие абсолютные пути `RESEARCH_DB_PATH`/`BOT_DB_PATH` в этом файле. Не включайте старый event receiver. Не копируйте Telegram-сессии из исследовательского проекта. Bot API требует токен; сборщик Telegram отдельно требует ваши API ID/hash и явный `collection setup-telegram`, `collection login-telegram` (скрытый ввод телефона, кода и при необходимости 2FA).

Включение `bot_enabled=true` в operations запускает polling при launcher. `delivery_enabled=true` отдельно разрешает исполнителю **настоящие отправки** из очереди; до этого настройте токен, ADMIN ID и доступ. Эти два режима независимы, по умолчанию оба false. В личном чате ADMIN допускает USER через `/allow ID`, отзывает `/deny ID`. USER и ADMIN видят/удаляют только свои подписки, даже при знании чужого subscription ID. Веб предоставляет поиск и администрирование, личные подписки управляются в Telegram; диагностический веб-раздел показывает только общие количества, без получателей и фильтров.

### Запуск и остановка

```powershell
# Весь локальный комплект: веб + один исполнитель + бот, только если включён:
.venv/Scripts/python.exe -m market_system.operations launch
# Либо отдельные терминалы (не одновременно с launcher):
.venv/Scripts/python.exe -m market_system.operations web
.venv/Scripts/python.exe -m market_system.operations worker
# Явная команда только после настройки собственных секретов:
.venv/Scripts/python.exe -m market_system.operations bot
```

Откройте http://127.0.0.1:8000. Все listeners привязаны к 127.0.0.1. Ctrl+C останавливает launcher и его детей. Исполнитель заканчивает текущий пакет; checkpoint сохраняется, running восстанавливается после перезапуска. Ожидание ограничено `shutdown_seconds`, затем дерево процессов завершается принудительно: на Windows Job Object, на Linux группы процессов. При выходе любого компонента весь комплект останавливается с ошибкой, чтобы частичный запуск не оставался незамеченным. Второй launcher и второй исполнитель отвергаются OS-блокировками. При принудительном завершении во время внешней отправки сохраняется прежнее окно возможного дубля по ID события.

### Диагностика и резервирование

```powershell
.venv/Scripts/python.exe -m market_system.operations diagnose
.venv/Scripts/python.exe -m market_system.operations diagnose --full
# Остановите launcher/worker; веб можно оставить (его записи временно блокируются):
.venv/Scripts/python.exe -m market_system.operations backup --target backups/my-snapshot
.venv/Scripts/python.exe -m market_system.operations verify-backup backups/my-snapshot
.venv/Scripts/python.exe -m market_system.operations restore backups/my-snapshot .artifacts/restored-check
```

`diagnose` проверяет quick_check/FK, версии и SHA-256 миграций, активный ready-набор/реестр, очереди processing/collection, handoff, задолженность доставки и подписки needs_update. Свежесть основана на последнем успешном сборе, не на дате публикации; отсутствие сведений явно unknown. `--full` дополнительно выполняет integrity_check, точное покрытие/связи ready и проверку SHA-256/зависимостей модели **до десериализации**. Выход 1 означает ошибки; warnings требуют внимания, но не заменяют данные догадками. ADMIN имеет `/diagnostics`, USER и анонимный посетитель к нему доступа не получают. Незавершённые experiments требуют ручного разбора; автоматической оценки/перезапуска нет.

Backup получает общую блокировку исполнителя; если он работает, команда отказывает. Для всех существующих SQLite (app, web, bot, annotations) берутся BEGIN IMMEDIATE-блокировки, затем создаются согласованные снимки **SQLite Backup API**, включая committed WAL. Файлы модели/словарей/разметки/кода и CSV источников проверяются на изменение во время копирования. Manifest фиксирует файлы/размеры/SHA-256, миграции, runtime и прежние корни. Секреты, Telegram-сессии, окружения, кеши и логи исключены. Копии содержат исходные тексты, личные подписки и хеши паролей: храните их с правами владельца, переносите на защищённый носитель. SHA-256 защищает от повреждения, не является подписью автора архива. Прерванная копия без manifest считается непригодной; имя для следующей попытки должно быть новым.

Restore сначала проверяет весь manifest/inventory/SHA-256, целостность SQLite и совместимость миграций. Целевой каталог должен **не существовать**, перезапись рабочей системы запрещена. Файлы копируются обычным копированием; исторические спецификации/научные данные БД не переписываются. Конфигурация переводится в local, polling/доставка выключаются; браузерные сессии сбрасываются, аккаунты/задания/подписки сохраняются. Отдельный процесс из восстановленного `src` выполняет `diagnose --full`; результат — `restore-verification.json`. При ошибке каталог остаётся для разбора, рабочая система не заменяется.

Проверяйте копию отдельно. На время проверки не включайте сбор/расписания/доставку; worker запускайте только если разрешено исполнить сохранённые задания. Для просмотра только веба остановите другие восстановленные процессы и задайте свободный порт в её operations.

```powershell
$env:MARKET_HOME = "$PWD\.artifacts\restored-check"
$env:PYTHONPATH = "$env:MARKET_HOME\src"
# Окружение чистой установки может быть общим для проверочного запуска:
.venv/Scripts/python.exe -m market_system.operations diagnose --full
.venv/Scripts/python.exe -m market_system.operations web
# Явный малый offline smoke: 3 реальные карточки и predict, без обучения/сети:
.venv/Scripts/python.exe "$env:MARKET_HOME\scripts\verify_stage11_restore.py"
# После проверки вернуть рабочий корень и убрать PYTHONPATH:
$env:MARKET_HOME = "$PWD"
Remove-Item Env:PYTHONPATH
```

### Будущий Linux-хост — НЕ ПРОВЕРЕНО НА LINUX

Шаблоны `deploy/linux/` подготовлены, но не запускались на Linux. Linux lock проверяется на доступность wheels CPython 3.12 x86_64 и SHA-256; это не Linux-тест, не проверка glibc/браузера/служб или запуска после перезагрузки. Перед размещением обязательно провести чистую установку, `pip check`, полный diagnose, восстановление и smoke на целевом Linux. Нужен x86_64 с glibc, совместимой с выбранными manylinux wheels (ориентир glibc >=2.28); другие архитектуры требуют отдельного lock. Playwright browser/системные библиотеки нужны только для DesignHunters и устанавливаются отдельно пользователем; это не требуется для поиска/веба/offline smoke.

Предполагается `/srv/labor-market` под отдельным системным пользователем `labor-market`, без публичного shell-доступа USER. Исходники `src/`, окружение `.venv/`, config, models, dictionaries доступны службе для чтения; data/state/logs/backups/.artifacts постоянные и доступны для записи согласно unit-файлам. Secrets только в `state/secrets/` вне исходников, не в Git. Все переменные TEMP/TMP/TMPDIR/PIP_CACHE_DIR при установке направьте в `.artifacts`; установите Python 3.12, создайте `.venv`, выполните из корня:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.linux.lock.txt
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/python -m pip check
MARKET_HOME=/srv/labor-market .venv/bin/python -m market_system.operations init
MARKET_HOME=/srv/labor-market .venv/bin/python -m market_system.operations diagnose --full
```

Замените `labor.example.org` на свой домен в `deploy/linux/operations.example.json`, скопируйте его в config/operations.json и самостоятельно настройте DNS и сертификат. Linux mode включает Secure cookie и доверяет forwarded scheme только от 127.0.0.1. Nginx-шаблон выполняет HTTP→HTTPS, TLS 1.2/1.3 и передаёт Host/scheme; Python-порт наружу не открывается. Пользователь отдельно устанавливает reverse proxy, сертификат и firewall (наружу только нужные HTTPS/SSH); настройки не выполнены автоматически.

После Linux-проверок пользователь копирует units в `/etc/systemd/system/`, выполняет `systemctl daemon-reload` и `systemctl enable --now market-web market-worker`. Это обеспечивает запуск после перезагрузки; один `market-worker` обслуживает processing, collection и необязательную доставку под общей блокировкой. Polling отдельно: `market-bot` включается только после настройки секретов. На сервере используйте units, launcher предназначен для локального комплекта. `KillMode=control-group` завершает потомков; journald ограничьте политикой хоста, Nginx-логи — штатным logrotate. Перед backup остановите worker (и bot для минимизации UI-записей), выполните CLI backup от `labor-market`, затем запустите службы обратно. Реальный сервер, HTTPS, токены, внешние отправки и перезагрузка Linux на этапе 11 не проверялись.

## Этап 10: модели и эксперименты

ADMIN-раздел `/models` показывает реестр, наборы результатов и эксперименты. Текущий локальный Ensemble зарегистрирован как active по прежнему каталогу; рабочая выдача не переключалась. Пять архивов исследовательских JSON/CSV доступны без обращения приложения к старому проекту. Непроверенные research joblib/npz не загружаются.

```powershell
.venv/Scripts/python.exe -m market_system.model_management list
.venv/Scripts/python.exe -m market_system.model_management results
.venv/Scripts/python.exe -m market_system.model_management experiments
# Ограниченная диагностика известных документов: обязательные ID, количество и секунды.
.venv/Scripts/python.exe -m market_system.model_management experiment ensemble_20260924T064346Z_32b7a759 --ids 162509,1849,3825 --max-documents 3 --seconds 120 --cohort development --purpose diagnostic
```

Эти три документа относятся к исторической целевой выборке с пересечением train/test; происхождение сохраняется, новым независимым аудитом результат не является. Test запрещён для selection. Ноль ошибок validation не гарантирует качество. Метрики полей рассчитываются только при совместимой подтверждённой разметке; исторические метки типа не подтверждают поля. Команда experiment оценивает фиксированные параметры, не обучает и не запускает старый benchmark. Время ограничивается для отдельного процесса загрузки/оценки; максимум 100 документов и 300 секунд. Сбор и processing worker не могут выполняться одновременно с этим экспериментом.

Регистрация нового доверенного локального комплекта: `model_management register --config config/candidate.json --adapter ensemble-v1`. Формат config совпадает с runtime.json, все пути остаются внутри нового проекта. Manifest должен иметь новую model_version при изменении артефактов, словарей или определений. Поддержанные runtime-зависимости и SHA-256 проверяются; непрочитанный формат даёт ошибку. Регистрация не обучает и не создаёт полную обработку.

`model_management designate VERSION` (или кнопка ADMIN) одобряет кандидата и ставит полную пересборку в очередь. Выполните её отдельным существующим jobs worker только когда готовы обрабатывать весь зафиксированный корпус. Назначение и завершение задания не меняют выдачу. После проверки ready-набора владелец явно выполняет `model_management activate RESULT_SET_ID` или нажимает активацию в `/models`. Та же команда с прежним ready ID — откат с сохранением актуальных человеческих исправлений. Прежние версии остаются в БД. Для подписок нужны подтверждённые соответствия классов в каждом направлении; совпадающие названия ничего не переносят автоматически.

`model_management approve VERSION` и `retire VERSION` управляют пригодностью модели. Рабочую модель сначала требуется сменить. Запуск веба выполняет миграцию 8, без загрузки модели, обучения или создания эксперимента. На этапе 10 полная пересборка, массовое обучение, сбор и реальные отправки не запускались. Проверки и ограничения — в журнале реализации.

## Этап 8: Telegram-поиск и уведомления

Создайте только в новом проекте файл `state/telegram-bot.env` (каталог state исключён из Git): `BOT_TOKEN` с собственным токеном BotFather и `ADMIN_USER_IDS` с Telegram ID владельца, через запятую для нескольких. Не копируйте старые секреты или сессии. Используйте `REPOSITORY_BACKEND=sqlite` (по умолчанию); `EVENTS_ENABLED` оставьте false. Для бота Bot API не нужна Telethon-сессия сборщика.

```powershell
# Поиск и управление собственными подписками:
.venv/Scripts/python.exe -m market_system.telegram_bot --env-file state/telegram-bot.env
# В другом терминале — единственный исполнитель. Флаг включает реальные отправки:
.venv/Scripts/python.exe -m market_system.jobs worker --telegram-env state/telegram-bot.env
```

Без `--telegram-env` worker собирает/обрабатывает данные и сохраняет события, но не отправляет их. Уже работающий worker следует остановить перед запуском с флагом: два исполнителя запрещены. В рабочей БД доступ пока никому не открыт, подписок и событий нет; токен отсутствует, живой Telegram не проверялся.

В личном чате владелец выполняет `/allow 123456789` для допуска друга; `/deny 123456789` отзывает доступ. Каждый, включая ADMIN, управляет только своими подписками. Команды:

```text
/search {"post_type":"vacancy","work_format":"remote"}
/classes skill Python
/search {"post_type":"vacancy","skill_ids":[123],"skills_mode":"ALL"}
/next
/subscribe
/subscriptions
/unsubscribe ID_ПОДПИСКИ
```

ID 123 — пример; используйте настоящий ID из `/classes` с нужным namespace/version. `/help` перечисляет все поля общего фильтра. `/subscribe` сохраняет последний фильтр без курсора; можно передать JSON напрямую. Даты — фиксированные ISO-интервалы с часовым поясом. Поиск доступен также по резюме; уведомления первой версии — только вакансии.

Уведомления приходят один раз на новый уникальный текст и пользователя, кроме возможного повтора при аварии после внешней отправки до записи квитанции. ID события остаётся тем же. Повторная публикация, правки старой карточки и пересборка прежнего корпуса не рассылаются. Данные первоначальной установки не становятся рассылочной очередью. Событие относится к готовому снимку; ручная активация каталога не является условием отправки.

При смене модели несовместимая подписка видна в `/subscriptions` как «Требует обновления» с причиной. Не заменяйте класс по названию: ADMIN явно подтверждает `/map OLD_ID NEW_ID MODEL_VERSION`, либо пользователь создаёт новую подписку с подходящими ID. Перенесённые старые фильтры с названиями требуют пересоздания. Старые файлы состояния сохраняются. История доставки находится в telegram_outbox/telegram_deliveries/telegram_attempts рабочей БД.

## Проверка документов

Войдите как ADMIN и откройте «Проверка» (`/review`) или ссылку исправления в карточке. Выберите очередь либо создайте случайную выборку аудита. Форма позволяет исправить тип, профессии, навыки с ролями, зарплату и условия, добавить пропущенные значения, отметить несколько предложений. Сохраняйте промежуточные изменения как draft/uncertain; confirmed заменяет рабочие значения. При конфликте ревизий перечитайте документ и согласуйте изменения.

Для неизвестного термина создайте предложение и отдельно подтвердите определение словаря, затем подтвердите документ. Одинаковое название не означает одинаковый класс. Машинные основания и предыдущие ревизии сохраняются. Подтверждение создаёт новую полную копию каталога: на большой БД операция требует времени и дополнительного места. Неоднозначная зарплата нескольких предложений не попадает в аналитику по профессии.

Инициализация и явный идемпотентный импорт перенесённой разметки (в рабочей БД уже выполнены):

```powershell
.venv/Scripts/python.exe -m market_system.review init
.venv/Scripts/python.exe -m market_system.review import-legacy
```

Старый исследовательский редактор отключён; архив и train/development/test сохранены. Метки типа не подтверждают остальные поля; импорт не запускает обучение.

Python 3.12, отдельное окружение `.venv`. Команды выполняются из этого каталога в PowerShell:

```powershell
$env:TEMP = "$PWD\.artifacts\tmp"
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = "$PWD\.artifacts\pip-cache"
New-Item -ItemType Directory -Force $env:TEMP,$env:PIP_CACHE_DIR | Out-Null
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
powershell -ExecutionPolicy Bypass -File scripts\verify.ps1
```

Локальная рабочая БД — `data/app.db`; модель и её параметры указаны в `config/runtime.json`. Токены и Telegram-сессии не переносились. Процессы запускаются явно через operations.

Первоначальный импорт (только при отсутствии рабочей БД и целевых артефактов):

```powershell
.venv\Scripts\python.exe -m market_system.initial_import --source-file config/import-source.json --model ensemble_20260924T064346Z_32b7a759
```

`config/import-source.json` используется только командой переноса; обычный запуск его не читает. Источник SQLite открывается `mode=ro`, снимок создаётся Backup API с учётом WAL. Существующая `app.db` приводит к отказу с кодом 2 до чтения источника. Это не команда обновления; автоматического слияния/перезаписи нет. После прерванной публикации не удаляйте файлы для повторного запуска: сверяйте staging и манифест, восстанавливайте только по отдельному явному решению.

Автономный запуск с тремя реальными текстами, без обучения и серверов:

```powershell
.venv\Scripts\python.exe -m market_system smoke --output .artifacts/stage1-smoke.json
.venv\Scripts\python.exe scripts/check_stage1_isolation.py
.venv\Scripts\python.exe scripts/verify_stage1.py
```

Последняя команда проверяет равенство **исходному снимку импорта**. После сознательных изменений рабочей БД отличие от снимка ожидаемо: не запускайте повторный импорт для его устранения. Манифест — `data/import_manifest.json`. Проверка изоляции запрещает старый каталог через audit hook и не изменяет его права доступа. Подробные ограничения — в журнале.


Поисковая схема: миграции 1–2, активный набор `initial_saved_v1`. Команды:

```powershell
.venv\Scripts\python.exe -m market_system.search.store migrate
.venv\Scripts\python.exe scripts/verify_stage2.py
.venv\Scripts\python.exe -m market_system.search.store activate --id initial_saved_v1
```

Обычная проверка этапа 2 не переключает активный набор; `--activate` делает это явно после проверок. Повторная сборка требует нового ID. Полная схема, правила ролей, неоднозначных зарплат, дат и отката — в docs/CONTRACTS.md. Общая служба поиска реализована на этапе 3; веб подключён на этапе 4, Telegram — на этапе 8.


Общий поиск (без запуска интерфейсов):

```python
from market_system.search import Catalog, SearchFilter

filters = SearchFilter(
    post_type="vacancy",
    salary_min=100_000, currency="RUB", salary_period="month",
    tax_mode="net", work_format="remote",
)
page = Catalog().search(filters, page_size=20)
if page.next_cursor:
    next_page = Catalog().search(filters, page.next_cursor, page_size=20)
```

Классы задаются через `occupation_ids` и `skill_ids` из `classes.class_id`, не названиями. Семантика неизвестных значений, ролей, дат и курсоров — в разделе этапа 3 CONTRACTS.md. Миграция поискового индекса: `.venv/Scripts/python.exe -m market_system.search.optimize`. Поиск открывает БД только для чтения и не мигрирует её автоматически. Измерения на копии данных: [docs/STAGE3_BENCHMARK.md](docs/STAGE3_BENCHMARK.md).


## Веб-интерфейс

В PowerShell, из корня нового проекта:

```powershell
Set-Location 'C:\Study\MatchArt\labor_market_system'
.venv\Scripts\python.exe -m market_system.web create-admin admin
.venv\Scripts\python.exe -m market_system.web serve
```

Владельца создайте один раз: пароль вводится скрыто с подтверждением (можно оставить пустым, нажав Enter дважды). Затем откройте [http://127.0.0.1:8000](http://127.0.0.1:8000). При последующих запусках нужна только команда `serve`. Учётных записей и паролей по умолчанию нет. Для пользователя поиска: `.venv\Scripts\python.exe -m market_system.web create-user <логин>`.

ADMIN видит обзор, каталог, карточки и настройки. USER — каталог и карточки; административные GET/POST закрыты на сервере. Настройки размера страницы, начального типа публикации и порога свежести сохраняются в `state/web.db`. Каталог открывается только для чтения; правила фильтрации использует общая служба этапа 3.

Новые зависимости установлены в .venv и закреплены в requirements.lock.txt; при восстановлении окружения установите lock-файл. Политика сессий, CSRF, переменные окружения и границы этапа — в разделе этапа 4 CONTRACTS.md. Браузерная проверка выполнена на реальном каталоге с отдельными временными аккаунтами; эти аккаунты отключены.


## Этап 5: постоянная обработка

Команды выполняются из корня нового проекта. Модель берётся из config/runtime.json; обучение не запускается.

```powershell
.venv/Scripts/python.exe -m market_system.jobs init
.venv/Scripts/python.exe -m market_system.jobs create --kind incremental --batch-size 64
.venv/Scripts/python.exe -m market_system.jobs show
.venv/Scripts/python.exe -m market_system.jobs worker
```

Worker запускается отдельным процессом и держит блокировку одной SQLite. Для исполнения одного задания используйте `worker --once`. После аварии перезапустите worker: завершённые пакеты сохраняются, незавершённый пакет обрабатывается повторно.

```powershell
.venv/Scripts/python.exe -m market_system.jobs create --kind sample --ids 1,3,6 --batch-size 2
.venv/Scripts/python.exe -m market_system.jobs show JOB_ID
.venv/Scripts/python.exe -m market_system.jobs cancel JOB_ID
.venv/Scripts/python.exe -m market_system.jobs retry JOB_ID
```

`sample` не активируется. `full` создаёт набор на всём зафиксированном корпусе; запускайте его явно только при необходимости новой модели/полной пересборки. `incremental --ids ...` повторяет выбранные документы и требует включить все новые ID. Модель не получает новую версию из-за новой порции данных. `--key` задаёт ключ идемпотентности создания; `--db PATH` перед подкомандой выбирает другую локальную БД.

В вебе ADMIN видит раздел «Задания», создаёт задания, просматривает прогресс/ошибки, повторяет и отменяет их. Для продвижения queued нужен отдельно запущенный worker. HTTP не выполняет ML. Ready-набор автоматически не активируется; действующий каталог остаётся доступным. Подтверждённые исправления сохраняются отдельно от машинного результата и имеют приоритет в проекции.

Итоги и ограничения: docs/IMPLEMENTATION_STATE.md, контракт: docs/CONTRACTS.md. Worker обслуживает обработку, сбор и явно включённую доставку.


## Этап 6: источники, сбор и расписание

ADMIN открывает раздел «Источники», добавляет Telegram или DesignHunters, запускает историю/регулярный сбор, видит ошибки и повторяет задания. Источник можно связать с уже импортированным Source, указав его прежние тип и название; это необходимо, если его URL уже присутствуют в базе. Расписание изначально выключено. Интервал задаётся в секундах; задания исполняются только при работающем worker.

```powershell
.venv/Scripts/python.exe -m market_system.collection init
.venv/Scripts/python.exe -m market_system.collection list
.venv/Scripts/python.exe -m market_system.collection add telegram ИМЯ_ИСТОЧНИКА https://t.me/CHANNEL_NAME
.venv/Scripts/python.exe -m market_system.collection collect SOURCE_ID --mode history
.venv/Scripts/python.exe -m market_system.jobs worker
```

Telegram требует отдельной настройки в новом проекте:

```powershell
.venv/Scripts/python.exe -m market_system.collection setup-telegram
.venv/Scripts/python.exe -m market_system.collection login-telegram
```

Введите собственные API ID/hash, телефон, код и 2FA через скрытый ввод. Файлы создаются только в state/collection. Старую сессию не копировать. Worker не выполняет интерактивный вход. Эти команды не запускались автоматически; живой сбор на этапе 6 не проверен.

Сначала запустите history. Одна порция по умолчанию ограничена 1000 сообщениями; следующий history продолжит её. Затем new собирает новые ID и проверяет последние 3 дня, максимум 500 сообщений перекрытия. Параметры overlap_days/overlap_limit доступны в форме JSON. Старые правки вне окна и удаления не гарантированно обнаруживаются.

DesignHunters использует локальный CSV первого столбца с HTTPS URL designhunters.ru, без заголовка. В форме выберите тип DesignHunters, адрес https://designhunters.ru и параметры, например `{"csv_path":"config/designhunters-urls.csv"}`. CSV нужно создать с действительными URL. history продолжает список; new регулярно перепроверяет его. Новые URL сайт автоматически не обнаруживает. Для запуска потребуется установленный Chromium Playwright; наличие браузера и актуальность селекторов на живом сайте этим этапом не проверялись.

Повтор и передача в ML:

```powershell
.venv/Scripts/python.exe -m market_system.collection retry COLLECTION_JOB_ID
.venv/Scripts/python.exe -m market_system.collection dispatch
```

Сохранение пакета и курсора атомарно; исправление текста того же URL создаёт ревизию, старый текст остаётся. Worker создаёт incremental-задания обработки после сохранения. Если активный набор несовместим с настроенной моделью, данные остаются в базе, ADMIN видит ошибку передачи; массовая full-пересборка автоматически не запускается. Готовый набор также не активируется автоматически.

На этапе 6 рабочая БД получила миграцию 4 без реального сбора. Текущее состояние, проверки и ограничения записаны в docs/IMPLEMENTATION_STATE.md; приёмка — в docs/ACCEPTANCE.md.

## Аналитика ADMIN — этап 9

В существующем веб-приложении откройте `/analytics` под ADMIN. Доступны шесть отчётов общего каталога, общий период UTC/источники/тип, переходы к документам и CSV с manifest в ZIP. Сохранение научного среза фиксирует входы, эффективные исправления, определения, версии, фильтры, SHA-256, алгоритм и результаты; последующая смена каталога или исправлений не меняет срез. USER не имеет доступа.

Полный архив среза содержит `inputs.json`, `reports.json`, `engine.py`, CSV и `manifest.json`. После распаковки внутри нового проекта воспроизведите стандартной библиотекой:

```powershell
.venv\Scripts\python.exe <каталог-архива>\engine.py <каталог-архива>\inputs.json <каталог-архива>\replay.json
Get-FileHash <каталог-архива>\replay.json -Algorithm SHA256
```

Сравните SHA-256 с `files_sha256["reports.json"]` из manifest. Рабочая БД для автономного пересчёта не нужна. Репосты — дополнительные публикации того же текста; навыки считаются один раз на документ. Зарплаты разделены по валюте, периоду и gross/net; неизвестные единицы не подменяются месячными. Все выводы ограничены выбранными источниками. Подробные единицы и ограничения — в отчётах и `docs/CONTRACTS.md`.

