#!/usr/bin/env python3
"""Start the three independent client refresh scripts with one live catalog snapshot."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import refresh_common as common
from refresh_common import RefreshError

CLIENT_SCRIPTS = {"openclaw": "openclaw", "opencode": "opencode", "hermes": "hermes-agent"}


def run_client(kind, environ, catalog, reload_required):
    script = Path(__file__).resolve().with_name(CLIENT_SCRIPTS[kind])
    argv = [sys.executable, str(script), "--catalog-stdin"]
    if reload_required:
        argv.append("--reload")
    try:
        result = subprocess.run(argv, input=json.dumps(catalog), capture_output=True, text=True,
                                timeout=180, env={**os.environ, **environ})
        report = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise RefreshError(f"{kind}: refresh script did not complete") from None
    if result.returncode or report.get("status") != "ok" or report.get("client") != kind:
        raise RefreshError(f"{kind}: refresh script failed")
    return report


def refresh(environ, status_path):
    catalog = common.discover(environ)
    plans, clients = common.plan_configs(environ, catalog)
    digest = common.catalog_digest(catalog)
    previous = json.loads(status_path.read_text()) if status_path.is_file() else {}
    pending = set(previous.get("pending_services", []))
    attempted = set(previous.get("reload_attempted", []))
    for kind in CLIENT_SCRIPTS:
        if clients[kind]["changed"] or previous.get("catalog_sha256") != digest:
            pending.add(kind)
    report = {"status": "pending", "provider": catalog["id"], "catalog_sha256": digest,
              "discovered": len(catalog["models"]), "clients": clients,
              "pending_services": sorted(pending), "reload_attempted": sorted(attempted)}
    common.atomic_write(status_path, (json.dumps(report) + "\n").encode())
    # Preserve the all-configs validation/concurrency guard before any runtime update.
    common.commit_plans(plans)
    attempted.update(CLIENT_SCRIPTS)
    report["reload_attempted"] = sorted(attempted)
    common.atomic_write(status_path, (json.dumps(report) + "\n").encode())
    failures = []
    with ThreadPoolExecutor(max_workers=3) as workers:
        jobs = {workers.submit(run_client, kind, environ, catalog, kind in pending): kind
                for kind in CLIENT_SCRIPTS}
        for job in as_completed(jobs):
            kind = jobs[job]
            try:
                result = job.result()
                clients[kind]["runtime"] = result["runtime"]
                pending.discard(kind)
            except Exception as exc:
                pending.add(kind)
                clients[kind]["runtime"] = "error"
                failures.append(str(exc) if isinstance(exc, RefreshError) else f"{kind}: refresh failed")
            attempted.discard(kind)
            report["pending_services"] = sorted(pending)
            report["reload_attempted"] = sorted(attempted)
            common.atomic_write(status_path, (json.dumps(report) + "\n").encode())
    if failures:
        raise RefreshError("; ".join(sorted(failures)))
    for path, _, after in plans:
        if yaml.safe_load(path.read_bytes()) != yaml.safe_load(after):
            raise RefreshError("A client changed configuration values during reload")
    report["status"] = "ok"
    common.atomic_write(status_path, (json.dumps(report) + "\n").encode())
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    runtime = Path(os.environ.get("REFRESH_RUNTIME_DIR") or "/var/lib/litellm-free")
    status = Path(os.environ.get("REFRESH_STATUS_FILE") or runtime / "refresh-models.status.json")
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (runtime / "refresh-models.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "busy"}))
            return 75
        try:
            report = refresh(dict(os.environ), status)
        except Exception as exc:
            report = json.loads(status.read_text()) if status.is_file() else {}
            report.update(status="error", error=str(exc) if isinstance(exc, RefreshError) else type(exc).__name__)
            common.atomic_write(status, (json.dumps(report) + "\n").encode())
            print(json.dumps(report))
            return 1
        print(json.dumps(report, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
