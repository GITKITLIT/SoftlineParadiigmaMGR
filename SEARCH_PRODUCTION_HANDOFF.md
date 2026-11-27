# Yandex Search API на production VPS

Инструкция для владельца Deployment на основе [живой приёмки](SEARCH_LIVE_ACCEPTANCE.md) из коммита `574b3917ad57b8370f408552ffab6ea69462f831`. Команды предназначены для согласованного окна на Linux VPS.

## Подготовка без платного запроса

1. Согласуйте исполнителя, публичное название продукта, тему, точный FQDN и бюджет одного синхронного запроса до **0,488 ₽ с НДС**. Для VPS используйте отдельный production-ключ со сроком действия и областью `yc.search-api.execute`. Сервисному аккаунту нужна роль `search-api.webSearch.user` на каталоге с активным биллингом. Убедитесь, что с VPS разрешён исходящий HTTPS к `searchapi.api.cloud.yandex.net:443`. [Аутентификация Search API](https://aistudio.yandex.ru/ru/docs/search-api/api-ref/authentication)
2. В закрытой административной сессии проверьте, что `/etc/paradiigma/app.env` — обычный файл `root:root` с правами `0600`. Через `sudoedit` внесите `YANDEX_SEARCH_API_KEY` и `YANDEX_SEARCH_FOLDER_ID` из хранилища секретов. Установите `PARADIIGMA_MODE=private`. На время smoke оставьте в основном сервисе `YANDEX_SEARCH_ENABLED=false`. Оставьте `YANDEX_SEARCH_LIVE_SMOKE`, `YANDEX_SEARCH_MONITOR_ENABLED` и `YANDEX_SEARCH_PAGE_FETCH_ENABLED` равными `false`. Systemd читает файл напрямую. Содержимое файла и окружения процесса в отчёт или журнал не выводите. [Шаблон](deploy/app.env.example) · [Unit приложения](deploy/systemd/paradiigma.service)

```
sudo test ! -L /etc/paradiigma/app.env &&
sudo test -f /etc/paradiigma/app.env &&
sudo test "$(sudo stat -c '%u:%g:%a' /etc/paradiigma/app.env)" = 0:0:600 &&
sudoedit /etc/paradiigma/app.env
```

Проверьте службу после перезапуска. Штатный `health.py` использует loopback и локальную SQLite. Отдельный временный unit проверяет формат credentials без сетевых запросов и выводит только булевы значения.

```
sudo systemctl restart paradiigma &&
sudo systemctl is-active paradiigma &&
sudo -u pm-app /usr/bin/python3 /opt/paradiigma/current/scripts/deploy/health.py
```

```
sudo systemd-run --wait --pipe --collect \
    -p User=pm-app -p Group=pm-app -p PrivateNetwork=yes \
    -p WorkingDirectory=/opt/paradiigma/current \
    -p EnvironmentFile=/etc/paradiigma/app.env \
    -p InaccessiblePaths=/etc/paradiigma \
    /opt/paradiigma/current/.venv/bin/python -B - <<'PY'
import json
import os
from pm.yandex_search.config import Config

names = ("YANDEX_SEARCH_API_KEY", "YANDEX_SEARCH_FOLDER_ID")
try:
    valid = Config.from_env().validate_credentials() is None
except ValueError:
    valid = False
print(json.dumps({name: bool(os.getenv(name)) for name in names} | {"valid_configuration": valid}))
raise SystemExit(0 if valid else 1)
PY
```

## Один production-smoke

После допуска выполните следующий блок один раз. Обычный вызов `collect_competitor_evidence` заполняет SQLite-кеш, поэтому он используется и для единственного живого запроса, и для проверки кеша. Штатный `scripts/search_smoke.py --live` обходит кеш. Его дополнительный запуск израсходовал бы ещё одну попытку. Временный unit получает секреты через `EnvironmentFile`. Тестовые параметры применяются внутри Python, а кеш и PIM пишутся в отдельные каталоги. [Адаптер](pm/web_search.py) · [Штатный smoke CLI](scripts/search_smoke.py)

Запустите команды в Bash. Существующий каталог останавливает блок до Search API. Оставьте каталог после любого исхода. Повтор требует отдельного допуска.

```
read -r -p 'Согласованный публичный продукт: ' SEARCH_PRODUCT
read -r -p 'Согласованный точный FQDN: ' SEARCH_FQDN
read -r -p 'Согласованная тема, например licensing: ' SEARCH_TOPIC
SEARCH_STATE=/var/lib/paradiigma/search-production-smoke

sudo -v &&
sudo mkdir -m 0700 -- "$SEARCH_STATE" &&
sudo chown pm-app:pm-app "$SEARCH_STATE" &&
sudo install -d -m 0700 -o pm-app -g pm-app "$SEARCH_STATE/search" "$SEARCH_STATE/pim" &&
sudo systemd-run --wait --pipe --collect \
    -p User=pm-app -p Group=pm-app -p UMask=0077 -p Restart=no \
    -p WorkingDirectory=/opt/paradiigma/current \
    -p EnvironmentFile=/etc/paradiigma/app.env \
    -p InaccessiblePaths=/etc/paradiigma \
    /opt/paradiigma/current/.venv/bin/python -B - \
    "$SEARCH_STATE" "$SEARCH_PRODUCT" "$SEARCH_FQDN" "$SEARCH_TOPIC" <<'PY'
import http.client
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from pm import web_search
from pm.search_security import public_queries
from pm.yandex_search import client
from pm.yandex_search.config import Config
from pm.yandex_search.snapshots import estimate_cost
from scripts.search_import_competitors import private_json_target, write_private_json

state, product, domain, topic = sys.argv[1:]
root = Path(state) / "search"
os.environ.update(
    PM_PRIVATE_DIR=str(root), YANDEX_SEARCH_ENABLED="true",
    YANDEX_SEARCH_LIVE_SMOKE="false", YANDEX_SEARCH_MONITOR_ENABLED="false",
    YANDEX_SEARCH_PAGE_FETCH_ENABLED="false", YANDEX_SEARCH_PAGE_ALLOWED_DOMAINS="[]",
    YANDEX_SEARCH_PUBLIC_PRODUCTS=json.dumps([product]),
    YANDEX_SEARCH_ALLOWED_DOMAINS=json.dumps([domain]), YANDEX_SEARCH_PUBLIC_ALIASES="{}",
    YANDEX_SEARCH_MAX_RESULTS="5", YANDEX_SEARCH_MAX_QUERIES_PER_RUN="1",
    YANDEX_SEARCH_DAILY_LIMIT="1", YANDEX_SEARCH_CACHE_TTL="86400",
)
scope = {"product": product, "domains": [domain], "topics": [topic]}
config = Config.from_env()
if config.validate_credentials() is not None:
    raise SystemExit("Invalid Search credentials")
public_queries(product, [domain], [topic], config.products, config.domains, config.aliases)
target = private_json_target(root / "live-v1.json")
database = root / "yandex-search.sqlite3"
if database.exists():
    raise SystemExit("Existing Search state. Stop.")
with (root / "attempt.lock").open("x", encoding="ascii") as marker:
    marker.write("One authorized attempt. Do not retry.\n")

send, validate = http.client.HTTPSConnection.request, client._validate_status
calls, statuses = [], []

def request(connection, method, path, *args, **kwargs):
    if calls or (connection.host, method, path) != (client._HOST, "POST", client._PATH):
        raise AssertionError("Unexpected outbound request blocked")
    calls.append(1)
    return send(connection, method, path, *args, **kwargs)

def capture(response):
    statuses.append(response.status)
    return validate(response)

def count_attempts():
    if not database.is_file():
        return None
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("SELECT COALESCE(SUM(attempts), 0) FROM daily").fetchone()[0]

with patch.object(http.client.HTTPSConnection, "request", request), \
     patch.object(client, "_validate_status", capture):
    live = web_search.collect_competitor_evidence(**scope)

http_status = statuses[0] if statuses else None
live.update(mode="live", http_status=http_status, cost=estimate_cost(live))
write_private_json(target, live)
before = count_attempts()
metrics = {
    "status": live["status"], "reason": live["reason"], "http_status": http_status,
    "transport_calls": len(calls), "usage": live["usage"], "cost": live["cost"],
    "attempt_ms": [x["elapsed_ms"] for x in live["attempts"]],
    "total_ms": live["metrics"]["elapsed_ms"], "results": len(live["items"]),
    "daily_attempts_before_cache": before,
}
if (http_status != 200 or live["status"] != "ok" or len(calls) != 1 or before != 1
        or live["usage"] != {"requests": 1, "cache_hits": 0}):
    print(json.dumps(metrics))
    raise SystemExit("Search unsuccessful. Do not retry.")

def block(*args, **kwargs):
    raise AssertionError("Network blocked during cache verification")

with patch.object(web_search, "request_once", block), \
     patch.object(http.client.HTTPSConnection, "request", block):
    cached = web_search.collect_competitor_evidence(**scope)
after = count_attempts()
metrics.update(cache_status=cached["status"], cache_usage=cached["usage"],
               cache_ms=cached["metrics"]["elapsed_ms"], same_items=cached["items"] == live["items"],
               daily_attempts_after_cache=after, cache_cost=estimate_cost(cached))
write_private_json(root / "metrics.json", metrics)
print(json.dumps(metrics))
if (cached["status"] != "ok" or cached["usage"] != {"requests": 0, "cache_hits": 1}
        or not metrics["same_items"] or after != before):
    raise SystemExit("Cache verification failed. Do not retry.")
if not live["items"]:
    raise SystemExit("Empty result. Transport and cache verified; PIM not verified.")
PY
```

В закрытой сессии прочитайте кандидатов из сохранённого ответа. Запишите номера строк, относящихся к согласованному продукту и теме. Проверьте схему HTTPS и точный FQDN. Доступность и содержимое страницы требуют отдельного допуска на прямой HTTPS.

```
sudo -u pm-app /usr/bin/python3 -B - "$SEARCH_STATE" <<'PY'
import json
import sys
from pathlib import Path

archive = Path(sys.argv[1]) / "search" / "live-v1.json"
for index, item in enumerate(json.loads(archive.read_text(encoding="utf-8"))["items"], 1):
    fields = {name: item[name] for name in ("title", "url", "text")}
    print(json.dumps({"index": index} | fields, ensure_ascii=False))
PY
```

Импортируйте только утверждённые строки. Процесс импорта работает без `EnvironmentFile`, с `PrivateNetwork=yes` и с сохранённым конвертом вместо сетевого клиента. Повторный импорт проверяет дедупликацию в той же изолированной PIM.

```
read -r -p 'Номера утверждённых строк через пробел: ' -a SEARCH_ROWS
sudo systemd-run --wait --pipe --collect \
    -p User=pm-app -p Group=pm-app -p UMask=0077 -p PrivateNetwork=yes \
    -p WorkingDirectory=/opt/paradiigma/current \
    /opt/paradiigma/current/.venv/bin/python -B - \
    "$SEARCH_STATE" "$SEARCH_PRODUCT" "$SEARCH_FQDN" "$SEARCH_TOPIC" "${SEARCH_ROWS[@]}" <<'PY'
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from pm import ai_store, analysis_context, memory
from pm.search_security import source_url

state, product, domain, topic, *numbers = sys.argv[1:]
root = Path(state)
archive = json.loads((root / "search" / "live-v1.json").read_text(encoding="utf-8"))
if (archive["mode"], archive["source"], archive["schema_version"], archive["http_status"],
        archive["status"], archive["usage"]["requests"]) != ("live", "yandex_search", 1, 200, "ok", 1):
    raise SystemExit("Archive failed verification")
positions = sorted(set(map(int, numbers)))
if not positions or positions[0] < 1 or positions[-1] > len(archive["items"]):
    raise SystemExit("Select valid result numbers")
items = [archive["items"][index - 1] for index in positions]
if any(source_url(item["url"], domain) != item["url"] or urlsplit(item["url"]).scheme != "https"
       or item["content_type"] != "snippet" for item in items):
    raise SystemExit("Selected source failed HTTPS, FQDN or snippet validation")

os.environ.update(PM_PRIVATE_DIR=str(root / "pim"), YANDEX_SEARCH_ENABLED="true",
                  YANDEX_SEARCH_ALLOWED_DOMAINS=json.dumps([domain]),
                  YANDEX_SEARCH_PUBLIC_PRODUCTS=json.dumps([product]))
scope = {"product": product, "domains": [domain], "topics": [topic]}
status, reason, evidence = analysis_context.public_search(
    scope, collector=lambda **_: archive | {"items": items},
)
if status != "ok" or len(evidence) != len(items):
    raise SystemExit(f"AI Core normalization failed: {status}/{reason}")

def import_rows():
    with ai_store.database() as db:
        return [memory.record_web(db, item) for item in evidence]

first = import_rows()
with ai_store.database() as db:
    before = db.execute("SELECT count(*) FROM memory_observations").fetchone()[0]
    for item, (key, _) in zip(evidence, first, strict=True):
        row = db.execute(
            "SELECT reference, retrieved, event_date, quality FROM memory_observations WHERE id=?", (key,),
        ).fetchone()
        date = memory.stamp(item["observed_at"])[:10] if item.get("observed_at") else None
        expected = (item["locator"], memory.stamp(item["retrieved_at"]), date, "snippet")
        if row is None or tuple(row) != expected:
            raise SystemExit("PIM observation failed verification")
second = import_rows()
with ai_store.database() as db:
    after = db.execute("SELECT count(*) FROM memory_observations").fetchone()[0]
same_ids = [key for key, _ in first] == [key for key, _ in second]
created, duplicates = (sum(inserted for _, inserted in batch) for batch in (first, second))
print(json.dumps({"accepted": len(evidence), "created": created, "replay_created": duplicates,
                  "same_ids": same_ids, "rows_before_replay": before,
                  "rows_after_replay": after, "new_search_requests": 0}))
if created != len(evidence) or before != len(evidence) or duplicates or not same_ids or after != before:
    raise SystemExit("PIM persistence or deduplication failed")
PY
```

### Критерии успешной проверки

| Проверка | Критерий |
| --- | --- |
| Подключение | Служба активна, `health.py` возвращает 0, обе переменные присутствуют и `valid_configuration=true`. Авторизация подтверждается HTTP 200 в живом прогоне. |
| Поиск | Конверт v1 со статусом `ok`, один POST и `usage.requests=1`. Зафиксированы задержка и расчётная стоимость. Для оценки выдачи нужен хотя бы один релевантный сниппет с допустимым URL. |
| Кеш | Те же результаты, `cache_hits=1` и `requests=0` при заблокированном транспорте. Суточный счётчик остался равен 1. |
| PIM | Принятые сниппеты записаны в отдельную `pim/ai.sqlite3` и перечитаны с теми же URL, временем получения и типом доказательства. |
| Дедупликация | Повторный импорт вернул те же ID, создал 0 строк и сохранил прежнее число наблюдений. |

После допуска к эксплуатации внесите в `app.env` рабочие JSON-списки `YANDEX_SEARCH_PUBLIC_PRODUCTS` и `YANDEX_SEARCH_ALLOWED_DOMAINS`, согласованные лимиты и `YANDEX_SEARCH_ENABLED=true`. Оставьте smoke, мониторинг и обход страниц выключенными. Выполните тот же перезапуск и локальную проверку здоровья. Для отката верните `YANDEX_SEARCH_ENABLED=false` и перезапустите службу. После обновления детализации сверьте начисление Cloud Billing по каталогу и сервису. [Инструкция Deployment](docs/combined_release_deployment.md) · [Детализация расходов](https://yandex.cloud/ru/docs/billing/operations/check-charges)

## Обезличенная сводка для презентации

Результаты одного живого прогона 27.11.2026. [Отчёт живой приёмки](SEARCH_LIVE_ACCEPTANCE.md)

| Показатель | Измерение |
| --- | --- |
| Синхронный поиск | 1 попытка, HTTP 200 |
| Задержка | 1,58 с на вызов адаптера, включая 1,52 с на запрос и разбор |
| Выдача | 8 документов в XML, 5 сниппетов с проверенными форматом HTTPS и доменом |
| PIM | 3 релевантных наблюдения, 0 дубликатов при повторном импорте |
| Кеш | 7,4 мс, 1 попадание, 0 повторных сетевых запросов |
| Подтверждено в Cloud Billing | Потребление **0,49 ₽**, скидка **0,49 ₽**, к оплате **0,00 ₽** |

## Тарифы и ограничения

Синхронный текстовый поиск в РФ, сверено 27.11.2026. Официальный рублёвый прайс включает НДС. Стоимость без НДС рассчитана делением на 1,22. [Тарифы Search API](https://aistudio.yandex.ru/ru/docs/search-api/pricing) · [Ставка НДС Yandex Cloud](https://yandex.cloud/ru/blog/vat-changes-ru-2025)

| Окно, МСК | С НДС за 1 000 | С НДС за запрос | Без НДС за запрос |
| --- | ---: | ---: | ---: |
| 08:00:00–23:59:59 | 488 ₽ | 0,488 ₽ | **0,40 ₽** |
| 00:00:00–07:59:59 | 366 ₽ | 0,366 ₽ | **0,30 ₽** |

- Квоты синхронного поиска — 1 запрос в секунду и 10 000 в час. Лимиты — 400 символов, 40 слов и не более 250 результатов. Провайдер не тарифицирует внутренние серверные ошибки и ошибки авторизации. [Квоты и лимиты](https://aistudio.yandex.ru/ru/docs/search-api/concepts/limits) · [Правила тарификации](https://aistudio.yandex.ru/ru/docs/search-api/pricing)
- Адаптер отправляет только допустимые публичные входы, обрабатывает XML и по умолчанию оставляет до 5 результатов. Размер HTTP-ответа ограничен 2 МиБ, сетевой таймаут — 5 секунд. Суточный счётчик SQLite учитывает попытки, включая повторы, и обнуляется в 00:00 UTC. Smoke ограничен одной попыткой за запуск и сутки. [Транспорт](pm/yandex_search/client.py) · [Бюджет и кеш](pm/yandex_search/storage.py)
- TTL кеша в smoke составляет 86 400 секунд. Ключ зависит от каталога, текста запроса и лимита результатов. Пустой ответ может подтвердить транспорт и кеш. Для PIM нужны релевантные сниппеты. По сниппету нельзя подтвердить актуальность цен, содержимое страницы или отсутствующую дату публикации. [Контракт результата](docs/search_release_handoff.md)
