#!/usr/bin/env python3
"""Restrict the two f24-sales LiteLLM virtual keys to green probe routes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import ssl
from urllib.parse import urlsplit, urlunsplit

import httpx

from free_sync import read_env


ROOT = Path(__file__).resolve().parent
CLIENT_ROUTES = ["/v1/models", "/v1/chat/completions"]
WEB_ROUTES = ["/v1/chat/completions"]


def endpoint(env: dict[str, str]) -> str:
    raw = (env.get("LITELLM_BASE_URL") or "https://127.0.0.1").strip()
    port = (env.get("LITELLM_PORT") or "2001").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Invalid LITELLM_BASE_URL")
    if parsed.port is None:
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("Invalid LITELLM_PORT")
        parsed = parsed._replace(netloc=f"{parsed.hostname}:{port}")
    return urlunsplit(parsed).rstrip("/")


def green_model_names(path: Path = ROOT / "model_probe_results.json") -> list[str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("source_type") != "model_probe" or not isinstance(document.get("models"), dict):
        raise ValueError("The probe SOT is missing or not a model-probe document")
    result = []
    seen = set()
    for row in document["models"].values():
        model = row.get("model") if isinstance(row, dict) else None
        if isinstance(row, dict) and row.get("status") == "green" and isinstance(model, str) and model and model not in seen:
            result.append(model)
            seen.add(model)
    if not result:
        raise ValueError("Probe SOT contains no green model routes; refusing to empty virtual keys")
    return result


def key_rows(client: httpx.Client) -> list[dict]:
    rows = []
    for page in range(1, 10001):
        response = client.get("/key/list", params={"return_full_object": "true", "page": page, "size": 100})
        response.raise_for_status()
        payload = response.json()
        page_rows = payload.get("keys")
        if not isinstance(page_rows, list):
            raise ValueError("LiteLLM /key/list response is missing keys")
        rows.extend(row for row in page_rows if isinstance(row, dict))
        if len(page_rows) < 100 or page >= int(payload.get("total_pages") or page):
            return rows
    raise ValueError("LiteLLM /key/list pagination limit exceeded")


def update_key(client: httpx.Client, row: dict, alias: str, models: list[str], routes: list[str]) -> dict:
    key_hash = row.get("token") or row.get("key")
    if not isinstance(key_hash, str) or len(key_hash) != 64:
        raise ValueError(f"{alias}: key hash missing from LiteLLM key list")
    response = client.post("/key/update", json={"key": key_hash, "models": models, "allowed_routes": routes})
    response.raise_for_status()
    info = client.get("/key/info", params={"key": key_hash}).json().get("info")
    if not isinstance(info, dict):
        raise ValueError(f"{alias}: key readback is invalid")
    if set(info.get("models") or []) != set(models) or info.get("allowed_routes") != routes:
        raise ValueError(f"{alias}: green model restriction readback failed")
    return {"models": len(models), "routes": routes}


def sync(root: Path = ROOT) -> dict:
    env, _ = read_env(root / ".env")
    sot_path = Path(env.get("PROBE_SOT_FILE") or root / "model_probe_results.json").expanduser()
    if not sot_path.is_absolute():
        sot_path = root / sot_path
    models = green_model_names(sot_path)
    client_alias = env.get("CLIENT_KEY_ALIAS") or "litellm-free"
    web_alias = env.get("FREE_WEB_CLIENT_KEY_ALIAS") or "litellm-free-web"
    verify = str(env.get("LITELLM_TLS_VERIFY", "0")).strip().lower() in {"1", "true", "yes", "on"}
    with httpx.Client(base_url=endpoint(env), headers={"Authorization": "Bearer " + env["LITELLM_ADMIN_KEY"],
                                                        "Accept": "application/json"},
                     verify=verify, trust_env=False, timeout=45, follow_redirects=False) as client:
        rows = key_rows(client)
        by_alias = {row.get("key_alias"): row for row in rows if isinstance(row.get("key_alias"), str)}
        result = {}
        for alias, routes in ((client_alias, CLIENT_ROUTES), (web_alias, WEB_ROUTES)):
            row = by_alias.get(alias)
            if row is None:
                raise ValueError(f"Virtual key not found: {alias}")
            result[alias] = update_key(client, row, alias, models, routes)
    return {"status": "ok", "green_models": len(models), "keys": result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(sync(args.root), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
