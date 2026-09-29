#!/usr/bin/env python3
"""Sync free chat deployments through LiteLLM HTTP APIs. Python 3.11 + httpx.

No direct database access or provider SDKs. Secrets stay in .env/LiteLLM. See README for verified API limitations.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import re
import stat
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit
import uuid

import httpx

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("free-sync")
OWNER = "free-sync"
PROVIDERS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "kilo": "https://api.kilo.ai/api/gateway",
    "nous": "https://inference-api.nousresearch.com/v1",
    "opencode": "https://opencode.ai/zen/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
}
KEY_ENV = {p: p.upper() + "_API_KEY" for p in PROVIDERS}
SECRET_NAMES = {*KEY_ENV.values(), "CLIENT_KEY", "LITELLM_ADMIN_KEY"}
SECRETS: set[str] = set()
# Official Groq per-model values. Metadata must ALSO advertise reasoning support.
GROQ_EFFORTS = {
    "openai/gpt-oss-20b": ("high", "low"),
    "openai/gpt-oss-120b": ("high", "low"),
    "qwen/qwen3-32b": ("default", "none"),
    "qwen/qwen3.8-27b": ("high", "none"),
}


class Failure(Exception):
    """Safe, non-secret operational diagnostic."""


class Fatal(Failure):
    pass


class APIError(Failure):
    def __init__(self, status: int, label: str, missing_tags=False):
        self.status = status
        self.missing_tags = missing_tags
        super().__init__(f"{label}: HTTP {status} (response body suppressed)")


def digest(value) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


class Redact(logging.Filter):
    def filter(self, record):
        text = record.getMessage()
        for value in sorted(SECRETS, key=len, reverse=True):
            if value:
                text = text.replace(value, "[REDACTED]")
        record.msg = text.replace("\r", "\\r").replace("\n", "\\n")
        record.args = ()
        return True


def read_env(path: Path) -> tuple[dict, str]:
    if not path.exists():
        return {}, ""
    if path.is_symlink():
        raise Fatal("Refusing a symlink .env")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        LOG.warning(".env is accessible to other users; use chmod 600 .env")
    text = path.read_text()
    values = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*)", line)
        if not match:
            raise Fatal(f"Invalid .env syntax at line {number}")
        name, value = match.groups()
        if value.startswith(('"', "'")):
            q = value[0]
            end = value.rfind(q)
            if end == 0 or (value[end + 1:].strip() and not value[end + 1:].strip().startswith("#")):
                raise Fatal(f"Invalid quoted .env value at line {number}")
            value = value[1:end]  # literal: no shell expansion, interpolation or execution
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[name] = value
    return values, text


def atomic_write(path: Path, text: str, mode=0o600):
    if path.is_symlink():
        raise Failure("Refusing to replace a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_config(path: Path | None) -> dict:
    if not path or not path.exists():
        return {}
    try:
        # JSON is a YAML 1.2 subset: intentionally no third-party YAML parser.
        cfg = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise Fatal("Config must use JSON syntax (valid YAML 1.2); see config.yaml") from exc
    if not isinstance(cfg, dict):
        raise Fatal("Config must be an object")
    if set(cfg) & SECRET_NAMES:
        raise Fatal("Secrets belong in .env/process environment, not config")
    return cfg


class HTTP:
    def __init__(self, timeout=30, retries=3):
        self.client = httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False)
        self.retries = retries

    def request(self, method, url, *, key=None, label="HTTP request", retry=False, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if key:
            headers["Authorization"] = "Bearer " + key
        headers["Accept"] = "application/json"
        for attempt in range(self.retries + 1):
            try:
                response = self.client.request(method, url, headers=headers, **kwargs)
            except httpx.RequestError:
                if (method == "GET" or retry) and attempt < self.retries:
                    time.sleep(min(30, 2 ** attempt) + random.random())
                    continue
                raise Failure(f"{label}: transport error; write outcome may be unknown") from None
            if response.status_code == 429 or response.status_code >= 500:
                if (method == "GET" or retry) and attempt < self.retries:
                    delay = response.headers.get("Retry-After", "")
                    time.sleep(min(60, int(delay)) if delay.isdigit() else min(30, 2 ** attempt) + random.random())
                    continue
            if not 200 <= response.status_code < 300:
                raise APIError(response.status_code, label, bool(re.search(r"missing.{0,30}tags|tags.{0,30}(required|missing)", response.text, re.I)))
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                raise Failure(f"{label}: expected JSON") from None
        raise Failure(f"{label}: retries exhausted")


class Schema:
    def __init__(self, document):
        if not isinstance(document, dict) or "paths" not in document:
            raise Fatal("Proxy did not return OpenAPI")
        self.doc = document

    def resolve(self, schema):
        while "$ref" in schema:
            ref = schema["$ref"]
            if not ref.startswith("#/components/schemas/"):
                raise Fatal("Unsupported non-local OpenAPI reference")
            schema = self.doc["components"]["schemas"][ref.rsplit("/", 1)[1]]
        if "anyOf" in schema:
            candidates = [x for x in schema["anyOf"] if x.get("type") != "null"]
            if len(candidates) == 1:
                return self.resolve(candidates[0])
        return schema

    def operation(self, path, method):
        try:
            return self.doc["paths"][path][method.lower()]
        except KeyError:
            raise Fatal(f"Required API absent: {method} {path}; verify proxy version") from None

    def body(self, path, method):
        op = self.operation(path, method)
        try:
            return self.resolve(op["requestBody"]["content"]["application/json"]["schema"])
        except KeyError:
            raise Fatal(f"Unverified request schema: {method} {path}") from None

    def check_object(self, schema, data, label):
        schema = self.resolve(schema)
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in data:
                raise Fatal(f"Required schema field missing: {label}.{key}")
        for key, value in data.items():
            if key not in props:
                # Do not rely on unspecified Pydantic extra handling.
                if schema.get("additionalProperties") is not True and not isinstance(schema.get("additionalProperties"), dict):
                    raise Fatal(f"Unverified schema field: {label}.{key}")
            elif isinstance(value, dict):
                child = self.resolve(props[key])
                if "properties" in child or child.get("type") == "object":
                    self.check_object(child, value, label + "." + key)

    def check(self, path, method, data):
        self.check_object(self.body(path, method), data, path)


class Proxy:
    def __init__(self, http, base, admin, dry):
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise Fatal("Invalid LITELLM_BASE_URL")
        self.http, self.base, self.admin, self.dry = http, base.rstrip("/"), admin, dry
        self.schema = Schema(self.get("/openapi.json"))
        for path, method in [("/credentials", "get"), ("/credentials", "post"),
                             ("/credentials/{credential_name}", "patch"), ("/credentials/{credential_name}", "delete"),
                             ("/model/info", "get"), ("/model/new", "post"),
                             ("/model/{model_id}/update", "patch"), ("/model/delete", "post"),
                             ("/key/list", "get"), ("/key/info", "get"), ("/key/generate", "post"),
                             ("/key/update", "post"), ("/key/delete", "post"), ("/v1/models", "get")]:
            self.schema.operation(path, method)
        self.credentials = self.load_credentials()  # authenticated admin call, before mutations

    def get(self, path, **kwargs):
        return self.http.request("GET", self.base + path, key=self.admin, label=path, **kwargs)

    def write(self, method, path, data=None, template=None, retry=False):
        if data is not None:
            self.schema.check(template or path, method, data)
        if self.dry:
            LOG.info("DRY-RUN %s %s", method, template or path)
            return {}
        return self.http.request(method, self.base + path, key=self.admin, label=template or path,
                                 json=data, retry=retry)

    def load_credentials(self):
        data = self.get("/credentials")
        rows = data.get("credentials")
        if not isinstance(rows, list):
            raise Fatal("Unrecognized /credentials response")
        return {row["credential_name"]: row for row in rows}

    def models(self):
        # /model/info is the complete non-paginated router list in the verified API.
        data = self.get("/model/info")
        rows = data.get("data")
        if not isinstance(rows, list) or any(not isinstance(r, dict) or "model_info" not in r for r in rows):
            raise Failure("Unrecognized /model/info response; no deletions")
        return rows

    def keys(self, alias):
        result = []
        for page in range(1, 10001):
            data = self.get("/key/list", params={"key_alias": alias, "return_full_object": "true", "page": page, "size": 100})
            rows = data.get("keys")
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise Failure("Unrecognized /key/list response")
            result.extend(r for r in rows if r.get("key_alias") == alias)
            total_pages = data.get("total_pages")
            if (isinstance(total_pages, int) and page >= total_pages) or len(rows) < 100:
                return result
        raise Failure("Key pagination exceeded limit")


def catalog_rows(data):
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise Failure("Provider catalog has no OpenAI data array; no deletions")
    rows = data["data"]
    if any(not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r["id"] for r in rows):
        raise Failure("Invalid model IDs in provider catalog; no deletions")
    if len({r["id"] for r in rows}) != len(rows):
        raise Failure("Duplicate IDs in provider catalog; no deletions")
    return rows


def read_catalog(http, proxy, provider, cfg, key=None, validate=False):
    # Local candidate credentials MUST be validated against the provider, not against
    # an already working stored key on a proxy route.
    path = cfg.get("proxy_catalog_path") if not validate else None
    if path:
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "?" in path:
            raise Failure("proxy_catalog_path must be a local proxy path")
        if path in {"/models", "/v1/models"}:
            raise Failure("LiteLLM /v1/models is not an upstream catalog (no prices/new upstream IDs)")
        return catalog_rows(proxy.get(path))
    if provider == "groq" and not key:
        raise Failure("Groq catalog needs a local key or configured proxy_catalog_path; stored credential is masked")
    base = cfg.get("api_base", PROVIDERS[provider]).rstrip("/")
    if urlsplit(base).scheme != "https":
        raise Failure("Provider api_base must use HTTPS")
    return catalog_rows(http.request("GET", base + "/models", key=key, label=provider + " catalog"))


def zero(value):
    if value is None or isinstance(value, bool):
        return False
    try:
        n = Decimal(str(value))
        return n.is_finite() and n == 0
    except InvalidOperation:
        return False


def positive(*values):
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def matches(patterns, text):
    return any(re.search(p, text) for p in patterns)


def free_models(provider, rows, cfg):
    suffix_present = any(r["id"].endswith(":free") for r in rows)
    result = []
    for row in rows:
        name, price = row["id"], row.get("pricing") or {}
        if row.get("active") is False:
            continue
        if provider == "groq":
            free = not re.search(r"whisper|tts|guard|speech|transcri|orpheus", name, re.I)
        elif provider == "nous":
            free = name.endswith(":free") or (not suffix_present and matches(cfg.get("free_allowlist", []), name))
        elif provider == "opencode":
            # Zen exposes paid and free models together and marks free entries
            # with a ``-free`` suffix (some compatible catalogs use ``:free``).
            suffixes = cfg.get("free_suffixes", ["-free", ":free"])
            free = any(name.endswith(suffix) for suffix in suffixes
                       if isinstance(suffix, str) and suffix)
        elif provider == "nvidia":
            # NVIDIA's OpenAI-compatible catalog does not expose reliable
            # pricing/free metadata. Use an explicit audited allowlist instead
            # of treating the entire catalog as free.
            allowlist = cfg.get("free_allowlist", [])
            if not isinstance(allowlist, list) or not allowlist:
                raise Failure("NVIDIA catalog requires an explicit free_allowlist")
            free = matches(allowlist, name)
        else:
            free = (provider == "openrouter" and name.endswith(":free")) or (zero(price.get("prompt")) and zero(price.get("completion")))
            if provider == "kilo" and free:
                # Reject extra charges too; unknown or negative sentinel prices are not free.
                free = all(zero(v) for k, v in price.items() if k != "overrides") and not price.get("overrides")
        if not free:
            continue
        output = (row.get("architecture") or {}).get("output_modalities")
        if output is not None and "text" not in output:
            continue
        if cfg.get("allowlist") and not matches(cfg["allowlist"], name):
            continue
        if matches(cfg.get("denylist", []), name):
            continue
        result.append(row)
    if provider == "nous" and not suffix_present and not cfg.get("free_allowlist"):
        raise Failure("Nous catalog has no :free IDs; configure a verified free_allowlist; no deletions")
    return result


def desired_models(provider, rows, cfg, config, group):
    result = {}
    for row in free_models(provider, rows, cfg):
        upstream = row["id"]
        name = provider + "/" + upstream
        # Explicit routing-group merges only. Deployment IDs remain provider-specific.
        for rule in config.get("merge_rules", []):
            if name in rule.get("members", []):
                name = rule["name"]
                break
        top = row.get("top_provider") or {}
        max_in = positive(row.get("max_input_tokens"), row.get("context_window"), row.get("context_length"), top.get("context_length"))
        max_out = positive(row.get("max_output_tokens"), row.get("max_completion_tokens"), top.get("max_completion_tokens"))
        supported = row.get("supported_parameters") or []
        reasoning = row.get("reasoning") or {}
        supports_reasoning = ("reasoning" in supported or "reasoning_effort" in supported
                              or "include_reasoning" in supported or bool(reasoning)
                              or (row.get("capabilities") or {}).get("reasoning") is True)
        variants = [("base", "", {})]
        enabled = cfg.get("reasoning_variants", config.get("reasoning_variants", True))
        if enabled and supports_reasoning and provider == "openrouter" and not reasoning.get("mandatory", False):
            variants = [("think", "-think", {"extra_body": {"reasoning": {"enabled": True}}}),
                        ("fast", "-fast", {"extra_body": {"reasoning": {"enabled": False}}})]
        elif enabled and supports_reasoning and provider == "groq":
            efforts = (row.get("reasoning") or {}).get("supported_efforts")
            pair = GROQ_EFFORTS.get(upstream)
            if pair and (not efforts or all(v in efforts for v in pair)):
                variants = [("think", "-think", {"reasoning_effort": pair[0]}),
                            ("fast", "-fast", {"reasoning_effort": pair[1]})]
            else:
                LOG.warning("Groq reasoning values unverified for %s; keeping base variant", upstream)
        for variant, suffix, extra in variants:
            identity = provider + "/" + upstream + "/" + variant
            deployment_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "litellm-free:" + identity))
            params = {"model": "openai/" + upstream, "litellm_credential_name": "free-sync-" + provider,
                      "input_cost_per_token": 0, "output_cost_per_token": 0,
                      "rpm": cfg.get("rpm"), "tpm": cfg.get("tpm"), "extra_body": {}, "reasoning_effort": None}
            if provider == "nous" and cfg.get("tags_mode", "auto") == "always":
                params["extra_body"] = {"tags": ["user=free-sync"]}
            params.update(extra)
            info = {"id": deployment_id, "access_groups": [group], "managed_by": OWNER,
                    "free_sync_provider": provider, "free_sync_model_id": upstream, "free_sync_variant": variant,
                    "max_input_tokens": max_in, "max_output_tokens": max_out,
                    "supported_parameters": supported, "input_cost_per_token": 0, "output_cost_per_token": 0,
                    "free_sync_catalog": {k: row[k] for k in
                        ("owned_by", "provider", "providers", "architecture", "capabilities", "reasoning", "pricing")
                        if k in row}}
            value = {"model_name": name + suffix, "litellm_params": params, "model_info": info}
            info["free_sync_hash"] = digest(value)
            result[deployment_id] = value
    return result


def managed(row, provider=None):
    info = row.get("model_info") or {}
    return info.get("managed_by") == OWNER and (provider is None or info.get("free_sync_provider") == provider)


def equal_deployment(old, new):
    if old.get("model_name") != new["model_name"]:
        return False
    params = old.get("litellm_params") or {}
    if any(params.get(k) for k in ("api_key", "api_base")):
        raise Failure("Managed deployment has inline credentials; remove them via admin API before syncing")
    return all(params.get(k) == v for k, v in new["litellm_params"].items()) and all(
        (old.get("model_info") or {}).get(k) == v for k, v in new["model_info"].items())


def key_hash(row):
    value = row.get("token") or row.get("key")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise Failure("Key API did not supply a SHA-256 identifier")
    return value


def load_green_probe_models(path: Path) -> list[str] | None:
    """Read the last full probe without ever reading a bearer from it."""
    if not path.exists():
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("source_type") != "model_probe" or not isinstance(document.get("models"), dict):
        raise Failure("Probe SOT is invalid; refusing to broaden the client key")
    result = []
    seen = set()
    for row in document["models"].values():
        model = row.get("model") if isinstance(row, dict) else None
        if isinstance(row, dict) and row.get("status") == "green" and isinstance(model, str) and model and model not in seen:
            result.append(model)
            seen.add(model)
    if not result:
        raise Failure("Probe SOT has no green models; refusing to broaden or empty the client key")
    return result


def restricted(info, group, models=None):
    model_scope = info.get("models") or []
    expected = [group] if models is None else models
    model_match = model_scope == [group] if models is None else set(model_scope) == set(models)
    return (model_match and not info.get("team_id") and not info.get("organization_id")
            and not info.get("project_id") and not info.get("access_group_ids") and not info.get("aliases")
            and not info.get("blocked") and not info.get("allowed_passthrough_routes")
            and set(info.get("allowed_routes") or []) == {"/v1/models", "/v1/chat/completions"})


class Sync:
    def __init__(self, proxy, config, env, args):
        self.proxy, self.config, self.env, self.args = proxy, config, env, args
        self.errors = 0
        self.scrubbable = set()
        self.summary = {p: dict(created=0, updated=0, deleted=0, unchanged=0) for p in PROVIDERS}
        self.group = env.get("ACCESS_GROUP") or config.get("access_group", "litellm-free")
        self.alias = env.get("CLIENT_KEY_ALIAS") or config.get("client_key_alias", "litellm-free")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.group):
            raise Fatal("ACCESS_GROUP must be a literal name, not a wildcard")
        probe_path = Path(env.get("PROBE_SOT_FILE") or ROOT / "model_probe_results.json").expanduser()
        if not probe_path.is_absolute():
            probe_path = ROOT / probe_path
        self.green_models = load_green_probe_models(probe_path)
        self.client_target_models = None
        self.rows = proxy.models()

    def error(self, message):
        self.errors += 1
        LOG.error("%s", message)

    def credential(self, provider, cfg):
        name = "free-sync-" + provider
        old = self.proxy.credentials.get(name)
        key = self.env.get(KEY_ENV[provider], "").strip()
        if old and (old.get("credential_info") or {}).get("managed_by") != OWNER:
            raise Failure(provider + ": credential name is already owned by another application")
        if not key:
            if not old:
                LOG.warning("%s: no local or stored provider credential; skipped", provider)
                return None, False
            rows = read_catalog(self.proxy.http, self.proxy, provider, cfg)
            return rows, False
        # A public /models endpoint returning 200 proves reachability/schema, not key validity.
        rows = read_catalog(self.proxy.http, self.proxy, provider, cfg, key=key, validate=True)
        if provider == "openrouter":
            self.proxy.http.request("GET", cfg.get("api_base", PROVIDERS[provider]).rstrip("/") + "/key",
                                    key=key, label="OpenRouter key validation")
        elif provider in {"kilo", "nous", "opencode", "nvidia"}:
            LOG.warning("%s: public catalog HTTP 200 does not prove key validity; verify authentication with inference", provider)
        base = cfg.get("api_base", PROVIDERS[provider]).rstrip("/")
        fingerprint = digest(key)[:8]
        old_info = (old or {}).get("credential_info") or {}
        info = {**old_info, "managed_by": OWNER, "provider": provider,
                "api_key_sha256_8": fingerprint, "api_base_sha256": digest(base)}
        changed = not old or any(old_info.get(k) != info[k] for k in ("api_key_sha256_8", "api_base_sha256"))
        if changed:
            body = {"credential_name": name, "credential_values": {"api_key": key, "api_base": base}, "credential_info": info}
            if old:
                self.proxy.write("PATCH", "/credentials/" + quote(name, safe=""), body,
                                 template="/credentials/{credential_name}", retry=True)
            else:
                self.proxy.write("POST", "/credentials", body)
            LOG.info("%s: credential %s (sha256:%s)", provider, "updated" if old else "created", fingerprint)
            if not self.args.dry_run:
                persisted = self.proxy.load_credentials().get(name, {})
                if (persisted.get("credential_info") or {}).get("api_key_sha256_8") != fingerprint:
                    raise Failure(provider + ": credential readback failed")
                self.proxy.credentials[name] = persisted
        # Verify the no-local-key catalog path before allowing scrub.
        if provider != "groq" or cfg.get("proxy_catalog_path"):
            read_catalog(self.proxy.http, self.proxy, provider, cfg)
            self.scrubbable.add(KEY_ENV[provider])
        return rows, changed

    def sync_provider(self, provider, cfg):
        try:
            rows, rotated = self.credential(provider, cfg)
            if rows is None:
                return
            desired = desired_models(provider, rows, cfg, self.config, self.group)
            # Preserve verified tags in the local desired catalog as well as in deployments.
            for row in self.rows:
                ident = str(row.get("model_info", {}).get("id"))
                prior = row.get("model_info", {}).get("free_sync_nous_tags")
                if provider == "nous" and ident in desired and cfg.get("tags_mode", "auto") == "auto" and prior in {"required", "not-required"}:
                    desired[ident]["model_info"]["free_sync_nous_tags"] = prior
                    if prior == "required":
                        desired[ident]["litellm_params"]["extra_body"] = {"tags": ["user=free-sync"]}
            desired = self.journal.plan(provider, desired, self.group, self.alias)
            existing = {str(r["model_info"].get("id")): r for r in self.rows if managed(r, provider)}
            occupied = {str(r.get("model_info", {}).get("id")): r for r in self.rows}
            for ident, body in desired.items():
                if ident in occupied and not managed(occupied[ident], provider):
                    raise Failure(provider + ": deployment ID collides with a foreign model")
                # Sharing a routing name with foreign deployments could leak paid routes.
                if any(r.get("model_name") == body["model_name"] and not managed(r) for r in self.rows):
                    raise Failure(provider + ": model name collides with an unmanaged deployment")
                old = existing.get(ident)
                # Preserve an already verified Nous tags result through ordinary syncs.
                if old and provider == "nous" and cfg.get("tags_mode", "auto") == "auto":
                    prior = (old.get("model_info") or {}).get("free_sync_nous_tags")
                    if prior in {"required", "not-required"}:
                        body["model_info"]["free_sync_nous_tags"] = prior
                        if prior == "required":
                            body["litellm_params"]["extra_body"] = {"tags": ["user=free-sync"]}
                # PATCH after credential rotation recreates router deployment clients; no restart API assumptions.
                if old and equal_deployment(old, body) and not rotated:
                    self.summary[provider]["unchanged"] += 1
                    continue
                if old:
                    self.proxy.write("PATCH", "/model/" + quote(ident, safe="") + "/update", body,
                                     template="/model/{model_id}/update", retry=True)
                    self.summary[provider]["updated"] += 1
                else:
                    self.proxy.write("POST", "/model/new", body)
                    self.summary[provider]["created"] += 1
            # Only after the complete fetch/normalization/upsert succeeds is deletion permitted.
            for ident in existing.keys() - desired.keys():
                if self.args.no_delete:
                    LOG.info("%s: --no-delete keeps obsolete deployment %s", provider, ident)
                    continue
                self.proxy.write("POST", "/model/delete", {"id": ident})
                self.summary[provider]["deleted"] += 1
        except Failure as exc:
            self.error(str(exc))
        except (KeyError, TypeError, ValueError):
            self.error(provider + ": unexpected catalog/schema data; remaining changes stopped")

    def forget(self):
        for requested in self.args.forget or []:
            provider = requested.removeprefix("free-sync-")
            if provider not in PROVIDERS:
                self.error("--forget only accepts provider names or free-sync-<provider>")
                continue
            name = "free-sync-" + provider
            if name not in self.proxy.credentials:
                continue
            try:
                if self.proxy.credentials[name].get("credential_info", {}).get("managed_by") != OWNER:
                    raise Failure("Refusing to forget an unmanaged credential")
                refs = [r for r in self.rows if r.get("litellm_params", {}).get("litellm_credential_name") == name]
                if any(not managed(r, provider) for r in refs):
                    raise Failure(provider + ": foreign deployment references credential; cannot delete")
                if refs and not self.args.forget_cascade:
                    raise Failure(provider + ": deployments still reference credential; use --forget-cascade")
                if self.args.no_delete:
                    LOG.info("--no-delete keeps credential and referencing deployments for %s", provider)
                    continue
                for row in refs:
                    self.proxy.write("POST", "/model/delete", {"id": row["model_info"]["id"]})
                self.proxy.write("DELETE", "/credentials/" + name, template="/credentials/{credential_name}")
                self.journal.plan(provider, {}, self.group, self.alias)
            except Failure as exc:
                self.error(str(exc))

    def client(self):
        target_models = self.client_model_scope()
        self.client_target_models = target_models
        supplied = self.env.get("CLIENT_KEY", "").strip()
        keys = self.proxy.keys(self.alias)
        chosen = None
        if supplied:
            if supplied == self.proxy.admin:
                raise Failure("CLIENT_KEY must not equal the admin key")
            if not supplied.startswith("sk-") or len(supplied) < 16 or any(c.isspace() for c in supplied):
                raise Failure("CLIENT_KEY must be a valid sk- token (minimum 16 characters)")
            try:
                response = self.proxy.get("/key/info", params={"key": digest(supplied)})
                info = response.get("info")
                if not isinstance(info, dict):
                    raise Failure("Invalid key info response")
                if info.get("key_alias") != self.alias:
                    raise Failure("CLIENT_KEY already belongs to a different alias; refusing takeover")
                chosen = next((r for r in keys if key_hash(r) == digest(supplied)), None)
                if chosen is None:
                    raise Failure("Key info and alias listing disagree")
            except APIError as exc:
                if exc.status != 404:
                    raise
        elif keys:
            LOG.warning("Local CLIENT_KEY absent; stored hashes cannot recover the bearer; rotating alias")
        if chosen is None:
            if keys and self.args.no_delete:
                raise Failure("Client rotation requires deletion; --no-delete preserves the old key")
            body = {"key_alias": self.alias, "models": target_models,
                    "metadata": {"managed_by": OWNER}, "allowed_routes": ["/v1/models", "/v1/chat/completions"],
                    "allowed_passthrough_routes": []}
            if supplied:
                body["key"] = supplied
            response = self.proxy.write("POST", "/key/generate", body)
            if self.args.dry_run:
                return supplied or None
            raw = response.get("key")
            if not isinstance(raw, str) or not raw.startswith("sk-") or (supplied and raw != supplied):
                raise Failure("Key generation did not return the expected raw key; old key preserved")
            SECRETS.add(raw)
            # Intentionally the ONLY plaintext secret output, only for server-generated keys.
            # Output immediately, before any later failure, so a persisted key is not lost.
            if not supplied:
                print("CLIENT_KEY=" + raw, flush=True)
            info = self.proxy.get("/key/info", params={"key": digest(raw)}).get("info", {})
            if not restricted(info, self.group, target_models):
                raise Failure("Generated key permissions differ; old key preserved")
            if not supplied:
                self.save_client(raw)
            self.smoke(raw)
            for old in keys:
                if key_hash(old) != digest(raw):
                    self.proxy.write("POST", "/key/delete", {"keys": [key_hash(old)]})
            if supplied:
                self.scrubbable.add("CLIENT_KEY")
            return raw
        ident = key_hash(chosen)
        info = self.proxy.get("/key/info", params={"key": ident}).get("info", {})
        if not restricted(info, self.group, target_models):
            # Team/org grants can expand access. Do not silently edit outside ownership.
            if any(info.get(k) for k in ("team_id", "organization_id", "project_id", "access_group_ids")):
                raise Failure("Client key has inherited grants; detach them before syncing")
            self.proxy.write("POST", "/key/update", {"key": ident, "models": target_models, "aliases": {},
                "allowed_routes": ["/v1/models", "/v1/chat/completions"], "allowed_passthrough_routes": []}, retry=True)
            if not self.args.dry_run:
                after = self.proxy.get("/key/info", params={"key": ident}).get("info", {})
                if not restricted(after, self.group, target_models):
                    raise Failure("Client restriction readback failed")
        if supplied:
            if not self.args.dry_run:
                self.smoke(supplied)
            if not self.args.no_delete:
                for old in keys:
                    if key_hash(old) != ident:
                        self.proxy.write("POST", "/key/delete", {"keys": [key_hash(old)]})
            elif len(keys) > 1:
                LOG.warning("--no-delete retains other keys with the same alias")
            self.scrubbable.add("CLIENT_KEY")
        return supplied or None

    def client_model_scope(self):
        if not self.green_models:
            return [self.group]
        available = {row.get("model_name") for row in self.proxy.models()
                     if self.group in ((row.get("model_info") or {}).get("access_groups") or [])}
        target = [model for model in self.green_models if model in available]
        if not target:
            raise Failure("No green probe models are currently present in the LiteLLM access group")
        return target

    def save_client(self, raw):
        path = self.args.env_file
        current = path.read_text() if path.exists() else ""
        if path.is_symlink() or current != self.env_original:
            raise Failure(".env changed during sync; generated key was output, old key preserved")
        lines = [line for line in current.splitlines(keepends=True)
                 if not re.match(r"\s*(?:export\s+)?CLIENT_KEY\s*=", line)]
        text = "".join(lines)
        if text and not text.endswith("\n"):
            text += "\n"
        text += "CLIENT_KEY=" + raw + "\n"
        atomic_write(path, text)
        self.env_original = text
        self.env["CLIENT_KEY"] = raw
        LOG.info("Generated CLIENT_KEY saved to .env (0600)")

    def smoke(self, client_key):
        data = self.proxy.http.request("GET", self.proxy.base + "/v1/models", key=client_key, label="Client /v1/models")
        visible = catalog_rows(data)
        allowed = set(self.client_target_models or [r["model_name"] for r in self.proxy.models()
                   if self.group in (r.get("model_info", {}).get("access_groups") or [])])
        returned = {r["id"] for r in visible}
        if returned - allowed:
            raise Failure("Client sees models outside its access group")
        if allowed - returned:
            raise Failure("Client model list is missing access-group deployments")
        LOG.info("Client /v1/models: %d permitted model names", len(returned))

    def nous_probe(self):
        cfg = self.config.get("providers", {}).get("nous", {})
        if self.args.dry_run or cfg.get("tags_mode", "auto") != "auto":
            return
        rows = [r for r in self.proxy.models() if managed(r, "nous")
                and not r["model_info"].get("free_sync_nous_tags")]
        for row in rows:
            # Probe the exact deployment ID, never a merged routing group.
            ident = row["model_info"]["id"]
            payload = {"model": ident, "messages": [{"role": "user", "content": "Reply OK."}], "max_tokens": 8}
            status = "not-required"
            try:
                self.proxy.http.request("POST", self.proxy.base + "/v1/chat/completions", key=self.proxy.admin,
                                        label="Nous initial tags probe", json=payload)
            except APIError as exc:
                if exc.status != 400 or not exc.missing_tags:
                    raise
                payload["extra_body"] = {"tags": ["user=free-sync"]}
                self.proxy.http.request("POST", self.proxy.base + "/v1/chat/completions", key=self.proxy.admin,
                                        label="Nous tags workaround probe", json=payload)
                status = "required"
            info = dict(row["model_info"], free_sync_nous_tags=status)
            body = {"model_info": info}
            if status == "required":
                body["litellm_params"] = {"extra_body": {"tags": ["user=free-sync"]}}
            self.proxy.write("PATCH", "/model/" + quote(ident, safe="") + "/update", body,
                             template="/model/{model_id}/update", retry=True)
            saved = self.journal.load()
            desired = {k: v["deployment"] for k, v in saved["models"].items() if v["aggregator"] == "nous"}
            if ident in desired:
                desired[ident]["model_info"]["free_sync_nous_tags"] = status
                if status == "required":
                    desired[ident]["litellm_params"]["extra_body"] = {"tags": ["user=free-sync"]}
                self.journal.plan("nous", desired, self.group, self.alias)
            LOG.info("Nous deployment %s: tags %s", ident, status)


def scrub(path, original, env_values, eligible):
    if not path.exists() or path.read_text() != original:
        raise Failure(".env changed during sync; refusing scrub")
    removed = []
    lines = []
    for line in original.splitlines(keepends=True):
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=", line)
        name = match.group(1) if match else None
        if name in eligible and name != "LITELLM_ADMIN_KEY" and env_values.get(name):
            removed.append(name)
        else:
            lines.append(line)
    if removed:
        backup = path.with_name(path.name + ".bak")
        atomic_write(backup, original)
        atomic_write(path, "".join(lines))
        LOG.info("Removed %d accepted secret lines; .env.bak retains the original (0600)", len(removed))


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_file(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as output:
        fcntl.flock(output, fcntl.LOCK_EX)
        for secret in sorted(SECRETS, key=len, reverse=True):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        output.write(text)
        output.flush()
        os.fsync(output.fileno())


class Journal:
    """Local desired catalog, append-only changes and a separate run ledger."""
    def __init__(self, args):
        self.args = args
        self.started = utcnow()
        self.runs = ROOT / "runs.log"
        self.news = ROOT / "news.md"
        self.path = ROOT / "free_models.json"
        self.summary = {}

    def configure(self, env, config):
        for attr, key, default in (("runs", "RUN_LOG", "runs.log"),
                                   ("news", "NEWS_FILE", "news.md"),
                                   ("path", "MODELS_FILE", "free_models.json")):
            setattr(self, attr, Path(env.get(key) or config.get(key.lower()) or ROOT / default).resolve())
        if len({self.runs, self.news, self.path, self.args.env_file.resolve()}) != 4:
            raise Fatal("Catalog, logs and .env paths must be distinct")

    def load(self):
        if not self.path.exists():
            return {"schema_version": 1, "created_at": utcnow(), "updated_at": utcnow(), "models": {}}
        data = json.loads(self.path.read_text())
        if data.get("schema_version") != 1 or not isinstance(data.get("models"), dict):
            raise Fatal("Invalid local free_models.json; refusing to overwrite")
        return data

    @staticmethod
    def entry(body, previous=None):
        info = body["model_info"]
        source = info.get("free_sync_catalog") or {}
        now = utcnow()
        return {"created_at": previous["created_at"] if previous else now, "updated_at": now,
                "aggregator": info["free_sync_provider"],
                # Model author is distinct from the actual routed inference provider.
                "provider": source.get("provider") or source.get("owned_by"),
                "providers": source.get("providers"),
                "model_author": info["free_sync_model_id"].split("/")[0]
                    if "/" in info["free_sync_model_id"] else None,
                "supported_parameters": info.get("supported_parameters", []),
                "deployment": body}

    def change(self, action, entry, before=None):
        if self.args.dry_run:
            return
        # JSON code blocks retain arbitrary model names safely and include all advertised capabilities.
        details = {"action": action, **entry}
        if before:
            details["previous"] = before
        text = "\n## " + utcnow() + " — " + action + "\n\n"
        text += "```json\n" + json.dumps(details, ensure_ascii=True, indent=2).replace("`", "\\u0060") + "\n```\n"
        append_file(self.news, text)

    def plan(self, provider, desired, group, alias):
        if self.args.dry_run:
            return desired
        data = self.load()
        entries = data["models"]
        old_ids = {ident for ident, entry in entries.items() if entry["aggregator"] == provider}
        events = []
        for ident, body in desired.items():
            previous = entries.get(ident)
            if previous and previous["deployment"] == body:
                continue
            entry = self.entry(body, previous)
            entries[ident] = entry
            events.append(("GEÄNDERT" if previous else "AUFGENOMMEN", entry, previous))
        for ident in old_ids - desired.keys():
            if not self.args.no_delete:
                events.append(("ENTFERNT", entries.pop(ident), None))
        metadata_changed = data.get("access_group") != group or data.get("client_key_alias") != alias
        data.update(access_group=group, client_key_alias=alias)
        if events or metadata_changed or not self.path.exists():
            data["updated_at"] = utcnow()
            # Persist intent before applying it. Failed API writes are retried from this catalog next run.
            atomic_write(self.path, json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True) + "\n")
            for action, entry, before in events:
                self.change(action, entry, before)
        saved = self.load()
        return {ident: entry["deployment"] for ident, entry in saved["models"].items()
                if entry["aggregator"] == provider}

    def finish(self, code):
        record = {"started_at": self.started, "finished_at": utcnow(),
                  "status": "OK" if code == 0 else "PARTIAL_ERROR" if code == 1 else "FATAL",
                  "exit_code": code, "dry_run": self.args.dry_run, "providers": self.summary}
        append_file(self.runs, json.dumps(record, ensure_ascii=True) + "\n")
        if not self.args.dry_run and self.path.exists() and (ROOT / "model_metadata.json").exists():
            from catalog_store import CatalogStore
            try:
                CatalogStore(ROOT, source=self.path, news=self.news, runs=self.runs).refresh()
            except (OSError, ValueError, KeyError, TypeError):
                LOG.warning("Website history refresh failed; source catalog and change log are retained")


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--provider", choices=PROVIDERS, action="append")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-delete", action="store_true")
    parser.add_argument("--forget", metavar="NAME", action="append")
    parser.add_argument("--forget-cascade", action="store_true")
    parser.add_argument("--scrub-env", action="store_true")
    return parser.parse_args()


def run(args, journal):
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(Redact())
    LOG.addHandler(handler)
    LOG.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    http = None
    try:
        file_env, original = read_env(args.env_file)
        # Nonempty process environment wins. Empty values never erase either source.
        env = dict(file_env)
        env.update({k: v for k, v in os.environ.items() if v.strip()})
        SECRETS.update(env.get(k, "") for k in SECRET_NAMES)
        admin = env.get("LITELLM_ADMIN_KEY", "").strip()
        if not admin:
            raise Fatal("LITELLM_ADMIN_KEY is required on every run")
        config = load_config(args.config)
        journal.configure(env, config)
        if args.forget_cascade and not args.forget:
            raise Fatal("--forget-cascade requires --forget")
        state = Path(env.get("STATE_FILE") or config.get("state_file", str(ROOT / "free_sync_state.json"))).resolve()
        # The lock contains no data. dry-run neither creates the state nor changes .env.
        lock_path = state.with_name(state.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Fatal("Another free-sync process holds the lock") from None
            http = HTTP(timeout=config.get("timeout_seconds", 30), retries=config.get("retries", 3))
            try:
                base = env.get("LITELLM_BASE_URL") or config.get("litellm_base_url", "http://localhost:4000")
                parsed = urlsplit(base)
                port = env.get("LITELLM_PORT")
                if port and parsed.port is None:
                    if not port.isdigit() or not 1 <= int(port) <= 65535:
                        raise Fatal("LITELLM_PORT must be between 1 and 65535")
                    base = parsed._replace(netloc=parsed.netloc + ":" + port).geturl()
                proxy = Proxy(http, base, admin, args.dry_run)
            except APIError as exc:
                raise Fatal("Admin authentication/schema check failed: " + str(exc)) from None
            sync = Sync(proxy, config, env, args)
            sync.env_original = original
            sync.journal = journal
            journal.summary = sync.summary
            if args.forget:
                sync.forget()  # separate mode: do not immediately recreate forgotten credentials
            else:
                for provider in args.provider or PROVIDERS:
                    cfg = config.get("providers", {}).get(provider, {})
                    if cfg.get("enabled", True):
                        sync.sync_provider(provider, cfg)
                try:
                    raw = sync.client()
                    if not args.dry_run:
                        if raw:
                            sync.smoke(raw)
                        else:
                            LOG.warning("Client exists and its group restriction was checked; raw bearer unavailable, /v1/models smoke skipped")
                        if (not args.provider or "nous" in args.provider) and config.get("providers", {}).get("nous", {}).get("enabled", True):
                            sync.nous_probe()
                except Failure as exc:
                    sync.error(str(exc))
            if not args.dry_run:
                final_rows = proxy.models()
                # State is only an optional cache; ownership is always read from model_info.
                cache = {str(r["model_info"]["id"]): r["model_info"].get("free_sync_hash", digest(r["model_name"]))
                         for r in final_rows if managed(r)}
                atomic_write(state, json.dumps(cache, sort_keys=True, indent=2) + "\n")
                if args.scrub_env and not sync.errors and not args.forget:
                    # Never remove a .env line shadowed by a different process-environment key.
                    eligible = {k for k in sync.scrubbable if file_env.get(k) == env.get(k)}
                    unavailable = {KEY_ENV[p] for p in args.provider or PROVIDERS if file_env.get(KEY_ENV[p])} - eligible
                    if unavailable:
                        LOG.warning("Keeping unverified/required local provider keys in .env")
                    scrub(args.env_file, sync.env_original, file_env, eligible)
            for provider, counts in sync.summary.items():
                LOG.info("%s: created=%d updated=%d deleted=%d unchanged=%d", provider, *counts.values())
            return 1 if sync.errors else 0
    except Fatal as exc:
        LOG.error("%s", exc)
        return 2
    except Failure as exc:
        LOG.error("%s", exc)
        return 1
    except (OSError, ValueError, TypeError, KeyError):
        LOG.error("Invalid config, filesystem or API data; details suppressed to protect secrets")
        return 2
    finally:
        if http:
            http.client.close()


def main():
    args = arguments()
    journal = Journal(args)
    code = 2
    try:
        code = run(args, journal)
    finally:
        try:
            journal.finish(code)
        except (OSError, Failure):
            LOG.error("Cannot append runs.log")
            if code == 0:
                code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
