# Установка на Ubuntu 24.04 LTS

Для сборки с AI Core v6 и SMTP используйте [инструкцию объединённого релиза](combined_release_deployment.md). В ней описаны обязательная независимая копия v5, переход схемы и возврат к согласованной паре кода и данных.

Администратор выполняет команды в согласованное окно на выделенной VM с systemd. Нужны исходящий HTTPS к GitHub, репозиториям Ubuntu и PyPI, место под две версии кода с venv, SQLite, снимки и рабочие файлы восстановления. Проверяйте место на обоих томах командой `df -h /opt /var`. Образ Ubuntu, архитектуру, ядро и фактические лимиты VPS фиксирует капитан.

## Подготовка и первый запуск

Капитан передаёт полный SHA проверенной ревизии по доверенному каналу, включая одобренные изменения AI Core, lock-файл и каталог развёртывания. Просмотрите diff и результаты тестов этой ревизии. В production применяется конкретный SHA. Ветка, тег или HEAD в качестве аргумента установщика отклоняются. Для приватного репозитория используйте отдельный ключ чтения с проверенным ключом хоста GitHub или защищённый Git credential helper. Токены в URL и shell history раскрывают доступ.

Из административной оболочки на выделенном Ubuntu-хосте:

```bash
apt-get update
apt-get install -y git ca-certificates
install -d -o root -g root -m 0700 /srv/paradiigma-admin
git clone --no-checkout https://github.com/GITKITLIT/SoftlineParadiigmaMGR.git /srv/paradiigma-admin/repo
cd /srv/paradiigma-admin/repo
read -r -p 'Полный утверждённый SHA: ' sha
git cat-file -e "$sha^{commit}"
git switch --detach "$sha"
git diff --exit-code HEAD
bash scripts/deploy/install.sh "$sha"
systemctl status paradiigma.service paradiigma-backup.timer --no-pager
curl --noproxy '*' -fsS -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8080/api/analysis/settings
systemctl start paradiigma-backup.service
```

Установщик извлекает tracked-файлы через `git archive`, ставит проверенные по SHA-256 колёса Python в venv релиза и прогоняет синтетический smoke от имени pm-app. Файл `requirements.txt` задаёт диапазоны разработчика. При их изменении обновите и проверьте `deploy/requirements.lock` на Ubuntu. Пакеты Ubuntu получают исправления из согласованных репозиториев ОС. Повторный запуск для установленного SHA проверяет службу, сохраняет закрытую конфигурацию, знания, документы и снимки. После изменения app.env вручную перезапустите службу. Частично собранный каталог при повторе переносится в `*.incomplete.*` для проверки администратором.

| Каталог | Владелец и назначение |
|---|---|
| /srv/paradiigma-admin/repo | Закрытый root checkout для получения проверенных ревизий |
| /opt/paradiigma/releases/SHA | Принадлежащий root код, venv и запись проверки |
| /opt/paradiigma/current, previous | Текущий и предыдущий релиз |
| /etc/paradiigma/app.env | root:root 0600, PID 1 загружает в окружение Backend |
| /var/lib/paradiigma/db | pm-app:pm-db 2750, постоянные SQLite, journals и индекс |
| /var/lib/paradiigma/documents | root:pm-app 0750, утверждённые исходные файлы с режимом 0640 |
| /var/lib/paradiigma/models | root:pm-qwen 0750, веса модели |
| /var/backups/paradiigma | Закрытое независимое хранилище pm-backup |
| /var/lib/paradiigma-backup-status | Статус резервирования, доступный для чтения Backend |
| journal и PrivateTmp | Системные события systemd и раздельные временные файлы служб |

## Конфигурация и запуск

По умолчанию пустые интеграции и режим `PARADIIGMA_MODE=demo` загружают встроенные примеры. Публичную демонстрацию проводите на **отдельной чистой VM** по инструкции доступа. Смена флага в существующей рабочей инсталляции сохраняет её базу и непригодна для изоляции данных.

Для закрытой инсталляции администратор открывает `sudoedit /etc/paradiigma/app.env`, выбирает `PARADIIGMA_MODE=private` и вносит согласованные параметры. Синтаксис соответствует EnvironmentFile systemd. Абсолютные пути PM_WORKBOOK, PM_CASE_CONFIG, PM_SNAPSHOT, PM_EVIDENCE, PM_BENCHMARK и PM_LLM_PROMPT_FILE должны указывать на читаемые файлы в /var/lib/paradiigma/documents. Импорт выполняется с правами pm-app в тот же PM_PRIVATE_DIR. Постоянный каталог DB отделён от исходных документов. Значения METRIKA_COUNTER_ID и METRIKA_TOKEN задаются парой. Для корпоративного счётчика задайте METRIKA_SCOPE=store_counter. Поиск включается после интеграции проверенного pm.web_search и настройки учётных данных и явных allowlist. Списки JSON удобно записывать в одинарных кавычках. Пример строки для формата файла — `YANDEX_SEARCH_ALLOWED_DOMAINS='["example.org"]'`.

```bash
systemctl restart paradiigma.service
systemctl is-active paradiigma.service
systemctl list-timers paradiigma-backup.timer
sudo -u pm-app /usr/bin/python3 /opt/paradiigma/current/scripts/deploy/health.py
```

ExecStartPre проверяет окружение и открывает реальную SQLite через AI Core. Для сообщения Preflight failed проверьте парность ключей, JSON, разрешённые пути, права и интервалы. Запуск диагностического импорта или печать окружения с реальными секретами в журнале раскрывает данные. Systemd отправляет SIGINT для штатной остановки фоновых workers, затем применяет TimeoutStopSec. На время обслуживания закройте вход Nginx и остановите командные туннели, дождитесь завершения активных исследований. Длинный запрос может выходить за время остановки.

## Обновление и возврат к прежней версии

1. Запишите действующий SHA через `basename "$(readlink -e /opt/paradiigma/current)"`. Закройте доступ клиентов. Зафиксируйте совместимость прикладного формата, порядок восстановления внешних документов и локальный тест восстановления. Внешние SQL-инструменты и импорты должны прекратить запись на время переключения.
2. Из корневого административного checkout получите новый одобренный SHA и запустите транзакцию:

```bash
cd /srv/paradiigma-admin/repo
git fetch origin
read -r -p 'Полный утверждённый SHA: ' sha
git switch --detach "$sha"
git diff --exit-code HEAD
bash scripts/deploy/update.sh "$sha"
systemctl is-active paradiigma.service paradiigma-backup.timer
systemctl start paradiigma-backup.service
```

3. Проверьте отчёт состояния, ранее известные ID исследования и задачи, модель (если включена) и последнюю независимую копию. Откройте вход после приёмки. Активная модель закреплена за старым процессом до отдельного `systemctl restart paradiigma-qwen.service`. Изменения бинарника, весов, профиля AppArmor и приватной конфигурации проверяются администратором как часть обновления.
4. Для ручного отката передайте записанный ранее SHA в `scripts/deploy/update.sh` из доверенного checkout. Откат проходит через ту же остановку, копирование и проверку. При ошибке переключения скрипт возвращает предыдущие код и unit-файлы и проверяет доступность Backend. `flock` запрещает параллельные активации. Новая версия готовится до остановки работающего приложения.
5. Автоматический путь требует совпадения SQLite user_version установленной базы и синтетической базы новой версии. Одинаковое число само по себе не гарантирует совместимость данных. До авторизации SHA проверьте обратную совместимость чтения и записи на синтетической копии. Для миграции со сменой схемы или семантики данных остановите клиентов, подготовьте план переноса и проверьте восстановление старой пары код/данные в изолированной среде по DR-регламенту. После сбоя питания проверьте `current`, `previous`, активность timer и возраст проверенной копии до открытия входа.

## Qwen и ресурсы

Штатный CPU-профиль запускает существующий scripts/run_model.py, который сверяет SHA-256 GGUF и задаёт модель, контекст, потоки, порт и шаблон. Согласуйте с Амиром релиз llama.cpp, зависимости, контрольные суммы бинарника и весов, p95 и расход RAM для реальной нагрузки. Ориентир AI Core — от 8 ГБ RAM, предпочтительно 12 ГБ суммарно. Отдельно измерьте память Backend, SQLite cache, ОС и свободное место. Установщик оставляет модель выключенной до подготовки этих файлов.

Разместите согласованный CPU-бинарник в /usr/local/bin/llama-server (root:root 0755), его зависимости в системных каталогах или /usr/local/lib и проверенные веса в /var/lib/paradiigma/models/Qwen3-4B-Q4_K_M.gguf (root:pm-qwen 0640). AppArmor допускает запуск только штатного worker из Python-профиля. Рабочий процесс наследует профиль службы с запретом запуска командных оболочек и Python, запрет исходящего connect и ограничения loopback. Для конкретной сборки проверьте загрузку модели и разрешения библиотек на стенде. При несовместимости уточните профиль для конкретных путей.

```bash
install -o root -g root -m 0644 /opt/paradiigma/current/deploy/apparmor/paradiigma-qwen /etc/apparmor.d/paradiigma-qwen
apparmor_parser -r /etc/apparmor.d/paradiigma-qwen
systemctl enable --now paradiigma-qwen.service
curl --noproxy '*' -fsS http://127.0.0.1:18081/health
```

Задайте PM_LLM_URL=http://127.0.0.1:18081/v1 и PM_LLM_MODEL=qwen3-4b в закрытом app.env, перезапустите Backend и выполните синтетический smoke. Если память VPS недостаточна, разместите модель на отдельном разрешённом вычислительном узле в том же контуре. Оставьте llama-server на его loopback и организуйте выделенный SSH local forward с VPS на узел модели (локальное связывание 127.0.0.1:18081, PermitOpen только 127.0.0.1:18081, StrictHostKeyChecking=yes, ExitOnForwardFailure=yes, индивидуальный ключ отдельной службы). Политики допуска должны разрешать передачу контекста модели по этому каналу.

Пределы MemoryHigh, MemoryMax, TasksMax и CPUQuota определены раздельно в unit-файлах. Измените их через `systemctl edit paradiigma.service` или `systemctl edit paradiigma-qwen.service` после замеров. Примените `systemctl daemon-reload`, перезапустите изменённую службу, проверьте `systemctl show -p MemoryCurrent -p MemoryPeak -p NRestarts` с именем службы. Логи Qwen и stderr Backend подавлены для защиты содержимого запросов, события запуска/останова остаются в journal.
