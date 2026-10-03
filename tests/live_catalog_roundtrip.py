#!/usr/bin/env python3
"""Explicit opt-in live add/remove test; never run by unit-test discovery."""
import argparse
import json
from pathlib import Path
import pwd
import ssl
import subprocess
import time
from uuid import uuid4

import httpx
from dotenv import dotenv_values

PROBE = r"""
import base64, json, os, subprocess, sys
from pathlib import Path
from urllib.request import Request, urlopen
import yaml
provider = "litellm-free"
obj = json.loads(Path(os.environ.get("OPENCLAW_CONFIG") or "/root/.openclaw/openclaw.json").read_text())
block = obj["models"]["providers"][provider]
key = os.environ[block["apiKey"]["id"]]
with urlopen(Request(block["baseUrl"].rstrip("/") + "/models", headers={"Authorization": "Bearer " + key}), timeout=15) as r:
    upstream = sorted(row["id"] for row in json.load(r)["data"])
if sys.argv[1] == "upstream":
    print(json.dumps(upstream)); raise SystemExit(0)
headers = {}
if os.environ.get("OPENCODE_SERVER_PASSWORD"):
    credentials = (os.environ.get("OPENCODE_SERVER_USERNAME") or "opencode") + ":" + os.environ["OPENCODE_SERVER_PASSWORD"]
    headers["Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode()
with urlopen(Request("http://127.0.0.1:4096/provider", headers=headers), timeout=15) as r:
    opencode = next(row for row in json.load(r)["all"] if row["id"] == provider)
native = subprocess.run(["/usr/local/lib/hermes-agent/venv/bin/python", "-c",
    "import sys,json; sys.path.insert(0,'/usr/local/lib/hermes-agent'); from hermes_cli.config_providers import get_compatible_custom_providers; print(json.dumps(sorted(next(row['models'] for row in get_compatible_custom_providers() if row.get('provider_key')=='litellm-free'))))"],
    capture_output=True, text=True, timeout=60)
if native.returncode: raise RuntimeError("Hermes native catalog failed")
hermes = json.loads(native.stdout)
native_claw = subprocess.run(["openclaw", "models", "list", "--provider", provider, "--json"],
                            capture_output=True, text=True, timeout=120)
if native_claw.returncode: raise RuntimeError("OpenClaw native catalog failed")
claw = json.loads(native_claw.stdout)
claw_ids = sorted(row.get("key", row.get("id", "")).removeprefix(provider+"/") for row in claw["models"])
voice = yaml.safe_load(Path("/root/.hermes/config.yaml").read_text())
import hashlib
voice_hash = hashlib.sha256(json.dumps({k:voice.get(k) for k in ["tts","stt","voice","plugins"]},sort_keys=True).encode()).hexdigest()
print(json.dumps({"upstream": upstream, "openclaw": claw_ids, "hermes": hermes,
                  "opencode": sorted(opencode["models"]), "voice_sha256": voice_hash}))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args()
    env = {**dotenv_values(args.config_root / "config.conf", interpolate=False),
           **dotenv_values(args.config_root / ".env", interpolate=False)}
    account = pwd.getpwnam(args.user)
    podman = ["sudo", "-n", "-u", args.user, "env", f"XDG_RUNTIME_DIR=/run/user/{account.pw_uid}", "podman"]

    def run(arguments, input=None, timeout=240):
        result = subprocess.run([*podman, "exec", "-i", args.container, *arguments],
                                input=input, capture_output=True, text=True, cwd="/tmp", timeout=timeout)
        if result.returncode:
            raise RuntimeError("Container check failed: " + arguments[0])
        return json.loads(result.stdout)

    def probe(mode="all"):
        return run(["python3", "-", mode], input=PROBE)

    def refresh():
        result = run(["bash", "/opt/safrano9999/litellm-free/ops/refresh-models.sh"], timeout=300)
        if result.get("status") != "ok":
            raise RuntimeError("Model refresh did not succeed")
        return result

    def assert_catalog(present):
        for _ in range(20):
            found = marker in probe("upstream")
            if found == present:
                break
            time.sleep(2)
        else:
            raise RuntimeError("Live /v1/models did not reflect test mutation")
        refresh()
        current = probe()
        if current["voice_sha256"] != before["voice_sha256"]:
            raise RuntimeError("Hermes voice settings changed")
        for kind in ("openclaw", "hermes", "opencode"):
            if current[kind] != current["upstream"]:
                raise RuntimeError(kind + " catalog differs from live /v1/models")
        return {kind: len(current[kind]) for kind in ("upstream", "openclaw", "hermes", "opencode")}

    # No secrets appear in output; the existing virtual key value never changes.
    ident = str(uuid4())
    marker = "catalog-refresh-probe-" + ident
    context = ssl.create_default_context()
    if env.get("LITELLM_CA_FILE"):
        context.load_verify_locations(cafile=env["LITELLM_CA_FILE"])
    from urllib.parse import urlsplit, urlunsplit
    parsed = urlsplit(env["LITELLM_BASE_URL"])
    if parsed.port is None and env.get("LITELLM_PORT"):
        parsed = parsed._replace(netloc=parsed.hostname + ":" + env["LITELLM_PORT"])
    with httpx.Client(base_url=urlunsplit(parsed).rstrip("/"), verify=context, trust_env=False,
                      headers={"Authorization": "Bearer " + env["LITELLM_ADMIN_KEY"]},
                      timeout=30, follow_redirects=False) as client:
        def request(method, path, **kwargs):
            result = client.request(method, path, **kwargs)
            result.raise_for_status()
            return result.json()
        keys = request("GET", "/key/list", params={"return_full_object": "true", "size": 100})["keys"]
        matching = [row for row in keys if row.get("key_alias") == "litellm-free"]
        if len(matching) != 1:
            raise RuntimeError("Expected exactly one target key")
        key = matching[0]["token"]
        original_models = matching[0]["models"]
        before = probe()
        print(json.dumps({"phase": "before", "models": len(before["upstream"]), "probe": marker}), flush=True)
        created = False
        try:
            # No provider request is sent. The synthetic route has a mock response
            # and a non-routable backend as an additional accidental-use safeguard.
            request("POST", "/model/new", json={
                "model_name": marker,
                "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "catalog-probe-placeholder",
                                  "api_base": "http://127.0.0.1:1/v1", "mock_response": "Catalog propagation probe"},
                "model_info": {"id": ident, "managed_by": "model-refresh-test", "access_groups": ["litellm-free"]}})
            created = True
            request("POST", "/key/update", json={"key": key, "models": [*original_models, marker]})
            print(json.dumps({"phase": "added", "counts": assert_catalog(True)}), flush=True)
        finally:
            # Remove only our marker, preserving any concurrent unrelated update.
            current = request("GET", "/key/info", params={"key": key})["info"]["models"]
            if marker in current:
                request("POST", "/key/update", json={"key": key, "models": [m for m in current if m != marker]})
            if created:
                request("POST", "/model/delete", json={"id": ident})
            print(json.dumps({"phase": "removed", "counts": assert_catalog(False), "cleanup": "verified"}), flush=True)
        print(json.dumps({"status": "ok", "voice": "unchanged", "credentials": "unchanged"}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # HTTP errors may contain a key hash in their request URL.
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}))
        raise SystemExit(1)
