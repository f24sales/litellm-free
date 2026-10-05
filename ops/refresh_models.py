#!/usr/bin/env python3
"""Refresh one live provider in all three clients without rebuilding their config."""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener

import yaml


class RefreshError(RuntimeError):
    """Only fixed, credential-free error messages may be reported."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RefreshError("Provider redirects are not accepted")


def discover(environ):
    # Reuse OpenClaw's URL and group parsing, but require a live result.
    sys.path.insert(0, "/usr/local/lib/openclaw-ephemeral")
    from openclaw_ephemeral.providers import discover_openai_v1_providers

    wanted = environ.get("LITELLM_FREE_PROVIDER", "litellm-free").strip()
    matches = []
    for key, value in environ.items():
        match = re.fullmatch(r"OPENAI_V1_PROVIDER(?:_(\d+))?", key)
        if match and value.strip().lower() == wanted.lower():
            matches.append(int(match[1] or 1))
    if len(set(matches)) != 1:
        raise RefreshError("Exactly one matching OPENAI_V1 provider is required")
    selected = matches[0]
    scoped = dict(environ)
    for key in list(scoped):
        match = re.fullmatch(r"OPENAI_V1_(PROVIDER|URL|PORT|KEY|API_KEY_ALIAS|STREAM|DISCOVERY_HEADERS|MODELS)(?:_(\d+))?", key)
        if match and (int(match[2] or 1) != selected or match[1] == "MODELS"):
            del scoped[key]
    opener = build_opener(NoRedirect()).open
    for attempt in range(3):
        providers, warnings = discover_openai_v1_providers(scoped, opener=opener, timeout=15)
        if len(providers) == 1 and not warnings and providers[0].models:
            provider = providers[0]
            return {"id": provider.provider_id, "base_url": provider.base_url,
                    "models": list(provider.models), "key_env": provider.key_env,
                    "openclaw_models": provider.openclaw_config()["models"]}
        if attempt < 2:
            time.sleep(attempt + 1)
    raise RefreshError("Live provider discovery failed; existing catalogs retained")


def update_catalog(kind, original, catalog):
    config = deepcopy(original)
    provider, ids = catalog["id"], catalog["models"]
    if not ids:
        raise RefreshError("Empty catalogs are not accepted")
    blocks = (config.get("models", {}).get("providers", {}) if kind == "openclaw"
              else config.get("providers" if kind == "hermes" else "provider", {}))
    block = blocks.get(provider)
    if not isinstance(block, dict):
        raise RefreshError(f"{kind}: provider configuration is missing")
    base = (block.get("baseUrl") or block.get("base_url") or
            block.get("options", {}).get("baseURL") or "").rstrip("/")
    if base != catalog["base_url"].rstrip("/"):
        raise RefreshError(f"{kind}: provider endpoint differs from injected environment")
    if kind == "openclaw":
        rows = {row["id"]: row for row in block.get("models", [])}
        fresh = {row["id"]: row for row in catalog["openclaw_models"]}
        block["models"] = [deepcopy(rows.get(model, fresh[model])) for model in ids]
        agents = config.get("agents", {})
        for agent in [agents.get("defaults", {}), *agents.get("entries", {}).values()]:
            if isinstance(agent.get("models"), dict):
                allowed = agent["models"]
                previous = dict(allowed)
                for key in list(allowed):
                    if key.startswith(provider + "/"):
                        del allowed[key]
                for model in ids:
                    key = provider + "/" + model
                    allowed[key] = previous.get(key, {})
    else:
        previous = block.get("models", {})
        block["models"] = {model: deepcopy(previous.get(model, {})) for model in ids}
        if kind == "hermes" and config.get("model", {}).get("provider") == provider:
            config["model"]["available"] = list(ids)
    return config


def paths(environ):
    home = Path(environ.get("HOME") or "/root")
    claw_home = environ.get("OPENCLAW_STATE_DIR") or environ.get("OPENCLAW_HOME") or home / ".openclaw"
    return {
        "openclaw": Path(environ.get("OPENCLAW_CONFIG") or environ.get("OPENCLAW_CONFIG_PATH") or Path(claw_home) / "openclaw.json").resolve(),
        "hermes": Path(environ.get("HERMES_CONFIG_PATH") or Path(environ.get("HERMES_HOME") or home / ".hermes") / "config.yaml").resolve(),
        "opencode": Path(environ.get("OPENCODE_CONFIG") or environ.get("OPENCODE_CONFIG_PATH") or home / ".config/opencode/opencode.json").resolve(),
    }


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    previous = path.stat() if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=".model-refresh-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), previous.st_mode & 0o777 if previous else 0o600)
            if previous and os.geteuid() == 0:
                os.fchown(stream.fileno(), previous.st_uid, previous.st_gid)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def plan_configs(environ, catalog):
    plans, results = [], {}
    for kind, path in paths(environ).items():
        if not path.is_file():
            raise RefreshError(f"{kind}: configuration is not ready")
        before = path.read_bytes()
        original = yaml.safe_load(before) if kind == "hermes" else json.loads(before)
        updated = update_catalog(kind, original, catalog)
        changed = updated != original
        content = (yaml.safe_dump(updated, sort_keys=False, allow_unicode=True) if kind == "hermes"
                   else json.dumps(updated, indent=2, ensure_ascii=False) + "\n")
        plans.append((path, before, content.encode() if changed else before))
        results[kind] = {"models": len(catalog["models"]), "changed": changed}
    return plans, results


def commit_plans(plans):
    for path, before, _ in plans:
        if path.read_bytes() != before:
            raise RefreshError("Client config changed during discovery; retry required")
    for path, before, after in plans:
        if before != after:
            if path.read_bytes() != before:
                raise RefreshError("Client config changed during commit; retry required")
            atomic_write(path, after)


def command(argv, *, timeout=120):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RefreshError(f"{argv[0]} did not complete") from exc
    if result.returncode:
        raise RefreshError(f"{argv[0]} failed")
    return result.stdout


def running(service):
    return subprocess.run(["systemctl", "is-active", "--quiet", service],
                          capture_output=True, timeout=10).returncode == 0


def opencode_request(environ, path, method="GET", data=None):
    port = int(environ.get("OPENCODE_API_PORT") or 4096)
    if not 1 <= port <= 65535:
        raise RefreshError("Invalid OpenCode port")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    password = environ.get("OPENCODE_SERVER_PASSWORD", "")
    if password:
        username = environ.get("OPENCODE_SERVER_USERNAME") or "opencode"
        headers["Authorization"] = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
    return Request(f"http://127.0.0.1:{port}{path}", headers=headers, method=method, data=data)


def reload_opencode(environ):
    # Reload instances/config without rebinding the HTTP socket. Restarting a
    # wildcard listener can collide with same-port Tailscale Serve listeners.
    try:
        # Global config is cached separately from instances. An empty patch
        # reads our updated file and invalidates that cache without changing
        # any configuration values (only native JSON formatting may change).
        with build_opener(NoRedirect()).open(
                opencode_request(environ, "/global/config", "PATCH", b"{}"), timeout=30) as response:
            response.read(8 * 1024 * 1024 + 1)
        with build_opener(NoRedirect()).open(
                opencode_request(environ, "/global/dispose", "POST"), timeout=30) as response:
            if json.load(response) is not True:
                raise RefreshError("OpenCode reload was not acknowledged")
    except RefreshError:
        raise
    except Exception as exc:
        raise RefreshError("OpenCode API reload failed") from exc


def verify_opencode(environ, catalog):
    request = opencode_request(environ, "/provider")
    opener = build_opener(NoRedirect()).open
    for attempt in range(15):
        try:
            with opener(request, timeout=3) as response:
                payload = response.read(8 * 1024 * 1024 + 1)
            if len(payload) > 8 * 1024 * 1024:
                raise RefreshError("OpenCode response exceeds limit")
            rows = json.loads(payload)["all"]
            actual = next(row["models"] for row in rows if row["id"] == catalog["id"])
            if set(actual) == set(catalog["models"]):
                return
        except Exception:
            pass
        if attempt < 14:
            time.sleep(1)
    raise RefreshError("Running OpenCode API did not load the current catalog")


def verify_hermes(catalog):
    # Use Hermes' own provider normalization (the model picker's input), not
    # merely the YAML shape. No provider credentials leave that subprocess.
    code = (
        "import sys,json; sys.path.insert(0,'/usr/local/lib/hermes-agent'); "
        "from hermes_cli.config_providers import get_compatible_custom_providers; "
        "rows=get_compatible_custom_providers(); "
        "print(json.dumps(sorted(next(row['models'] for row in rows "
        "if row.get('provider_key')==sys.argv[1]))))"
    )
    actual = json.loads(command(["/usr/local/lib/hermes-agent/venv/bin/python", "-c", code, catalog["id"]]))
    if actual != sorted(catalog["models"]):
        raise RefreshError("Hermes native model catalog differs from the live provider")


def refresh(environ, status_path):
    catalog = discover(environ)
    plans, clients = plan_configs(environ, catalog)
    digest = hashlib.sha256(json.dumps([catalog["id"], catalog["base_url"], catalog["models"]],
                                     sort_keys=True).encode()).hexdigest()
    previous = json.loads(status_path.read_text()) if status_path.is_file() else {}
    pending = set(previous.get("pending_services", []))
    attempted = set(previous.get("reload_attempted", []))
    for kind in ("opencode", "hermes"):
        if clients[kind]["changed"] or previous.get("catalog_sha256") != digest:
            pending.add(kind)
    report = {"status": "pending", "provider": catalog["id"], "catalog_sha256": digest,
              "discovered": len(catalog["models"]), "clients": clients,
              "pending_services": sorted(pending), "reload_attempted": sorted(attempted)}
    # Durable intent BEFORE replacing files: retry failed reloads even if the
    # next download is unchanged.
    atomic_write(status_path, (json.dumps(report) + "\n").encode())
    commit_plans(plans)
    for kind in ("opencode", "hermes"):
        service = kind + ".service"
        if not running(service) and kind not in attempted:
            raise RefreshError(f"{kind}: service is not running")
        if kind in pending:
            attempted.add(kind)
            report["reload_attempted"] = sorted(attempted)
            atomic_write(status_path, (json.dumps(report) + "\n").encode())
            if kind == "opencode":
                reload_opencode(environ)
            # Hermes' /model picker reads its mtime-cached config on each
            # invocation. Restarting its gateway would also stop the dashboard
            # through Requires=, needlessly rebinding that Serve-shared port.
        if not running(service):
            raise RefreshError(f"{kind}: restart did not activate the service")
        if kind == "opencode":
            verify_opencode(environ, catalog)
        else:
            verify_hermes(catalog)
        clients[kind]["runtime"] = "api_verified" if kind == "opencode" else "native_catalog_verified"
        pending.discard(kind)
        attempted.discard(kind)
        report["pending_services"] = sorted(pending)
        report["reload_attempted"] = sorted(attempted)
        atomic_write(status_path, (json.dumps(report) + "\n").encode())
    # The gateway watches its config; do not restart it from inside its hook.
    if not running("openclaw.service"):
        raise RefreshError("openclaw: gateway is not running")
    for path, _, after in plans:
        # Hermes may normalize YAML formatting on startup. Compare values,
        # including Voice/MCP settings, not serializer whitespace.
        if yaml.safe_load(path.read_bytes()) != yaml.safe_load(after):
            raise RefreshError("A client changed configuration values during reload")
    clients["openclaw"]["runtime"] = "config_watch"
    report["status"] = "ok"
    atomic_write(status_path, (json.dumps(report) + "\n").encode())
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    runtime = Path(os.environ.get("REFRESH_RUNTIME_DIR") or "/var/lib/litellm-free")
    status = Path(os.environ.get("REFRESH_STATUS_FILE") or runtime / "refresh-models.status.json")
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (runtime / "refresh-models.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "busy"}))
            return 75
        try:
            report = refresh(dict(os.environ), status)
        except Exception as exc:
            # Preserve retry state. Never emit URLs, request headers or keys.
            report = json.loads(status.read_text()) if status.is_file() else {}
            report.update(status="error", error=str(exc) if isinstance(exc, RefreshError) else type(exc).__name__)
            atomic_write(status, (json.dumps(report) + "\n").encode())
            print(json.dumps(report))
            return 1
        print(json.dumps(report, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
