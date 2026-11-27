# PARADИИGMA · передача синтетического состояния жюри

Переносимый снимок подготовлен из сохранённой репетиции `dress`. В рабочем каталоге прежней репетиции находился оригинал. Штатный `verify-replay` запущен на отдельной копии в `ParadiigmaMGR-public-demo/runs` с одноразовым локальным владельцем. Отчёт, диалог и согласованная задача открылись при пустом `PM_LLM_URL`. Содержимое сверено со сводкой опубликованного прогона. [Лаунчер и запуск](https://github.com/GITKITLIT/SoftlineParadiigmaMGR/blob/cd00670bef2d4bdba4f07a4e0a88afacdaa553e7/demo/README.md) · [Сводка](https://github.com/GITKITLIT/SoftlineParadiigmaMGR/blob/cd00670bef2d4bdba4f07a4e0a88afacdaa553e7/demo/rehearsal-summary.json) · [Сценарий показа](https://github.com/GITKITLIT/SoftlineParadiigmaMGR/blob/4f77e0039a93ce0871cae5ab489564a59b653b3a/DEMO_VIDEO_SCRIPT.md)

## Что получает оператор

По закрытому каналу передаётся `PARADIIGMA-jury-demo-state-v6.zip`. В нём один штатный каталог `pim-20261127T174832Z-20355ac54b6e` с файлами `ai.sqlite3` и `manifest.json`. Манифест — формата 1, база — SQLite v6, размер базы 344 064 байта. SHA-256 ниже рассчитан до назначения персональных владельцев.

| Файл | SHA-256 |
| --- | --- |
| Архив ZIP | `99d71be342ed6ba87410684b5a437eea5b5a2f380e07540336721b5e7afd2b5b` |
| `ai.sqlite3` | `b71abc6ac43dfa0cf7deac03cd810f38c41c687a720c895a9ee7122f86e2c810` |

- Два сохранённых отчёта. Фоновое расследование содержит одну гипотезу, восемь доказательств и трассу действий. В PIM по обоим отчётам 14 связей с доказательствами и две записи гипотез.
- Один синтетический диалог из трёх обменов. Сохранённое уточнение ссылается на предыдущий ответ. В переносимой базе он принадлежит недоступному через API шаблону `jury-demo-template-v1`.
- Одна согласованная задача с тегом ИИ-помощника и планом оценки. Цель 110, фактический статус — ожидание данных. Для симуляции используется штатная публичная фикстура `data/effect-demo.json` вне снимка.
- 51 наблюдение PIM, шесть рядов, три источника в режиме `synthetic`. Для ряда «Демо · история заказов» сравнение 2024-05-08 и 2025-05-08 возвращает 20 → 25, разницу +5 и +25%.
- Снимок содержит только SQLite и её манифест. Почтовая очередь и счётчик доставок очищены. Рабочие документы, интервью, эталоны, адресаты, ключи TLS, браузерные данные и исходная привязка владельца исключены. Архив и база хранятся вне Git.

## Восстановление на отдельном демо-экземпляре

1. Выберите отдельную демо-ВМ или изолированный экземпляр со своим сервисом, HTTPS-хостом и каталогом состояния. Используйте принятую сборку со схемой 6. Существующие 49 наблюдений Yandex Cloud остаются в их экземпляре. Шаги ниже работают с новым каталогом и не импортируют строки в имеющуюся PIM. Штатный `restore.sh` заменяет активное состояние и для этого переноса не применяется. [Штатный CLI](https://github.com/GITKITLIT/SoftlineParadiigmaMGR/blob/5e956ca08f67cd0d4501596d85c14493239b8ffa/docs/backup_and_recovery.md)
2. Задайте `JURY_ARCHIVE` как абсолютный путь к полученному архиву, `DEMO_ROOT` как отдельный закрытый каталог нового экземпляра вне Git, `RELEASE` как корень сборки SQLite v6. Каталог `DEMO_ROOT` подготовьте заранее с доступом только для оператора. Выполните команды в Bash.

```bash
set -euo pipefail
umask 077
snapshot='pim-20261127T174832Z-20355ac54b6e'
zip_sha='99d71be342ed6ba87410684b5a437eea5b5a2f380e07540336721b5e7afd2b5b'
printf '%s  %s\n' "$zip_sha" "$JURY_ARCHIVE" | sha256sum -c -
JURY_STAGE="$(mktemp -d "$DEMO_ROOT/jury-XXXXXXXX")"
export JURY_STAGE
python3 -m zipfile -e "$JURY_ARCHIVE" "$JURY_STAGE/incoming"
SNAPSHOT="$JURY_STAGE/incoming/$snapshot"
python3 "$RELEASE/scripts/pim_backup.py" verify --snapshot "$SNAPSHOT"
python3 "$RELEASE/scripts/pim_backup.py" restore-test --snapshot "$SNAPSHOT" --output "$JURY_STAGE/state"
db_sha='b71abc6ac43dfa0cf7deac03cd810f38c41c687a720c895a9ee7122f86e2c810'
printf '%s  %s\n' "$db_sha" "$JURY_STAGE/state/ai.sqlite3" | sha256sum -c -
```

3. До запуска Backend назначьте шаблону персональных владельцев по шагу ниже. Для демо-сервиса используйте `PM_PRIVATE_DIR=$JURY_STAGE/state` с фактическим абсолютным путём и приведите `ReadWritePaths` отдельной службы в соответствие. Дайте пользователю Backend право прохода по родительским каталогам и записи в `state`, где он создаёт журнал SQLite. При необходимости используйте ACL с правом прохода на родителях. Оставьте `incoming`, roster и резервную копию с доступом только оператору. Рабочий том и рабочие секреты к демо-экземпляру не подключайте.
4. Для просмотра сохранённого состояния оставьте `PM_LLM_URL` и `PM_MONITOR_INTERVAL` пустыми, `PM_COLLECTION_INTERVAL=0`, `PM_EMAIL_ENABLED=false`, `YANDEX_SEARCH_ENABLED=false` и оставьте Метрику без рабочих credentials. Фоновый сигнал и отчёт уже сохранены. Для живого исторического вопроса из конкурсного сценария локальную модель подключают отдельно. [Порядок эксплуатации](https://github.com/GITKITLIT/SoftlineParadiigmaMGR/blob/5e956ca08f67cd0d4501596d85c14493239b8ffa/docs/final_release_operator.md)

## Персональные диалоги через доверенный прокси

В режиме `local` владелец вычисляется из случайного браузерного cookie. Для HTTPS Backend вычисляет ключ владельца из подтверждённого прокси имени. Шаблон имеет значение, которое оба режима выдачи владельца сформировать не могут. Для каждого аккаунта создаётся отдельный диалог с новым ID и собственной копией трёх обменов. Разговоры, которые жюри продолжит, будут доступны только своим владельцам. Общие синтетические отчёты, задачи и PIM остаются общими для допущенных к демо. [Режим HTTPS-чата](https://github.com/GITKITLIT/SoftlineParadiigmaMGR/blob/5e956ca08f67cd0d4501596d85c14493239b8ffa/docs/chat_integration.md)

В закрытом roster с правами `0600` запишите по одному точному имени аккаунта Nginx на строку и передайте путь в переменной `JURY_ROSTER`. Используйте имена с тем же регистром, что у `$remote_user`. Пароли и ключи в roster не нужны. На остановленном отдельном демо выполните код ниже сразу после `restore-test` и до первого запуска приложения. Для другого состава участников восстановите новую отдельную копию. Повтор на уже изменённой базе завершится без записи.

```bash
python3 - <<'PY'
import hashlib
import os
import re
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from uuid import uuid4

template_owner = "jury-demo-template-v1"
expected = "b71abc6ac43dfa0cf7deac03cd810f38c41c687a720c895a9ee7122f86e2c810"
stage = Path(os.environ["JURY_STAGE"]).resolve(strict=True)
database = stage / "state" / "ai.sqlite3"
roster = Path(os.environ["JURY_ROSTER"])
if (database.resolve(strict=True) != database or not database.is_file()
        or not roster.is_file() or roster.is_symlink()
        or any(database.with_name(database.name + suffix).exists()
               for suffix in ("-wal", "-shm", "-journal"))):
    raise SystemExit("Expected a fresh offline demo restore and a protected roster")
if os.name == "posix" and stat.S_IMODE(roster.stat().st_mode) & 0o077:
    raise SystemExit("Use a private roster file, mode 0600")
with database.open("rb") as source:
    if hashlib.file_digest(source, "sha256").hexdigest() != expected:
        raise SystemExit("Database does not match the untouched synthetic snapshot")

names = roster.read_text(encoding="ascii").splitlines()
pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,95}")
if not names or len(set(names)) != len(names) or any(not pattern.fullmatch(name) for name in names):
    raise SystemExit("Provide unique, exact authenticated usernames, one per line")

with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)) as db, db:
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("BEGIN IMMEDIATE")
    if db.execute("PRAGMA user_version").fetchone() != (6,):
        raise SystemExit("Expected SQLite schema v6")
    rows = db.execute("SELECT id,updated,version FROM qwen_dialogs WHERE owner=?", (template_owner,)).fetchall()
    if len(rows) != 1 or db.execute("SELECT count(*) FROM qwen_dialogs").fetchone() != (1,):
        raise SystemExit("Expected exactly one synthetic template and no personal dialogs")
    template, updated, version = rows[0]
    turns = db.execute("SELECT position,body FROM qwen_turns WHERE dialog=? ORDER BY position", (template,)).fetchall()
    if version != 3 or [position for position, _ in turns] != [1, 2, 3]:
        raise SystemExit("Synthetic conversation is incomplete")

    for name in names:
        owner = hashlib.sha256(f"proxy:{name}".encode("ascii")).hexdigest()
        dialog = uuid4().hex
        db.execute("INSERT INTO qwen_dialogs (id,owner,updated,version) VALUES (?,?,?,?)",
                   (dialog, owner, updated, version))
        db.executemany("INSERT INTO qwen_turns (dialog,position,body) VALUES (?,?,?)",
                       [(dialog, position, body) for position, body in turns])
    db.execute("DELETE FROM qwen_dialogs WHERE id=? AND owner=?", (template, template_owner))
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise SystemExit("Foreign-key check failed")
print(f"Personal synthetic dialogs created: {len(names)}")
PY
```

## Проверка перед выдачей доступа

- На отдельном демо настройте `PM_QWEN_OWNER_MODE=proxy` и `PM_QWEN_CHAT_ENABLED=true`. Соберите Nginx с `--mode demo --qwen-chat`. Nginx передаёт только имя из проверенной HTTP-аутентификации и собственный служебный ключ Backend. Храните ключ отдельно от снимка и списка аккаунтов. Backend слушает loopback. Проверьте конфигурацию через `nginx -t`. [Изоляция демо](https://github.com/GITKITLIT/SoftlineParadiigmaMGR/blob/5e956ca08f67cd0d4501596d85c14493239b8ffa/docs/infrastructure_security.md) · [Доверенный прокси](https://github.com/GITKITLIT/SoftlineParadiigmaMGR/blob/5e956ca08f67cd0d4501596d85c14493239b8ffa/docs/chat_integration.md)
- Откройте два персональных сеанса. В каждом найдите один исходный синтетический диалог и три сохранённых обмена. Убедитесь, что идентификаторы различаются и чужой диалог также нельзя прочитать по известному ID. Аккаунт без подготовленной копии не получает чужой истории. Запрос без подтверждения прокси должен получить 403.
- Откройте «Журнал и задачи», фоновый отчёт с гипотезой, согласованную задачу и ожидание полных недель. В «Базе знаний» сравните два окна в режиме «Синтетические». Если проверка не прошла, оставьте демо закрытым и восстановите исходный снимок в новый каталог. Состояние Yandex Cloud с 49 наблюдениями в этот процесс не входит.

Локально штатные `verify` и `restore-test` прошли. `integrity_check` вернул `ok`, `foreign_key_check` — пустой результат, SHA-256 восстановления совпал. На отдельной восстановленной базе Backend в режиме `proxy` подтвердил отдельные истории двух тестовых аккаунтов, отказ чтения чужого диалога, отказ без доверенного ключа и отказ повторного назначения на изменённую базу. Также открылись отчёты, задача и план, PIM вернула 20 → 25. Изолированная контрольная база из 49 записей сохранила SHA-256. Фактический HTTPS-хост и облачное состояние в проверку не входили.
