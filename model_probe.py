#!/usr/bin/env python3
"""Sequential per-aggregator model probes for the local LiteLLM backend.

The runner never writes credentials to the catalog.  Each run keeps a dated
result and atomically refreshes ``model_probe_results.json`` as the stable
availability SOT used by the website.  LiteLLM deployment definitions remain
in ``free_models.json`` for the synchronizer.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from free_sync import read_env
from green_keys import sync as sync_green_keys


ROOT = Path(__file__).resolve().parent
PROMPT = "Howdy. Reply with exactly OK."
OPENCODE_ACCESS_NOTE = (
    "OpenCode free routes must be used through the OpenCode software; direct "
    "LiteLLM/API calls are not accepted."
)
SECRET_PATTERN = re.compile(r"\b(?:sk|key|token)-[A-Za-z0-9._-]{8,}\b")
PERMANENT_FAILURE_MARKERS = (
    "no longer free",
    "paid variant",
    "requires payment",
    "purchase",
    "buy access",
    "billing required",
    "not available for free",
    "only be used from within opencode",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def enabled(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def endpoint(env: dict[str, str]) -> str:
    raw = (env.get("PROBE_LITELLM_BASE_URL") or env.get("LITELLM_BASE_URL") or "https://127.0.0.1").strip()
    port = (env.get("PROBE_LITELLM_PORT") or env.get("LITELLM_PORT") or "2001").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Probe LiteLLM URL must be an http(s) URL without credentials or query parameters")
    if parsed.port is None and port:
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("Probe LiteLLM port must be between 1 and 65535")
        parsed = parsed._replace(netloc=f"{parsed.hostname}:{port}")
    return urlunsplit(parsed).rstrip("/")


def _string_list(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _positive_int(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def catalog_snapshot(entry: dict[str, object], model: str, aggregator: str) -> dict[str, object]:
    """Copy only public, non-secret deployment facts into the probe SOT."""
    deployment = entry.get("deployment") if isinstance(entry.get("deployment"), dict) else {}
    info = deployment.get("model_info") if isinstance(deployment, dict) and isinstance(deployment.get("model_info"), dict) else {}
    catalog = info.get("free_sync_catalog") if isinstance(info.get("free_sync_catalog"), dict) else {}
    architecture = catalog.get("architecture") if isinstance(catalog.get("architecture"), dict) else {}
    reasoning = catalog.get("reasoning") if isinstance(catalog.get("reasoning"), dict) else {}
    alias = deployment.get("model_name") if isinstance(deployment, dict) else None
    return {
        "id": info.get("id") if isinstance(info.get("id"), str) else None,
        "upstream_id": info.get("free_sync_model_id") if isinstance(info.get("free_sync_model_id"), str) else model,
        "alias": alias if isinstance(alias, str) else model,
        "variant": info.get("free_sync_variant") if isinstance(info.get("free_sync_variant"), str) else "base",
        "author": entry.get("model_author") if isinstance(entry.get("model_author"), str) else None,
        "reported_provider": entry.get("provider") if isinstance(entry.get("provider"), str) else None,
        "reported_providers": _string_list(entry.get("providers")),
        "parameters": _string_list(entry.get("supported_parameters")),
        "context_tokens": _positive_int(info.get("max_input_tokens")),
        "max_output_tokens": _positive_int(info.get("max_output_tokens")),
        "reasoning": {
            "supported": bool(reasoning) or "reasoning" in _string_list(entry.get("supported_parameters")),
            "mandatory": reasoning.get("mandatory") if type(reasoning.get("mandatory")) is bool else None,
            "default_enabled": reasoning.get("default_enabled") if type(reasoning.get("default_enabled")) is bool else None,
            "budget_supported": reasoning.get("supports_max_tokens") if type(reasoning.get("supports_max_tokens")) is bool else None,
            "effort_levels": _string_list(reasoning.get("supported_efforts")),
            "default_effort": reasoning.get("default_effort") if isinstance(reasoning.get("default_effort"), str) else None,
        },
        "input_modalities": _string_list(architecture.get("input_modalities")),
        "output_modalities": _string_list(architecture.get("output_modalities")),
        "created_at": entry.get("created_at") if isinstance(entry.get("created_at"), str) else None,
        "updated_at": entry.get("updated_at") if isinstance(entry.get("updated_at"), str) else None,
    }


def load_models(path: Path) -> list[dict[str, object]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    models = document.get("models") if isinstance(document, dict) else None
    if not isinstance(models, dict) or not models:
        raise ValueError(f"Invalid or empty model catalog: {path}")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in models.values():
        if not isinstance(entry, dict):
            continue
        deployment = entry.get("deployment") or {}
        model = str(deployment.get("model_name") or "").strip()
        aggregator = str(entry.get("aggregator") or model.split("/", 1)[0]).strip()
        if not model or not aggregator or model in seen:
            continue
        seen.add(model)
        result.append({"model": model, "aggregator": aggregator, "catalog": catalog_snapshot(entry, model, aggregator)})
    if not result:
        raise ValueError(f"Catalog contains no usable deployments: {path}")
    # Keep discovery chronology in the SOT.  The website deliberately renders
    # the resulting first-seen order newest-first, as before.
    return sorted(result, key=lambda item: (
        str(item["catalog"].get("created_at") or "9999"),
        str(item["catalog"].get("updated_at") or "9999"),
        str(item["model"]),
    ))


def sanitize(value: object, secrets: set[str], limit: int = 2000) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        text = str(value or "")
    for secret in sorted((x for x in secrets if x), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = SECRET_PATTERN.sub("[REDACTED]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def error_detail(response: httpx.Response | None, exc: BaseException | None, secrets: set[str]) -> dict[str, object]:
    if exc is not None:
        return {
            "kind": "transport",
            "exception": type(exc).__name__,
            "message": sanitize(str(exc) or type(exc).__name__, secrets),
        }
    assert response is not None
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            detail = {key: error[key] for key in ("type", "code", "message") if key in error}
        else:
            detail = {"message": payload.get("message") or payload}
    else:
        detail = {"message": payload}
    detail["kind"] = "http" if response.status_code >= 400 else "invalid_response"
    return {str(key): sanitize(value, secrets) if key != "kind" else value for key, value in detail.items()}


def classify(status: int | None, detail: dict[str, object] | None = None, transport: bool = False) -> tuple[str, str]:
    if transport:
        return "yellow", "unknown"
    if status is None or status in {408, 425, 429} or status >= 500:
        return "yellow", "transient"
    text = json.dumps(detail or {}, ensure_ascii=False).lower()
    if any(marker in text for marker in PERMANENT_FAILURE_MARKERS):
        return "red", "confirmed_failure"
    return "yellow", "unknown"


def reply_present(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    for value in (message.get("content"), message.get("reasoning_content"), first.get("text")):
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and value:
            return True
    return False


async def probe_one(client: httpx.AsyncClient, base: str, key: str, item: dict[str, str], timeout: float, secrets: set[str]) -> dict[str, object]:
    started = time.monotonic()
    response: httpx.Response | None = None
    error: BaseException | None = None
    status: int | None = None
    try:
        response = await client.post(
            base + "/v1/chat/completions",
            headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
            json={
                "model": item["model"],
                "messages": [{"role": "user", "content": PROMPT}],
                "max_tokens": 8,
                "stream": False,
            },
            timeout=timeout,
        )
        status = response.status_code
        if 200 <= status < 300:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if reply_present(payload):
                result: dict[str, object] = {"status": "green", "assessment": "success", "reply_received": True}
            else:
                detail = error_detail(response, None, secrets)
                result = {"status": "yellow", "assessment": "unknown", "reply_received": False, "error": detail}
        else:
            detail = error_detail(response, None, secrets)
            status_label, assessment_label = classify(status, detail)
            result = {"status": status_label, "assessment": assessment_label, "reply_received": False, "error": detail}
    except Exception as exc:  # one provider failure must not stop the other workers
        error = exc
        result = {"status": "yellow", "assessment": "unknown", "reply_received": False, "error": error_detail(None, error, secrets)}
    result.update({
        "aggregator": item["aggregator"],
        "model": item["model"],
        "catalog": item.get("catalog") or {},
        "access_note": OPENCODE_ACCESS_NOTE if item["aggregator"] == "opencode" else None,
        "http_status": status,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "checked_at": now(),
    })
    return result


async def run(args: argparse.Namespace, env: dict[str, str], models: list[dict[str, str]], output: Path) -> int:
    base = args.base_url or endpoint(env)
    key = env.get("PROBE_LITELLM_API_KEY") or env.get("CLIENT_KEY", "")
    if not key:
        raise ValueError("No probe key configured; set CLIENT_KEY or PROBE_LITELLM_API_KEY")
    secrets = {value for value in env.values() if isinstance(value, str) and len(value) >= 8}
    verify = enabled(env.get("PROBE_TLS_VERIFY", "0"))
    timeout = httpx.Timeout(args.timeout, connect=min(10.0, args.timeout))
    limits = httpx.Limits(max_connections=max(1, len(set(item["aggregator"] for item in models))), max_keepalive_connections=20)
    async with httpx.AsyncClient(verify=verify, trust_env=False, timeout=timeout, limits=limits) as client:
        catalog_response = await client.get(base + "/v1/models", headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
        catalog_response.raise_for_status()
        catalog_data = catalog_response.json()
        live_ids = {str(row.get("id")) for row in catalog_data.get("data", []) if isinstance(row, dict) and row.get("id")}
        groups: dict[str, list[dict[str, str]]] = defaultdict(list)
        for item in models:
            item = dict(item)
            item["listed_in_proxy"] = str(item["model"] in live_ids).lower()
            groups[item["aggregator"]].append(item)

        async def worker(aggregator: str, group: list[dict[str, str]]) -> list[dict[str, object]]:
            results = []
            for number, item in enumerate(group, 1):
                result = await probe_one(client, base, key, item, args.timeout, secrets)
                result["listed_in_proxy"] = item["listed_in_proxy"] == "true"
                results.append(result)
                print(f"[{aggregator}] {number}/{len(group)} {result['model']} -> {result['status']}", flush=True)
                await asyncio.sleep(args.delay)
            return results

        batches = await asyncio.gather(*(worker(aggregator, group) for aggregator, group in sorted(groups.items())))

    result_rows = [row for batch in batches for row in batch]
    summary: dict[str, dict[str, int]] = {}
    for row in result_rows:
        stats = summary.setdefault(str(row["aggregator"]), {
            "total": 0, "green": 0, "yellow": 0, "red": 0,
            "confirmed_failures": 0, "transient": 0, "unknown": 0,
        })
        stats["total"] += 1
        stats[str(row["status"])] += 1
        if row["assessment"] == "confirmed_failure":
            stats["confirmed_failures"] += 1
        elif row["assessment"] in {"transient", "unknown"}:
            stats[str(row["assessment"])] += 1
    document = {
        "schema_version": 1,
        "source_type": "model_probe",
        "generated_at": now(),
        "endpoint": base + "/v1/chat/completions",
        "prompt": PROMPT,
        "delay_seconds": args.delay,
        "timeout_seconds": args.timeout,
        "proxy_catalog_count": len(live_ids),
        "catalog_count": len(models),
        "catalog_only_models": sorted(item["model"] for item in models if item["model"] not in live_ids),
        "proxy_only_models": sorted(live_ids - {item["model"] for item in models}),
        "sot": {
            "access_group": env.get("ACCESS_GROUP") or "litellm-free",
            "client_key_alias": env.get("CLIENT_KEY_ALIAS") or "litellm-free",
            "bearer_env": "CLIENT_KEY",
            "research_file": "model_metadata.json",
            "deployment_file": "free_models.json",
        },
        "aggregator_notes": {"opencode": OPENCODE_ACCESS_NOTE},
        "summary": summary,
        "models": {str(row["model"]): row for row in sorted(result_rows, key=lambda row: (
            str((row.get("catalog") or {}).get("created_at") or "9999"),
            str((row.get("catalog") or {}).get("updated_at") or "9999"),
            str(row["model"]),
        ))},
    }
    def persist(path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            temporary = path.with_name("." + path.name + ".tmp")
            temporary.write_text(json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)

    persist(output)
    stable = Path(env.get("PROBE_SOT_FILE") or ROOT / "model_probe_results.json").expanduser()
    if not stable.is_absolute():
        stable = ROOT / stable
    if stable.resolve() != output.resolve():
        persist(stable)
    if args.providers:
        document["key_sync"] = {"status": "skipped", "reason": "provider_subset"}
    else:
        try:
            from review_models import run_review
            document["model_review"] = await asyncio.to_thread(run_review, ROOT, document, env)
        except Exception as exc:
            document["model_review"] = {"status": "error", "exception": type(exc).__name__}
            print(f"model review failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        try:
            document["key_sync"] = sync_green_keys(ROOT)
        except Exception as exc:  # A key restriction failure must not erase the scan result.
            document["key_sync"] = {"status": "error", "exception": type(exc).__name__}
            print(f"green key sync failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        persist(output)
        if stable.resolve() != output.resolve():
            persist(stable)
    total_green = sum(1 for row in result_rows if row["status"] == "green")
    print(f"probe complete: {total_green}/{len(result_rows)} green; results={output}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--models-file", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--delay", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--provider", action="append", dest="providers")
    args = parser.parse_args()
    if args.delay < 0 or args.timeout <= 0:
        parser.error("--delay must be non-negative and --timeout must be positive")
    env, _ = read_env(Path(args.env_file).expanduser())
    model_path = Path(args.models_file or env.get("MODELS_FILE") or ROOT / "free_models.json").expanduser()
    if not model_path.is_absolute():
        model_path = ROOT / model_path
    configured_output = args.output or env.get("PROBE_RESULTS_FILE")
    if configured_output:
        output = Path(configured_output).expanduser()
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = ROOT / f"model_probe_results-{stamp}.json"
    if not output.is_absolute():
        output = ROOT / output
    models = load_models(model_path)
    if args.providers:
        selected = set(args.providers)
        models = [item for item in models if item["aggregator"] in selected]
        if not models:
            raise SystemExit("No catalog models match --provider")
    return asyncio.run(run(args, env, models, output))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, httpx.HTTPError) as exc:
        print(f"model probe failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
