#!/usr/bin/env bash
set -euo pipefail
if [[ "$#" -ne 2 ]]; then printf 'Usage: update_local.sh INBOX REPORTS\n' >&2; exit 2; fi
"${PWB_PYTHON:-python3}" -m pwb update-local --inbox "$1" --output "$2"
