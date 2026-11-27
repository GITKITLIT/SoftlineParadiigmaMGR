# Qwen · приёмка на целевом Linux

Базовый SHA `5e956ca08f67cd0d4501596d85c14493239b8ffa`. Владелец инфраструктуры — Максим. Владелец интеграционного решения — Андрей. Рабочие перезапуски и изменения постоянного туннеля проводятся владельцем Deployment в согласованное окно.

Приёмка проводится на одном модельном VPS 1Gb.ru с 4 vCPU Intel Sandy Bridge, 7,8 ГиБ RAM, 1,4 ГиБ swap и Ubuntu 24.04. Backend размещается отдельно в Яндекс Облаке. На VPS модели нужен `llama.cpp b12800` с `libggml-cpu-sandybridge.so` и `libgomp1`. Аппаратная конфигурация утверждена. Задержки и эффективные параметры этого сервера ещё предстоит измерить.

## Статус и результаты

| Проверка | Фактический результат |
| --- | --- |
| Однослотовая Qwen, `/health`, `/v1/models`, живой ответ через SSH | Пройдена локальная проверка OpenSSH на Mac с настоящей Qwen и синтетическим запросом. |
| Восстановление `qwen.ok` после разрыва | Пройдено локальное выключение и повторный запуск SSH-сервера и клиента. |
| Синтетические сценарии через восстановленный локальный SSH | История 1,467 с, расследование 13,333 с, два клиента 27,886 с за пару. Все проверки прошли. |
| Перезапуск процесса модели при открытом SSH | Локально на Mac: `qwen.ok` сменился `true → false → true`, SSH-процесс сохранился, новый процесс Qwen ответил на запрос. |
| Совместимость скрипта с API `b12800` | Официальная сборка Mac приняла однослотовый тестовый профиль 6144, K `q8_0` и V `f16`. Исторический запрос прошёл численные сверки, оба шага записали тайм-аут и токены в отчёт. |
| SSH и конфигурация модельного VPS | Вход по согласованному ключу прошёл. Фактически Ubuntu 24.04 LTS x86‑64, 4 vCPU Intel Xeon E312xx Sandy Bridge, 7,75 ГиБ RAM и 1,36 ГиБ swap. AVX и SSE4.2 доступны. AVX2, FMA, F16C и AVX‑512 отсутствуют. |
| Бинарник и Sandy Bridge backend | На VPS подтверждены `llama.cpp b12800`, commit `47d6192fc`, и `libgomp1` `14.2.0-4ubuntu2~24.04.1`. SHA‑256 бинарника и семи библиотек совпали с архивом. При отдельной диагностике загрузчик инициализировал `libggml-cpu-sandybridge.so` и `libgomp.so.1`. |
| SHA GGUF, запуск и AppArmor | SHA‑256 GGUF совпал. После запуска служба отвечает на `127.0.0.1:18081` и сообщает 1 слот с контекстом 8192. У процесса подтверждены 4 потока генерации, 8 потоков батча, кеш 512 МиБ, CPU, `reasoning off` и `paradiigma-launcher (enforce)`. Чтение `/proc/<pid>/maps` осталось за оператором. |
| История, полное синтетическое расследование и пара клиентов на Linux | Измерения ожидаются. |
| Постоянный канал Backend → модель, восстановление после перезапуска systemd | Ожидается подтверждение со стороны Backend и согласованное окно. |

## 1. Сверка модельного узла

На развернутом узле разрешения каталога `/var/lib/paradiigma/models` составляют `0750 root:pm-qwen`. Для первой серии Максим выполняет две команды в согласованное окно. Если `sha256sum` подтвердит ожидаемое значение из [runtime-инструкции](QWEN_FINAL_RUNTIME.md), он запускает установленную службу.

```sh
sudo -u pm-qwen /usr/bin/sha256sum /var/lib/paradiigma/models/Qwen3-4B-Q4_K_M.gguf
sudo /usr/bin/systemctl start paradiigma-qwen.service
```

Выполнить read-only диагностику на модельном VPS. В отчёт для команды сохранить только аппаратные характеристики, версии, хэш и разрешённые агрегаты. Не переносить адреса, ключи, содержимое EnvironmentFile или рабочие тексты.

```sh
uname -srmo
lscpu
free -m
swapon --show --bytes
systemctl show paradiigma-qwen.service \
  -p ActiveState -p SubState -p MainPID -p ControlGroup -p MemoryHigh -p MemoryMax \
  -p CPUQuotaPerSecUSec -p AppArmorProfile
sudo ss -lntp 'sport = :18081'
sudo sha256sum /var/lib/paradiigma/models/Qwen3-4B-Q4_K_M.gguf
dpkg-query -W -f='${Status} ${Version}\n' libgomp1
runtime=/usr/local/lib/paradiigma-llama-b12800
env LD_LIBRARY_PATH="$runtime" ldd /usr/local/bin/llama-server
env LD_LIBRARY_PATH="$runtime" ldd "$runtime/libggml-cpu-sandybridge.so"
env LD_LIBRARY_PATH="$runtime" GGML_BACKEND_PATH="$runtime/libggml-cpu-sandybridge.so" \
  /usr/local/bin/llama-server --version
curl --noproxy '*' -fsS http://127.0.0.1:18081/health
curl --noproxy '*' -fsS http://127.0.0.1:18081/v1/models | jq -e 'any(.data[]; .id == "qwen3-4b")'
curl --noproxy '*' -fsS http://127.0.0.1:18081/props |
  jq -e '.total_slots == 1 and .default_generation_settings.n_ctx == 8192'
```

В `ldd` ожидаются `libgomp.so.1` у Sandy Bridge backend и отсутствие `not found` в обоих выводах. Убедиться, что порт слушает только `127.0.0.1`. Получить фактический PID `llama-server` из cgroup: `MainPID` может принадлежать Python-лаунчеру. В `/proc/<pid>/attr/current` ожидается профиль `paradiigma-launcher` в режиме enforce. В `/proc/<pid>/maps` проверить, что из `libggml-cpu-*.so` загружен только Sandy Bridge. Для хэша GGUF ожидается значение из [runtime-инструкции](QWEN_FINAL_RUNTIME.md). Записать `b12800` и фактический commit, который печатает бинарник. Принятый SHA приложения проверяется отдельно.

```sh
group=$(systemctl show paradiigma-qwen.service -p ControlGroup --value)
test -n "$group" && while read -r pid; do
  if [ "$(sudo readlink "/proc/$pid/exe")" = /usr/local/bin/llama-server ]; then
    sudo cat "/proc/$pid/attr/current"
    sudo awk '$NF ~ /\/libggml-cpu-[^/]+\.so$/ {print $NF}' "/proc/$pid/maps" | sort -u
  fi
done < "/sys/fs/cgroup$group/cgroup.procs"
```

## 2. Короткий бенчмарк по рабочему туннелю

На Backend использовать обычную Python-среду принятой ревизии и одноразовую копию исходников. Скопировать `production_benchmark.py` из пакета в `scripts/qwen/` копии. Запустить через существующий рабочий SSH forward. Режим `--existing-endpoint` не запускает процессы llama.cpp и связывается через loopback. Скрипт использует синтетическую SQLite и fixtures. Рабочий Search, Метрика и SMTP в этом прогоне выключены или заменены тестовым адаптером. Окружение рабочей службы Backend не загружать.

```sh
umask 077
acceptance_state=$(mktemp -d)
python scripts/qwen/production_benchmark.py \
  --existing-endpoint --port 18081 --expected-slots 1 \
  --case history --case drop --case concurrent --warm-repeats 1 \
  --output "$acceptance_state/through-tunnel"
```

`history` проверяет выбор исторического инструмента и точное сравнение окон. `drop` проверяет обязательный выбор продаж и воронки, ссылочную целостность и проверяемые гипотезы. `concurrent` одновременно запускает два клиента и сохраняет время каждого, полное время пары и качество. SSE-проба отдельная. Этапы структурированных запросов пишутся без содержания промпта. Snapshot ресурсов модели берётся на самом модельном VPS, потому что в этом режиме клиент не видит удалённый PID.

В `model_http[].timings` скорость prefill считать как `1000 × prompt_n / prompt_ms`, скорость decode как `1000 × predicted_n / predicted_ms`. `prompt_n` — фактически вычисленные токены по счётчику сервера, `cache_n` нужно сохранять рядом. Для итоговой скорости по серии суммировать токены и длительности каждой фазы отдельно. TTFT брать из `stream.result.ttft_seconds` и подписывать как измерение отдельной SSE-пробы через тот же канал. Полную задержку расследования брать из `result.wall_seconds`, время пары и отдельные времена клиентов указывать раздельно. При отсутствии фазовых таймингов указывать пропуск, а не вычислять их из `wall_seconds`.

На модельном узле из корня копии ветки `handoff/qwen-final` запустить `sample_linux_resources.py` в отдельной SSH-сессии до первого запроса. Остановить `Ctrl+C` после завершения обоих клиентов и SSE-пробы. Скрипт с интервалом в секунду читает cgroup v2 службы Qwen и данные Linux-хоста. По UTC-меткам свести его строки с `started_at` и `finished_at` из `results.jsonl`. Для CPU брать разность `cpu.usage_usec` за интервал, делённую на длительность в микросекундах. Умножение на 100 даёт процент одного vCPU. Рост `nr_throttled` и `throttled_usec` показывает действие квоты. На хосте долю `steal` считать по разности одноимённых тиков, делённой на сумму разностей всех полей `host_cpu_ticks`.

```sh
umask 077
resource_log=$(mktemp /tmp/qwen-resources.XXXXXXXX)
printf '%s\n' "$resource_log"
python3 handoff/qwen-final/sample_linux_resources.py > "$resource_log"
```

Максимум `memory_current_bytes` показывает наблюдаемый пик cgroup в выбранном окне. `memory_lifetime_peak_bytes` — накопленный пик cgroup, в который могут входить более ранние запросы. RSS модели брать отдельно по `llama_process_memory_kib[].VmRSS`; `VmHWM` — пик за жизнь конкретного PID. Сравнить `swap_current_bytes` службы и рост `pswpin`/`pswpout` хоста. Прирост `oom` и `oom_kill` в `memory_events` зафиксировать отдельно от остановок модели оператором. Пустые значения и `service_cgroup_present=false` отмечают недоступность счётчиков. После перезапуска или смены cgroup интервал измерить заново. Если узел не использует cgroup v2, собрать эквивалентные метрики штатными средствами хоста.

Если форвардинг пока недоступен, тот же тест запускается на модели через её собственный loopback. Это даёт локальный Linux runtime-результат. Проверку из Backend потом выполнить отдельно.

## 3. Подбор однослотового профиля на 1Gb.ru

Сначала измерить штатную службу с 8 потоками батча. Для сравнения на VPS Максим может поставить [временный systemd drop-in для 4 потоков батча](runtime/90-qwen-acceptance-batch4.conf). Он заменяет `ExecStart` на прямой запуск того же `llama-server` с теми же параметрами и сохраняет системную роль, AppArmor, библиотечный каталог, ограничения сети и loopback. Перед запуском с drop-in Максим сверяет SHA‑256 весов вручную, поскольку этот запуск не проходит через `run_model.py`. На время замера нужен один экземпляр модели. После проверки восстановить штатный `ExecStart` удалением только созданного файла.

```sh
# Выполняет Максим из корня ветки handoff/qwen-final в согласованное окно.
sudo -u pm-qwen /usr/bin/sha256sum /var/lib/paradiigma/models/Qwen3-4B-Q4_K_M.gguf
# Продолжить после совпадения SHA-256 из QWEN_FINAL_RUNTIME.md.
sudo install -d -o root -g root -m 0755 /run/systemd/system/paradiigma-qwen.service.d
sudo install -o root -g root -m 0644 handoff/qwen-final/runtime/90-qwen-acceptance-batch4.conf \
  /run/systemd/system/paradiigma-qwen.service.d/90-qwen-acceptance-batch4.conf
sudo systemctl daemon-reload
sudo systemctl restart paradiigma-qwen.service
# После замеров вернуть исходный профиль.
sudo rm -- /run/systemd/system/paradiigma-qwen.service.d/90-qwen-acceptance-batch4.conf
sudo systemctl daemon-reload
sudo systemctl restart paradiigma-qwen.service
```

Альтернативный способ для временного внешнего экземпляра. Его применять только в согласованном окне, освободив RAM остановкой штатной службы. Для тяжёлого синтетического сценария сопоставить прогоны после запуска и с прогретым кешем. Команда запускается из одноразовой копии Backend с установленными зависимостями и скриптом в `scripts/qwen/`.

```sh
umask 077
trial=$(mktemp -d)
runtime=/usr/local/lib/paradiigma-llama-b12800
env LD_LIBRARY_PATH="$runtime" GGML_BACKEND_PATH="$runtime/libggml-cpu-sandybridge.so" \
  python scripts/qwen/production_benchmark.py \
  --server /usr/local/bin/llama-server --model /var/lib/paradiigma/models/Qwen3-4B-Q4_K_M.gguf \
  --port 18181 --profile one-slot-baseline --profile one-slot \
  --threads 4 --threads-batch 4 --batch-size 2048 --ubatch-size 512 --ctx-per-slot 8192 \
  --case history --case drop --cold-case drop --cold-repeats 1 --warm-repeats 1 \
  --output "$trial/threads"
```

- **Контекст.** Оставить 8192, пока профиль помещается в память и проходит проверку. Если память мешает работе, отдельный кандидат — `--ctx-per-slot 6144` с теми же потоками, F16 и батчем. Убедиться, что максимальный вход каждого вызова вместе с его бюджетом `max_tokens` помещается в окно. Сопоставить выбранные инструменты, ссылки, численные сверки, гипотезы и признаки обрезки. Перед принятием укороченного окна проверить длинный разрешённый случай.
- **KV.** При подтверждённой нехватке RAM сравнить отдельный `--kv-k q8_0 --kv-v f16`, сохранив 8192 токена. Изменять V только отдельным прогоном после успешной загрузки на данном CPU. Flash Attention с квантованным V требует поддержки backend, которую подтверждает фактический запуск. Не включать swap в бюджет памяти и не считать меньший размер KV ускорением без таймингов.
- **Тайм-ауты.** В принятом Core планирование ограничено 60 с, один шаг — до 26 с. Синтез имеет общий бюджет 180 с и до 110 с на HTTP-попытку. Клиент чата ждёт до 270 с. В `model_http` фиксируются выделенный транспортный тайм-аут, результат, число токенов и времена фаз от сервера, когда доступны. При `decision_unavailable` проверить конкретный вызов и набор действительно выбранных моделью инструментов. Для тайм-аутов при паре клиентов учесть очередь единственного слота. Кандидат на изменение лимитов готовится только после фактических замеров, с повторной проверкой полной задержки и качества через Backend. [Лимит планировщика](https://github.com/GITKITLIT/SoftlineParadiigmaMGR/blob/5e956ca08f67cd0d4501596d85c14493239b8ffa/pm/agent.py) · [Лимиты Qwen](https://github.com/GITKITLIT/SoftlineParadiigmaMGR/blob/5e956ca08f67cd0d4501596d85c14493239b8ffa/pm/qwen.py)
- **Приёмка кандидата.** Сравнить ссылки, выбранные инструменты, гипотезы, численные результаты, тайм-ауты, cgroup и давление на swap. При выигрыше повторить многошаговый запрос и пару клиентов через постоянный туннель из Яндекс Облака. Передать Максиму измеренные параметры. До этого применяется штатный однослотовый профиль.

## 4. Восстановление

1. Из Backend выполнить `python scripts/deploy/health.py --expect-qwen`. Зафиксировать `qwen.ok` и общий код возврата. Проверить разрешённый синтетический запрос через туннель. На модельном узле подтвердить loopback listener.
2. В согласованное окно на Backend остановить только сервис туннеля. Дождаться, что `qwen.ok=false`, а штатный Backend доступен. Запустить туннель, дождаться `qwen.ok=true` и повторить `history`. Для проверки политики автоперезапуска оператор завершает только главный процесс SSH туннеля и сверяет увеличение `NRestarts`, новый PID и успешный probe. Принудительный разрыв действующих запросов во время проверки ожидаем.
3. В согласованное окно перезапустить модельную службу. Зафиксировать длительность восстановления, `qwen.ok`, параметры модели и повторный живой запрос. Восстановление после reboot Backend и модели проводить отдельно, при наличии безопасного окна и внешнего пути администрирования. Успех на Mac или обычная смена соединения не подтверждают эту часть systemd.
4. Записать по сценарию UTC, SHA кода, хэш GGUF, версию, CPU, RAM, профиль, время, статус, число вызовов, токены, тайм-ауты, пройденные проверки и ресурсные метрики. Значение `not_needed` допустимо для истории при подтверждённом вызове планировщика. Ответ `ok` с тайм-аутом шага считать деградацией. Результат и точную конфигурацию передать Максиму и Андрею.
