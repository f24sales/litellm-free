"""Stable, credential-free diffs for LiteLLM model route lists."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def _secret_key(key: str) -> bool:
    name = re.sub(r"[^a-z0-9]", "", key.lower())
    return (any(part in name for part in ("apikey", "password", "secret", "credential", "accesskey"))
            or (name.endswith("token") and not name.endswith("pertoken"))
            or name.endswith(("authorization", "headers", "cookie", "cookies"))
            or name in {"auth", "bearer", "privatekey", "serviceaccount"})


def _without_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_secrets(item)
            for key, item in value.items()
            if not _secret_key(key)
        }
    if isinstance(value, list):
        return [_without_secrets(item) for item in value]
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        # Endpoint/source URLs can contain credentials in userinfo or query strings.
        try:
            url = urlsplit(value)
            return urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))
        except ValueError:
            return "[invalid URL]"
    return deepcopy(value)


def public_row(row: Any) -> dict[str, Any]:
    """Return a bounded diff row without credentials or encrypted values."""
    if not isinstance(row, dict):
        raise ValueError("LiteLLM model rows must be objects")
    return _without_secrets(row)


def _row_key(row: dict[str, Any]) -> str:
    info = row.get("model_info")
    if isinstance(info, dict) and isinstance(info.get("id"), str) and info["id"]:
        return "id:" + info["id"]
    name = row.get("model_name")
    if isinstance(name, str) and name:
        return "name:" + name
    raise ValueError("LiteLLM model row has no stable id or model_name")


def _canonical(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for original in rows:
        row = public_row(original)
        key = _row_key(row)
        if key in result:
            raise ValueError("LiteLLM model list contains duplicate route identities")
        result[key] = row
    return result


def _fingerprint(rows: dict[str, dict[str, Any]]) -> str:
    encoded = json.dumps([rows[key] for key in sorted(rows)], ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def model_diff(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a deterministic added/removed/updated diff for model routes."""
    old, new = _canonical(before), _canonical(after)
    added = [new[key] for key in sorted(new.keys() - old.keys())]
    removed = [old[key] for key in sorted(old.keys() - new.keys())]
    updated = [
        {"key": key, "before": old[key], "after": new[key]}
        for key in sorted(old.keys() & new.keys())
        if old[key] != new[key]
    ]
    return {
        "changed": bool(added or removed or updated),
        "before_count": len(old),
        "after_count": len(new),
        "before_sha256": _fingerprint(old),
        "after_sha256": _fingerprint(new),
        "added": added,
        "removed": removed,
        "updated": updated,
    }


def empty_diff() -> dict[str, Any]:
    return model_diff([], [])
