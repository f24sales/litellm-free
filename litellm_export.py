"""Portable LiteLLM configurations containing environment references, never keys."""
from __future__ import annotations

import uuid

from catalog_store import model_cards

CONFIG_URL = "https://www.f24-sales.com/litellm-config.json"
OWNER = "f24-sales-import"
PROVIDERS = {
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "kilo": ("https://api.kilo.ai/api/gateway", "KILO_API_KEY"),
    "nous": ("https://inference-api.nousresearch.com/v1", "NOUS_API_KEY"),
    "opencode": ("https://opencode.ai/zen/v1", "OPENCODE_API_KEY"),
    "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
}
GROQ_EFFORTS = {
    "openai/gpt-oss-20b": ("high", "low"), "openai/gpt-oss-120b": ("high", "low"),
    "qwen/qwen3-32b": ("default", "none"), "qwen/qwen3.8-27b": ("high", "none"),
}


def model_id(provider, upstream, variant):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{CONFIG_URL}#{provider}/{upstream}/{variant}"))


def route_params(provider, upstream, variant):
    base, key = PROVIDERS[provider]
    params = {"model": "openai/" + upstream, "api_base": base, "api_key": "os.environ/" + key}
    if provider == "openrouter" and variant in {"think", "fast"}:
        params["extra_body"] = {"reasoning": {"enabled": variant == "think"}}
    elif provider == "groq" and variant in {"think", "fast"} and upstream in GROQ_EFFORTS:
        params["reasoning_effort"] = GROQ_EFFORTS[upstream][0 if variant == "think" else 1]
    elif variant != "base":
        raise ValueError("Unsupported route preset in export")
    if provider == "nous":
        params["extra_body"] = {"tags": ["user=free-sync"]}
    return params


def export_config(data):
    if data.get("source_type") != "model_probe":
        raise ValueError("A completed model probe is required for export")
    visible = {route["id"] for card in model_cards(data, probe_status="green")
               if card["entry_type"] == "model" for route in card["routes"]}
    models = []
    for record in data["models"].values():
        route = record["details"]
        if not record["active"] or route["id"] not in visible or route.get("research_status") != "reviewed":
            continue
        provider, upstream, variant = route["aggregator"], route["upstream_id"], route["variant"]
        if provider not in PROVIDERS:
            continue
        models.append({
            "model_name": provider + "/" + upstream + ("-" + variant if variant != "base" else ""),
            "litellm_params": route_params(provider, upstream, variant),
            "model_info": {"id": model_id(provider, upstream, variant), "managed_by": OWNER,
                           "f24_provider": provider, "f24_upstream_id": upstream, "f24_variant": variant,
                           "source": CONFIG_URL, "checked_at": route["probe_checked_at"],
                           "access_groups": ["litellm-free"]},
        })
    if not models:
        raise ValueError("No passing chat routes available for export")
    return {"model_list": sorted(models, key=lambda row: row["model_name"])}


def validate_config(document):
    """Downloaded config cannot redirect a locally supplied key to another host."""
    if not isinstance(document, dict) or set(document) != {"model_list"}:
        raise ValueError("Expected a LiteLLM model_list document")
    rows = document["model_list"]
    if not isinstance(rows, list) or not rows or len(rows) > 5000:
        raise ValueError("Invalid or empty model_list")
    ids, names = set(), set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"model_name", "litellm_params", "model_info"}:
            raise ValueError("Unexpected fields in model entry")
        info = row["model_info"]
        if not isinstance(info, dict):
            raise ValueError("Invalid model_info")
        provider, upstream, variant = info.get("f24_provider"), info.get("f24_upstream_id"), info.get("f24_variant")
        if (not isinstance(provider, str) or provider not in PROVIDERS or not isinstance(upstream, str) or not upstream
                or any(ord(c) < 32 for c in upstream) or not isinstance(variant, str) or variant not in {"base", "think", "fast"}):
            raise ValueError("Unknown provider, upstream model or preset")
        expected_name = provider + "/" + upstream + ("-" + variant if variant != "base" else "")
        if row["model_name"] != expected_name or row["litellm_params"] != route_params(provider, upstream, variant):
            raise ValueError("Model name, API endpoint, key reference or preset differs from the supported export")
        if (info.get("managed_by") != OWNER or info.get("source") != CONFIG_URL
                or info.get("id") != model_id(provider, upstream, variant)
                or info.get("access_groups") != ["litellm-free"]
                or set(info) != {"id", "managed_by", "f24_provider", "f24_upstream_id", "f24_variant", "source", "checked_at", "access_groups"}):
            raise ValueError("Invalid import ownership metadata")
        if info["id"] in ids or row["model_name"] in names:
            raise ValueError("Duplicate model in import")
        ids.add(info["id"])
        names.add(row["model_name"])
    return rows


def env_template():
    lines = ["# https://www.f24-sales.com/ — LiteLLM import", "# Copy to .env; never commit real keys.",
             "# Configure only the providers you use. The importer skips blank keys."]
    lines.extend(key + "=" for _, key in PROVIDERS.values())
    lines.extend(["", "# Optional: HTTP API import into the LiteLLM database", "LITELLM_BASE_URL=http://127.0.0.1:4000",
                  "LITELLM_ADMIN_KEY=", "", "# Direct SQL import outside a LiteLLM container only:",
                  "# Use the EXISTING database URL and encryption key of your proxy.",
                  "DATABASE_URL=", "LITELLM_SALT_KEY=", "LITELLM_MASTER_KEY=", ""])
    return "\n".join(lines)
