#!/usr/bin/env bash
set -Eeuo pipefail
exec python3 "$(dirname -- "$(readlink -f -- "$0")")/refresh_models.py" "$@"
