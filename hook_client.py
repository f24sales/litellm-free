"""Small, optional curl-based client for the LiteLLM-Free webhook."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4


def _local_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        import ipaddress
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def hook_url(value: str | None) -> str:
    url = (value or "").strip()
    if not url:
        return ""
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.fragment
            or any(ord(char) < 32 for char in url)
            or (parsed.scheme == "http" and not _local_host(parsed.hostname))):
        raise ValueError("IMPORT_HOOK_URL must use HTTPS or a local HTTP endpoint")
    return url


def hook_timeout(value: str | None) -> int:
    try:
        timeout = int(float(value or "30"))
    except (TypeError, ValueError):
        raise ValueError("IMPORT_HOOK_TIMEOUT_SECONDS must be between 1 and 300") from None
    if not 1 <= timeout <= 300:
        raise ValueError("IMPORT_HOOK_TIMEOUT_SECONDS must be between 1 and 300")
    return timeout


def _curl_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def post_hook(event: str, payload: dict, env: dict[str, str], *, source: str = "litellm-free") -> dict | None:
    """Send an optional event, retrying transient failures with the same event ID."""
    url = hook_url(env.get("IMPORT_HOOK_URL"))
    bearer = (env.get("IMPORT_HOOK_BEARER") or "").strip()
    if not url or not bearer:
        return None
    body = {**payload, "schema_version": 1, "source": source, "event": event,
            "event_id": str(uuid4())}
    timeout = hook_timeout(env.get("IMPORT_HOOK_TIMEOUT_SECONDS"))
    with tempfile.TemporaryDirectory(prefix="litellm-free-hook-") as directory:
        root = Path(directory)
        body_path = root / "payload.json"
        config_path = root / "curl.conf"
        body_path.write_text(json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
        body_path.chmod(0o600)
        config_path.write_text("\n".join([
            f"url = {_curl_quote(url)}",
            "request = POST",
            f"header = {_curl_quote('Authorization: Bearer ' + bearer)}",
            'header = "Content-Type: application/json"',
            'header = "Accept: application/json"',
            'header = "User-Agent: litellm-free/1"',
            f"data-binary = {_curl_quote('@' + str(body_path))}",
            *( [f"cacert = {_curl_quote(env['IMPORT_HOOK_CA_FILE'].strip())}"]
               if (env.get("IMPORT_HOOK_CA_FILE") or "").strip() else [] ),
            'output = "/dev/null"',
            'write-out = "%{http_code}"',
        ]) + "\n")
        config_path.chmod(0o600)
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    ["curl", "--silent", "--show-error", "--fail-with-body",
                     "--max-time", str(timeout), "--config", str(config_path)],
                    capture_output=True, text=True, timeout=timeout + 5, check=False,
                    env={**os.environ, "NO_PROXY": "*", "no_proxy": "*"},
                )
                try:
                    status_code = int((result.stdout or "0").strip())
                except ValueError:
                    status_code = 0
                sent = result.returncode == 0 and 200 <= status_code < 300
                receipt = {"sent": sent, "event": event, "event_id": body["event_id"],
                           "status_code": status_code, "attempts": attempt}
                if not sent:
                    # curl diagnostics may contain URLs; keep credentials and remote bodies out.
                    receipt["error"] = f"HTTP {status_code}" if status_code else f"curl exit {result.returncode}"
                transient = (status_code in {408, 425, 429, 500, 502, 503, 504}
                             or (not status_code and result.returncode in {5, 6, 7, 18, 28, 35, 52, 55, 56}))
            except subprocess.TimeoutExpired:
                receipt = {"sent": False, "event": event, "event_id": body["event_id"],
                           "attempts": attempt, "error": "Webhook request timed out"}
                transient = True
            if receipt["sent"] or not transient or attempt == 3:
                return receipt
            time.sleep(attempt)
