"""LLM7's route-level capabilities, shared by imports, client refresh and request adapters."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.request import Request, urlopen

CATALOG_URL = "https://api.llm7.io/v1/models"
TOOL_FIELDS = ("tools", "tool_choice", "parallel_tool_calls", "functions", "function_call")


def cache_path():
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return Path(os.environ.get("LITELLM_FREE_LLM7_CACHE") or state / "litellm-free/llm7/capabilities.json")


def read_policy(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def refresh_policy(names):
    requested = {name for name in names if name.startswith("llm7/")}
    if not requested:
        return {}
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = read_policy(path)
        if requested <= old.keys() and time.time() - path.stat().st_mtime < 300:
            return {name: old[name] for name in requested}
        req = Request(CATALOG_URL, headers={"Accept": "application/json", "User-Agent": "litellm-free/1"})
        try:
            with urlopen(req, timeout=10) as response:
                rows = json.loads(response.read(4_000_001))["data"]
            latest = {}
            for row in rows:
                caps = row.get("capabilities") or {}
                if isinstance(row.get("id"), str) and all(type(caps.get(k)) is bool for k in ("vision", "tools")):
                    latest["llm7/" + row["id"]] = {"vision": caps["vision"], "tools": caps["tools"]}
            if not requested <= latest.keys():
                raise ValueError("LLM7 catalog lacks capabilities for a selected route")
        except (OSError, ValueError, KeyError, TypeError):
            if not requested <= old.keys():
                raise
            return {name: old[name] for name in requested}
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w") as tmp:
            json.dump(latest, tmp, sort_keys=True)
            tmp.write("\n")
        try:
            os.replace(tmp.name, path)
        finally:
            Path(tmp.name).unlink(missing_ok=True)
        return {name: latest[name] for name in requested}


def update_deployments(models):
    policies = refresh_policy(row["model_name"] for row in models)
    for row in models:
        caps = policies.get(row["model_name"])
        if caps is None:
            continue
        row["model_info"].update(supports_vision=caps["vision"], supports_function_calling=caps["tools"])
        if not caps["tools"]:
            row["litellm_params"]["additional_drop_params"] = list(TOOL_FIELDS)


def enrich_catalog(catalog):
    policies = refresh_policy(catalog["models"])
    if not policies:
        return catalog
    metadata = dict(catalog.get("metadata", {}))
    for name, caps in policies.items():
        metadata[name] = {**metadata.get(name, {}), "llm7": caps}
    return {**catalog, "metadata": metadata}


def image_schema(schema):
    if isinstance(schema, list):
        return any(image_schema(value) for value in schema)
    if not isinstance(schema, dict):
        return False
    properties = schema.get("properties")
    if isinstance(properties, dict) and "image_url" in properties:
        return True
    return any(image_schema(value) for value in schema.values())


def filter_request(request, capabilities):
    """Leave messages intact. Only change incompatible tool declarations and selections."""
    if not capabilities or (capabilities["tools"] and capabilities["vision"]):
        return request
    result = dict(request)
    if not capabilities["tools"]:
        for key in TOOL_FIELDS:
            result.pop(key, None)
        return result
    for field, choice in (("tools", "tool_choice"), ("functions", "function_call")):
        if not isinstance(request.get(field), list):
            continue
        kept, removed = [], set()
        for item in request[field]:
            fn = item.get("function", item) if isinstance(item, dict) else {}
            if isinstance(fn, dict) and (fn.get("name") in {"vision_analyze", "image"} or image_schema(fn.get("parameters"))):
                removed.add(fn.get("name"))
            else:
                kept.append(item)
        if not removed:
            continue
        if kept:
            result[field] = kept
            selected = result.get(choice)
            forced = selected.get("function", selected) if isinstance(selected, dict) else None
            if isinstance(forced, dict) and forced.get("name") in removed:
                result.pop(choice, None)
        else:
            result.pop(field, None)
            result.pop(choice, None)
            if field == "tools":
                result.pop("parallel_tool_calls", None)
    return result


def filter_model_request(request, model):
    if not isinstance(model, str) or not model.startswith("llm7/"):
        return request
    return filter_request(request, read_policy(cache_path()).get(model))


def register(ctx):
    """Hermes request middleware. Read the same capability snapshot as the updater."""
    def prepare(request, model, **kwargs):
        updated = filter_model_request(request, model)
        if updated != request:
            return {"request": updated}
    ctx.register_middleware("llm_request", prepare)
