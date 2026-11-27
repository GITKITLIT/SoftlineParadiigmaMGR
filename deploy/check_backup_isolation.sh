#!/bin/sh
# Run as a Linux administrator after provisioning users and directories.
set -eu
test "$(id -u)" = 0
for account in paradiigma paradiigma-model paradiigma-backup; do id "$account" >/dev/null; done
db=/var/lib/paradiigma/ai.sqlite3
vault=/var/lib/paradiigma-backup
status=/var/lib/paradiigma-backup-status/status.json
test -f "$db"
test -d "$vault"
test -f "$status"
denied() {
    account=$1
    shift
    if runuser -u "$account" -- test "$@"; then
        echo "Unexpected permission for $account: $*" >&2
        exit 1
    fi
}
runuser -u paradiigma-backup -- test -r "$db"
denied paradiigma-backup -w "$db"
denied paradiigma-backup -w /var/lib/paradiigma
for account in paradiigma paradiigma-model; do
    denied "$account" -r "$vault"
    denied "$account" -w "$vault"
    denied "$account" -x "$vault"
done
denied paradiigma-model -r "$db"
runuser -u paradiigma -- test -r "$status"
denied paradiigma -w "$status"
denied paradiigma -w /var/lib/paradiigma-backup-status
echo "Filesystem permission checks passed. Continue with systemd and restore smoke."
