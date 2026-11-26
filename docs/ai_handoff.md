# Передача AI Core

Базовая ревизия общей ветки — 215396f. Основные коммиты — e433b37 (контекст, Qwen, SQLite) и 154b9f7 (UI, проверка эффекта, мониторинг и экспорт). Последующие изменения и прогоны описаны в [отчёте](../NIGHT_REPORT.md). Ветка feature/ai-core самостоятельна.

## Запуск и проверка

Используйте Python 3.11+, команды установки и синтетического запуска из README. Для отдельной рабочей копии задайте собственную директорию состояния вне репозитория.

    # PowerShell
    $env:PM_PRIVATE_DIR = "$env:TEMP/ParadiigmaMGR-ai-core"
    python app.py serve --port 8080

    # Linux
    export PM_PRIVATE_DIR="$HOME/.local/share/paradiigmamgr-ai-core"
    python app.py serve --port 8080

Для локального Qwen3-4B запустите имеющийся llama-server и проверенные веса командой ниже в отдельном терминале. Ориентир для CPU-запуска — от 8 ГБ ОЗУ, предпочтительно 12 ГБ с запасом для системы и контекста. Фактическая доступная память и скорость зависят от машины. Используйте --download только после согласования загрузки весов.

    python scripts/run_model.py --server PATH_TO_LLAMA_SERVER --model PATH_TO_APPROVED_GGUF

В оболочке приложения задайте PM_LLM_URL равным http://127.0.0.1:18081/v1 и при необходимости PM_LLM_MODEL, PM_LLM_PROMPT_FILE или PM_LLM_SELECT_STEP. Для проверки используйте синтетические источники и следующий сценарий.

    python -m unittest discover -s tests -v
    node --check web/app.js
    python scripts/qa_browser.py
    python scripts/ai_smoke.py --port 8080 --mode real --runs 3

Для прогона без модели используйте --mode fallback. Импорт UTF-8 доступен через python app.py import --file PATH.txt --kind Интервью. Для Excel требуются --sheet и --range, поддерживаются XLSX/XLSM. Диапазоны с эталонного листа отклоняются. Для p95 скрипт требует не менее 10 наблюдений. Он выводит замеры и статусы без текстов отчётов. Файлы реальных отчётов и Redmine payload можно получить через scripts/ai_export.py вне репозитория.

## Сборка и контракты

app.py маршрутизирует существующий HTTP API и подключает pm/investigation.py. pm/analysis_context.py собирает источники, рассчитывает доступность и нормализует доказательства. pm/qwen.py отправляет контекст только loopback-серверу, валидирует результат и допускает один повтор исправления JSON. pm/ai_store.py хранит результаты и решения. pm/monitor.py делает локальную проверку порога. Поля и маршруты описаны в [контракте](ai_contract.md).

Параметры PM_WORKBOOK, PM_CASE_CONFIG, PM_SNAPSHOT, PM_EVIDENCE и PM_PRIVATE_DIR относятся к локальным данным. METRIKA_COUNTER_ID, METRIKA_TOKEN, METRIKA_SCOPE, METRIKA_GOALS и METRIKA_SEGMENTS используются только на сервере. Названия метрик и настройка фильтров сверены с документацией. [Метрики посещаемости](https://yandex.ru/dev/metrika/ru/stat/presets/preset_traffic) и [сегментация](https://yandex.ru/dev/metrika/ru/stat/segmentation)

PM_MONITOR_INTERVAL пуст по умолчанию. При настройке на 60–86400 секунд приложение проверяет локальный порог без сетевых обращений. В журнале есть состояния и задержки вызовов. Корпоративный Redmine получает только локально подготовленный payload при экспорте, отправка требует отдельного разрешения.

## Интеграция второй ветки

1. Подключить pm/web_search.py из ветки владельца адаптера при отдельном согласованном слиянии. Сохранять контракт v1 и текущий ленивый импорт.
2. Проверить совпадение значений YANDEX_SEARCH_ALLOWED_DOMAINS, YANDEX_SEARCH_PUBLIC_PRODUCTS и флагов по .env.example. До настройки закрыть режим поиска.
3. Повторить тест контракта с установленным адаптером. Live smoke с ключом и бюджетом выполняет владелец инфраструктуры по отдельному разрешению.
4. Подключить данные Softline через PM_WORKBOOK и PM_CASE_CONFIG либо PM_SNAPSHOT и PM_EVIDENCE только в разрешённом контуре. Отдельно сверить периоды, контроль и источники вне эталонного листа.

Для отката запустите версию 215396f в отдельной рабочей копии с отдельным PM_PRIVATE_DIR. Сохраните текущий ai.sqlite3 для возврата к расследованиям. Исходные файлы и пользовательские данные не требуют миграции обратно.
