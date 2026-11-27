#!/usr/bin/env bash
set -euo pipefail
umask 077

repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
revision=$(git -C "$repo" rev-parse HEAD)
run=$(mktemp -d "${TMPDIR:-/tmp}/qwen-sandy-bridge.XXXXXXXX")
printf 'Probe output: %s\n' "$run" >&2
printf '%s\n' "$revision" > "$run/revision"

tree="$run/source"
mkdir "$tree"
sampler=""
cleanup() {
    if [[ -n "$sampler" ]]; then
        kill "$sampler" 2>/dev/null || true
        wait "$sampler" 2>/dev/null || true
    fi
    rm -rf -- "$tree"
}
trap cleanup EXIT

git -C "$repo" archive "$revision" | tar -xf - -C "$tree"
cd "$tree"
package=handoff/qwen-final
git apply "$package/runtime/ai-core-sandy-bridge-one-slot.patch"
git apply "$package/runtime/ai-core-stable-planner-prefix.patch"
cp "$package/production_benchmark.py" scripts/qwen/production_benchmark.py
cp "$package/runtime/probe_sandy_bridge_ai_core.py" .

if [[ "${QWEN_SAMPLE_LINUX_RESOURCES:-0}" == 1 ]]; then
    python3 -u "$package/sample_linux_resources.py" > "$run/resources.jsonl" 2> "$run/resources.err" &
    sampler=$!
fi

status=0
python3 -u probe_sandy_bridge_ai_core.py "$@" 2> "$run/probe.err" | tee "$run/outcomes.jsonl" || status=$?
printf '%s\n' "$status" > "$run/status"
exit "$status"
