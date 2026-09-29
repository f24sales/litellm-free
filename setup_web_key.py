"""Idempotently provision a dedicated demo key and local content-filter guardrails.

Reads the existing admin credential, never prints or copies it to the frontend.
Only creates resources named litellm-free-web; never modifies the shared free key.
"""
import json
from pathlib import Path
import ssl

from dotenv import dotenv_values
import httpx

from catalog_store import ROOT, write_json
from free_sync import atomic_write
from green_keys import green_model_names


def provision(root=ROOT):
    path = Path(root) / ".env"
    env = dict(dotenv_values(path))
    sot_path = Path(env.get("PROBE_SOT_FILE") or root / "model_probe_results.json").expanduser()
    if not sot_path.is_absolute():
        sot_path = Path(root) / sot_path
    web_models = green_model_names(sot_path) if sot_path.exists() else ["litellm-free"]
    web_alias = env.get("FREE_WEB_CLIENT_KEY_ALIAS") or "litellm-free-web"
    endpoint = (env.get("LITELLM_BASE_URL") or "https://127.0.0.1").rstrip("/")
    if endpoint == "https://127.0.0.1":
        endpoint += ":" + (env.get("LITELLM_PORT") or "2001")
    with httpx.Client(base_url=endpoint, headers={"Authorization": "Bearer " + env["LITELLM_ADMIN_KEY"]},
                      verify=ssl.create_default_context(), timeout=45, follow_redirects=False) as client:
        available = client.get("/guardrails/ui/add_guardrail_settings")
        available.raise_for_status()
        options = available.json()["content_filter_settings"]
        categories = [{"category": c["name"], "enabled": True, "action": "BLOCK", "severity_threshold": "low"}
                      for c in options["content_categories"] if c["name"].startswith(("harm", "bias_", "prompt_injection_")) or c["name"] == "denied_insults"]
        names = {"weapons_firearms", "weapons_other", "explosives", "violence_threats", "terrorism", "self_harm_suicide", "illegal_activities", "harassment_hate", "aws_access_key", "aws_secret_key", "github_token", "slack_token", "generic_api_key"}
        patterns = [{"pattern_type": "prebuilt", "pattern_name": p["name"], "action": "BLOCK"}
                    for p in options["prebuilt_patterns"] if p["name"] in names]
        patterns.append({"pattern_type": "regex", "name": "explicit-sexual-content", "action": "BLOCK",
                         "pattern": r"(?i)\b(?:porn(?:ography|ographic)?|erotic(?:a)?|explicit\s+sex(?:ual)?|sexual\s+intercourse|orgasm\w*|masturbat\w*|genitals?|penis|vagina|fellatio|cunnilingus|nude\s+(?:photo|image)s?|sexuell\w*|pornograf\w*|pornograph\w*|geschlechtsverkehr|vergewaltig\w*)\b"})
        response = client.get("/v2/guardrails/list")
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("guardrails", []) if isinstance(payload, dict) else payload
        existing = {g["guardrail_name"]: g for g in rows}
        guards = []
        for mode in ("pre_call", "post_call"):
            name = "litellm-free-web-" + mode
            guards.append(name)
            if name not in existing:
                response = client.post("/guardrails", json={"guardrail": {
                    "guardrail_name": name,
                    "litellm_params": {"guardrail": "litellm_content_filter", "mode": mode, "default_on": False,
                                        "categories": categories, "patterns": patterns, "severity_threshold": "low"},
                    "guardrail_info": {"description": "f24-sales fixed-prompt demo: content and secret filters; rule-based, not a guarantee of semantic moderation."}}})
                response.raise_for_status()
            print("Guardrail ready:", name)
        token = env.get("FREE_WEB_LITELLM_API_KEY")
        binding = "backend-enforced request guardrails"
        if not token:
            key_request = {"key_alias": web_alias, "models": web_models,
                "guardrails": guards, "allowed_routes": ["/v1/chat/completions"],
                "metadata": {"purpose": "f24-sales fixed-prompt demo"}}
            response = client.post("/key/generate", json=key_request)
            if response.status_code == 403 and "Enterprise" in response.text and "guardrails" in response.text:
                # Supported OSS workflow: mandatory backend-supplied request guardrails.
                # No license bypass; the key itself does not enforce the guardrails.
                del key_request["guardrails"]
                response = client.post("/key/generate", json=key_request)
            else:
                binding = "key-bound guardrails plus backend enforcement"
            response.raise_for_status()
            token = response.json()["key"]
        response = client.get("/key/info", params={"key": token})
        response.raise_for_status()
        if response.json()["info"].get("key_alias") != web_alias or token == env.get("CLIENT_KEY"):
            raise RuntimeError("Refusing to modify a key not dedicated to the website")
        # key_type=llm_api would replace explicit routes with the broad preset.
        response = client.post("/key/update", json={"key": token, "models": web_models,
                                                    "allowed_routes": ["/v1/chat/completions"]})
        response.raise_for_status()
        response = client.get("/key/info", params={"key": token})
        response.raise_for_status()
        info = response.json()["info"]
        if set(info.get("models") or []) != set(web_models) or info.get("allowed_routes") != ["/v1/chat/completions"]:
            raise RuntimeError("Dedicated website key restrictions could not be verified")
        changes = {"FREE_WEB_LITELLM_BASE_URL": "https://127.0.0.1", "FREE_WEB_LITELLM_PORT": env.get("LITELLM_PORT", "2001"),
                   "FREE_WEB_LITELLM_API_KEY": token, "FREE_WEB_GUARDRAILS": ",".join(guards)}
        original = path.read_text()
        lines = [line for line in original.splitlines()
                 if line.split("=", 1)[0].strip() not in changes and line != "# f24-sales website backend only"]
        atomic_write(path, "\n".join(lines) + "\n\n# f24-sales website backend only\n" + "\n".join(k + "=" + v for k, v in changes.items()) + "\n")
        write_json(Path(root) / "web_guardrails.json", {"schema_version": 1, "guardrails": guards, "categories": categories,
                    "patterns": patterns, "key_alias": web_alias, "models": web_models,
                    "allowed_routes": ["/v1/chat/completions"], "rate_limits": "No additional limits configured; upstream quotas still apply.",
                    "enforcement": binding,
                    "limitations": "Built-in regex/keyword filters, not exhaustive semantic moderation. Website accepts only fixed harmless prompts."})
        print("Dedicated key saved to .env (0600). No secret values printed.")


if __name__ == "__main__":
    provision()
