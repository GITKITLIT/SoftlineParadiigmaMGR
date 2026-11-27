#!/usr/bin/env bash
set -euo pipefail
(( EUID == 0 )) || { echo 'Run provisioning as administrator' >&2; exit 1; }
base=$(cd -- "$(dirname -- "$0")/../.." && pwd -P)
id pm-app >/dev/null
getent group pm-db >/dev/null
getent group pm-backup >/dev/null || groupadd --system pm-backup
id pm-backup >/dev/null 2>&1 || useradd --system --gid pm-backup --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin pm-backup
[[ $(id -u pm-backup) != 0 && $(id -gn pm-backup) == pm-backup ]]
install -d -o pm-backup -g pm-backup -m 0700 /var/backups/paradiigma
# The setgid parent gives atomically replaced 0640 status files the app group.
install -d -o pm-backup -g pm-app -m 2750 /var/lib/paradiigma-backup-status
for unit in run-paradiigma_backup_input.mount paradiigma-backup-reader.service paradiigma-backup.service paradiigma-backup.timer; do
    install -o root -g root -m 0644 "$base/deploy/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now paradiigma-backup.timer
