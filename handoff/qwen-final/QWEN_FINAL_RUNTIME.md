# Qwen3‑4B · подключение к отдельному VPS

База приёмки `feature/integration` на `5e956ca08f67cd0d4501596d85c14493239b8ffa`. Пакет для Максима и Андрея. Конфигурации в `ssh/` подготовлены для согласования с Deployment. Результаты узла и статусы проверок ведутся в [QWEN_LINUX_ACCEPTANCE.md](QWEN_LINUX_ACCEPTANCE.md).

## Схема соединения

- Backend использует `PM_LLM_URL=http://127.0.0.1:18081/v1`. На Backend этот порт занимает SSH-клиент, на VPS модели — `llama-server`.
- Backend открывает исходящее SSH-соединение к приватному адресу или согласованному VPN-адресу VPS модели. OpenSSH направляет локальный порт на `127.0.0.1:18081` на стороне модели. Вход TCP/SSH на VPS модели ограничивается адресом Backend или утверждённой частной сетью. Порт модели снаружи закрыт для IPv4 и IPv6.
- `pm/qwen.py` продолжает разрешать только HTTP на loopback. `paradiigma-qwen.service` сохраняет AppArmor, системный запрет `connect` и сетевые ограничения. Исходящее SSH-соединение запускается отдельным процессом на Backend, где служба приложения работает в своей роли `pm-app`. Доступ других процессов к loopback виден на границе хоста, поэтому проверять доверие локальных аккаунтов и изоляцию Backend.
- Healthcheck на Backend уже делает `GET /health` с `127.0.0.1:18081`. `--expect-qwen` делает доступность модели обязательной в сводной проверке вместе с Backend, SQLite и резервными копиями. Отчёт `qwen.ok` показывает именно состояние модели через туннель. `ExitOnForwardFailure` проверяет создание слушателя. До готовности приложения на другом конце сам слушатель ещё не доказывает готовность модели.

[Принятый healthcheck](https://github.com/GITKITLIT/SoftlineParadiigmaMGR/blob/5e956ca08f67cd0d4501596d85c14493239b8ffa/scripts/deploy/health.py) · [OpenSSH client](https://man.openbsd.org/ssh_config) · [OpenSSH server](https://man.openbsd.org/sshd_config)

## Что согласует и устанавливает Максим

1. На модельном VPS выделить сервисный аккаунт `pm-model-forward` с `nologin`. Установить [блок Match](ssh/sshd_config.model-forward) в конце `sshd_config` после глобальных директив и с сохранением существующих правил для человеческого доступа. Файл ключа в `/etc/ssh/pm-model-authorized-keys/` — root-owned. Для постоянного ключа использовать отдельную строку `restrict,port-forwarding,permitopen="127.0.0.1:18081"` перед содержимым `.pub`. При фиксированном адресе источника допустимо добавить `from=` с подтверждённым адресом Backend. Проверить, что существующие `AllowUsers`, `AllowGroups` и предыдущие блоки Match позволяют ровно согласованный доступ. `MaxSessions 0` запрещает shell и подсистемы, сохраняя нужный TCP forwarding.
2. На Backend выделить системный аккаунт `pm-qwen-tunnel` без интерактивного входа. Создать отдельную пару ключей для постоянной службы. Приватный ключ хранится на Backend в `/etc/paradiigma/qwen-tunnel/identity` с владельцем root и режимом 0600. Unit передаёт его процессу через `LoadCredential`. Передать на сервер модели только публичный ключ. Использовать подтверждённый SSH-отпечаток хоста из доверенного канала для `known_hosts`. Пример [SSH-конфигурации](ssh/ssh_config.example) требует заменить имя хоста и при необходимости порт. `ssh_config` и `known_hosts` доступны для чтения службе и изменяемы root. Ключ диагностического входа оператора хранится отдельно.
3. Перед reload OpenSSH сохранить существующее административное соединение и выполнить `sshd -t` и проверку эффективных настроек с параметрами фактического адреса Backend. Для сервисного аккаунта ожидаются `maxsessions 0`, `allowtcpforwarding local`, `permitopen 127.0.0.1:18081`, `permitlisten none`. Проверить новый операторский вход и отдельно подтверждённый доступ службы.
4. На Backend установить [systemd unit](ssh/paradiigma-qwen-tunnel.service) как `/etc/systemd/system/paradiigma-qwen-tunnel.service`. Проверить, что порт 18081 свободен. Если на Backend уже работает локальная модель, согласовать окно переключения с оператором. После валидации `systemd-analyze verify` оператор выполняет `systemctl daemon-reload` и `systemctl enable --now paradiigma-qwen-tunnel.service`. Unit повторяет подключение каждые 5 с после выхода SSH. Проверка живости SSH настроена на 15 с × 3 пропуска. Backend продолжает обслуживать исторические отчёты при временной недоступности модели.
5. После установления канала задать принятые настройки Backend. Перезапуск Backend требуется только при изменении окружения. Точную последовательность и критерии восстановлений см. в [приёмке Linux](QWEN_LINUX_ACCEPTANCE.md).

```dotenv
PM_LLM_URL=http://127.0.0.1:18081/v1
PM_LLM_MODEL=qwen3-4b
PM_LLM_SELECT_STEP=true
PM_QWEN_QUALITATIVE=true
```

## Начальный профиль Qwen

Утверждённый узел модели — 1Gb.ru, Ubuntu 24.04 x86‑64, Intel Sandy Bridge, 4 vCPU, 7,8 ГиБ RAM и 1,4 ГиБ swap. Backend и SQLite находятся в Яндекс Облаке. На 1Gb.ru проверены AVX и SSE4.2. AVX2, FMA, F16C и AVX‑512 отсутствуют.

Использовать Qwen3‑4B Q4_K_M с SHA‑256 `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5`. Лаунчер проверяет хэш и задаёт CPU, один слот, 8192 токена, F16 KV по умолчанию, кеш префиксов 512 МиБ и non-thinking. Контрольный профиль использует 4 потока генерации и 8 потоков батча. В проверочном окне Максим включил профиль 4/4 через [временный drop-in](runtime/90-qwen-acceptance-batch4.conf). Он нужен для воспроизводимого сравнения. Постоянная конфигурация должна проходить через лаунчер с проверкой хэша.

RSS модели достиг **4,12 ГиБ** в диагностическом прогоне. Swap службы, OOM и throttling не наблюдались. Оставить 8192 токена, F16 KV и кеш 512 МиБ. На сервере требуется запас сверх наблюдаемого RSS для ОС и кеша. Существующих 7,8 ГиБ хватило под проверенную нагрузку. Для проверки туннеля доступен текущий 4/4. Сравнение при штатных лимитах не установило выигрыш 4 потоков батча, поэтому для постоянного запуска базовой остаётся конфигурация лаунчера 4/8. Пределы для следующего замера и условия приёмки приведены в [отчёте Linux](QWEN_LINUX_ACCEPTANCE.md).

### Поставка b12800 для Максима

Релиз `b12800`, commit `47d6192fce301f627c4d3e1c6910588c364dce0e`. Официальный файл `llama-b12800-bin-ubuntu-x64.tar.gz`, SHA‑256 `5cdb713ff7ba383095308139e7a27ac2f68781e2999253c9ff343543d8fa4426`. Архив скачан и проверен локально. ELF-метаданные `libggml-cpu-sandybridge.so` содержат зависимость `libgomp.so.1`. Установить штатный пакет Ubuntu `libgomp1`. Бинарник и библиотеки одной версии размещаются под уже разрешёнными путями AppArmor. [Релиз llama.cpp](https://github.com/ggml-org/llama.cpp/releases/tag/b12800) · [libgomp1 для Ubuntu 24.04](https://packages.ubuntu.com/noble/libgomp1)

На 1Gb.ru уже установлены `/usr/local/bin/llama-server` и библиотеки в `/usr/local/lib/paradiigma-llama-b12800`. Их SHA‑256 совпали с официальным архивом. Действующий drop-in `runtime.conf` указывает на этот каталог. Команды ниже описывают установку при восстановлении узла. Существующие файлы и конфигурацию следует сохранить в согласованной точке отката.

```sh
# Из корня ветки handoff/qwen-final. runtime_source — распакованный каталог llama-b12800.
sudo apt-get install libgomp1
sudo install -d -o root -g root -m 0755 /usr/local/lib/paradiigma-llama-b12800
for name in libllama-server-impl.so libllama-common.so.1 libmtmd.so.1 \
  libllama.so.1 libggml.so.1 libggml-base.so.1 libggml-cpu-sandybridge.so; do
  sudo install -o root -g root -m 0644 "$runtime_source/$name" "/usr/local/lib/paradiigma-llama-b12800/$name"
done
sudo install -o root -g root -m 0755 "$runtime_source/llama-server" /usr/local/bin/llama-server
sudo install -d -o root -g root -m 0755 /etc/systemd/system/paradiigma-qwen.service.d
sudo install -o root -g root -m 0644 handoff/qwen-final/runtime/20-llama-b12800.conf \
  /etc/systemd/system/paradiigma-qwen.service.d/20-llama-b12800.conf
```

[Drop-in](runtime/20-llama-b12800.conf) задаёт каталог библиотек и явно указывает файл Sandy Bridge backend. Проверить, что в `/usr/local/bin` и рабочем каталоге службы нет старых `libggml-cpu*.so`, которые могли бы подключиться обычным поиском ggml. Loader `GGML_BACKEND_PATH` загружает дополнительный backend и сам по себе не отменяет автоматический поиск. После применения drop-in требуется проверить `/proc/<llama-pid>/maps`: из CPU-модулей должен быть загружен только `libggml-cpu-sandybridge.so`. Если сборка не стартует под текущим AppArmor, зафиксировать конкретное сообщение OS и передать Максиму. [Загрузка backend в b12800](https://github.com/ggml-org/llama.cpp/blob/b12800/ggml/src/ggml-backend-reg.cpp)

```sh
/usr/bin/python3 -B scripts/run_model.py \
  --server /usr/local/bin/llama-server \
  --model /var/lib/paradiigma/models/Qwen3-4B-Q4_K_M.gguf \
  --parallel 1 --gpu-layers 0
```

Это команда для запуска внутри штатной модели Deployment или в отдельной изоляции. Рабочий сервис при этом запускается существующим `paradiigma-qwen.service` с `AppArmorProfile=paradiigma-launcher`. Если измерения потребуют иной конфигурации, отправить Максиму эффективные параметры, версию бинарника и результаты измерения для согласования.

## Фактически проверено локально

На Mac проверены синтаксис OpenSSH и эффективные ограничения сервисного аккаунта. В отдельном локальном тесте OpenSSH forward пропустил штатный `/health`, список моделей и синтетическую генерацию настоящей Qwen. После остановки форварда штатный probe показал недоступность. После запуска SSH-сервера и клиента probe снова вернул `ok`. Через восстановленный канал прошли история, многошаговое расследование и запросы двух параллельных клиентов. Это подтверждает совместимость протокола и healthcheck. Автоматический рестарт systemd между двумя VPS проверяет владелец Deployment по инструкции из приёмки.

Отдельно проверен перезапуск процесса `llama-server` при открытом SSH-туннеле. Слушатель и процесс SSH оставались активны. Healthcheck показал недоступность, затем вернулся к `ok`. Следующий запрос получил живой ответ от нового процесса Qwen.
