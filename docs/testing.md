# Тестирование

Подготовьте окружение по [README](../README.md). Проверки ниже используют синтетические данные, локальные процессы и фикстуры Search.

```text
python -m unittest discover -s tests -v
python scripts/qwen/evaluate.py --mode fallback --concurrent
python scripts/qwen/historical_smoke.py
python scripts/qwen/effect_smoke.py
python scripts/search_smoke.py --summary
python scripts/search_investigation_smoke.py
python scripts/search_release_smoke.py
node --check web/app.js
node --check llm_lab/integration/chat.js
```

## Браузер и PIM

```text
python -m pip install playwright
python -m playwright install chromium
python scripts/qa_browser.py
python scripts/pim_acceptance.py
```

`PM_CHROMIUM` позволяет выбрать установленный Chrome или Edge. Браузерный smoke проходит маршруты Dashboard, открывает сохранённый отчёт и ссылку на доказательство после перезагрузки, проверяет историю чата, согласование и скачивание Redmine JSON. Он проверяет также загрузку материалов, симуляцию эффекта и мобильную навигацию под политикой CSP, сформированной для текущего JavaScript. [Ручная проверка Dashboard](dashboard.md) Проверка PIM проверяет сравнение истории, связи с доказательствами, отделение режимов данных и восстановление SQLite.

## Воспроизведение сохранённых данных

`demo/rehearse.py` хранит синтетические сеансы под `ParadiigmaMGR-public-demo/runs` в системном временном каталоге. Для уже подготовленного прогона `python demo/rehearse.py verify-replay ИМЯ` проверяет сохранённые отчёт, диалог и задачу без модели. `python demo/rehearse.py replay ИМЯ` открывает тот же сеанс в Chrome. Создание нового прогона с `rehearse` требует локальной Qwen и TLS-инструментов для тестовой почты.

Проверки Nginx используют утилиты `nginx`, `htpasswd` и `openssl`. POSIX-права и системные сценарии развёртывания проверяйте в одноразовой Linux-ВМ. Состав и условия запуска перечислены в [workflow](../.github/workflows/deployment.yml).
