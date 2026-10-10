#!/usr/bin/env python3
"""Load F24 SALES' passing chat routes into a config file or LiteLLM database."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import httpx
from dotenv import dotenv_values
import yaml

from config_files import archive_enabled, publish_file
from litellm_export import CATALOG_URL, CONFIG_URL, OWNER, PROVIDERS, model_config_fingerprint, route_params, validate_config
try:
    from model_diff import empty_diff, model_diff
except ModuleNotFoundError:  # Keep the standalone file importer self-contained.
    def empty_diff():
        return {"changed": False, "before_count": 0, "after_count": 0,
                "added": [], "removed": [], "updated": []}

    def model_diff(before, after):
        return {"changed": before != after, "before_count": len(before), "after_count": len(after),
                "added": [], "removed": [], "updated": []}


def load_env(paths, use_header_defaults=False):
    header = sys.modules.get("python_header")
    default_values = (header.env if use_header_defaults else header._process_env) if header else {}
    values = {key: value for key, value in default_values.items() if value and value.strip().lower() != "blank"}
    for path in paths:
        if not path.is_file():
            raise ValueError("An explicitly selected .env file does not exist")
        values.update({k: v for k, v in dotenv_values(path, interpolate=False).items() if v and v.strip().lower() != "blank"})
    values.update({key: value for key, value in os.environ.items() if value and value.strip().lower() != "blank"
                   and (header is None or key in header._process_env or key not in header.env or value != header.env[key])})
    for name, file_name in list(values.items()):
        if not name.endswith("_API_KEY_FILE") or not file_name.strip():
            continue
        key_name = name[:-5]
        if values.get(key_name, "").strip():
            continue
        secret_path = Path(file_name.strip()).expanduser()
        if not secret_path.is_absolute() or not secret_path.is_file() or secret_path.is_symlink():
            raise ValueError(f"Invalid secret file for {key_name}")
        secret = secret_path.read_text().strip()
        if secret:
            values[key_name] = secret
    return values


def local_host(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def tls_context(cafile=None):
    context = ssl.create_default_context()
    if cafile:
        context.load_verify_locations(cafile=cafile)
    return context


def load_document(path=None, env=None, source_url=None):
    env = env or {}
    if path:
        raw = Path(path).read_bytes()
        is_yaml = Path(path).suffix.lower() in {".yaml", ".yml"}
    else:
        source = source_url or env.get("IMPORT_SOURCE_URL") or CATALOG_URL
        parsed = urlsplit(source)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or (parsed.scheme == "http" and not local_host(parsed.hostname))):
            raise ValueError("Import source must use HTTPS or a local HTTP endpoint")
        uds = env.get("IMPORT_SOURCE_UDS")
        if uds and not local_host(parsed.hostname):
            raise ValueError("A source Unix socket requires a localhost or loopback URL")
        options = {"timeout": 30, "follow_redirects": False, "trust_env": False,
                   "verify": tls_context(env.get("IMPORT_SOURCE_CA_FILE") or env.get("LITELLM_CA_FILE"))}
        if uds:
            options["transport"] = httpx.HTTPTransport(uds=uds, verify=options["verify"], trust_env=False)
        with httpx.Client(**options) as client:
            response = client.get(source)
            response.raise_for_status()
            raw = response.content
            is_yaml = (Path(parsed.path).suffix.lower() in {".yaml", ".yml"}
                       or "yaml" in response.headers.get("content-type", "").lower())
    if len(raw) > 5_000_000:
        raise ValueError("Config file exceeds the import size limit")
    document = yaml.safe_load(raw) if is_yaml else json.loads(raw)
    validate_config(document)
    return document


def select_models(document, env, include_all=False, resolve=False):
    models, skipped = [], set()
    for original in validate_config(document):
        row = routing_model(original)
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


def write_document(path, document, force=False, format="json", archive=None):
    archive = archive_enabled(os.environ.get("LITELLM_FREE_ARCHIVE", "0") if archive is None else archive)
    text = (yaml.safe_dump(document, allow_unicode=True, sort_keys=False) if format == "yaml"
            else json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    return publish_file(path, text, force=force, archive=archive)


def write_config(path, models, force=False, format="json", archive=None):
    return write_document(path, {"model_list": models}, force=force, format=format, archive=archive)


def read_api_models(client):
    response = client.get("/model/info")
    response.raise_for_status()
    rows = response.json().get("data")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("LiteLLM returned an invalid model list")
    return rows


def patch_metadata(client, models, rows, owner, dry_run):
    if not owner or len(owner) > 128 or any(ord(c) < 32 for c in owner):
        raise ValueError("Invalid owner for metadata patch")
    counts = {"patched": 0, "unchanged": 0, "skipped_existing": 0, "dry_run": dry_run}
    changed = []
    for model in models:
        matches = [row for row in rows if row.get("model_name") == model["model_name"]
                   and (row.get("model_info") or {}).get("managed_by") == owner]
        if len(matches) > 1:
            raise ValueError("Multiple existing routes match the requested metadata owner and name")
        if not matches:
            counts["skipped_existing"] += 1
            continue
        row = matches[0]
        old = row["model_info"]
        ident = old.get("id")
        if not isinstance(ident, str) or not ident or any(c in ident for c in "/?#"):
            raise ValueError("Existing route has an invalid model ID")
        descriptive = {key: deepcopy(value) for key, value in model["model_info"].items() if key.startswith("f24_")}
        merged = {**old, **descriptive}
        if merged == old:
            counts["unchanged"] += 1
            continue
        if not dry_run:
            response = client.patch(f"/model/{ident}/update", json={"model_info": merged})
            response.raise_for_status()
            changed.append((ident, model["model_name"], descriptive))
        counts["patched"] += 1
    if changed:
        readback = read_api_models(client)
        for ident, name, descriptive in changed:
            matches = [row for row in readback if row.get("model_name") == name
                       and (row.get("model_info") or {}).get("id") == ident
                       and (row.get("model_info") or {}).get("managed_by") == owner]
            if (len(matches) != 1
                    or any(matches[0]["model_info"].get(key) != value for key, value in descriptive.items())):
                raise ValueError("LiteLLM did not preserve the patched metadata; readback failed")
    return counts


def route_identity(info, expected, prefix):
    upstream_key = "free_sync_model_id" if prefix == "free_sync_" else "f24_upstream_id"
    return (info.get(prefix + "provider") == expected["f24_provider"]
            and info.get(upstream_key) == expected["f24_upstream_id"]
            and info.get(prefix + "variant") == expected["f24_variant"])


def source_hash(model):
    return hashlib.sha256(json.dumps(model, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


def routing_model(model):
    """Keep the public import limited to routing and stable route identity."""
    result = deepcopy(model)
    info = result.get("model_info") or {}
    info.pop("f24_metadata", None)
    result["model_info"] = info
    return result


def deployment_matches(row, model, fingerprint):
    """A cached import hash alone cannot detect drift in the live deployment."""
    info, params = row.get("model_info") or {}, row.get("litellm_params") or {}
    return (info.get("f24_import_hash") == fingerprint
            and all(info.get(key) == value for key, value in model["model_info"].items() if key != "id")
            and all(params.get(key) == value for key, value in model["litellm_params"].items()
                    if key != "api_key"))  # LiteLLM may redact the persisted key.


def owned_routing_rows(rows):
    """Return only successful routes owned by this importer for diffs."""
    result = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("model_info"), dict):
            continue
        info = row["model_info"]
        if info.get("managed_by") != OWNER or info.get("access_groups") != ["litellm-free"]:
            continue
        result.append({"model_name": row.get("model_name"),
                       "provider": info.get("f24_upstream_id"),
                       "aggregator": info.get("f24_provider"),
                       "variant": info.get("f24_variant")})
    return result


def verify_readback(client, changed):
    rows = read_api_models(client)
    for ident, name, info in changed:
        matches = [row for row in rows if row.get("model_name") == name
                   and (row.get("model_info") or {}).get("id") == ident]
        if len(matches) != 1 or any(matches[0]["model_info"].get(key) != value for key, value in info.items()):
            raise ValueError("LiteLLM did not preserve imported model information; readback failed")


def prune_owned(client, rows, models, env, dry_run, adopt_managed_by=None):
    active_names = {model["model_name"] for model in models}
    providers = {provider for provider, (_, key) in PROVIDERS.items() if env.get(key, "").strip()}
    providers.update(model["model_info"]["f24_provider"] for model in models
                     if model["litellm_params"].get("api_key")
                     and not model["litellm_params"]["api_key"].startswith("os.environ/"))
    deleted = []
    for row in rows:
        info = row.get("model_info") or {}
        if info.get("managed_by") == OWNER and info.get("source") == CONFIG_URL:
            provider, upstream, variant = info.get("f24_provider"), info.get("f24_upstream_id"), info.get("f24_variant")
        elif adopt_managed_by and adopt_managed_by != OWNER and info.get("managed_by") == adopt_managed_by:
            # Adoption must also reconcile obsolete routes from the explicitly
            # selected legacy manager, not just names present in today's YAML.
            provider, upstream, variant = (info.get("free_sync_provider"),
                                           info.get("free_sync_model_id"), info.get("free_sync_variant"))
        else:
            continue
        if (info.get("access_groups") != ["litellm-free"] or provider not in providers
                or row.get("model_name") in active_names or not isinstance(upstream, str) or not upstream
                or any(ord(c) < 32 for c in upstream) or variant not in {"base", "think", "fast"}):
            continue
        name = provider + "/" + upstream + ("-" + variant if variant != "base" else "")
        if row.get("model_name") != name:
            continue
        try:
            route_params(provider, upstream, variant)
        except (ValueError, KeyError):
            continue
        ident = info.get("id")
        if not isinstance(ident, str) or not ident or any(ord(c) < 32 for c in ident):
            raise ValueError("Obsolete imported route has an invalid model ID")
        if not dry_run:
            response = client.post("/model/delete", json={"id": ident})
            response.raise_for_status()
        deleted.append(ident)
    if deleted and not dry_run:
        remaining = {(row.get("model_info") or {}).get("id") for row in read_api_models(client)}
        if any(ident in remaining for ident in deleted):
            raise ValueError("LiteLLM still lists a removed route; readback failed")
    return len(deleted)


def import_api(models, env, dry_run=False, patch_managed_by=None, adopt_managed_by=None,
               prune=False, capture_diff=False):
    if not models:
        raise ValueError("No validated routes selected; import and pruning require a nonempty model list")
    if patch_managed_by and adopt_managed_by:
        raise ValueError("Metadata patch and manager adoption are mutually exclusive")
    if patch_managed_by and prune:
        raise ValueError("Metadata-only patch cannot prune deployments")
    if adopt_managed_by and (len(adopt_managed_by) > 128 or any(ord(c) < 32 for c in adopt_managed_by)):
        raise ValueError("Invalid owner for model adoption")
    models = [routing_model(model) for model in models]
    base = env.get("LITELLM_BASE_URL", "").rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("Set LITELLM_BASE_URL to your LiteLLM proxy")
    allow_http = env.get("LITELLM_ALLOW_HTTP", "").strip().lower() in {"true", "yes", "1", "on"}
    if parsed.scheme == "http" and not local_host(parsed.hostname) and not allow_http:
        raise ValueError("Use HTTPS for a remote LiteLLM proxy or explicitly configure LITELLM_ALLOW_HTTP")
    if parsed.port is None and env.get("LITELLM_PORT"):
        port = env["LITELLM_PORT"]
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("Invalid LITELLM_PORT")
        hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        base = urlunsplit(parsed._replace(netloc=f"{hostname}:{port}"))
    token = env.get("LITELLM_ADMIN_KEY") or env.get("LITELLM_VIRTUAL_KEY")
    if not token:
        raise ValueError("LITELLM_ADMIN_KEY is required for API import")
    counts = {"inserted": 0, "updated": 0, "adopted": 0, "unchanged": 0, "pruned": 0, "skipped_existing": 0, "dry_run": dry_run}
    tls = tls_context(env.get("LITELLM_CA_FILE"))
    with httpx.Client(base_url=base, headers={"Authorization": "Bearer " + token},
                      verify=tls, timeout=60, follow_redirects=False, trust_env=False) as client:
        rows = read_api_models(client)
        before_owned = owned_routing_rows(rows)
        if patch_managed_by:
            result = patch_metadata(client, models, rows, patch_managed_by, dry_run)
            if capture_diff:
                result["diff"] = model_diff(before_owned, owned_routing_rows(read_api_models(client)))
            return result
        existing = {(row.get("model_info") or {}).get("id"): row for row in rows}
        changed = []
        for model in models:
            ident, name = model["model_info"]["id"], model["model_name"]
            occupied = existing.get(ident)
            if occupied and (occupied.get("model_info") or {}).get("managed_by") != OWNER:
                raise ValueError("Model ID belongs to another manager")
            matches = [row for row in rows if row.get("model_name") == name]
            if len(matches) > 1:
                raise ValueError("Multiple existing routes have the same model name")
            old = matches[0] if matches else None
            adopted = False
            if old:
                info = old.get("model_info") or {}
                manager = info.get("managed_by")
                if manager == OWNER:
                    if not route_identity(info, model["model_info"], "f24_"):
                        raise ValueError("Imported model identity differs from the published route")
                elif adopt_managed_by and manager == adopt_managed_by:
                    if not route_identity(info, model["model_info"], "free_sync_"):
                        raise ValueError("Existing model identity differs from the requested adoption")
                    adopted = True
                else:
                    if prune:
                        raise ValueError("Published route belongs to another manager; exact reconciliation refused")
                    counts["skipped_existing"] += 1
                    continue
                ident = info.get("id")
                if not isinstance(ident, str) or not ident or any(c in ident for c in "/?#") or any(ord(c) < 32 for c in ident):
                    raise ValueError("Existing route has an invalid model ID")
                if occupied and occupied is not old:
                    raise ValueError("Published model ID conflicts with another existing route")
            elif occupied:
                raise ValueError("Published model ID belongs to a different route name")
            digest = source_hash(model)
            if old and not adopted and deployment_matches(old, model, digest):
                counts["unchanged"] += 1
                continue
            body = deepcopy(model)
            old_info = dict((old or {}).get("model_info") or {})
            old_info.pop("f24_metadata", None)
            body["model_info"] = {**old_info, **body["model_info"],
                                  "id": ident, "managed_by": OWNER, "f24_import_hash": digest}
            action = "adopted" if adopted else "updated" if old else "inserted"
            if not dry_run:
                response = (client.patch(f"/model/{ident}/update", json=body) if old
                            else client.post("/model/new", json=body))
                response.raise_for_status()
                changed.append((ident, name, body["model_info"]))
            counts[action] += 1
        if changed:
            verify_readback(client, changed)
        if prune:
            counts["pruned"] = prune_owned(client, rows, models, env, dry_run, adopt_managed_by)
        if capture_diff:
            counts["diff"] = model_diff(before_owned, owned_routing_rows(read_api_models(client)))
    return counts


def import_sql(models, env, container=None, engine="podman", dry_run=False):
    models = [routing_model(model) for model in models]
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


def _hook_payload(mode, status, diff, env):
    return {
        "schema_version": 1,
        "mode": mode,
        "run_id": env.get("IMPORT_HOOK_RUN_ID"),
        "status": {key: value for key, value in status.items() if key != "diff"},
        "diff": diff,
    }


def run_pre_success_command(status, env):
    """Run an optional local prerequisite after commit and before notification."""
    raw = (env.get("IMPORT_PRE_SUCCESS_COMMAND_JSON") or "").strip()
    if not raw:
        return None
    try:
        command = json.loads(raw)
        if (not isinstance(command, list) or not command
                or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command)):
            raise ValueError("Expected a nonempty JSON argv array")
        timeout = int(env.get("IMPORT_PRE_SUCCESS_TIMEOUT_SECONDS") or "180")
        if not 1 <= timeout <= 3600:
            raise ValueError("Invalid prerequisite timeout")
    except (TypeError, ValueError):
        return {"ok": False, "error": "Invalid pre-success command configuration"}
    try:
        settings = {**os.environ, **env}
        if "model_names" in status:
            # Pass the exact imported snapshot, never a subsequently changed scan.
            settings["IMPORT_MODEL_NAMES_JSON"] = json.dumps(status["model_names"])
        result = subprocess.run(command, input=json.dumps(status), text=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=timeout, check=False, env=settings)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Pre-success command timed out"}
    except OSError:
        return {"ok": False, "error": "Pre-success command could not start"}
    return {"ok": result.returncode == 0, "returncode": result.returncode}


def fire_import_hook(mode, status, env):
    """POST the final import result; missing hook credentials disable it."""
    diff = status.get("diff") or empty_diff()
    on_no_change = env.get("IMPORT_HOOK_ON_NO_CHANGE", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }
    if not diff.get("changed") and not on_no_change:
        return {"sent": False, "reason": "no_change"}
    try:
        from hook_client import post_hook
        return post_hook("import_succeeded", _hook_payload(mode, status, diff, env), env)
    except Exception as exc:
        # The import is complete; make hook failure visible without making a
        # successful database reconciliation run again and duplicate changes.
        return {"sent": False, "error": str(exc) or type(exc).__name__}


def fire_import_error_hook(mode, error, env, *, import_ok=False):
    """Report an import failure without exposing provider credentials or raw bodies."""
    try:
        from hook_client import post_hook
        return post_hook("import_failed", {
            "mode": mode,
            "run_id": env.get("IMPORT_HOOK_RUN_ID"),
            "status": "error",
            "phase": "post_import" if import_ok else "import",
            "import_ok": import_ok,
            "error": str(error)[:300] or type(error).__name__,
        }, env)
    except Exception as exc:
        return {"sent": False, "error": str(exc) or type(exc).__name__}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("pull", "file", "api", "sql"):
        p = sub.add_parser(mode)
        p.add_argument("--config", type=Path, help="Optional config.conf settings; python_header loads the project defaults")
        p.add_argument("--env-file", type=Path, action="append", help="Repeat for layered .env files; process environment wins")
        p.add_argument("--input", type=Path, help="Use an already downloaded YAML or JSON document")
        p.add_argument("--source-url", help="Configuration source; default: IMPORT_SOURCE_URL or www.f24-sales.com/litellm-config.yaml")
        p.add_argument("--delay-seconds", type=float, help="Pause before an actual online pull; default: IMPORT_DELAY_SECONDS or 10")
        p.add_argument("--dry-run", action="store_true")
        if mode == "pull":
            p.add_argument("--output", type=Path)
            p.add_argument("--format", choices=["json", "yaml"], default="yaml")
            p.add_argument("--yaml-output", type=Path, help="Optional additional native LiteLLM YAML output")
            p.add_argument("--force", action="store_true")
        if mode == "file":
            p.add_argument("--output", type=Path)
            p.add_argument("--format", choices=["json", "yaml"], default="yaml")
            p.add_argument("--all-providers", action="store_true")
            p.add_argument("--force", action="store_true")
        if mode == "api":
            owner_options = p.add_mutually_exclusive_group()
            owner_options.add_argument("--patch-managed-by", help="Only add descriptive f24_* metadata to existing routes owned by this exact manager")
            owner_options.add_argument("--adopt-managed-by", help="Adopt exact-name, matching free-sync routes from this manager, preserving existing IDs")
            p.add_argument("--prune", action="store_true", help="Remove obsolete imported routes and explicitly adopted legacy routes after successful import")
        if mode == "sql":
            p.add_argument("--container", help="Existing LiteLLM container, e.g. litellm-database")
            p.add_argument("--engine", choices=["podman", "docker"], default="podman")
    args = parser.parse_args(argv)
    env = {}
    try:
        config = args.config if args.config is not None else (Path("config.conf") if Path("config.conf").is_file() else None)
        paths = ([config] if config is not None else []) + (args.env_file if args.env_file is not None else ([Path(".env")] if Path(".env").is_file() else []))
        env = load_env(paths, use_header_defaults=args.config is None and args.env_file is None)
        archive = archive_enabled(env.get("LITELLM_FREE_ARCHIVE", "0"))
        delay = args.delay_seconds if args.delay_seconds is not None else float(env.get("IMPORT_DELAY_SECONDS") or "10")
        if not 0 <= delay <= 3600:
            raise ValueError("Import delay must be between 0 and 3600 seconds")
        if delay and not args.input and not args.dry_run:
            time.sleep(delay)
        document = load_document(args.input, env, args.source_url)
        if args.mode == "pull":
            models = validate_config(document)
            args.output = args.output or Path("litellm-free.yaml" if args.format == "yaml" else "litellm-free.json")
            if args.yaml_output and args.output.resolve() == args.yaml_output.resolve():
                raise ValueError("Additional YAML must use a different output path")
            if not args.force and (args.output.exists() or args.output.is_symlink()
                                  or (args.yaml_output and (args.yaml_output.exists() or args.yaml_output.is_symlink()))):
                raise ValueError("Output exists; choose another file or use --force")
            if not args.dry_run:
                write_config(args.output, models, args.force, format=args.format, archive=archive)
                if args.yaml_output:
                    write_config(args.yaml_output, models, args.force, format="yaml", archive=archive)
            status = {"output": str(args.output), "models": len(models), "dry_run": args.dry_run}
            if args.yaml_output:
                status["yaml_output"] = str(args.yaml_output)
            print(json.dumps({"ok": True, "mode": args.mode, **status}))
            return 0
        patch_owner = (getattr(args, "patch_managed_by", None) or env.get("IMPORT_PATCH_MANAGED_BY")) if args.mode == "api" else None
        adopt_owner = (getattr(args, "adopt_managed_by", None) or env.get("IMPORT_ADOPT_MANAGED_BY")) if args.mode == "api" else None
        if patch_owner and adopt_owner:
            raise ValueError("Metadata patch and manager adoption are mutually exclusive")
        models, skipped = select_models(document, env, include_all=bool(patch_owner) or getattr(args, "all_providers", False),
                                        resolve=args.mode in {"api", "sql"} and not patch_owner)
        if args.mode == "file":
            args.output = args.output or Path("litellm-free.yaml" if args.format == "yaml" else "litellm-free.json")
            if not args.dry_run:
                write_config(args.output, models, args.force, format=args.format, archive=archive)
            status = {"output": str(args.output), "models": len(models), "dry_run": args.dry_run}
        elif args.mode == "api":
            prune = args.prune or env.get("IMPORT_PRUNE", "").strip().lower() in {"true", "yes", "1", "on"}
            capture_diff = bool(env.get("IMPORT_HOOK_URL") and env.get("IMPORT_HOOK_BEARER") and not args.dry_run)
            import_options = {"patch_managed_by": patch_owner, "adopt_managed_by": adopt_owner, "prune": prune}
            if capture_diff:
                import_options["capture_diff"] = True
            status = import_api(models, env, args.dry_run, **import_options)
            status["fingerprint"] = model_config_fingerprint(document)
            status["model_names"] = sorted(model["model_name"] for model in models)
        else:
            status = import_sql(models, env, args.container, args.engine, args.dry_run)
        if args.mode in {"api", "sql"} and not args.dry_run:
            prerequisite = run_pre_success_command(status, env)
            if prerequisite is not None:
                status["post_import"] = prerequisite
            if prerequisite is not None and not prerequisite["ok"]:
                hook = fire_import_error_hook(args.mode, "Post-import prerequisite failed", env, import_ok=True)
            else:
                hook = fire_import_hook(args.mode, status, env)
            if hook is not None:
                status["hook"] = hook
        print(json.dumps({"ok": True, "mode": args.mode, "skipped_missing_keys": skipped, **status}))
        return 0
    except ValueError as exc:
        if args.mode in {"api", "sql"} and not getattr(args, "dry_run", False) and env:
            fire_import_error_hook(args.mode, exc, env)
        message = "Invalid JSON config or API response" if isinstance(exc, json.JSONDecodeError) else str(exc)
        print(json.dumps({"ok": False, "error": message}))
    except Exception as exc:
        if args.mode in {"api", "sql"} and not getattr(args, "dry_run", False) and env:
            fire_import_error_hook(args.mode, exc, env)
        print(json.dumps({"ok": False, "error": "Import failed", "type": type(exc).__name__}))
    return 1


if __name__ == "__main__":
    import python_header  # CLI-only SOT bootstrap; library imports do not load local credentials.

    raise SystemExit(main())
