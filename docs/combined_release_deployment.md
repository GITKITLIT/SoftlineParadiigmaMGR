# Развёртывание объединённого релиза PARADИИGMA

## Ревизия релиза

Берите полный SHA сборки из [отчёта интеграции](../INTEGRATION_REPORT.md). Эта ревизия содержит AI Core, Search v1, чат Qwen и Deployment. Патчи Qwen уже вошли в код приложения и рендерер прокси. SQLite создаётся со схемой v6. После подготовки установщик записывает номер схемы в `.schema`.

Используйте одну раскладку Deployment с pm-app, pm-backup и pm-qwen. Версия `scripts/pim_backup.py` проверяет исходный путь и WAL. Резервная служба публикует проверенный снимок, проверку восстановления и статус, а также выполняет ротацию по `PARADIIGMA_RETENTION_DAYS`. `retention-plan` показывает независимый план для администратора. Кеш и суточные счётчики Search остаются в `yandex-search.sqlite3`. Пересчитайте остаток бюджета Search отдельно от восстановления `ai.sqlite3`.

[Команды запуска и эксплуатационной приёмки](final_release_operator.md)

## Окружение и доступ

Используйте закрытый `/etc/paradiigma/app.env` с владельцем root:root и режимом 0600. PID 1 читает его и передаёт только процессу приложения. Файл не подключается к Qwen или резервному сервису. Начальный шаблон находится в `deploy/app.env.example`. Существующий файл установщик сохраняет, поэтому добавьте недостающие параметры вручную. Рабочие данные требуют `PARADIIGMA_MODE=private`. Синтетическая демо-VM использует отдельный каталог данных и запрет исходящего API, а preflight отклоняет рабочие пути, Search и SMTP в демо-конфигурации.

| Компонент | Что задать и проверить |
| --- | --- |
| SQLite | `PM_PRIVATE_DIR=/var/lib/paradiigma/db`. `PM_BACKUP_STATUS_FILE` и `PM_MIGRATION_STATUS_FILE` указывают на `/var/lib/paradiigma-backup-status/status.json`. Первая переменная нужна для показа статуса, вторая включает проверку перед миграцией. `PM_DB_MAX_MIB` при необходимости ограничивает размер основной базы, допустимо 64–65536 МиБ. |
| Qwen3-4B | `PM_LLM_URL=http://127.0.0.1:18081/v1`, `PM_LLM_MODEL=qwen3-4b` для псевдонима действующего CPU-unit. `PM_LLM_PROMPT_FILE` при необходимости указывает на согласованный файл в /var/lib/paradiigma/documents. Проверьте хеш GGUF, CPU-профиль, /health и идентификатор из /v1/models. Локальные замеры Mac с Metal не задают задержку или память Linux VPS. Для отдельного вычислительного узла используйте закрытый туннель с тем же локальным URL. |
| Яндекс.Метрика | Допущенный `METRIKA_COUNTER_ID` и `METRIKA_TOKEN` задаются парой. Выберите `METRIKA_SCOPE=store_counter` или `demo_counter` по происхождению счётчика. Часовой пояс, разрешённые цели, сегменты и дополнительные метрики задаются `METRIKA_COUNTER_TIMEZONE`, `METRIKA_GOALS`, `METRIKA_SEGMENTS` и `METRIKA_EXTRA_METRICS`. Фоновый сбор требует `PM_COLLECT_METRIKA=true`, интервала `PM_COLLECTION_INTERVAL` и согласованного `PM_METRIKA_DAILY_LIMIT`. |
| Yandex Search API | После допуска задайте `YANDEX_SEARCH_ENABLED=true`, ID каталога, ограниченный ключ, явные JSON-списки публичных продуктов и доменов. `YANDEX_SEARCH_MAX_QUERIES_PER_RUN` и `YANDEX_SEARCH_DAILY_LIMIT` ограничивают попытки, включая повторы. Просмотр первоисточников требует отдельного согласования, `YANDEX_SEARCH_PAGE_FETCH_ENABLED` и доменов страниц. Оставьте `YANDEX_SEARCH_LIVE_SMOKE` и `YANDEX_SEARCH_MONITOR_ENABLED` выключенными между отдельными согласованными прогонами. [Передача Search RC](search_release_handoff.md) |
| SMTP | `PM_EMAIL_ENABLED=false` по умолчанию. Для пробного письма после допуска укажите `PM_EMAIL_TEST_MODE=true`, разрешённый `PM_SMTP_HOST`, `PM_SMTP_PORT` и `PM_SMTP_SECURITY=starttls` или `tls`. Если нужна аутентификация, задайте оба `PM_SMTP_USERNAME` и `PM_SMTP_PASSWORD`. Задайте `PM_EMAIL_FROM`, от одного до пяти утверждённых `PM_EMAIL_TO`, `PM_EMAIL_DAILY_LIMIT` от 1 до 20 и `PM_EMAIL_REPORT_BASE_URL` с защищённым HTTPS-корнем или локальным адресом согласованного SSH-туннеля. Фоновая отправка использует `PM_MONITOR_INTERVAL` от 60 до 86400 секунд. [Контракт SMTP AI Core](../EMAIL_NOTIFICATION_HANDOFF.md) |

Исходящие TCP/587 или TCP/465 разрешайте только к одобренному SMTP-реле. Для рабочей сети согласуйте DNS и исходящий HTTPS к разрешённым API, закрыв внутренние порты приложения, SQLite и модели. Перед изменением SMTP-режима или адресатов учтите, что приложения с одним назначением и одной БД разделяют очередь. После ошибочного `uncertain` проверьте реле и ящик. Автоматический повтор такого письма отключён.

Для отдельно утверждённого HTTPS-чата добавьте `PM_QWEN_CHAT_ENABLED=true`, `PM_QWEN_OWNER_MODE=proxy` и `PM_QWEN_PROXY_KEY` из защищённого хранилища. Согласованная версия рендерера принимает `--qwen-chat`. Тот же ключ нужен в root-owned include Nginx с правами 0600. Проверяйте два аккаунта, запрет прямого обращения к маршрутам чата и хеш CSP при каждом переключении. [Точные настройки чата и прокси](chat_integration.md)

## Чистая установка v6

На чистой Ubuntu 24.04 поставьте проверенный объединённый SHA командой `bash scripts/deploy/install.sh "$approved_sha"` из закрытого административного checkout. Приложение создаст схему v6. Сначала запустите его на loopback с отключёнными внешними источниками и SMTP. Проверьте `/api/analysis/settings`, независимый снимок и результат восстановления. Затем включайте согласованные интеграции по одной. Конфигурацию открытого входа применяйте после приёмки доступа и источников. [Установка и update](deployment.md) · [SSH и HTTPS](infrastructure_security.md)

## Переход рабочей v5 → v6

Вся последовательность выполняется в согласованное окно от имени администратора. Закройте Nginx и командные туннели, остановите CLI импорта, планировщики и остальные процессы, пишущие в SQLite. Оставьте SMTP выключенным. Запишите предыдущий SHA, значения версии и текущий RPO. Реквизиты окружения и исходные документы сохраните в разрешённых хранилищах.

До подготовки v6 убедитесь, что действующая v5 уже использует резервный сервис с полями `snapshot_id`, `source_schema` и `source_quiescent`. Если установлен ранний Deployment, сначала выполните отдельное совместимое обновление в пределах v5. Капитан утверждает обслуживающий SHA с действующим кодом v5 и актуальным пакетом Deployment. Репетиция v6 предполагает эту подготовленную v5.

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

5. Для возврата к v5 используйте сохранённую пару SHA v5 и снимок v5. В закрытом окне остановите Backend, таймер и резервные процессы. Сохраните v6 в карантине, если уже была рабочая запись, и согласуйте её перенос. Оставьте SMTP и внешние фоновые задания отключёнными. Возвращение к коду v5 через update при данных v6 отклоняется до переключения. Выполнение `restore.sh` при активном маркере `.schema=6` также отклонит снимок v5. Администратор выбирает прежний код и только затем запускает восстановление.

```sh
old_release="/opt/paradiigma/releases/$previous_v5_sha"
test "$(cat "$old_release/.schema")" = 5 || exit 1
systemctl stop paradiigma-backup.timer paradiigma.service paradiigma-backup.service paradiigma-backup-reader.service
ln -sfnT "$old_release" /opt/paradiigma/.next
mv -fT /opt/paradiigma/.next /opt/paradiigma/current
for unit in paradiigma.service paradiigma-qwen.service; do
    install -m 0644 "$old_release/deploy/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
bash "$old_release/scripts/deploy/restore.sh" "$snapshot_id"
systemctl start paradiigma-backup.timer
```

Сверьте восстановленную запись и доступность старого Backend с закрытым входом. Для отдельного тома применяется регламент ниже. [Восстановление и отдельные тома](disaster_recovery.md)

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
