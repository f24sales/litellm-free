"""Portable LiteLLM configurations containing environment references, never keys."""
from __future__ import annotations

import hashlib
import json
import math
import uuid

CONFIG_URL = "https://www.f24-sales.com/litellm-config.json"
CATALOG_URL = "https://www.f24-sales.com/litellm-config.yaml"
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




def validate_metadata(value, max_bytes=131072, max_items=10000):
    """Keep descriptive model information bounded, inert JSON data."""
    if not isinstance(value, dict):
        raise ValueError("Invalid descriptive model metadata")
    count = 0

    def visit(item, depth=0):
        nonlocal count
        count += 1
        if depth > 12 or count > max_items:
            raise ValueError("Model metadata exceeds the nesting or item limit")
        if item is None or isinstance(item, (bool, int)):
            return
        if isinstance(item, float) and math.isfinite(item):
            return
        if isinstance(item, str) and len(item) <= 32768:
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, dict) and all(isinstance(key, str) and len(key) <= 256 for key in item):
            for child in item.values():
                visit(child, depth + 1)
            return
        raise ValueError("Model metadata must contain bounded JSON values")

    visit(value)
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > max_bytes:
        raise ValueError("Model metadata exceeds the size limit")


def validate_config(document):
    """Downloaded config cannot redirect a locally supplied key to another host."""
    if not isinstance(document, dict):
        raise ValueError("Expected a LiteLLM model_list document")
    if set(document) != {"model_list"}:
        expected = {"schema_version", "catalog_updated_at", "checked_at", "models", "aggregators", "model_list"}
        if (set(document) not in (expected, expected | {"research"})
                or type(document.get("schema_version")) is not int or document.get("schema_version") != 1
                or not isinstance(document.get("catalog_updated_at"), str)
                or not isinstance(document.get("checked_at"), str)
                or not isinstance(document.get("models"), list)
                or not isinstance(document.get("aggregators"), dict)):
            raise ValueError("Expected a supported published catalog or LiteLLM model_list document")
        if "research" in document:
            validate_metadata(document["research"], max_bytes=5_000_000, max_items=100000)
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
        required_info = {"id", "managed_by", "f24_provider", "f24_upstream_id", "f24_variant", "source", "checked_at", "access_groups"}
        if (info.get("managed_by") != OWNER or info.get("source") != CONFIG_URL
                or info.get("id") != model_id(provider, upstream, variant)
                or info.get("access_groups") != ["litellm-free"]
                or set(info) not in (required_info, required_info | {"f24_metadata"})
                or not isinstance(info.get("checked_at"), str)):
            raise ValueError("Invalid import ownership metadata")
        if "f24_metadata" in info:
            validate_metadata(info["f24_metadata"])
        if info["id"] in ids or row["model_name"] in names:
            raise ValueError("Duplicate model in import")
        ids.add(info["id"])
        names.add(row["model_name"])
    return rows


def model_config_fingerprint(config):
    """Hash meaningful model configuration, using the original environment references."""
    volatile = {"checked_at", "created_at", "updated_at", "researched_at",
                "identity_reviewed_at", "open_weights_checked_at"}

    def stable(value):
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items()
                    if key not in volatile and not key.startswith("probe_")}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    # Validation also ensures that secret bearers have not replaced key references.
    rows = sorted(validate_config(config), key=lambda row: row["model_name"])
    encoded = json.dumps(stable(rows), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def env_template():
    lines = ["# https://www.f24-sales.com/ — LiteLLM import", "# Copy to .env; never commit real keys.",
             "# Configure only the providers you use. The importer skips blank keys."]
    lines.extend(key + "=" for _, key in PROVIDERS.values())
    lines.extend(["", "# Optional: API import into the LiteLLM database. Set the proxy URL in config.conf.",
                  "# Master key or an admin virtual bearer with model-management scopes.",
                  "LITELLM_ADMIN_KEY=", "# Optional fallback when LITELLM_ADMIN_KEY is blank; ordinary chat keys cannot manage models.",
                  "LITELLM_VIRTUAL_KEY=", "", "# Direct SQL import outside a LiteLLM container only:",
                  "# Use the EXISTING database URL and encryption key of your proxy.",
                  "DATABASE_URL=", "LITELLM_SALT_KEY=", "LITELLM_MASTER_KEY=", "",
                  "# Optional OpenClaw webhook for independent scan/import events. Keep both values local.",
                  "IMPORT_HOOK_URL=", "IMPORT_HOOK_BEARER=", "IMPORT_HOOK_TIMEOUT_SECONDS=30", "IMPORT_HOOK_CA_FILE=",
                  "# Send import_succeeded even when the model diff is empty.",
                  "IMPORT_HOOK_ON_NO_CHANGE=false", ""])
    return "\n".join(lines)


def config_template():
    """Portable nonsecret defaults; the generated config example uses these bytes."""
    return "\n".join([
        "# Non-secret settings. Copy to config.conf; .env holds credentials.",
        "#default-preset: " + CATALOG_URL,
        "IMPORT_SOURCE_URL=" + CATALOG_URL,
        "IMPORT_SOURCE_UDS=", "IMPORT_SOURCE_CA_FILE=", "IMPORT_DELAY_SECONDS=10",
        "# Retain older configuration files in ./archiv only when explicitly enabled.",
        "LITELLM_FREE_ARCHIVE=0",
        "LITELLM_BASE_URL=https://your-litellm.example", "LITELLM_PORT=", "LITELLM_CA_FILE=",
        "LITELLM_ALLOW_HTTP=false",
        "# Optional API metadata-only mode for exactly this existing manager.",
        "IMPORT_PATCH_MANAGED_BY=",
        "# Optional exact free-sync route adoption, preserving existing model IDs.",
        "# Mutually exclusive with IMPORT_PATCH_MANAGED_BY.",
        "IMPORT_ADOPT_MANAGED_BY=",
        "# Remove obsolete imported routes only after successful import/readback.",
        "IMPORT_PRUNE=false",
        "# Optional local prerequisite after import/readback, before the success webhook.",
        '# JSON argv, without a shell: ["/absolute/path/script", "--argument"]',
        "IMPORT_PRE_SUCCESS_COMMAND_JSON=", "IMPORT_PRE_SUCCESS_TIMEOUT_SECONDS=180", "",
        "# Existing OPENAI_V1_* provider used by the twice-daily client refresh.",
        "#default-preset: litellm-free", "LITELLM_FREE_PROVIDER=litellm-free", "",
    ])
