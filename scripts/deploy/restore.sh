#!/usr/bin/env bash
# Run as administrator in a closed maintenance window without other DB writers.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077

[[ $EUID == 0 && $# == 1 && $1 =~ ^pim-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}$ ]] ||
    { echo 'Usage as administrator: restore.sh SNAPSHOT_BASENAME' >&2; exit 1; }
exec 9>/run/lock/paradiigma-release.lock
flock -n 9 || { echo 'Another release or restore operation is running' >&2; exit 1; }

state=/var/lib/paradiigma/db
vault=/var/backups/paradiigma
snapshot=$vault/$1
engine=/opt/paradiigma/current/scripts/pim_backup.py
timer=paradiigma-backup.timer
mount_unit=run-paradiigma_backup_input.mount
[[ -d $state && ! -L $state && -d $snapshot && ! -L $snapshot && -f $engine ]] ||
    { echo 'Check the installed application and snapshot location' >&2; exit 1; }
if mountpoint -q "$state" || [[ $(stat -c %d "$state") != "$(stat -c %d "$(dirname "$state")")" ]]; then
    echo 'The state is on a dedicated mount. Use the separate volume procedure.' >&2
    exit 1
fi

stage=$(mktemp -d /var/lib/paradiigma/recovery.XXXXXXXX)
timer_active=0
app_stopped=0
moved=0
switched=0
ready=0
cleanup() {
    local result=$?
    trap - EXIT INT TERM HUP
    if (( result != 0 && app_stopped && ! ready )); then
        systemctl stop paradiigma.service "$mount_unit" || true
        if (( switched )); then
            mv -T "$state" "$stage/failed-db" || {
                echo 'Cannot move failed database. Keep access closed and use the DR runbook.' >&2
                exit 1
            }
        fi
        if (( moved )); then
            mv -T "$stage/previous-db" "$state" || {
                echo 'Cannot return previous database. Keep access closed and use the DR runbook.' >&2
                exit 1
            }
        fi
        systemctl reset-failed paradiigma.service || true
        systemctl start paradiigma.service || true
    fi
    if (( timer_active )); then
        systemctl start "$timer" || result=1
    fi
    printf 'Recovery workspace %s\n' "$stage"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

if systemctl is-active --quiet "$timer"; then
    timer_active=1
    systemctl stop "$timer"
fi
systemctl stop paradiigma-backup.service paradiigma-backup-reader.service
chown pm-backup:pm-backup "$stage"
runuser -u pm-backup -- python3 -IB "$engine" restore-test --snapshot "$snapshot" --output "$stage/verified"
chown root:root "$stage"

python3 - "$stage/verified/ai.sqlite3" /opt/paradiigma/current/.schema <<'PY'
import sqlite3
import sys
from pathlib import Path

uri = Path(sys.argv[1]).as_uri() + "?mode=ro"
with sqlite3.connect(uri, uri=True) as db:
    if str(db.execute("PRAGMA user_version").fetchone()[0]) != Path(sys.argv[2]).read_text().strip():
        raise SystemExit("Select a release compatible with the snapshot schema before restoring")
PY
install -d -o pm-app -g pm-db -m 2750 "$stage/new-db"
install -o pm-app -g pm-db -m 0640 "$stage/verified/ai.sqlite3" "$stage/new-db/ai.sqlite3"
if [[ -f $stage/verified/sources.json ]]; then
    install -o pm-app -g pm-db -m 0640 "$stage/verified/sources.json" "$stage/new-db/sources.json"
fi

app_stopped=1
systemctl stop paradiigma.service
systemctl stop "$mount_unit"
mv -T "$state" "$stage/previous-db"
moved=1
mv -T "$stage/new-db" "$state"
switched=1
systemctl reset-failed paradiigma.service || true
systemctl start paradiigma.service
for attempt in {1..20}; do
    if systemctl is-active --quiet paradiigma.service &&
       curl --noproxy '*' -fsS --max-time 2 -H 'Content-Type: application/json' --data '{}' \
         http://127.0.0.1:8080/api/analysis/settings 2>/dev/null |
       python3 -c 'import json,sys; assert type(json.load(sys.stdin)["model_configured"]) is bool' 2>/dev/null; then
        ready=1
        break
    fi
    sleep 1
done
(( ready )) || { echo 'Restored database failed the Backend health check' >&2; exit 1; }
systemctl start paradiigma-backup.service
