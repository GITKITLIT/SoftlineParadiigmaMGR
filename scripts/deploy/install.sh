#!/usr/bin/env bash
# Provision an administrator-approved, immutable Git tree on a dedicated Ubuntu VM.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 022
fail() { echo "$*" >&2; exit 1; }
[[ $EUID == 0 ]] || fail 'Administrator required'
[[ $# == 1 && $1 =~ ^[a-f0-9]{40}$ ]] || fail 'Usage: install.sh FULL_APPROVED_COMMIT_SHA'
source /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 && -d /run/systemd/system ]] || fail 'Ubuntu 24.04 with systemd required'
src=$(cd "$(dirname "$0")/../.." && pwd -P)
sha=$1
[[ $(git -C "$src" rev-parse --verify "$sha^{commit}") == "$sha" ]] || fail 'Commit unavailable'
base=/opt/paradiigma
release=$base/releases/$sha
exec 9>/run/lock/paradiigma-release.lock
flock -n 9 || fail 'Another release operation is running'
install -d -m 0755 -o root -g root "$base" "$base/releases"
old=$(readlink -e "$base/current" || true)
if [[ -z $old ]]; then
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-venv ca-certificates curl apparmor
fi
getent group pm-db >/dev/null || groupadd --system pm-db
for user in pm-app pm-qwen; do
    getent group "$user" >/dev/null || groupadd --system "$user"
    id "$user" >/dev/null 2>&1 || useradd --system --gid "$user" --shell /usr/sbin/nologin --home-dir /nonexistent --no-create-home "$user"
    [[ $(id -u "$user") != 0 && $(id -gn "$user") == "$user" && $(getent passwd "$user" | cut -d: -f7) == /usr/sbin/nologin ]] || fail 'Unexpected service account'
done
install -d -o root -g root -m 0755 /var/lib/paradiigma
install -d -o pm-app -g pm-db -m 2750 /var/lib/paradiigma/db
install -d -o root -g pm-app -m 0750 /var/lib/paradiigma/documents
install -d -o root -g pm-qwen -m 0750 /var/lib/paradiigma/models
install -d -o root -g root -m 0700 /etc/paradiigma
if [[ ! -f $release/.prepared ]]; then
    if [[ -e $release ]]; then
        [[ $old != "$release" ]] || fail 'Current release lacks preparation marker'
        mv -T "$release" "$release.incomplete.$(date +%s%N)"
    fi
    mkdir -m 0755 "$release"
    git -C "$src" archive "$sha" | tar -x --no-same-owner -C "$release"
    /usr/bin/python3 -m venv "$release/.venv"
    "$release/.venv/bin/python" -m pip --isolated install --disable-pip-version-check --require-hashes --only-binary=:all: -r "$release/deploy/requirements.lock"
    "$release/.venv/bin/python" -m pip check
    runuser -u pm-app -- env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
        "$release/.venv/bin/python" "$release/scripts/deploy/check_candidate.py" > "$release/.schema"
    touch "$release/.prepared"
fi
if [[ ! -e /etc/paradiigma/app.env ]]; then
    install -o root -g root -m 0600 "$release/deploy/app.env.example" /etc/paradiigma/app.env
fi
[[ $(stat -c '%u:%g:%a' /etc/paradiigma/app.env) == 0:0:600 && ! -L /etc/paradiigma/app.env ]] || fail 'Expected root:root 0600 app.env'
# Resolve unit files and the backup helper through the selected release.
select_release() {
    local target=$1
    ln -sfnT "$target" "$base/.next" || return 1
    mv -fT "$base/.next" "$base/current" || return 1
    for unit in paradiigma.service paradiigma-qwen.service; do
        install -m 0644 "$target/deploy/systemd/$unit" "/etc/systemd/system/$unit" || return 1
    done
    systemctl daemon-reload
}
check_backend() {
    for attempt in {1..20}; do
        if systemctl is-active --quiet paradiigma.service &&
           curl --noproxy '*' -fsS --max-time 2 -H 'Content-Type: application/json' --data '{}' \
             http://127.0.0.1:8080/api/analysis/settings 2>/dev/null |
           /usr/bin/python3 -c 'import json,sys; assert type(json.load(sys.stdin)["model_configured"]) is bool' 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}
if [[ $old == "$release" ]]; then
    systemctl enable --now paradiigma.service
    check_backend || fail 'Existing release health check failed'
    bash "$release/scripts/deploy/install-backup.sh"
    exit 0
fi
recover() {
    local status=$?
    trap - EXIT
    if (( status != 0 )); then
        systemctl stop paradiigma.service || true
        if [[ -n $old ]]; then
            if select_release "$old"; then
                systemctl reset-failed paradiigma.service || true
                systemctl start paradiigma.service && check_backend ||
                    echo 'Backend recovery failed. Keep ingress closed and follow disaster_recovery.md.' >&2
                bash "$old/scripts/deploy/install-backup.sh" ||
                    echo 'Backup service configuration recovery failed' >&2
            else
                echo 'Release pointer recovery failed. Follow disaster_recovery.md.' >&2
            fi
        elif [[ $(readlink -e "$base/current" || true) == "$release" ]]; then
            systemctl disable --now paradiigma.service paradiigma-backup.timer 2>/dev/null || true
            unlink "$base/current"
        fi
    fi
    exit "$status"
}
trap recover EXIT
trap 'exit 130' INT TERM HUP
if [[ -n $old ]]; then
    systemctl stop paradiigma-backup.timer
    systemctl stop paradiigma.service
    systemctl start paradiigma-backup.service
    # The automatic path only handles schema-compatible releases. Migrations require an approved DR window.
    installed_schema=$(runuser -u pm-app -- /usr/bin/python3 -c 'import sqlite3; print(sqlite3.connect("file:/var/lib/paradiigma/db/ai.sqlite3?mode=ro",uri=True).execute("PRAGMA user_version").fetchone()[0])')
    candidate_schema=$(cat "$release/.schema")
    [[ $installed_schema == "$candidate_schema" ]] || fail 'Schema change requires an administrator-led migration'
fi
select_release "$release"
systemctl reset-failed paradiigma.service || true
systemctl enable --now paradiigma.service
check_backend || fail 'New release health check failed'
bash "$release/scripts/deploy/install-backup.sh"
if [[ -n $old ]]; then
    ln -sfnT "$old" "$base/previous"
fi
