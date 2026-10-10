#!/usr/bin/env python3
"""Shared discovery and atomic configuration helpers for independent client scripts."""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import fcntl
import hashlib
from io import BytesIO
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

# Use the bundled helper when invoked directly from the baked repository.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm_model_metadata import parse_catalog, render_model


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
    live_models = parse_catalog(None)

    def read_models(request, **kwargs):
        # Keep the metadata before the existing provider parser reduces the
        # response to IDs. No client-package patch or second API call required.
        nonlocal live_models
        with opener(request, **kwargs) as response:
            payload = response.read(8 * 1024 * 1024 + 1)
        if len(payload) > 8 * 1024 * 1024:
            raise RefreshError("Provider response exceeds limit")
        live_models = parse_catalog(json.loads(payload))
        return BytesIO(payload)

    for attempt in range(3):
        live_models = parse_catalog(None)
        providers, warnings = discover_openai_v1_providers(scoped, opener=read_models, timeout=15)
        if (len(providers) == 1 and not warnings and live_models
                and set(providers[0].models) == set(live_models)):
            provider = providers[0]
            return {"id": provider.provider_id, "base_url": provider.base_url,
                    "models": list(provider.models), "key_env": provider.key_env,
                    "metadata": live_models.metadata}
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
        block["models"] = [render_model(kind, model, catalog["metadata"].get(model, {}), rows.get(model))
                           for model in ids]
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
        block["models"] = {model: render_model(kind, model, catalog["metadata"].get(model, {}), previous.get(model))
                           for model in ids}
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


def plan_configs(environ, catalog, kinds=None):
    plans, results = [], {}
    for kind, path in paths(environ).items():
        if kinds is not None and kind not in kinds:
            continue
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
        if kind == "openclaw":
            selection = original.get("agents", {}).get("defaults", {}).get("model", {})
            refs = ([selection] if isinstance(selection, str) else
                    [selection.get("primary"), *selection.get("fallbacks", [])])
            prefix = catalog["id"] + "/"
            missing = [ref for ref in refs if isinstance(ref, str) and ref.startswith(prefix)
                       and ref[len(prefix):] not in catalog["models"]]
            if missing:
                results[kind]["unavailable_selections_retained"] = missing
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
            expected = catalog["metadata"]
            if set(actual) == set(catalog["models"]) and all(
                    all(actual[model].get("limit", {}).get(key) == value
                        for key, value in expected.get(model, {}).items() if key in ("context", "output"))
                    for model in catalog["models"]):
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
        "print(json.dumps(next(row['models'] for row in rows "
        "if row.get('provider_key')==sys.argv[1])))"
    )
    actual = json.loads(command(["/usr/local/lib/hermes-agent/venv/bin/python", "-c", code, catalog["id"]]))
    if sorted(actual) != sorted(catalog["models"]) or any(
            any(actual[model].get(key) != value for key, value in render_model(
                "hermes", model, catalog["metadata"].get(model, {})).items())
            for model in catalog["models"]):
        raise RefreshError("Hermes native model catalog differs from the live provider")


def catalog_digest(catalog):
    return hashlib.sha256(json.dumps(
        [catalog["id"], catalog["base_url"], catalog["models"], catalog["metadata"]],
        sort_keys=True).encode()).hexdigest()


def run_single(kind, environ, catalog, update_runtime, *, force_reload=False):
    """Update only this client's config and runtime; other clients are not required."""
    catalog = catalog if catalog is not None else discover(environ)
    runtime = Path(environ.get("REFRESH_RUNTIME_DIR") or "/var/lib/litellm-free")
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    status = runtime / (kind + ".status.json")
    with (runtime / (kind + ".lock")).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RefreshError(f"{kind}: another refresh is running") from None
        report = {}
        try:
            plans, clients = plan_configs(environ, catalog, kinds=(kind,))
            previous = json.loads(status.read_text()) if status.is_file() else {}
            digest = catalog_digest(catalog)
            needs_reload = (force_reload or clients[kind]["changed"]
                            or previous.get("catalog_sha256") != digest
                            or previous.get("status") != "ok")
            report = {"status": "pending", "client": kind, "provider": catalog["id"],
                      "catalog_sha256": digest, **clients[kind]}
            atomic_write(status, (json.dumps(report) + "\n").encode())
            commit_plans(plans)
            if not running(kind + ".service"):
                raise RefreshError(f"{kind}: service is not running")
            report["runtime"] = update_runtime(environ, catalog, needs_reload)
            for path, _, after in plans:
                if yaml.safe_load(path.read_bytes()) != yaml.safe_load(after):
                    raise RefreshError(f"{kind}: config changed during refresh")
            report["status"] = "ok"
        except Exception as exc:
            report.update(status="error", error=str(exc) if isinstance(exc, RefreshError) else type(exc).__name__)
            atomic_write(status, (json.dumps(report) + "\n").encode())
            raise
        atomic_write(status, (json.dumps(report) + "\n").encode())
        return report


def client_main(kind, update_runtime, argv=None):
    parser = argparse.ArgumentParser(description=f"Refresh the {kind} litellm-free catalog only.")
    parser.add_argument("--catalog-stdin", action="store_true",
                        help="Use the orchestrator's live catalog snapshot from stdin")
    parser.add_argument("--reload", action="store_true", help="Retry a pending runtime reload")
    args = parser.parse_args(argv)
    try:
        catalog = json.load(sys.stdin) if args.catalog_stdin else None
        report = run_single(kind, dict(os.environ), catalog, update_runtime, force_reload=args.reload)
    except Exception as exc:
        print(json.dumps({"status": "error", "client": kind,
                          "error": str(exc) if isinstance(exc, RefreshError) else type(exc).__name__}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0
