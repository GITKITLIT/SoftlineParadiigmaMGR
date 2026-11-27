# Развёртывание объединённого релиза PARADИИGMA

## Что утверждает капитан

Адриан готовит единый ревизионный SHA и проверяет состав перед установкой. Для интеграционной репетиции зафиксированы AI Core `b71424d71468ccbdd9a2ceb7c44799fc07abe334` со схемой SQLite v6 и SMTP, Search `93de5460148982747093bfe50e34dbc57abbb88c` с контрактом v1 и пакет `feature/deployment`. Для Qwen3-4B применяется согласованная ревизия Амира и утверждённые бинарник и веса. Публикация Search закончена, предок `813e47c` и все четыре коммита Артура от принятого `9d6bf52` входят в историю `93de546`.

| Область | Разрешение пересечений |
| --- | --- |
| Backend, миграции, SMTP и интерфейс | Берите приложение, схему v6 и зависимости из AI Core. Миграцию выполняет штатный `pm.ai_store.database()`. Deployment и Search не заменяют приложение. |
| systemd, Nginx и окружение | Используйте `deploy/`, `scripts/deploy/` и lock-файл из ветки Deployment. У AI Core есть одноимённые демонстрационные units. Установщик должен получить файлы Deployment с пользователями pm-app, pm-backup и pm-qwen, отдельными каталогами и защищёнными правилами доступа. |
| SQLite Backup API | Для `scripts/pim_backup.py` используйте проверенную версию из Deployment. Она содержит `verify`, `restore-test` и `retention-plan` AI Core и сохраняет проверенный путь на `nosymfollow`-mount. Операционная служба отвечает за проверку, публикацию, ротацию и статус. `retention-plan` показывает план AI Core, фактическая локальная ротация сервиса определяется `PARADIIGMA_RETENTION_DAYS`. |
| Поиск | Подключите `pm/web_search.py`, `pm/search_security.py`, `pm/competitor_sources.py` и `pm/yandex_search/`, CLI и приёмочные тесты Search. Кеш и дневные счётчики Search хранятся в `yandex-search.sqlite3`. Перед восстановлением доступа к Search сверяйте его суточный бюджет отдельно от основной `ai.sqlite3`. |
| Qwen | Базовое расследование из AI Core работает с локальным OpenAI-совместимым endpoint. Если в состав входит чат Амира, капитан отдельно применяет его согласованные изменения владельца диалога, CSP и заголовков прокси. Прежняя репетиция Амира привязана к старому Deployment SHA. Повторное применение полного пакета правок резервного копирования поверх нового Deployment требует ручной сверки пересечения. [Настройка Qwen](https://github.com/GITKITLIT/SoftlineParadiigmaMGR/blob/5e42e94/docs/qwen_runtime.md) · [Интеграция чата](https://github.com/GITKITLIT/SoftlineParadiigmaMGR/blob/5e42e94/docs/chat_integration.md) |

Тестовая композиция формируется экспортом файлов в отдельном каталоге. Ветка Deployment сама содержит baseline AI Core со схемой v5. Для production установщик получает утверждённый SHA объединённой сборки, где маркер `.schema` после подготовки равен `6`.

## Окружение и доступ

Используйте закрытый `/etc/paradiigma/app.env` с владельцем root:root и режимом 0600. PID 1 читает его и передаёт только процессу приложения. Файл не подключается к Qwen или резервному сервису. Начальный шаблон находится в `deploy/app.env.example`. Существующий файл установщик сохраняет, поэтому добавьте недостающие параметры вручную. Рабочие данные требуют `PARADIIGMA_MODE=private`. Синтетическая демо-VM использует отдельный каталог данных и запрет исходящего API, а preflight отклоняет рабочие пути, Search и SMTP в демо-конфигурации.

| Компонент | Что задать и проверить |
| --- | --- |
| SQLite | `PM_PRIVATE_DIR=/var/lib/paradiigma/db`. `PM_BACKUP_STATUS_FILE` и `PM_MIGRATION_STATUS_FILE` указывают на `/var/lib/paradiigma-backup-status/status.json`. Первая переменная нужна для показа статуса, вторая включает проверку перед миграцией. `PM_DB_MAX_MIB` при необходимости ограничивает размер основной базы, допустимо 64–65536 МиБ. |
| Qwen3-4B | `PM_LLM_URL=http://127.0.0.1:18081/v1`, `PM_LLM_MODEL=qwen3-4b` для псевдонима действующего CPU-unit. `PM_LLM_PROMPT_FILE` при необходимости указывает на согласованный файл в /var/lib/paradiigma/documents. Проверьте хеш GGUF, CPU-профиль, /health и идентификатор из /v1/models. Локальные замеры Mac с Metal не задают задержку или память Linux VPS. Для отдельного вычислительного узла используйте закрытый туннель с тем же локальным URL. |
| Яндекс.Метрика | Допущенный `METRIKA_COUNTER_ID` и `METRIKA_TOKEN` задаются парой. Выберите `METRIKA_SCOPE=store_counter` или `demo_counter` по происхождению счётчика. Часовой пояс, разрешённые цели, сегменты и дополнительные метрики задаются `METRIKA_COUNTER_TIMEZONE`, `METRIKA_GOALS`, `METRIKA_SEGMENTS` и `METRIKA_EXTRA_METRICS`. Фоновый сбор требует `PM_COLLECT_METRIKA=true`, интервала `PM_COLLECTION_INTERVAL` и согласованного `PM_METRIKA_DAILY_LIMIT`. |
| Yandex Search API | После допуска задайте `YANDEX_SEARCH_ENABLED=true`, ID каталога, ограниченный ключ, явные JSON-списки публичных продуктов и доменов. `YANDEX_SEARCH_MAX_QUERIES_PER_RUN` и `YANDEX_SEARCH_DAILY_LIMIT` ограничивают попытки, включая повторы. Просмотр первоисточников требует отдельного согласования, `YANDEX_SEARCH_PAGE_FETCH_ENABLED` и доменов страниц. Оставьте `YANDEX_SEARCH_LIVE_SMOKE` и `YANDEX_SEARCH_MONITOR_ENABLED` выключенными между отдельными согласованными прогонами. [Передача Search RC](https://github.com/GITKITLIT/SoftlineParadiigmaMGR/blob/93de546/docs/search_release_handoff.md) |
| SMTP | `PM_EMAIL_ENABLED=false` по умолчанию. Для пробного письма после допуска укажите `PM_EMAIL_TEST_MODE=true`, разрешённый `PM_SMTP_HOST`, `PM_SMTP_PORT` и `PM_SMTP_SECURITY=starttls` или `tls`. Если нужна аутентификация, задайте оба `PM_SMTP_USERNAME` и `PM_SMTP_PASSWORD`. Задайте `PM_EMAIL_FROM`, от одного до пяти утверждённых `PM_EMAIL_TO`, `PM_EMAIL_DAILY_LIMIT` от 1 до 20 и `PM_EMAIL_REPORT_BASE_URL` с защищённым HTTPS-корнем или локальным адресом согласованного SSH-туннеля. Фоновая отправка использует `PM_MONITOR_INTERVAL` от 60 до 86400 секунд. [Контракт SMTP AI Core](https://github.com/GITKITLIT/SoftlineParadiigmaMGR/blob/b71424d/EMAIL_NOTIFICATION_HANDOFF.md) |

Исходящие TCP/587 или TCP/465 разрешайте только к одобренному SMTP-реле. Для рабочей сети согласуйте DNS и исходящий HTTPS к разрешённым API, закрыв внутренние порты приложения, SQLite и модели. Перед изменением SMTP-режима или адресатов учтите, что приложения с одним назначением и одной БД разделяют очередь. После ошибочного `uncertain` проверьте реле и ящик. Автоматический повтор такого письма отключён.

## Чистая установка v6

На чистой Ubuntu 24.04 поставьте проверенный объединённый SHA командой `bash scripts/deploy/install.sh "$approved_sha"` из закрытого административного checkout. Приложение создаст схему v6. Сначала запустите его на loopback с отключёнными внешними источниками и SMTP. Проверьте `/api/analysis/settings`, независимый снимок и результат восстановления. Затем включайте согласованные интеграции по одной. Конфигурацию открытого входа применяйте после приёмки доступа и источников. [Установка и update](deployment.md) · [SSH и HTTPS](infrastructure_security.md)

## Переход рабочей v5 → v6

Вся последовательность выполняется в согласованное окно от имени администратора. Закройте Nginx и командные туннели, остановите CLI импорта, планировщики и остальные процессы, пишущие в SQLite. Оставьте SMTP выключенным. Запишите предыдущий SHA, значения версии и текущий RPO. Реквизиты окружения и исходные документы сохраните в разрешённых хранилищах.

1. Подготовьте проверенную v6-сборку имеющимся update. При рабочей v5 установщик подготовит каталог `/opt/paradiigma/releases/$approved_sha` и остановит переключение с сообщением `Schema change requires an administrator-led migration`. Убедитесь, что прежний релиз сохранён и маркер кандидата содержит `6`. При другой причине отказа прервите процедуру до разбора.

2. Остановите опубликованный таймер и Backend, дождитесь остановки резервного процесса. Приведите рабочую БД к неподвижному состоянию через SQLite checkpoint. Закрытие писателей обязательно, поскольку метаданные снимка не заменяют остановку всех writers.

```sh
systemctl stop paradiigma-backup.timer paradiigma.service paradiigma-backup.service paradiigma-backup-reader.service
runuser -u pm-app -- /usr/bin/python3 - <<'PY'
from contextlib import closing
import sqlite3

with closing(sqlite3.connect("/var/lib/paradiigma/db/ai.sqlite3")) as db:
    if db.execute("PRAGMA user_version").fetchone()[0] != 5:
        raise SystemExit("Expected schema v5")
    if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
        raise SystemExit("Checkpoint busy")
PY
systemctl start paradiigma-backup.service
```

3. Сохраните имя снимка из поля `snapshot_id`. Подтвердите `status=ok`, `source_schema=5`, `source_quiescent=true` и время `last_restore_check`. Сверьте `source_mtime_ns` и `source_size` с закрытой рабочей SQLite, затем от администратора выполните `python3 -IB /opt/paradiigma/current/scripts/pim_backup.py verify --snapshot "/var/backups/paradiigma/$snapshot_id"`. Сохраните этот снимок на весь период возможного возврата. Новая запись после снимка требует повторной остановки и резервирования.

4. Выполните штатную миграцию новым Backend под непривилегированным пользователем. Команда передаёт только пути и не загружает токены. Нельзя запускать Core в этом окне с пустым `PM_MIGRATION_STATUS_FILE`.

```sh
release="/opt/paradiigma/releases/$approved_sha"
runuser -u pm-app -- env -i PATH=/usr/bin:/bin PARADIIGMA_MODE=demo \
    PM_PRIVATE_DIR=/var/lib/paradiigma/db \
    PM_MIGRATION_STATUS_FILE=/var/lib/paradiigma-backup-status/status.json \
    "$release/.venv/bin/python" "$release/scripts/deploy/preflight.py"
bash scripts/deploy/update.sh "$approved_sha"
```

Предварительный запуск с очищенным окружением проверяет встроенный синтетический UI и штатно обновляет схему постоянной БД. Обычный старт после переключения читает закрытую конфигурацию. Проверьте v6, старые расследования, таблицу `email_outbox` и отсутствие ретроспективной рассылки по v5. Создайте и проверьте снимок v6. До открытия входа проверьте статус, ссылку на отчёт и работу разрешённых источников. Если после миграции кандидат не стартует, установщик оставляет сервис остановленным при несовпадении схемы с прежним релизом.

5. Для возврата к v5 используйте сохранённую пару SHA v5 и снимок v5. В закрытом окне остановите Backend, таймер и резервные процессы. Сохраните v6 в карантине, если уже была рабочая запись, и согласуйте её перенос. Перед восстановлением переключите указатель `current` и units на прежний совместимый релиз, пока службы стоят. Выполните старый `restore.sh` с выбранным снимком и включите таймер после проверки. Возвращение к коду v5 через update при данных v6 отклоняется до переключения. Выполнение `restore.sh` при активном маркере `.schema=6` также отклонит снимок v5. [Восстановление и отдельные тома](disaster_recovery.md)

### Допущенная проверка SMTP

После настройки одобренного реле и тестового ящика администратор может вызвать существующую команду AI Core с тем же закрытым EnvironmentFile. Для разовой проверки оставьте SMTP в тестовом режиме и выключите фоновое расписание до подтверждения условий.

```sh
sudo systemd-run --wait --pipe --collect \
    -p User=pm-app -p Group=pm-app -p SupplementaryGroups=pm-db \
    -p UMask=0027 -p EnvironmentFile=/etc/paradiigma/app.env \
    /opt/paradiigma/current/.venv/bin/python /opt/paradiigma/current/app.py email-test
```

`submitted` подтверждает приём SMTP-реле. Доставку проверяют в тестовом ящике, а ссылку открывают как получатель через тот же защищённый HTTPS или туннель. Только после согласования назначения и содержимого включайте рабочий мониторинг. Снимок v6 включает очередь и суточный счётчик почты. Перед восстановлением старого снимка выключите отправку и сверьте уже принятые письма, поскольку после отката очередь и квота возвращаются к состоянию снимка.

## Воспроизводимость и внешние блокеры

В `tests/deployment_core_v6.sh` используется одноразовая Ubuntu после штатного clean-install с синтетической v5. Сценарий собирает тестовый Git-объект из зафиксированных файлов, проверяет отказ автоматической миграции, отказ старого маркера, переход на v6, TLS-доставку только на loopback, бэкап очереди и возврат к v5 по прежнему снимку. Это тест развёртывания. Объединённую ветку и релизный SHA утверждает капитан.

До запуска нужны утверждённый объединённый SHA и окно с владельцем VPS. На целевом сервере проверяются права и диски, DNS и сертификат, список получателей и TLS-реле, доступность разрешённых API и их бюджеты, модель и ресурсы Linux, а также проверенное независимое внешнее хранение. Если включён отдельный чат Qwen, требуется повторная проверка его CSP, заголовков прокси и разделения сессий на утверждённом SHA.
