# Эксплуатационная приёмка объединённой сборки

## Локальный запуск

Из корня интеграционной ветки на Python 3.12+ установите зависимости. Для синтетической репетиции используйте новый каталог состояния вне Git и пустое окружение для рабочих источников, Метрики, Search и SMTP.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
export PM_PRIVATE_DIR="$(mktemp -d)"
export PM_WORKBOOK= PM_SNAPSHOT= PM_CASE_CONFIG= PM_EVIDENCE= PM_BENCHMARK=
export METRIKA_COUNTER_ID= METRIKA_TOKEN=
export YANDEX_SEARCH_ENABLED=false PM_EMAIL_ENABLED=false
export PM_QWEN_CHAT_ENABLED=true PM_QWEN_OWNER_MODE=local
python app.py serve --port 8080
```

В PowerShell создайте окружение командой `python -m venv .venv`, используйте `.venv\Scripts\python.exe` и задавайте переменные через `$env:ИМЯ`. Backend читает окружение процесса. `.env.example` служит шаблоном значений.

Для Qwen запустите `python scripts/run_model.py --server "$LLAMA_SERVER" --model "$QWEN_GGUF"`. Лаунчер проверяет утверждённый SHA-256, использует loopback и включает лимит кеша 512 МиБ при поддержке флага. По умолчанию один слот на 8192 токенов. Для теста двух клиентов добавьте `--parallel 2`, для поддерживаемого Metal-профиля укажите `--gpu-layers 99`. В терминале Backend перед запуском задайте `PM_LLM_URL=http://127.0.0.1:18081/v1`, `PM_LLM_MODEL=qwen3-4b`, `PM_LLM_SELECT_STEP=true` и `PM_QWEN_QUALITATIVE=true`.

```sh
python scripts/qwen/evaluate.py --mode live --concurrent
python scripts/qwen/historical_smoke.py --live
python scripts/qwen/effect_smoke.py --live
```

## Метрика

Передайте одобренные `METRIKA_COUNTER_ID` и `METRIKA_TOKEN` процессу Backend из закрытого окружения. Выберите `METRIKA_SCOPE=store_counter` для магазина либо `demo_counter` для тестового счётчика. Задайте подтверждённые зону, метрики и сегменты. Пример ниже выводит только тип источника и статус. Передайте разрешённые прошедшие даты в `METRIKA_ACCEPT_START` и `METRIKA_ACCEPT_END`.

```sh
python - <<'PY'
import json
import os
from pm.metrika import metrika

result = metrika(os.environ["METRIKA_ACCEPT_START"], os.environ["METRIKA_ACCEPT_END"], detailed=True)
print(json.dumps({"status": result["status"], "scope": result["scope"], "available": result["available"]}))
raise SystemExit(result["status"] != "ok" or not result["available"])
PY
```

Для проверки появления наблюдений в PIM вызовите `/api/memory/sync` с `{"kind":"metrika","day":"YYYY-MM-DD"}` через защищённое приложение. Это отдельное обращение в пределах утверждённого бюджета. Тестовые наблюдения имеют собственную область и не участвуют в выводах о счётчике магазина.

## Один Search-запрос, первоисточник и PIM

После согласования запроса, расхода и точного публичного домена задайте `APPROVED_PUBLIC_PRODUCT`, `APPROVED_DOMAIN` и `APPROVED_TOPIC`. Тема выбирается из `TOPIC_TERMS`. Укажите `SEARCH_STATE` и `PIM_STATE` как разные каталоги вне Git. Ключ и folder ID передайте из защищённого окружения. Используйте постоянный для этой проверки каталог бюджета Search, включая инструментальные повторы.

```sh
umask 077
mkdir -p "$SEARCH_STATE" "$PIM_STATE"
export PM_PRIVATE_DIR="$SEARCH_STATE"
export YANDEX_SEARCH_ENABLED=true YANDEX_SEARCH_MONITOR_ENABLED=true
export YANDEX_SEARCH_MAX_QUERIES_PER_RUN=1 YANDEX_SEARCH_DAILY_LIMIT=1
export YANDEX_SEARCH_PAGE_FETCH_ENABLED=true YANDEX_SEARCH_MAX_PAGES_PER_RUN=1
export YANDEX_SEARCH_PAGE_REQUESTS_PER_RUN=2 YANDEX_SEARCH_PAGE_DAILY_LIMIT=2
export YANDEX_SEARCH_PUBLIC_PRODUCTS="$(python -c 'import json,sys; print(json.dumps([sys.argv[1]]))' "$APPROVED_PUBLIC_PRODUCT")"
export YANDEX_SEARCH_ALLOWED_DOMAINS="$(python -c 'import json,sys; print(json.dumps([sys.argv[1]]))' "$APPROVED_DOMAIN")"
export YANDEX_SEARCH_PAGE_ALLOWED_DOMAINS="$YANDEX_SEARCH_ALLOWED_DOMAINS"
python - "$SEARCH_STATE/plan.json" "$APPROVED_PUBLIC_PRODUCT" "$APPROVED_DOMAIN" "$APPROVED_TOPIC" <<'PY'
import json
import sys
with open(sys.argv[1], "x", encoding="utf8") as output:
    json.dump(dict(product=sys.argv[2], domains=[sys.argv[3]], topics=[sys.argv[4]]), output, ensure_ascii=False)
PY
python scripts/search_monitor.py --live --plan "$SEARCH_STATE/plan.json" \
  --history-dir "$SEARCH_STATE/history" --report "$SEARCH_STATE/capture.json"
PM_PRIVATE_DIR="$PIM_STATE" YANDEX_SEARCH_ENABLED=false \
  python scripts/search_pim_import.py --history-dir "$SEARCH_STATE/history" --mode local
```

На один запрос выделяется одна попытка Search API. Проверка страницы использует отдельный лимит на robots.txt и одну загрузку. Запрет, сбой или исчерпание лимита оставляют сниппет. Проверьте частный `capture.json` и статус первоисточника перед импортом. При согласовании только Search используйте `YANDEX_SEARCH_PAGE_FETCH_ENABLED=false`. Импорт прочитает локальный снимок и выведет курсор. Повтор с тем же снимком сохраняет существующие идентификаторы. Продолжить пакет можно через `--after` с последним подтверждённым курсором. [Контракт поиска и статусы покрытия](search_release_handoff.md)

## Проверка тестового email

1. Запустите изолированный экземпляр с новой SQLite, синтетическим обзором, `PM_EMAIL_TEST_MODE=true` и фиксированным адресом тестового ящика. На VPS для этой отдельной приёмки используется режим `private` с пустыми путями рабочих источников. Публичный синтетический демо-контур сохраняет SMTP выключенным.
2. Укажите разрешённое реле, порт, TLS, отправителя, тестовый адрес, защищённый URL и квоту минимум в две попытки. Выполните `python app.py email-test` с тем же `PM_PRIVATE_DIR`. Для systemd воспользуйтесь [одноразовой командой с закрытым EnvironmentFile](combined_release_deployment.md#допущенная-проверка-smtp).
3. Включите `PM_MONITOR_INTERVAL=60` и запустите Backend. Дождитесь нового существенного синтетического сигнала и завершения расследования. В журнале появится статус доставки. Сравните число попыток и убедитесь, что повторная проверка того же сигнала не создаёт второе уведомление.
4. Проверьте фактическое получение обоих писем в тестовом ящике. Откройте ссылку из автоматического письма под разрешённым аккаунтом через HTTPS, проверьте тот же отчёт и отказ без аутентификации. Для случая `uncertain` проверьте журнал реле и ящик.

[Параметры SMTP и трактовка статусов](../EMAIL_NOTIFICATION_HANDOFF.md)

## VPS и демонстрация жюри

1. Администратор сверяет полный принятый SHA из отчёта, проверяет чистый checkout, ресурсы, портовую схему и закрытый EnvironmentFile. На чистой Ubuntu 24.04 запускает `sudo bash scripts/deploy/install.sh "$approved_sha"`. Для v5 используется независимый свежий снимок остановленной БД и документированная процедура переключения. [Установка, миграция и откат](combined_release_deployment.md)
2. В HTTPS-контуре с чатом задаются индивидуальные аккаунты, `PM_QWEN_OWNER_MODE=proxy` и общий служебный ключ Backend/Nginx из закрытого хранилища. Создайте конфигурацию из того же SHA `python scripts/deploy/render_proxy.py "$DOMAIN" --mode production --qwen-chat`. Перед активацией выполните `nginx -t` и проверьте HTTPS, CSP, Host, Origin и изоляцию двух диалогов. Для жюри рендерите `--mode demo` на отдельной ВМ с синтетической SQLite и без рабочих токенов. [Вход через HTTPS и SSH](infrastructure_security.md)
3. Проверьте `scripts/deploy/health.py`, независимый vault и `sudo sh deploy/check_backup_isolation.sh`. Восстановите проверенный снимок в новый каталог и проверьте записи, отчёт, диалог, задачу и email-очередь. Объектное хранилище получает уже проверенные снимки отдельной служебной ролью, с версионированием и согласованным шифрованием. [Резервные копии и Object Storage](disaster_recovery.md)

Начальная ёмкость для совмещённого CPU-стенда Qwen3-4B — 12–16 ГБ RAM с одним слотом и мониторингом. Действующие units ограничивают Backend 200% CPU и 3 GiB, модель 500% CPU и 8 GiB. Это лимиты, бюджет CPU и RAM фиксируйте по контрольному прогону целевого VPS. Для GGUF выделите около 2,5 ГБ, затем добавьте место для runtime, двух кодовых релизов, SQLite с WAL, временного восстановления и независимого хранилища рассчитанной глубины. На Mac M5 с 16 ГБ и Metal рабочий профиль кеша 512 МиБ показал около 3,2 ГБ физического следа в поднаборе сценариев. Эти замеры относятся к macOS. [Профиль и замеры Qwen Engine](qwen_runtime.md)

В демонстрации откройте мониторинг на синтетическом сигнале, разверните доказательства и гипотезы, спросите в чате «Что проверить первым?» и «Как измерить эффект?». Отредактируйте и явно согласуйте черновик, откройте Redmine JSON, покажите четырёхнедельный план и очередь уведомлений. В отдельном разрешённом тестовом контуре покажите письмо и открытие отчёта. В конце сравните два периода в продуктовой памяти. Для демонстрации эффекта используйте `data/effect-demo.json`.
