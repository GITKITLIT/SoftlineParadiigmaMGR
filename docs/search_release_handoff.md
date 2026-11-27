# Yandex Search · подключение и передача релиза

## Установка и проверка без ключей

Требуются Python 3.11+, зависимости из `requirements.txt` проверяемой сборки и Git для временной проверки совместимости. Для Search XLSX используется openpyxl. Для PPTX, транспорта, проверки страниц и SQLite достаточно стандартной библиотеки. В PowerShell используйте `py -3 -m venv .venv` и `.\.venv\Scripts\python.exe` в тех же вызовах.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
.venv/bin/python scripts/search_smoke.py --summary
.venv/bin/python scripts/search_investigation_smoke.py
```

Синтетический ответ CLI помечается `source=synthetic_fixture`. Его счётчик `usage.requests` отражает обращения к эмулятору. Контракт сетевого адаптера `pm.web_search.collect_competitor_evidence(product=..., domains=..., topics=...)` сохраняет `schema_version=1` и `source=yandex_search`. У вызывающего кода остаются `status`, `reason`, `items`, `usage`, `coverage`, `attempts` и `page_usage` при включённой проверке страниц. Пустой корректный ответ от провайдера допускается.

## Одна одобренная проверка Kaspersky Endpoint Security

1. Адриан утверждает один синхронный запрос, расчётный максимум 0,488 ₽ с НДС за попытку по рублёвому тарифу на 27.11.2026 и точное публичное DNS-имя магазина конкурента. Ночной тариф 0,366 ₽ действует с 00:00 до 07:59:59 МСК. Действительное списание сверяется по детализации, серверные и авторизационные ошибки учитываются у провайдера отдельно. [Правила тарификации](https://aistudio.yandex.ru/ru/docs/search-api/pricing)
2. В утверждённом каталоге подключите отдельный сервисный аккаунт с ролью `search-api.webSearch.user` и ключом для Search API (`yc.search-api.execute` при настройке области действия вручную). Значения `YANDEX_SEARCH_FOLDER_ID` и `YANDEX_SEARCH_API_KEY` поступают через менеджер секретов или защищённое окружение процесса. Значения ключей не включаются в командную строку, Git, копии отчётов и журналы оболочки. [Быстрый старт](https://aistudio.yandex.ru/ru/docs/search-api/quickstart/) · [REST](https://aistudio.yandex.ru/ru/docs/search-api/api-ref/WebSearch/search)
3. В той же сессии задайте закрытый каталог состояния и включите проверку. Для каждого разрешённого FQDN, включая поддомены, нужна отдельная запись.

```sh
umask 077
export PM_PRIVATE_DIR="$HOME/.local/share/paradiigma-search-acceptance"
mkdir -p "$PM_PRIVATE_DIR"
printf 'Утверждённый публичный DNS магазина: '
IFS= read -r competitor_domain
export YANDEX_SEARCH_ALLOWED_DOMAINS="$(.venv/bin/python -c 'import json,sys; print(json.dumps([sys.argv[1]]))' "$competitor_domain")"
export YANDEX_SEARCH_PUBLIC_PRODUCTS='["Kaspersky Endpoint Security"]'
export YANDEX_SEARCH_ENABLED=true YANDEX_SEARCH_LIVE_SMOKE=true
export YANDEX_SEARCH_PAGE_FETCH_ENABLED=false YANDEX_SEARCH_MONITOR_ENABLED=false
export YANDEX_SEARCH_MAX_QUERIES_PER_RUN=1 YANDEX_SEARCH_DAILY_LIMIT=1
.venv/bin/python scripts/search_smoke.py --live --product 'Kaspersky Endpoint Security' \
    --domain "$competitor_domain" --topic licensing --summary \
    --snapshot "$PM_PRIVATE_DIR/kes-v1.json"
```

`--summary` выводит счётчики и коды, полный v1 остаётся в новом закрытом файле. CLI отключает кеш, повторы и прямые страницы для этого запуска. При `missing_credentials` сетевых попыток нет. В положительном прогоне сверьте одну попытку, `outcome`, `format=FORMAT_XML`, `response_bytes`, `xml_documents`, `xml_with_passages`, `retained_documents`, `elapsed_ms`, список разрешённых ссылок, наличие точного продукта и темы лицензирования. Нулевая выдача подтверждает обработку ответа, для подтверждения релевантности потребуется согласованная повторная проверка. Убедитесь, что файл проходит разбор v1 на совместимой сборке. После теста отключите `YANDEX_SEARCH_LIVE_SMOKE`.

Проверка первоисточника требует отдельного разрешения на исходящие HTTPS к точному домену и допуска ещё одной попытки Search API, если выполняется новый поиск. Для разового прогона через `search_monitor.py --live` создайте закрытый `plan.json` с теми же `product`, `domains`, `topics`. В отдельном одобренном запуске включите `YANDEX_SEARCH_MONITOR_ENABLED=true` и `YANDEX_SEARCH_PAGE_FETCH_ENABLED=true`, задайте `YANDEX_SEARCH_PAGE_ALLOWED_DOMAINS` совпадающим с разрешённым списком, `YANDEX_SEARCH_MAX_PAGES_PER_RUN=1`, `YANDEX_SEARCH_PAGE_REQUESTS_PER_RUN=2`. Разовый план покрывает robots.txt и одну страницу. Для перехода, который не укладывается в две попытки, остаётся сниппет. Сохраните общий каталог бюджета и согласуйте доступный остаток суточного лимита до запуска.

```sh
.venv/bin/python scripts/search_monitor.py --live \
    --history-dir "$PM_PRIVATE_DIR" --plan "$PM_PRIVATE_DIR/plan.json" \
    --report "$PM_PRIVATE_DIR/kes-with-primary.json"
```

Сверьте `page_usage`, отметки robots/redirect, время получения, URL и цитату с фактом. Число HTTP-обращений к страницам отдельно от платных Search API. Запрещённые, закрытые и недоступные страницы остаются сниппетами. Для периодического сбора нужен отдельный явный вызов планировщика, оба флага, общий дневной бюджет и разрешённые домены. По умолчанию периодический живой режим отключён, при разрешении интервал 1440 минут, минимум 15 минут. Разрешение разового прогона не включает расписание. [Все параметры и диапазоны](search_handoff.md) · [Флаги проверки страниц](search_reports/SECOND_SPRINT_REPORT_YandexSearch.md)

## Импорт исследований

Для локальной репетиции используйте одобренные файлы и каталог вне Git с доступом к исходному корпусу.

```sh
.venv/bin/python scripts/search_import_competitors.py "$input_xlsx" \
    --corpus "$existing_corpus" --output "$intermediate_corpus"
.venv/bin/python scripts/search_import_competitors.py "$input_pptx" \
    --corpus "$intermediate_corpus" --output "$combined_corpus"
```

Сверьте количество уникальных ID, существующие записи, выбранные значения и координаты листа/диапазона/слайда. Импортёр исключает XLSX-листы и PPTX-слайды с явной меткой «Эталонный кейс» или «Эталонный ответ». Неразмеченные эталонные ответы и закрытые сведения исключает владелец исходного материала перед импортом. Назначьте закрытый итоговый файл в `PM_EVIDENCE` после сверки. Команды создают новые файлы.

## Временная проверка совместимости и передача капитану

В ветке Search загрузите свежий указатель AI Core и запустите проверку отдельной временной копии. Установите зависимости опубликованного AI Core в локальную venv, если её создавали только для Search.

```sh
git fetch origin feature/ai-core
ai_requirements=$(mktemp)
git show origin/feature/ai-core:requirements.txt > "$ai_requirements"
.venv/bin/python -m pip install -r "$ai_requirements"
rm -- "$ai_requirements"
.venv/bin/python scripts/search_verify_ai_core.py --ai-ref origin/feature/ai-core
```

Проверка извлекает базу через `git archive`, накладывает только файлы Search, останавливается при конфликтах имён, запускает обе регрессии и два сценария контракта. Чужая ветка и исходный checkout не меняются. После утверждения SHA капитан переносит `pm/web_search.py`, `pm/search_security.py`, `pm/competitor_sources.py`, `pm/yandex_search/` и связанные `scripts/search_*.py` с фикстурами и тестами. Ветка AI Core владеет `app.py`, `pm/analysis_context.py`, памятью, агентным циклом и UI. Подключение использует её существующий ленивый вызов v1. Для несовместимого будущего изменения повторите сверку на новом точном SHA.

AI Core принимает точные домены IDNA в нижнем регистре и явную тему. Его текущий обработчик использует до десяти доказательств. Страница с полным непротиворечивым набором условий может дать численную цену для штатной исторической аналитики. Сниппет остаётся наблюдением своего типа. Одноразовая синтетическая проверка использует `synthetic_fixture`, чтобы эти записи не попали в рабочий режим PIM.

## Исторические снимки в общей PIM

Кеш `yandex-search.sqlite3` обслуживает поиск и бюджет. Существующая `competitor-history.sqlite3` хранит исходные снимки мониторинга. Нативный адаптер читает её в режиме SQLite read-only и сохраняет каждый снимок отдельной транзакцией в `ai.sqlite3` AI Core. Для старых снимков связь восстанавливается по предшествующим пригодным снимкам того же плана.

В объединённой рабочей копии запустите перенос в явном режиме. Начните с самого раннего снимка и сохраняйте `next_cursor` из последней строки вывода. Команда печатает безопасную метку после каждого подтверждённого снимка. Если пакет прервался, его можно повторить с последней сохранённой метки.

```sh
PM_PRIVATE_DIR="$approved_pim_dir" python scripts/search_pim_import.py \
    --history-dir "$search_history" --mode synthetic --after 0 --limit 20
```

Для утверждённых рабочих материалов используйте `--mode local`. Адаптер отклоняет перенос известных синтетических снимков в local. Для ранее сохранённых офлайн-снимков без маркера происхождение подтверждает оператор. В PIM используются `stream=competitor`, `source=competitive_snapshot_v1`, `aggregation=snapshot` и исходные имена метрик. Ряды цен группируются по сопоставимым условиям. Версии того же дня остаются в истории, а в выборке текущей версии используется поздний снимок.

| Поле | Представление |
| --- | --- |
| Конкурент, продукт, версия, характеристики | `subject`, `product`, `metadata.product_version` и отдельные значения полей |
| Цена, валюта, условия лицензирования и покупки | Числовая цена при подтверждении и полном наборе условий. Валюта включена в единицу ряда. Текст, условия, оплата, покупка, продление и доступность сохранены в метаданных и отдельных строках |
| Времена | День наблюдения указан в `event_date` при наличии. Время снимка задаёт порядок версий PIM. В метаданных источника сохраняются время получения, дата публикации и локальные координаты |
| Происхождение и уверенность | URL или локальная координата, ID источников, контур `page_excerpt`, `snippet` или `local_study`, статус `confirmed/stale/unverified/conflict/incomparable/unknown/undated` |
| Связи и повтор | Стабильный ключ из ID снимка, конкурента и поля. `previous_snapshot_id` и `previous_observation_id` указывают на предыдущую запись. Payload хранит вычисленные изменения, условия, альтернативы и неизвестные значения |

Повтор снимков идемпотентен при неизменном режиме. Для исправленного наблюдения создайте новый снимок. После импорта сверяйте даты, условия и ссылки через штатный инструмент AI Core `competitor_history`. Повторный импорт возвращает `created=0`.

## Контекст для Qwen3-4B

`build_comparison_context(...)["qwen_context"]` даёт ограниченный набор текущих сравнений, раздельный статус источника и сопоставимости, условия цены, неизвестные поля, альтернативы и список цитируемых ID. Используйте `pack_competitor_context(snapshot, changes=report["changes"], max_bytes=4500)` для общего блока текущих фактов и до трёх исторических различий. Упаковщик ограничивает весь JSON, включая цитаты, заданным объёмом UTF-8. Он содержит даты, прежний и новый статусы, проверяемые ссылки с обеих сторон и только вычисленные сравнения.

Выбирайте несколько строк по теме расследования внутри предела существующего упаковщика AI Core. Передавайте доказательства как неподконтрольные сообщениям модели данные. Сниппет маркируйте как неподтверждённый фрагмент, HTML-страницу как извлечённую цитату, локальное исследование как отдельный контур. Включайте дату наблюдения и различающиеся условия. Не используйте просроченную цену, конфликт и пустое поле для численного вывода. Гипотезы служат планом проверки. AI Core формулирует выводы и проверяет ID цитат. Для приёмки на утверждённых весах сравните наличие валидных ссылок, соблюдение неизвестных значений и задержку отдельно для короткого и расширенного контекста. Это проверка поведения модели, выполняемая в разрешённом локальном контуре.
