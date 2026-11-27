#!/usr/bin/env bash
# Run after deployment_clean_install.sh on the same disposable systemd VM.
set -euo pipefail
[[ $EUID == 0 && ${1:-} == --disposable-vm && -f /run/paradiigma-disposable-acceptance ]] ||
    { echo 'Requires the preceding clean-install test on a disposable VM' >&2; exit 1; }
source_tree=$(cd "$(dirname "$0")/.." && pwd -P)
sandbox=$(mktemp -d)
trap 'rm -rf -- "$sandbox"' EXIT
core=b71424d71468ccbdd9a2ceb7b44799fc07abe334
search=93de5460148982747093bfe50e34dbc57abbb88c
old=$(readlink -e /opt/paradiigma/current)
db=/var/lib/paradiigma/db/ai.sqlite3
status=/var/lib/paradiigma-backup-status/status.json
vault=/var/backups/paradiigma
mkdir "$sandbox/candidate"
cd "$sandbox/candidate"
git -c safe.directory="$source_tree" -C "$source_tree" archive "$core" | tar -x
git -c safe.directory="$source_tree" -C "$source_tree" archive "$search" \
    pm/competitor_sources.py pm/search_security.py pm/web_search.py pm/yandex_search \
    scripts/search_import_competitors.py scripts/search_smoke.py tests/fixtures/yandex | tar -x
# These are the exact deployment inputs under test. AI Core's application code stays at b71424d.
mv deploy "$sandbox/core-units"
cp -R "$source_tree/deploy" deploy
cp -R "$source_tree/scripts/deploy" scripts/
cp "$source_tree/scripts/pim_backup.py" scripts/
# Python 3.12 emits uppercase TO:, while 3.14 uses lowercase to:.
# Normalize only the synthetic receiver in the disposable test tree.
python3 - <<'PY'
from pathlib import Path
fixture = Path("tests/test_email_notifications.py")
source = fixture.read_text(encoding="utf-8")
prefix = 'arg.decode("ascii").removeprefix("to:")'
if source.count(prefix) != 1:
    raise SystemExit("Review the SMTP test receiver before running this rehearsal")
fixture.write_text(source.replace(prefix, 'arg.decode("ascii").split(":", 1)[1]'), encoding="utf-8")
PY
git init -q
git add .
git -c user.email=synthetic@example.invalid -c user.name='Disposable Core v6 check' commit -qm 'Synthetic tree of pinned components'
sha=$(git rev-parse HEAD)
release=/opt/paradiigma/releases/$sha

if bash scripts/deploy/update.sh "$sha"; then
    echo 'Automatic schema change unexpectedly accepted' >&2
    exit 1
fi
test "$(readlink -e /opt/paradiigma/current)" = "$old"
systemctl is-active --quiet paradiigma.service
test "$(cat "$release/.schema")" = 6

systemctl stop paradiigma-backup.timer paradiigma.service
runuser -u pm-app -- /usr/bin/python3 - "$db" <<'PY'
from contextlib import closing
import sqlite3
import sys
with closing(sqlite3.connect(sys.argv[1])) as db:
    assert db.execute("PRAGMA user_version").fetchone()[0] == 5
    db.execute("INSERT INTO deployment_probe VALUES (3)")
    db.commit()
    assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
PY
# The previous snapshot is stale after the write above. The guard must reject it.
preflight=(runuser -u pm-app -- env -i PATH=/usr/bin:/bin PARADIIGMA_MODE=demo
           PM_PRIVATE_DIR=/var/lib/paradiigma/db PM_MIGRATION_STATUS_FILE="$status"
           "$release/.venv/bin/python" "$release/scripts/deploy/preflight.py")
if "${preflight[@]}"; then
    echo 'Stale v5 snapshot unexpectedly authorized migration' >&2
    exit 1
fi
systemctl start paradiigma-backup.service
v5_snapshot=$(python3 - "$status" "$db" <<'PY'
import json
import sys
from pathlib import Path
state = json.loads(Path(sys.argv[1]).read_text())
source = Path(sys.argv[2]).stat()
assert state["source_schema"] == 5 and state["source_quiescent"] is True
assert (state["source_mtime_ns"], state["source_size"]) == (source.st_mtime_ns, source.st_size)
assert state["last_integrity_check"] and state["last_restore_check"]
print(state["snapshot_id"])
PY
)
# Verify and keep the exact v5 recovery point before invoking Core's migration.
python3 -IB "$release/scripts/pim_backup.py" verify --snapshot "$vault/$v5_snapshot"
"${preflight[@]}"
bash scripts/deploy/update.sh "$sha"
systemctl is-active --quiet paradiigma.service
test "$(readlink -e /opt/paradiigma/current)" = "$release"
runuser -u pm-app -- /usr/bin/python3 - "$db" <<'PY'
import sqlite3
import sys
with sqlite3.connect(sys.argv[1]) as db:
    assert db.execute("PRAGMA user_version").fetchone()[0] == 6
    assert db.execute("SELECT count(*) FROM email_outbox").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM imported_evidence WHERE body LIKE '%Synthetic persisted marker.%'").fetchone()[0] == 1
PY

# Exercise the upstream SMTP STARTTLS/TLS, queue, replay and confidentiality tests.
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 "$release/.venv/bin/python" -m unittest discover -s tests -v
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 "$release/.venv/bin/python" scripts/search_smoke.py --summary
"$release/.venv/bin/python" "$source_tree/tests/deployment_smtp_systemd.py" "$release" "$sandbox/candidate/tests"

# Verify that the outbox itself is included in the retained and restore-tested v6 state.
systemctl start paradiigma-backup.service
v6_snapshot=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["source_schema"]==6; print(d["snapshot_id"])' "$status")
bash scripts/deploy/restore.sh "$v6_snapshot"
runuser -u pm-app -- /usr/bin/python3 - "$db" <<'PY'
import sqlite3
import sys
with sqlite3.connect(sys.argv[1]) as db:
    assert db.execute("PRAGMA user_version").fetchone()[0] == 6
    assert db.execute("SELECT status,attempts FROM email_outbox WHERE id LIKE 'test-%'").fetchall() == [("submitted",1)]
PY
if bash scripts/deploy/update.sh "$(basename "$old")"; then
    echo 'Automatic schema downgrade unexpectedly accepted' >&2
    exit 1
fi
test "$(readlink -e /opt/paradiigma/current)" = "$release"

# After a closed-window rollback select the compatible code before restarting a service.
systemctl stop paradiigma-backup.timer paradiigma.service
ln -sfnT "$old" /opt/paradiigma/.next
mv -fT /opt/paradiigma/.next /opt/paradiigma/current
for unit in paradiigma.service paradiigma-qwen.service; do
    install -m 0644 "$old/deploy/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
bash "$old/scripts/deploy/restore.sh" "$v5_snapshot"
runuser -u pm-app -- /usr/bin/python3 - "$db" <<'PY'
import sqlite3
import sys
with sqlite3.connect(sys.argv[1]) as db:
    assert db.execute("PRAGMA user_version").fetchone()[0] == 5
    assert db.execute("SELECT count(*) FROM deployment_probe WHERE id=3").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM imported_evidence WHERE body LIKE '%Synthetic persisted marker.%'").fetchone()[0] == 1
PY
systemctl is-active --quiet paradiigma.service
echo 'v5 schema guard, v6 migration, SMTP outbox backup, and v5 restore verified'
