#!/usr/bin/env python3
"""JSON stdin/stdout handler; OpenClaw MCP owns authentication and delivery."""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
ALLOWED = {"litellm-free": {"import_succeeded", "import_failed"},
           "litellm-free-webui": {"scan_valid", "scan_failed"}}

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)

def scrub(value):
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()
                if not re.search(r"apikey|password|secret|credential|accesskey|authorization|headers|cookies|privatekey|serviceaccount|bearer", re.sub(r"[^a-z]", "", key.lower()))
                and not (key.lower().endswith("token") and not key.lower().endswith("pertoken"))}
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        try:
            url = urlsplit(value)
            return urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))
        except ValueError:
            return "[invalid URL]"
    return value

def validate(event):
    if not isinstance(event, dict) or event.get("schema_version") != 1 or event.get("event") not in ALLOWED.get(event.get("source"), set()):
        raise ValueError("invalid_event")
    if event["event"] == "scan_valid":
        report = event.get("report", {})
        if event.get("scan_state") not in {"ok_changed", "ok_unchanged"} or not isinstance(report, dict):
            raise ValueError("invalid_event")
        if not re.fullmatch(r"scan-report-\d{8}T\d{6}Z\.pdf", report.get("filename", "")) or report.get("content_type") != "application/pdf":
            raise ValueError("invalid_event")
        try:
            data = base64.b64decode(report["content_base64"], validate=True)
        except (KeyError, ValueError, TypeError):
            raise ValueError("invalid_event") from None
        if not data.startswith(b"%PDF-") or len(data) > 6 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != report.get("sha256"):
            raise ValueError("invalid_event")
    if event["event"] == "import_succeeded":
        diff = event.get("diff", {})
        if not isinstance(diff, dict) or any(not isinstance(diff.get(key), list) for key in ("added", "removed", "updated")):
            raise ValueError("invalid_event")
        if diff.get("changed") is not any(diff[key] for key in ("added", "removed", "updated")):
            raise ValueError("invalid_event")

def refresh(state, timeout):
    command = [sys.executable, str(ROOT / "lib/ops/refresh_models.py")]
    env = {**os.environ, "REFRESH_RUNTIME_DIR": str(state / "refresh")}
    child = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    try:
        code = child.wait(timeout=timeout)
        return {"ok": code == 0, "exitCode": code}
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        return {"ok": False, "error": "timeout"}

def text(event, result):
    kind = event["event"]
    if kind == "scan_valid":
        return "🟢 LiteLLM-Free: Scan valid. " + ("Model changes detected." if event["scan_state"] == "ok_changed" else "No model changes.")
    if kind == "scan_failed":
        return "🔴 LiteLLM-Free: Scan not valid."
    if kind == "import_failed":
        return "🔴 LiteLLM-Free: " + ("Import valid; post-import check failed." if event.get("import_ok") else "Import failed; models not reloaded.")
    if not result.get("ok"):
        return "🔴 LiteLLM-Free: Import valid; client model reload failed."
    lines = ["🟢 LiteLLM-Free: Update valid, models reloaded."]
    for key, marker in (("added", "➕"), ("removed", "➖"), ("updated", "↻")):
        rows = event["diff"][key]
        for row in rows[:6]:
            name = row.get("model_name") or row.get("key") or "Model" if isinstance(row, dict) else str(row)
            lines.append(marker + " " + re.sub(r"[\x00-\x1f]", " ", str(name))[:160])
        if len(rows) > 6:
            lines.append(marker + " …")
    return "\n".join(lines)[:3500]

def handle(request, refresh_runner=refresh):
    state = Path(request["state_dir"])
    config = request.get("config", {})
    if request.get("action") == "receipt":
        key = request.get("receipt_id", "")
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("invalid_event")
        path = state / "receipts" / (key + ".json")
        receipt = json.loads(path.read_text())
        receipt["delivery"] = request["delivery"]
        save(path, receipt)
        return {"ok": True}
    if request.get("action") != "prepare":
        raise ValueError("invalid_event")
    event = request.get("payload")
    validate(event)
    key = digest([event["source"], event["event"], event.get("event_id") or event.get("run_id") or digest(event)])
    path = state / "receipts" / (key + ".json")
    receipt = json.loads(path.read_text()) if path.exists() else {"digest": digest(event)}
    if receipt["digest"] != digest(event):
        raise ValueError("event_id_reused")
    delivery = receipt.get("delivery", {})
    if delivery.get("status") in {"sent", "suppressed", "partial_failed", "delivery_unknown"} or (receipt.get("delivery_attempted") and not delivery):
        return {"ok": receipt.get("refresh", {}).get("ok", True), "duplicate": True, "receipt_id": key, "delivery": delivery}
    if event["event"] == "import_succeeded" and not receipt.get("refresh", {}).get("ok"):
        deadline = time.monotonic() + min(int(config.get("refreshTimeoutSeconds", 300)), 360)
        for attempt in range(3):
            remaining = int(deadline - time.monotonic())
            if remaining < 1:
                break
            receipt["refresh"] = refresh_runner(state, remaining)
            receipt.setdefault("refreshHistory", []).append(receipt["refresh"])
            save(path, receipt)
            if receipt["refresh"]["ok"]:
                break
            if attempt < 2:
                time.sleep(attempt + 1)
    result = receipt.get("refresh", {"ok": True, "skipped": True})
    attachments = []
    if event["event"] == "scan_valid" and config.get("attachReport", True):
        file = state / "reports" / key / event["report"]["filename"]
        file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        file.write_bytes(base64.b64decode(event["report"]["content_base64"]))
        file.chmod(0o600)
        attachments.append(str(file))
    elif event["event"] == "import_succeeded" and result.get("ok") and event["diff"]["changed"] and config.get("attachDiff", True):
        file = state / "diffs" / key / "litellm-free-diff.json"
        save(file, scrub(event["diff"]))
        attachments.append(str(file))
    receipt["delivery_attempted"] = True
    receipt.pop("delivery", None)
    save(path, receipt)
    return {"ok": result.get("ok", False), "text": text(event, result), "attachments": attachments,
            "refresh": result, "receipt_id": key}

if __name__ == "__main__":
    try:
        print(json.dumps(handle(json.load(sys.stdin)), ensure_ascii=False))
    except ValueError as error:
        code = str(error) if str(error) in {"invalid_event", "event_id_reused"} else "invalid_event"
        print(json.dumps({"error": code}))
    except Exception:
        print(json.dumps({"error": "handler_failed"}))
