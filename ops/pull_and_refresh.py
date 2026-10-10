#!/usr/bin/env python3
"""Download the public catalog, then reconcile the authorized live client catalog."""
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from dotenv import dotenv_values
import yaml

from refresh_common import NoRedirect, RefreshError, atomic_write
from refresh_models import main as refresh_main


def pull(environ, destination, *, opener=None):
    source = environ.get("IMPORT_SOURCE_URL", "").strip()
    parsed = urlsplit(source)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise RefreshError("IMPORT_SOURCE_URL must be a credential-free HTTPS URL")
    request = Request(source, headers={"Accept": "application/yaml", "User-Agent": "litellm-free-refresh/1"})
    with (opener or build_opener(NoRedirect()).open)(request, timeout=30) as response:
        payload = response.read(5_000_001)
    if len(payload) > 5_000_000:
        raise RefreshError("Public catalog exceeds size limit")
    document = yaml.safe_load(payload)
    rows = document.get("model_list") if isinstance(document, dict) else None
    if not isinstance(rows, list) or not rows:
        raise RefreshError("Public catalog has no model list")
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("model_name"), str)
                or not row["model_name"].strip() or not isinstance(row.get("litellm_params"), dict)):
            raise RefreshError("Invalid public model catalog")
    changed = not destination.is_file() or destination.read_bytes() != payload
    if changed:
        atomic_write(destination, payload)
    return {"models": len(rows), "changed": changed, "sha256": hashlib.sha256(payload).hexdigest()}


def main():
    root = Path(__file__).resolve().parents[1]
    config = root / "config.conf"
    if not config.exists():
        config = root / "config.conf_example"
    settings = {**dotenv_values(config, interpolate=False),
                **dotenv_values(root / ".env", interpolate=False), **os.environ}
    os.environ.update({key: value for key, value in settings.items() if value is not None})
    runtime = Path(settings.get("REFRESH_RUNTIME_DIR") or "/var/lib/litellm-free")
    try:
        result = pull(settings, runtime / "published-catalog.yaml")
        print(json.dumps({"public_catalog": result}), flush=True)
        # Always refresh, even if the published file is unchanged. A client
        # restart, key allowlist change or previous failure can still need repair.
        return refresh_main([])
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
