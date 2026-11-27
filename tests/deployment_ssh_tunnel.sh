#!/usr/bin/env bash
# Run only after installing the synthetic application on a disposable Ubuntu CI VM.
set -euo pipefail
[[ $EUID == 0 && ${1:-} == --disposable-vm ]] ||
    { echo 'Requires a disposable VM and --disposable-vm' >&2; exit 1; }
tester=pm-ssh-acceptance
if getent passwd "$tester" >/dev/null || getent group pm-tunnel >/dev/null; then
    echo 'Requires fresh test identities' >&2
    exit 1
fi
root=$(cd "$(dirname "$0")/.." && pwd -P)
tmp=$(mktemp -d)
sshd_pid=
cleanup() {
    if [[ -n $sshd_pid ]]; then
        kill "$sshd_pid" 2>/dev/null || true
        wait "$sshd_pid" 2>/dev/null || true
    fi
    userdel "$tester" 2>/dev/null || true
    groupdel pm-tunnel 2>/dev/null || true
    rm -rf -- "$tmp"
}
trap cleanup EXIT
port() {
    python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'
}
ssh_port=$(port)
forward_port=$(port)
groupadd --system pm-tunnel
useradd --system -g pm-tunnel --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$tester"
ssh-keygen -q -t ed25519 -N '' -f "$tmp/host"
ssh-keygen -q -t ed25519 -N '' -f "$tmp/client"
ssh-keygen -q -t ed25519 -N '' -f "$tmp/other"
install -d -m 0755 /run/sshd "$tmp/keys"
chmod 0711 "$tmp"
install -m 0644 "$tmp/client.pub" "$tmp/keys/$tester"
printf '[127.0.0.1]:%s %s\n' "$ssh_port" "$(cut -d' ' -f1,2 "$tmp/host.pub")" > "$tmp/known_hosts"
cat > "$tmp/sshd_config" <<CONFIG
Port $ssh_port
ListenAddress 127.0.0.1
PidFile $tmp/sshd.pid
HostKey $tmp/host
UsePAM yes
PermitRootLogin no
AllowUsers $tester
CONFIG
sed "s@/etc/ssh/pm-authorized-keys/@$tmp/keys/@" "$root/deploy/ssh/tunnel-users.conf" >> "$tmp/sshd_config"
/usr/sbin/sshd -t -f "$tmp/sshd_config"
/usr/sbin/sshd -D -e -f "$tmp/sshd_config" >"$tmp/sshd.log" 2>&1 &
sshd_pid=$!
for _ in {1..50}; do
    if [[ -s $tmp/sshd.pid ]]; then
        break
    fi
    sleep 0.1
done
if [[ ! -s $tmp/sshd.pid ]]; then
    cat "$tmp/sshd.log" >&2
    exit 1
fi
client=(ssh -F /dev/null -p "$ssh_port" -o BatchMode=yes -o ConnectTimeout=5 -o IdentitiesOnly=yes
        -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$tmp/known_hosts")
target="$tester@127.0.0.1"
"${client[@]}" -i "$tmp/client" -o ExitOnForwardFailure=yes -S "$tmp/control" -MfNT \
  -L "127.0.0.1:$forward_port:127.0.0.1:8080" "$target"
curl --noproxy '*' -fsS --max-time 5 -H 'Content-Type: application/json' --data '{}' \
  "http://127.0.0.1:$forward_port/api/analysis/settings" >/dev/null
"${client[@]}" -S "$tmp/control" -O exit "$target"
if "${client[@]}" -i "$tmp/client" "$target" true; then
    echo 'Restricted identity unexpectedly opened a shell session' >&2
    exit 1
fi
if "${client[@]}" -i "$tmp/client" -W 127.0.0.1:18081 "$target" </dev/null > /dev/null 2>"$tmp/denied"; then
    echo 'Model port forwarding unexpectedly allowed' >&2
    exit 1
fi
[[ $(<"$tmp/denied") == *"administratively prohibited"* ]]
if "${client[@]}" -i "$tmp/other" -W 127.0.0.1:8080 "$target" </dev/null >/dev/null 2>"$tmp/denied"; then
    echo 'Unregistered key unexpectedly accepted' >&2
    exit 1
fi
[[ $(<"$tmp/denied") == *"Permission denied"* ]]
echo 'Personal SSH key and restricted forwarding verified'
