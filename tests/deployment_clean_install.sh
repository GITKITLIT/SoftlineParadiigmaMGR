#!/usr/bin/env bash
# Disposable Ubuntu 24.04 host with systemd, outbound PyPI/apt, no production state.
set -euo pipefail
[[ $EUID == 0 && ${1:-} == --disposable-vm && ! -e /opt/paradiigma && ! -e /var/lib/paradiigma && ! -e /etc/paradiigma && ! -e /var/backups/paradiigma ]] ||
    { echo 'Requires a clean disposable VM and --disposable-vm' >&2; exit 1; }
src=$(cd "$(dirname "$0")/.." && pwd -P)
sandbox=$(mktemp -d)
trap 'rm -rf -- "$sandbox"' EXIT
# Clone into a root-owned repository. The input may be owned by the Actions checkout user.
git -c safe.directory="$src" -c safe.directory="$src/.git" clone --no-local "$src" "$sandbox/repo"
cd "$sandbox/repo"
sha=$(git rev-parse --verify "${2:-HEAD}^{commit}")
git checkout --detach "$sha"
git config user.email synthetic@example.invalid
git config user.name 'Disposable acceptance'
bash scripts/deploy/install.sh "$sha"
bash scripts/deploy/install.sh "$sha"
systemctl is-enabled --quiet paradiigma.service
systemctl is-active --quiet paradiigma.service
systemctl is-enabled --quiet paradiigma-backup.timer
curl --noproxy '*' -fsS http://127.0.0.1:8080/cost.xlsx -o "$sandbox/cost.xlsx"
test -s "$sandbox/cost.xlsx"
[[ $(stat -c '%a:%U:%G' /etc/paradiigma/app.env) == 600:root:root ]]
[[ $(stat -c '%a:%U:%G' /var/lib/paradiigma/db) == 2750:pm-app:pm-db ]]
[[ $(systemctl show -p User --value paradiigma.service) == pm-app ]]
if runuser -u pm-qwen -- test -r /var/lib/paradiigma/db/ai.sqlite3; then exit 1; fi
if runuser -u pm-app -- test -w /opt/paradiigma/current/app.py; then exit 1; fi
apparmor_parser -QTK deploy/apparmor/paradiigma-qwen
bash tests/deployment_model_sandbox.sh --disposable-vm
curl --noproxy '*' -fsS --header 'Content-Type: application/json' --data '{"kind":"Интервью","name":"acceptance","text":"Synthetic persisted marker."}' http://127.0.0.1:8080/api/import >/dev/null
# A commit that prepares normally but fails during actual ExecStartPre (no working data touched).
echo 'raise SystemExit(77)' >> scripts/deploy/preflight.py
git commit -qam 'Synthetic activation failure'
broken=$(git rev-parse HEAD)
if bash scripts/deploy/update.sh "$broken"; then echo 'Broken release activated'; exit 1; fi
[[ $(basename "$(readlink -e /opt/paradiigma/current)") == "$sha" ]]
systemctl is-active --quiet paradiigma.service
# An ordinary update followed by explicit rollback.
git checkout "$sha" -- scripts/deploy/preflight.py
git commit -qam 'Restore valid preflight'
good=$(git rev-parse HEAD)
bash scripts/deploy/update.sh "$good"
[[ $(basename "$(readlink -e /opt/paradiigma/current)") == "$good" ]]
bash scripts/deploy/update.sh "$sha"
systemctl restart paradiigma.service
runuser -u pm-app -- /usr/bin/python3 - <<'PY'
import sqlite3
with sqlite3.connect("file:/var/lib/paradiigma/db/ai.sqlite3?mode=ro", uri=True) as db:
    assert db.execute("SELECT count(*) FROM imported_evidence WHERE body LIKE '%Synthetic persisted marker.%'").fetchone()[0] == 1
PY
# Use the real unit and read-only mount while the application identity owns an active WAL.
snapshot=$(/usr/bin/python3 - <<'PY'
from contextlib import closing
from pathlib import Path
import select
import sqlite3
import subprocess

vault = Path("/var/backups/paradiigma")
previous = set(vault.glob("pim-*"))
fixture = """
import sqlite3
import sys

with sqlite3.connect("/var/lib/paradiigma/db/ai.sqlite3") as db:
    assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    db.execute("CREATE TABLE deployment_probe (id INTEGER)")
    db.execute("INSERT INTO deployment_probe VALUES (1)")
    db.commit()
    db.execute("INSERT INTO deployment_probe VALUES (2)")
    print("ready", flush=True)
    sys.stdin.read(1)
    db.rollback()
"""
with subprocess.Popen(["runuser", "-u", "pm-app", "--", "/usr/bin/python3", "-c", fixture],
                      stdin=subprocess.PIPE, stdout=subprocess.PIPE) as writer:
    try:
        readable, _, _ = select.select([writer.stdout], [], [], 15)
        assert readable and writer.stdout.readline().strip() == b"ready"
        subprocess.run(["systemctl", "start", "paradiigma-backup.service"], check=True, timeout=30)
        snap, = set(vault.glob("pim-*")) - previous
        uri = (snap / "ai.sqlite3").as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as copy:
            assert copy.execute("SELECT id FROM deployment_probe").fetchall() == [(1,)]
        print(snap.name)
    finally:
        writer.stdin.close()
        writer.wait(timeout=10)
PY
)
test -n "$snapshot"
vault=/var/backups/paradiigma
test -f "$vault/$snapshot/ai.sqlite3"
if runuser -u pm-app -- unlink "$vault/$snapshot/ai.sqlite3"; then
    echo 'Application identity deleted its independent copy' >&2
    exit 1
fi
status=/var/lib/paradiigma-backup-status/status.json
runuser -u pm-app -- /usr/bin/python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["status"] == "ok"' "$status"
broken_snapshot=pim-20000101T000000Z-000000000001
cp -a "$vault/$snapshot" "$vault/$broken_snapshot"
printf synthetic-corruption >> "$vault/$broken_snapshot/ai.sqlite3"
if bash scripts/deploy/restore.sh "$broken_snapshot"; then
    echo 'Corrupt snapshot restored' >&2
    exit 1
fi
systemctl is-active --quiet paradiigma.service
curl --noproxy '*' -fsS --header 'Content-Type: application/json' --data \
    '{"kind":"Интервью","name":"after","text":"Synthetic post-snapshot content."}' \
    http://127.0.0.1:8080/api/import >/dev/null
bash scripts/deploy/restore.sh "$snapshot"
runuser -u pm-app -- /usr/bin/python3 - <<'PY'
import sqlite3
with sqlite3.connect("file:/var/lib/paradiigma/db/ai.sqlite3?mode=ro", uri=True) as db:
    assert db.execute("SELECT count(*) FROM imported_evidence WHERE body LIKE '%Synthetic persisted marker.%'").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM imported_evidence WHERE body LIKE '%Synthetic post-snapshot content.%'").fetchone()[0] == 0
PY
runuser -u pm-app -- /usr/bin/python3 /opt/paradiigma/current/scripts/deploy/health.py
install -m 0600 /dev/null /run/paradiigma-disposable-acceptance
echo 'Clean install, retry, failed update, rollback, backup isolation and actual restore OK'
