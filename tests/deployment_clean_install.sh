#!/usr/bin/env bash
# Disposable Ubuntu 24.04 host with systemd, outbound PyPI/apt, no production state.
set -euo pipefail
[[ $EUID == 0 && ${1:-} == --disposable-vm && ! -e /opt/paradiigma && ! -e /var/lib/paradiigma && ! -e /etc/paradiigma && ! -e /var/backups/paradiigma ]] ||
    { echo 'Requires a clean disposable VM and --disposable-vm' >&2; exit 1; }
src=$(cd "$(dirname "$0")/.." && pwd -P)
sandbox=$(mktemp -d)
trap 'rm -rf -- "$sandbox"' EXIT
# Clone into a root-owned repository. The input may be owned by the Actions checkout user.
git -c safe.directory="$src" clone --no-local "$src" "$sandbox/repo"
cd "$sandbox/repo"
sha=$(git -c safe.directory="$src" -C "$src" rev-parse HEAD)
git checkout --detach "$sha"
git config user.email synthetic@example.invalid
git config user.name 'Disposable acceptance'
bash scripts/deploy/install.sh "$sha"
bash scripts/deploy/install.sh "$sha"
systemctl is-enabled --quiet paradiigma.service
systemctl is-active --quiet paradiigma.service
systemctl is-enabled --quiet paradiigma-backup.timer
[[ $(stat -c '%a:%U:%G' /etc/paradiigma/app.env) == 600:root:root ]]
[[ $(stat -c '%a:%U:%G' /var/lib/paradiigma/db) == 2750:pm-app:pm-db ]]
[[ $(systemctl show -p User --value paradiigma.service) == pm-app ]]
if runuser -u pm-qwen -- test -r /var/lib/paradiigma/db/ai.sqlite3; then exit 1; fi
if runuser -u pm-app -- test -w /opt/paradiigma/current/app.py; then exit 1; fi
apparmor_parser -QTK deploy/apparmor/paradiigma-qwen
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
# Keep the temporary runner alive for further independent acceptance checks.
echo 'Clean install, retry, failed activation recovery, update, rollback and persistent marker OK'
