#!/usr/bin/env bash
set -euo pipefail
: "${PWB_DATA_ROOT:?Set PWB_DATA_ROOT to an external runtime data directory}"
PWB_SELECTED_PYTHON="${PWB_PYTHON:-python3}"
"$PWB_SELECTED_PYTHON" -c 'import pwb,nbclient,nbformat,jupyter_client'
mkdir -p "$PWB_DATA_ROOT"
nohup "$PWB_SELECTED_PYTHON" -m pwb --data-root "$PWB_DATA_ROOT" serve --port "${PWB_PORT:-8970}" > "$PWB_DATA_ROOT/server.log" 2>&1 < /dev/null &
PWB_STARTED_PID=$!
sleep 1
if ! kill -0 "$PWB_STARTED_PID" 2>/dev/null; then cat "$PWB_DATA_ROOT/server.log"; exit 1; fi
printf 'Workbench: http://127.0.0.1:%s  PID: %s\n' "${PWB_PORT:-8970}" "$PWB_STARTED_PID"
