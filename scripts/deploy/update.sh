#!/usr/bin/env bash
# Same serialized release transaction for a forward update or explicit rollback.
set -euo pipefail
[[ -L /opt/paradiigma/current ]] || { echo 'Install an initial release first' >&2; exit 1; }
exec bash "$(dirname "$0")/install.sh" "$@"
