#!/usr/bin/env bash
# Exercise the shipped CPU model sandbox with a tiny stand-in instead of model weights.
set -euo pipefail
[[ $EUID == 0 && ${1:-} == --disposable-vm && ! -e /usr/local/bin/llama-server &&
   ! -e /etc/apparmor.d/paradiigma-qwen && ! -e /run/systemd/system/paradiigma-qwen.service.d ]] ||
    { echo 'Requires disposable VM without a model installation' >&2; exit 1; }
stage=$(mktemp -d)
cleanup() {
    systemctl stop paradiigma-qwen.service || true
    rm -f /run/systemd/system/paradiigma-qwen.service.d/synthetic.conf /usr/local/bin/llama-server
    rmdir /run/systemd/system/paradiigma-qwen.service.d || true
    apparmor_parser -R /etc/apparmor.d/paradiigma-qwen || true
    rm -f /etc/apparmor.d/paradiigma-qwen
    rm -rf -- "$stage"
    systemctl daemon-reload
}
trap cleanup EXIT
command -v cc >/dev/null || apt-get install -y gcc libc6-dev
cat > "$stage/worker.c" <<'C'
#include <arpa/inet.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(void) {
    if (getuid() == 0 || getenv("METRIKA_TOKEN") || getenv("YANDEX_SEARCH_API_KEY"))
        return 1;
    if (!access("/etc/paradiigma/app.env", R_OK) || !access("/var/lib/paradiigma/db/ai.sqlite3", R_OK))
        return 2;
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in address = {.sin_family = AF_INET, .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
    if (fd < 0 || bind(fd, (struct sockaddr *)&address, sizeof(address)) || listen(fd, 1))
        return 3;
    address.sin_port = htons(8080);
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) != -1 || errno != EPERM)
        return 4;
    // If execution succeeds, /bin/false makes the unit fail.
    execl("/bin/false", "false", (char *)NULL);
    return errno == EACCES || errno == EPERM ? 0 : 5;
}
C
cc -Wall -Werror "$stage/worker.c" -o "$stage/worker"
install -o root -g root -m 0755 "$stage/worker" /usr/local/bin/llama-server
install -m 0644 /opt/paradiigma/current/deploy/apparmor/paradiigma-qwen /etc/apparmor.d/paradiigma-qwen
apparmor_parser -r /etc/apparmor.d/paradiigma-qwen
install -d -m 0755 /run/systemd/system/paradiigma-qwen.service.d
cat > /run/systemd/system/paradiigma-qwen.service.d/synthetic.conf <<'EOF'
[Service]
Type=oneshot
Restart=no
ExecStart=
ExecStartPre=/usr/bin/python3 -B scripts/run_model.py --help
ExecStart=/usr/bin/python3 -B -c "import subprocess; subprocess.run(['/usr/local/bin/llama-server'], check=True)"
EOF
systemctl daemon-reload
systemctl start paradiigma-qwen.service
echo 'Confinement verified with synthetic worker'
