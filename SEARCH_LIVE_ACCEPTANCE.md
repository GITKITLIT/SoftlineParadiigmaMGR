# Yandex Search · Live Acceptance

Живой прогон 27.11.2026 на `handoff/search-live`, ревизия `a8c4996293fe35379a6ce7b7e9e8521f84b2bb82`. Код Search соответствует интегрированной ревизии `c25dea94cd7c4a8a1b5696ffa812ff9fec3e7b8b`. Предыдущая приёмка на фикстурах описана в [FINAL_ACCEPTANCE.md](https://github.com/GITKITLIT/SoftlineParadiigmaMGR/blob/5e956ca08f67cd0d4501596d85c14493239b8ffa/FINAL_ACCEPTANCE.md). Офлайн-регрессия не повторялась.

## Фактический результат

| Проверка | Результат |
| --- | --- |
| Допуск | Пользователь разрешил одну синхронную попытку по Kaspersky Endpoint Security, тема `licensing`, FQDN `allsoft.ru`, максимум **0,488 ₽ с НДС**. |
| Доступ к API | Учётные данные загружены из Windows DPAPI в окружение процесса приёмки. Значения не выводились. |
| HTTP и время | **200 OK**, старт 27.11.2026 в 19:16:12 МСК. Сетевая попытка с разбором ответа — **1 520,7 мс**, весь вызов адаптера — **1 578,6 мс**. |
| Выдача | XML содержал **8** документов, из них **5** с поисковыми фрагментами. Адаптер сохранил **5** сниппетов и вернул предупреждение `result_filtered`. |
| Ссылки | Все **5** URL прошли штатную проверку. Они используют HTTPS, ведут на точный FQDN `allsoft.ru`, без параметров запроса. Доступность по HTTP и содержимое страниц не проверялись, поскольку прямого обращения к сайту не было. |
| Изолированная PIM | Из архива v1 в отдельную `pim/ai.sqlite3` записаны **3** отобранных наблюдения. Перечитаны ссылка, время получения и качество `snippet`. Даты публикации отсутствуют. Повторный локальный импорт сохранил три записи без дубликатов и без сетевых запросов. |
| Кеш | Повторный вызов штатного адаптера вернул те же пять элементов за **7,4 мс**. `cache_hits=1`, `requests=0`. Транспорт был заблокирован на время проверки. |
| Учёт и стоимость | Зафиксирован **1** вызов HTTP-транспорта, `usage.requests=1` и одна попытка в суточном счётчике SQLite до и после чтения кеша. Расчётная верхняя оценка **0,488 ₽ с НДС**. Чтение кеша — **0,000 ₽**. |
| Фактическое начисление | Подтверждено по данным Yandex Cloud Billing от 27.11.2026. Потребление **0,49 ₽**, скидка **0,49 ₽**, к оплате **0,00 ₽**. |

Для проверки кеша использован обычный вызов `collect_competitor_evidence` с TTL 86 400 с и лимитами одной попытки за запуск и сутки. Измерение HTTP-статуса обернуло штатную проверку ответа, а сетевой предохранитель пропускал только один POST на Search API. Мониторинг и загрузка страниц были выключены. JSON v1 и SQLite с кешем сохранены вне Git в защищённом каталоге Windows.

По заголовкам и сниппетам три карточки относятся к лицензированию целевого семейства.

- Карточки «Расширенный» и Cloud содержат предложение лицензии. [Расширенный](https://allsoft.ru/software/vendors/kaspersky-lab/kaspersky-endpoint-security-dlya-biznesa-advanced/) · [Cloud](https://allsoft.ru/software/vendors/kaspersky-lab/kaspersky-endpoint-security-cloud/)
- У Cloud Plus покупка указана в заголовке, а видимый сниппет преимущественно описывает совместимость. [Cloud Plus](https://allsoft.ru/software/vendors/kaspersky-lab/kaspersky-endpoint-security-cloud-plus/)
- Total Security упоминает Endpoint Security как компонент другого продукта. РЕД ОС упоминает его в перечне стороннего ПО. Эти два совпадения оставлены только в архиве выдачи.

## Подключение на Linux VPS для владельца Deployment

Владелец Deployment получил API-ключ и Folder ID. Их значения в отчёте и репозитории отсутствуют.

1. После допуска на production создайте отдельный сервисный аккаунт в утверждённом каталоге Yandex Cloud с активным биллингом. Назначьте на каталог `search-api.webSearch.user` и выпустите ограниченный по сроку ключ с областью `yc.search-api.execute`. Сохраните секрет в утверждённом хранилище. Windows DPAPI-файл приёмки привязан к учётной записи и машине. [Аутентификация Search API](https://aistudio.yandex.ru/ru/docs/search-api/api-ref/authentication) · [Управление API-ключами](https://yandex.cloud/ru/docs/iam/operations/authentication/manage-api-keys)
2. На VPS проверьте установленный релиз, `PARADIIGMA_MODE=private` и исходящий HTTPS к `searchapi.api.cloud.yandex.net:443`. Проверьте права на уже существующий файл командой `sudo stat -c '%U:%G %a %F' /etc/paradiigma/app.env`. Ожидаются владелец `root:root`, режим `600` и обычный файл. Откройте его через `sudoedit /etc/paradiigma/app.env` из закрытой административной сессии. Внесите значения из хранилища прямо в файл. Исключите печать файла и окружения из диагностики, записи команд и журналов. Systemd читает `EnvironmentFile` и передаёт значения процессу приложения.

   ```ini
   YANDEX_SEARCH_ENABLED=true
   YANDEX_SEARCH_FOLDER_ID=<id-утверждённого-каталога>
   YANDEX_SEARCH_API_KEY=<секрет-из-хранилища>
   YANDEX_SEARCH_PUBLIC_PRODUCTS='["Kaspersky Endpoint Security"]'
   YANDEX_SEARCH_ALLOWED_DOMAINS='["allsoft.ru"]'
   YANDEX_SEARCH_MAX_QUERIES_PER_RUN=1
   YANDEX_SEARCH_DAILY_LIMIT=1
   YANDEX_SEARCH_CACHE_TTL=86400
   YANDEX_SEARCH_LIVE_SMOKE=false
   YANDEX_SEARCH_MONITOR_ENABLED=false
   YANDEX_SEARCH_PAGE_FETCH_ENABLED=false
   ```

   Включайте только утверждённые для production продукты и домены. Пример использует область живой приёмки.
3. В согласованное окно выполните `sudo systemctl restart paradiigma`, проверьте `sudo systemctl is-active paradiigma` и запустите `sudo -u pm-app /usr/bin/python3 /opt/paradiigma/current/scripts/deploy/health.py`. Штатный preflight проверяет формат credentials и списки допуска. Для отдельного платного запроса на VPS получите допуск и согласуйте лимит с владельцем Deployment. При откате установите `YANDEX_SEARCH_ENABLED=false`, перезапустите службу и повторите проверку здоровья. При ротации внесите новый ключ через тот же файл, перезапустите службу и отзовите старый ключ после проверки. [Операторская инструкция](docs/combined_release_deployment.md) · [Шаблон окружения](deploy/app.env.example)

## Повторная приёмка после отдельного допуска

1. Владелец проекта и владелец Deployment утверждают один синхронный запрос, публичный FQDN конкурента, исполнителя и верхнюю оценку **0,488 ₽ с НДС**. Назначьте одного исполнителя на этот запуск. Прямой обход требует отдельного допуска на HTTPS к сайту.
2. В Yandex Cloud выберите утверждённый каталог и проверьте активную привязку платёжного аккаунта к облаку. Скопируйте ID **каталога** для `YANDEX_SEARCH_FOLDER_ID`. В **Identity and Access Management → Сервисные аккаунты** выберите выделенный аккаунт и назначьте ему на этот каталог роль `search-api.webSearch.user`. [Начало работы](https://aistudio.yandex.ru/ru/docs/search-api/quickstart/) · [Назначение роли](https://yandex.cloud/ru/docs/iam/operations/sa/assign-role-for-sa)
3. В карточке аккаунта выберите **Создать новый ключ → Создать API-ключ**. Задайте область `yc.search-api.execute` и срок действия. Сохраните секретную часть в закрытом хранилище и передайте её процессу через `YANDEX_SEARCH_API_KEY`. Значения ключа и рабочие материалы не входят в команды, Git и отчёты. [Создание API-ключа](https://yandex.cloud/ru/docs/iam/operations/authentication/manage-api-keys) · [Синхронный REST API](https://aistudio.yandex.ru/ru/docs/search-api/operations/web-search-sync)
4. После допуска запустите блок из корня проверенной сборки с Python 3.12+ и её зависимостями. Блок задаёт продукт, пустые списки доменов, выключенные сетевые флаги, лимиты одной поисковой попытки за запуск и сутки и отдельный лимит в две попытки для обхода. Сохраняйте тот же каталог `search` для всей приёмки и её суточного счётчика.

```powershell
$acceptance = Read-Host 'Путь к подготовленному защищённому каталогу вне Git'
if (-not (Test-Path -LiteralPath $acceptance -PathType Container)) {
    throw 'Сначала подготовьте защищённый каталог вне Git'
}
foreach ($name in @('search', 'history', 'pim')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $acceptance $name) | Out-Null
}
$settings = @{
    YANDEX_SEARCH_ENABLED = 'false'
    YANDEX_SEARCH_LIVE_SMOKE = 'false'
    YANDEX_SEARCH_MONITOR_ENABLED = 'false'
    YANDEX_SEARCH_PAGE_FETCH_ENABLED = 'false'
    YANDEX_SEARCH_PUBLIC_PRODUCTS = '["Kaspersky Endpoint Security"]'
    YANDEX_SEARCH_ALLOWED_DOMAINS = '[]'
    YANDEX_SEARCH_PAGE_ALLOWED_DOMAINS = '[]'
    YANDEX_SEARCH_MAX_QUERIES_PER_RUN = '1'
    YANDEX_SEARCH_DAILY_LIMIT = '1'
    YANDEX_SEARCH_MAX_PAGES_PER_RUN = '1'
    YANDEX_SEARCH_PAGE_REQUESTS_PER_RUN = '2'
    YANDEX_SEARCH_PAGE_DAILY_LIMIT = '2'
}
foreach ($setting in $settings.GetEnumerator()) {
    [Environment]::SetEnvironmentVariable($setting.Key, $setting.Value, 'Process')
}
$env:PM_PRIVATE_DIR = Join-Path $acceptance 'search'
$domain = Read-Host 'Согласованный публичный FQDN, нижний регистр и IDNA'
if ([string]::IsNullOrWhiteSpace($domain) -or
    [string]::IsNullOrWhiteSpace($env:YANDEX_SEARCH_API_KEY) -or
    [string]::IsNullOrWhiteSpace($env:YANDEX_SEARCH_FOLDER_ID)) {
    throw 'Не заполнены условия допуска'
}
$env:YANDEX_SEARCH_ALLOWED_DOMAINS = ConvertTo-Json -InputObject @($domain) -Compress
$env:ACCEPTANCE_DOMAIN = $domain
$env:ACCEPTANCE_V1_PATH = Join-Path $env:PM_PRIVATE_DIR 'kes-v1.json'
$env:YANDEX_SEARCH_ENABLED = 'true'
$env:YANDEX_SEARCH_LIVE_SMOKE = 'true'
try {
    python scripts/search_smoke.py --live --product 'Kaspersky Endpoint Security' --domain $domain --topic licensing --summary --snapshot $env:ACCEPTANCE_V1_PATH
} finally {
    $env:YANDEX_SEARCH_LIVE_SMOKE = 'false'
    $env:YANDEX_SEARCH_ENABLED = 'false'
}
```

5. Smoke пропускает кеш, повторы и прямой обход. В сохранённом JSON сверьте `mode=live`, `source=yandex_search`, `schema_version=1`, `usage.requests=1`, `attempts[].outcome` и `elapsed_ms`. Для успешного результата проверьте XML-диагностику, число найденных и отобранных документов, продукт и тему лицензирования, разрешённый домен URL и `content_type=snippet`. `retrieved_at` — время получения, `published_at=null` допустимо для неизвестной даты. Успешная пустая выдача подтверждает обработку ответа и оставляет релевантность непроверенной. Сохраните код ошибки, оценку `cost` и отдельно списание из биллинга.

### Первичный источник при отдельном допуске

Если допуск к сайту есть до поисковой попытки, выберите один обычный вызов v1 **вместо smoke**. В том же каталоге бюджета задайте `YANDEX_SEARCH_PAGE_FETCH_ENABLED=true`, присвойте `YANDEX_SEARCH_PAGE_ALLOWED_DOMAINS` тот же утверждённый список, задайте YANDEX_SEARCH_CACHE_TTL=0 для этого разового прогона и оставьте один поисковый запрос и две попытки на robots.txt и HTML. Вызовите `pm.web_search.collect_competitor_evidence(product="Kaspersky Endpoint Security", domains=[domain], topics=["licensing"])` один раз. Полный результат сохраняется вне Git через `scripts.search_import_competitors.write_private_json(Path(...), result)`. Номинальная оценка доступна через `pm.yandex_search.snapshots.estimate_cost(result)`. После запуска отключите Search и обход.

Сверьте `page_usage.requests`, `page_usage.cache_hits`, предупреждения, URL, `retrieved_at`, `content_type=page_excerpt`, происхождение и цитату. Встроенный загрузчик проверяет robots.txt, HTTPS, DNS, IP соединения, переходы, размеры и таймауты. При запрете, ошибке или исчерпании бюджета остаётся сниппет. Если smoke уже состоялся, второй платный запрос ради источника в рамках этой приёмки не выполняется. [Операторские ограничения](docs/final_release_operator.md#один-search-запрос-первоисточник-и-pim)

## Демонстрация на сохранённом v1

1. Покажите публичный запрос по Kaspersky Endpoint Security и дату получения найденных материалов. При повторном показе используйте первоначальный конверт с подписью «Сохранённый ответ» и счётчиком **0 новых Search-запросов**.
2. Проверьте источник в пределах допуска. Прочитайте тему, дату получения и дату публикации при наличии. Сопоставляйте цены только при совпадении продукта, версии, лицензии, срока, числа устройств, НДС, валюты и актуальной даты. Пометьте неизвестные, устаревшие и противоречивые сведения. Сниппет остаётся поисковым фрагментом.
3. Передайте сохранённый конверт штатному нормализатору AI Core и запишите принятые свидетельства в изолированную PIM. Перед запуском нижнего скрипта задайте `PM_PRIVATE_DIR` на подготовленный подкаталог `pim`. Используйте прежние `ACCEPTANCE_DOMAIN`, `ACCEPTANCE_V1_PATH`, `YANDEX_SEARCH_PUBLIC_PRODUCTS` и `YANDEX_SEARCH_ALLOWED_DOMAINS`. Код можно передать локальному `python -` из корня сборки после проверки происхождения файла. Сетевой клиент здесь заменён сохранённым ответом.

```python
import json
import os
from pathlib import Path

from pm import ai_store, analysis_context, memory

scope = {
    "product": "Kaspersky Endpoint Security",
    "domains": [os.environ["ACCEPTANCE_DOMAIN"]],
    "topics": ["licensing"],
}
v1 = json.loads(Path(os.environ["ACCEPTANCE_V1_PATH"]).read_text(encoding="utf-8"))
if (v1.get("source") != "yandex_search" or v1.get("schema_version") != 1
        or v1.get("usage", {}).get("requests") != 1):
    raise SystemExit("Expected the approved v1 archive")
for key in ("YANDEX_SEARCH_API_KEY", "YANDEX_SEARCH_FOLDER_ID"):
    os.environ.pop(key, None)
os.environ["YANDEX_SEARCH_ENABLED"] = "true"
status, reason, rows = analysis_context.public_search(scope, collector=lambda **_: v1)
if status not in {"ok", "partial"} or not rows:
    print(json.dumps({"status": status, "reason": reason, "accepted": 0}))
    raise SystemExit(1)
with ai_store.database() as db:
    saved = [memory.record_web(db, item) for item in rows]
print(json.dumps({"pim_ids": [key for key, _ in saved],
                  "created": sum(inserted for _, inserted in saved), "new_search_requests": 0}))
```

4. Сверьте ID, URL, дату и тип записей. Для демонстрации структурированных снимков используйте `search_monitor.py --offline --web-result` с этим файлом и штатный `search_pim_import.py`. Сохраните один снимок и его курсор. Повторный показ использует тот же снимок, без создания нового наблюдения.
5. Покажите в памяти два ранее сохранённых периода для того же конкурента и сопоставимого предложения, затем перейдите к источникам каждой точки. При отсутствии пары покажите «Недостаточно сопоставимых наблюдений». AI Core и локальная Qwen получают контекст для гипотезы, числовое изменение определяется PIM. [Контракт и история](docs/search_release_handoff.md#исторические-снимки-в-общей-pim)

## Экономика поиска

Официальный рублёвый тариф с НДС на 27.11.2026 — **488 ₽ за 1 000 дневных синхронных запросов** и **366 ₽ за 1 000 ночных**. Ночное окно 00:00–07:59:59 МСК. Провайдер не тарифицирует запросы с внутренней серверной ошибкой или ошибкой авторизации. Локальный лимит считает попытки, включая повторы. Опубликованные квоты синхронного поиска — 1 запрос в секунду и 10 000 в час. [Тарифы](https://aistudio.yandex.ru/ru/docs/search-api/pricing) · [Квоты](https://aistudio.yandex.ru/ru/docs/search-api/concepts/limits)

**Модель, а не фактический расход.** 30 календарных и 22 рабочих дня. На одного менеджера в рабочий день лёгкий режим — 1 расследование × 2 запроса, обычный — 1 × 4, интенсивный — 2 × 5. Для ручных запросов принято 50% попаданий в общий кеш с TTL 86 400 секунд. Один командный мониторинг добавляет 2 сетевые попытки в календарный день, всего 60 за месяц, без попаданий из кеша в этой части модели. Мониторинг отдельно согласуется. Таблица использует дневной тариф и исключает дополнительные повторы.

| Менеджеров | Нагрузка | Запросов без кеша, включая мониторинг | С кешем, до лимита | ₽ до лимита | Попыток при общем лимите 20/сутки | ₽ при 20/сутки |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | лёгкий | 104 | 82 | 40,02 | 82 | 40,02 |
| 1 | обычный | 148 | 104 | 50,75 | 104 | 50,75 |
| 1 | интенсивный | 280 | 170 | 82,96 | 170 | 82,96 |
| 5 | лёгкий | 280 | 170 | 82,96 | 170 | 82,96 |
| 5 | обычный | 500 | 280 | 136,64 | 280 | 136,64 |
| 5 | интенсивный | 1160 | 610 | 297,68 | 456 | 222,53 |
| 35 | лёгкий | 1600 | 830 | 405,04 | 456 | 222,53 |
| 35 | обычный | 3140 | 1600 | 780,80 | 456 | 222,53 |
| 35 | интенсивный | 7760 | 3910 | 1908,08 | 456 | 222,53 |

Формула колонки с лимитом — `22 × min(20, ручные попытки после кеша за рабочий день + 2) + 8 × 2`. Абсолютный потолок при заполнении 20 попыток ежедневно за 30 дней — 600 попыток и 292,80 ₽ по дневному тарифу. Лимит на общий файл бюджета обнуляется в 00:00 UTC. У нескольких отдельных БД бюджеты суммируются. Перенос 60 состоявшихся попыток мониторинга в ночное окно уменьшает расчёт на 7,32 ₽.

Для отдельно разрешённой проверки страниц принято до 2 HTTPS на кандидат, включая robots.txt, и по одному кандидату на ручное расследование и цикл мониторинга. В обычном режиме это до 104, 280 и 1 600 HTTP-попыток в месяц для 1, 5 и 35 менеджеров. Для 35 при суточном лимите страниц 30 и таком же распределении верхняя оценка — 676. Кеш страниц по умолчанию живёт 3 600 секунд. Попадания, запрет обхода и отсутствие кандидатов снижают спрос. Прямой HTTPS к сайтам учитывается отдельно от тарифа Search API. Трафик, серверы и работа оператора в денежных колонках отсутствуют. [Настройки обхода и мониторинга](docs/search_release_handoff.md)

## Ограничения и поправка для владельца интеграции

Проверка доступности и содержания страниц требует отдельного допуска на HTTPS к сайту. Для численного исторического сравнения нужна пара сопоставимых сохранённых наблюдений. Подключение на production остаётся у владельца Deployment.

Операторский пример `search_monitor.py --live` передаёт подкаталог `history`, а штатный `History` требует существующий путь. Это блокирует запуск по примеру из пустого каталога ещё до Search API. В локально подготовленной папке подкаталог уже есть. Минимальная поправка к `docs/final_release_operator.md`:

```diff
-mkdir -p "$SEARCH_STATE" "$PIM_STATE"
+mkdir -p "$SEARCH_STATE/history" "$PIM_STATE"
```
