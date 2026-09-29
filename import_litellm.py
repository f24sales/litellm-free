#!/usr/bin/env python3
"""Load F24 SALES' passing chat routes into a config file or LiteLLM database."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit, urlunsplit

import httpx
from dotenv import dotenv_values

from litellm_export import CONFIG_URL, OWNER, PROVIDERS, validate_config


def load_env(paths):
    values = {}
    for path in paths:
        if not path.is_file():
            raise ValueError("An explicitly selected .env file does not exist")
        values.update({k: v for k, v in dotenv_values(path, interpolate=False).items() if v})
    values.update({k: v for k, v in os.environ.items() if v})
    return values


def load_document(path=None):
    if path:
        raw = Path(path).read_bytes()
    else:
        with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
            response = client.get(CONFIG_URL)
            response.raise_for_status()
            raw = response.content
    if len(raw) > 5_000_000:
        raise ValueError("Config file exceeds the import size limit")
    document = json.loads(raw)
    validate_config(document)
    return document


def select_models(document, env, include_all=False, resolve=False):
    models, skipped = [], set()
    for original in document["model_list"]:
        row = deepcopy(original)
        provider = row["model_info"]["f24_provider"]
        env_name = PROVIDERS[provider][1]
        token = env.get(env_name, "").strip()
        if not token and not include_all:
            skipped.add(provider)
            continue
        if resolve:
            row["litellm_params"]["api_key"] = token
        models.append(row)
    if not models:
        raise ValueError("No provider keys configured; set keys in .env or use file --all-providers for a template")
    return models, sorted(skipped)


def write_config(path, models, force=False):
    path = Path(path)
    if path.exists() and not force:
        raise ValueError("Output exists; choose another file or use --force")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".litellm-config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump({"model_list": models}, output, indent=2, ensure_ascii=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        if force:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def import_api(models, env, dry_run=False):
    base = env.get("LITELLM_BASE_URL", "").rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("Set LITELLM_BASE_URL to your LiteLLM proxy")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Use HTTPS for a remote LiteLLM proxy")
    if parsed.port is None and env.get("LITELLM_PORT"):
        port = env["LITELLM_PORT"]
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("Invalid LITELLM_PORT")
        hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        base = urlunsplit(parsed._replace(netloc=f"{hostname}:{port}"))
    token = env.get("LITELLM_ADMIN_KEY")
    if not token:
        raise ValueError("LITELLM_ADMIN_KEY is required for API import")
    counts = {"inserted": 0, "updated": 0, "skipped_existing": 0, "dry_run": dry_run}
    with httpx.Client(base_url=base, headers={"Authorization": "Bearer " + token},
                      timeout=60, follow_redirects=False, trust_env=False) as client:
        response = client.get("/model/info")
        response.raise_for_status()
        rows = response.json().get("data")
        if not isinstance(rows, list):
            raise ValueError("LiteLLM returned an invalid model list")
        existing = {(row.get("model_info") or {}).get("id"): row for row in rows}
        for model in models:
            ident, name = model["model_info"]["id"], model["model_name"]
            old = existing.get(ident)
            if old and (old.get("model_info") or {}).get("managed_by") != OWNER:
                raise ValueError("Model ID belongs to another manager")
            if any(row.get("model_name") == name and (row.get("model_info") or {}).get("id") != ident for row in rows):
                counts["skipped_existing"] += 1
                continue
            action = "updated" if old else "inserted"
            if not dry_run:
                response = (client.patch(f"/model/{ident}/update", json=model) if old
                            else client.post("/model/new", json=model))
                response.raise_for_status()
            counts[action] += 1
    return counts


def import_sql(models, env, container=None, engine="podman", dry_run=False):
    if container:
        worker = Path(__file__).with_name("sql_import.py").read_text()
        result = subprocess.run([engine, "exec", "-i", "-e", "LITELLM_LOCAL_MODEL_COST_MAP=True", container,
                                 "python", "-c", worker], input=json.dumps({"models": models, "dry_run": dry_run}),
                                capture_output=True, text=True, timeout=180)
        try:
            status = json.loads(result.stdout)
        except ValueError:
            raise ValueError("Container import failed; check that LiteLLM and psycopg are installed") from None
        if result.returncode:
            raise ValueError(status.get("error", "Container SQL import failed"))
        return status
    for key in ("DATABASE_URL", "LITELLM_SALT_KEY", "LITELLM_MASTER_KEY"):
        if env.get(key):
            os.environ[key] = env[key]
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    from sql_import import import_models
    return import_models(models, dry_run=dry_run)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("file", "api", "sql"):
        p = sub.add_parser(mode)
        p.add_argument("--env-file", type=Path, action="append", help="Repeat for layered .env files; process environment wins")
        p.add_argument("--input", type=Path, help="Import downloaded JSON instead of the fixed www.f24-sales.com URL")
        p.add_argument("--dry-run", action="store_true")
        if mode == "file":
            p.add_argument("--output", type=Path, default=Path("litellm-free.json"))
            p.add_argument("--all-providers", action="store_true")
            p.add_argument("--force", action="store_true")
        if mode == "sql":
            p.add_argument("--container", help="Existing LiteLLM container, e.g. litellm-database")
            p.add_argument("--engine", choices=["podman", "docker"], default="podman")
    args = parser.parse_args(argv)
    try:
        paths = args.env_file if args.env_file is not None else ([Path(".env")] if Path(".env").is_file() else [])
        env = load_env(paths)
        document = load_document(args.input)
        models, skipped = select_models(document, env, include_all=getattr(args, "all_providers", False), resolve=args.mode != "file")
        if args.mode == "file":
            if not args.dry_run:
                write_config(args.output, models, args.force)
            status = {"output": str(args.output), "models": len(models), "dry_run": args.dry_run}
        elif args.mode == "api":
            status = import_api(models, env, args.dry_run)
        else:
            status = import_sql(models, env, args.container, args.engine, args.dry_run)
        print(json.dumps({"ok": True, "mode": args.mode, "skipped_missing_keys": skipped, **status}))
        return 0
    except ValueError as exc:
        message = "Invalid JSON config or API response" if isinstance(exc, json.JSONDecodeError) else str(exc)
        print(json.dumps({"ok": False, "error": message}))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": "Import failed", "type": type(exc).__name__}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
