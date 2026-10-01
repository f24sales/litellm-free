#!/usr/bin/env bash
set -Eeuo pipefail

provider="${LITELLM_FREE_PROVIDER:-litellm-free}"
runtime_dir="${REFRESH_RUNTIME_DIR:-/persistent/SCRIPTS}"
status_file="${REFRESH_STATUS_FILE:-$runtime_dir/refresh-models.status.json}"
lock_file="${REFRESH_LOCK_FILE:-$runtime_dir/refresh-models.lock}"
openclaw_config="${OPENCLAW_CONFIG_PATH:-/root/.openclaw/openclaw.json}"
hermes_config="${HERMES_CONFIG_PATH:-/root/.hermes/config.yaml}"
opencode_config="${OPENCODE_CONFIG_PATH:-/root/.config/opencode/opencode.json}"

mkdir -p "$runtime_dir"
exec 9>"$lock_file"
flock -n 9 || { echo "refresh already running" >&2; exit 75; }

workdir="$(mktemp -d "$runtime_dir/.refresh-models.XXXXXX")"
cleanup() { rm -rf "$workdir"; }

write_status() {
  local payload="$1"
  local temporary="${status_file}.tmp.$$"
  printf '%s\n' "$payload" > "$temporary"
  chmod 0600 "$temporary"
  mv -f "$temporary" "$status_file"
}

on_exit() {
  local code=$?
  trap - EXIT
  if ((code != 0)); then
    write_status "{\"status\":\"error\",\"exit_code\":$code}" || true
  fi
  cleanup
  return "$code"
}
trap on_exit EXIT

echo "[refresh-models] configuring OpenCode"
opencode-ephemeral configure

echo "[refresh-models] refreshing OpenClaw provider inventory"
openclaw models list --provider "$provider" --refresh --json > "$workdir/openclaw.json"

echo "[refresh-models] configuring Hermes"
hermes-ephemeral configure

report="$(python3 - "$workdir/openclaw.json" "$openclaw_config" "$hermes_config" "$opencode_config" "$provider" <<'PY'
import json
from pathlib import Path
import sys

import yaml

inventory_path, openclaw_path, hermes_path, opencode_path, provider = sys.argv[1:]


def values(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("models", "data", "items", "rows"):
            if key in value and isinstance(value[key], (dict, list)):
                return values(value[key])
        return list(value)
    return []


def model_id(value):
    if isinstance(value, dict):
        value = next((value.get(key) for key in ("id", "model", "key", "name")
                      if value.get(key)), "")
    value = str(value).strip()
    prefix = provider + "/"
    return value[len(prefix):] if value.startswith(prefix) else value


def model_set(value):
    return {model_id(row) for row in values(value) if model_id(row)}


inventory = json.loads(Path(inventory_path).read_text())
expected = model_set(inventory)
if not expected:
    raise SystemExit("OpenClaw returned an empty provider inventory")

openclaw = json.loads(Path(openclaw_path).read_text())
hermes = yaml.safe_load(Path(hermes_path).read_text())
opencode = json.loads(Path(opencode_path).read_text())

client_values = {
    "openclaw": openclaw["models"]["providers"][provider]["models"],
    "hermes": hermes["providers"][provider]["models"],
    "opencode": opencode["provider"][provider]["models"],
}
clients = {}
for name, raw in client_values.items():
    actual = model_set(raw)
    missing = sorted(expected - actual)
    if missing:
        raise SystemExit(f"{name} is missing {len(missing)} discovered models")
    clients[name] = {
        "models": len(actual),
        "discovered": len(expected),
        "configured_extra": len(actual - expected),
    }

print(json.dumps({"status": "ok", "provider": provider,
                  "discovered": len(expected), "clients": clients},
                 sort_keys=True))
PY
)"

echo "[refresh-models] restarting OpenCode and Hermes"
systemctl restart opencode.service
systemctl is-active --quiet opencode.service
systemctl restart hermes.service
systemctl is-active --quiet hermes.service

report="$(python3 - "$report" <<'PY'
import json
import sys
result = json.loads(sys.argv[1])
result["restarted"] = ["opencode.service", "hermes.service"]
print(json.dumps(result, sort_keys=True))
PY
)"

write_status "$report"
printf '%s\n' "$report"
