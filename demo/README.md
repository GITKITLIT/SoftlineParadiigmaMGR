# Синтетическая репетиция

Из корня клона установите зависимости. Нужны Chrome, OpenSSL в PATH и утверждённые веса Qwen3-4B Q4_K_M. Задайте переменные окружения LLAMA_SERVER и QWEN_GGUF путями к бинарнику и весам.

```
python -m pip install -r requirements.txt playwright
python scripts/run_model.py --server $env:LLAMA_SERVER --model $env:QWEN_GGUF --parallel 2
```

В другом терминале после готовности модели:

```
python -X utf8 demo/rehearse.py rehearse dress
python -X utf8 demo/rehearse.py verify-replay dress
python -X utf8 demo/rehearse.py replay dress
```

Первый тик через 60 секунд. Для нового состояния выберите свободное имя. Для интерактивного сценария используйте fresh show-01. Для живого запроса на сохранённой базе используйте replay dress --live-replay при запущенной модели. Подкоманда finish dress продолжает проверку на прежней базе после записи экспорта Redmine. Лаунчер сохраняет всё состояние в системном временном каталоге ParadiigmaMGR-public-demo/runs. Исторические данные состоят из двух синтетических точек. Письмо принимает локальное TLS-реле.

[Порядок показа](../DEMO_SCENARIO.md).
