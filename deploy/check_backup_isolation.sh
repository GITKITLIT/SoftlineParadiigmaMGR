#!/bin/sh
# Check the installed Linux identities without changing files.
set -eu
[ "$(id -u)" -eq 0 ] || { echo "Run as the deployment administrator" >&2; exit 1; }
vault=/var/backups/paradiigma
database=/var/lib/paradiigma/db/ai.sqlite3
status_dir=/var/lib/paradiigma-backup-status

denied() {
    account=$1
    shift
    if runuser -u "$account" -- test "$@"; then
        echo "Unexpected access for $account to $*" >&2
        exit 1
    fi
}

runuser -u pm-app -- test -r "$database"
runuser -u pm-app -- test -r "$status_dir/status.json"
runuser -u pm-backup -- test -w "$vault"
for account in pm-app pm-qwen; do
    denied "$account" -r "$vault"
    denied "$account" -w "$vault"
    denied "$account" -x "$vault"
done
denied pm-app -w "$status_dir"
denied pm-qwen -r "$database"
denied pm-qwen -r "$status_dir/status.json"
denied pm-backup -r /etc/paradiigma/app.env
echo "Linux identity permissions verified; check the backup mount with the systemd acceptance test"
