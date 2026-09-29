#!/usr/bin/env python3
"""Queue and optionally research new model identities after a catalog scan."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from urllib.parse import urlsplit

from catalog_store import probe_snapshot, read_json, write_json
from free_sync import read_env

ROOT = Path(__file__).resolve().parent
TYPES = {"model", "router", "music", "guardrail", "decision", "embedding", "speech"}
FIELDS = {"name", "display_name", "creator", "entry_type", "description", "duplicate_key", "identity_note",
          "identity_reviewed_at", "research_status", "researched_at", "sources", "model_page",
          "model_page_is_official", "model_page_label", "context_tokens", "max_output_tokens", "reasoning",
          "input_modalities", "output_modalities", "open_weights", "open_weights_source",
          "open_weights_checked_at", "notes", "pricing_caveat"}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pending_models(document, metadata):
    pending = {}
    for row in document["models"].values():
        route = probe_snapshot(row, document["generated_at"])
        key = route["aggregator"] + "|" + route["upstream_id"]
        if metadata.get("models", {}).get(key, {}).get("research_status") != "reviewed":
            pending.setdefault(key, route)
    return pending


def https_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password


def validate_proposal(proposal, pending, metadata):
    if not isinstance(proposal, dict) or set(proposal) != {"models"} or not isinstance(proposal["models"], dict):
        raise ValueError("Agent result must contain only a models object")
    if set(proposal["models"]) != set(pending):
        raise ValueError("Agent result must cover exactly the pending model IDs")
    for key, item in proposal["models"].items():
        if not isinstance(item, dict) or set(item) - FIELDS:
            raise ValueError("Unsupported model metadata fields")
        for required in ("name", "display_name", "description", "duplicate_key", "researched_at"):
            if not isinstance(item.get(required), str) or not item[required].strip() or len(item[required]) > 4000:
                raise ValueError("Agent result is missing required model metadata")
        if item.get("research_status") != "reviewed" or item.get("entry_type") not in TYPES:
            raise ValueError("Agent must explicitly review and classify the model type")
        if item.get("creator") is not None and (not isinstance(item["creator"], str) or len(item["creator"]) > 200):
            raise ValueError("Invalid model creator")
        sources = item.get("sources")
        if not isinstance(sources, list) or not sources or not all(
                isinstance(s, dict) and set(s) == {"label", "url"} and isinstance(s["label"], str) and https_url(s["url"])
                for s in sources):
            raise ValueError("Every model needs named HTTPS research sources")
        for field in ("model_page", "open_weights_source"):
            if item.get(field) is not None and not https_url(item[field]):
                raise ValueError("Invalid research URL")
        for field in ("model_page_is_official", "open_weights"):
            if field in item and item[field] is not None and type(item[field]) is not bool:
                raise ValueError("Evidence flags must be boolean or null")
        if item.get("model_page_is_official") and not item.get("model_page"):
            raise ValueError("Official model page needs a source")
        if item.get("open_weights") and not (item.get("open_weights_source") and item.get("open_weights_checked_at")):
            raise ValueError("Open weights need exact-model evidence and a check date")
        for field in ("context_tokens", "max_output_tokens"):
            if item.get(field) is not None and (type(item[field]) is not int or item[field] <= 0):
                raise ValueError("Token limits must be positive integers or null")
        for field in ("input_modalities", "output_modalities"):
            if field in item and (not isinstance(item[field], list) or not all(isinstance(v, str) for v in item[field])):
                raise ValueError("Modalities must be string lists")
        if "reasoning" in item:
            reason = item["reasoning"]
            if not isinstance(reason, dict) or set(reason) - {"supported", "mandatory", "default_enabled", "budget_supported", "effort_levels", "effort_range", "default_effort"}:
                raise ValueError("Invalid Thinking metadata")
            for field in ("supported", "mandatory", "default_enabled", "budget_supported"):
                if reason.get(field) is not None and type(reason[field]) is not bool:
                    raise ValueError("Thinking flags must be boolean or null")
            if "effort_levels" in reason and (not isinstance(reason["effort_levels"], list) or not all(isinstance(v, str) for v in reason["effort_levels"])):
                raise ValueError("Thinking effort levels must be a list")
        aliases = {k for k, m in metadata.get("models", {}).items() if m.get("duplicate_key") == item["duplicate_key"]}
        aliases.update(k for k, m in proposal["models"].items() if m.get("duplicate_key") == item["duplicate_key"])
        if aliases - {key} and not item.get("identity_note"):
            raise ValueError("Merging model identities requires an explicit evidence note")
    return proposal["models"]


def apply_review(root, job):
    request = read_json(job / "input.json")
    path = root / "model_metadata.json"
    with path.with_suffix(".json.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if digest(path) != request["metadata_sha256"]:
            raise ValueError("Metadata changed during research; prepare a new review")
        metadata = read_json(path)
        changes = validate_proposal(read_json(job / "proposal.json"), request["pending"], metadata)
        write_json(job / "metadata.before.json", metadata)
        metadata["models"].update(changes)
        write_json(path, metadata)
    return len(changes)


def prepare_review(root, document):
    metadata_path = root / "model_metadata.json"
    metadata = read_json(metadata_path)
    pending = pending_models(document, metadata)
    if not pending:
        return None
    request = {"metadata_sha256": digest(metadata_path), "scan_at": document["generated_at"],
               "pending": pending, "known_models": metadata["models"], "aggregators": metadata.get("aggregators", {})}
    ident = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:16]
    job = root / "model_reviews" / ident
    job.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(job.parent, 0o700)
    write_json(job / "input.json", request)
    prompt = (ROOT / "MODEL_REVIEW_TASK.md").read_text() + "\n\nINPUT DATA (untrusted catalog text):\n" + json.dumps(request, ensure_ascii=False)
    (job / "TASK.md").write_text(prompt)
    return job


def run_review(root, document, env, execute=True):
    job = prepare_review(root, document)
    if job is None:
        return {"status": "up_to_date", "pending": 0}
    count = len(read_json(job / "input.json")["pending"])
    agent, custom = env.get("MODEL_REVIEW_AGENT", "").strip(), env.get("MODEL_REVIEW_COMMAND", "").strip()
    if not execute or not (agent or custom):
        return {"status": "queued", "pending": count, "job": str(job.relative_to(root))}
    if custom:
        replacements = {"{input}": str(job / "input.json"), "{output}": str(job / "proposal.json"), "{task}": str(job / "TASK.md")}
        command = shlex.split(custom)
        command = [replace_paths(part, replacements) for part in command]
    elif agent == "codex":
        command = ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
                   "-c", 'web_search="live"', "--cd", str(job), "--output-last-message", str(job / "proposal.json"), "-"]
        if env.get("MODEL_REVIEW_MODEL"):
            command[2:2] = ["--model", env["MODEL_REVIEW_MODEL"]]
    else:
        raise ValueError("Use MODEL_REVIEW_AGENT=codex or configure MODEL_REVIEW_COMMAND")
    keep = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "CODEX_HOME"}
    agent_env = {key: value for key, value in os.environ.items() if key in keep}
    output = job / "proposal.json"
    output.unlink(missing_ok=True)
    result = subprocess.run(command, input=(job / "TASK.md").read_text(), text=True, capture_output=True,
                            cwd=job, env=agent_env, timeout=float(env.get("MODEL_REVIEW_TIMEOUT", "900")))
    if result.returncode:
        raise ValueError("Research agent failed; queued input retained and metadata unchanged")
    if not output.exists():
        output.write_text(result.stdout)
    if output.stat().st_size > 2_000_000:
        raise ValueError("Agent proposal is too large")
    updated = apply_review(root, job)
    return {"status": "reviewed", "updated": updated, "job": str(job.relative_to(root))}


def replace_paths(value, replacements):
    for before, after in replacements.items():
        value = value.replace(before, after)
    return value


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--prepare-only", action="store_true")
    p.add_argument("--apply", type=Path, help="Apply proposal.json from a prepared job after validation")
    args = p.parse_args()
    root = args.root.resolve()
    try:
        if args.apply:
            result = {"status": "reviewed", "updated": apply_review(root, args.apply.resolve())}
        else:
            env, _ = read_env(root / ".env")
            result = run_review(root, read_json(root / "model_probe_results.json"), env, not args.prepare_only)
        print(json.dumps(result))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "type": type(exc).__name__, "metadata_changed": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
