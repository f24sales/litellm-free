"""Fixed-prompt demo. Credentials and all upstream errors remain on the server."""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import ssl
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values
import httpx

from catalog_store import ROOT, non_chat_identities, read_json, write_json

QUESTIONS = {
    "howdy": "Howdy! How are you?",
    "denmark": "What is the capital of Denmark?",
    "france": "What is the capital of France?",
    "math": "What is two plus two?",
    "hello": "Say hello in three languages.",
    "rainbow": "Name the colors of a rainbow.",
    "haiku": "Write a friendly haiku about a sunny morning.",
    "funfact": "Tell me a fun fact about penguins.",
}
EMOJIS = {str(i): emoji for i, emoji in enumerate("👋 😊 🤖 🚀 🌈 ☀️ 🌻 🐧 🐱 🐶 🐰 🦊 🐮 🐼 🐸 🦋 🐝 🐢 🦄 🐬 🌍 🌙 ⭐ 🌸 🌱 🍀 🍎 🍋 🍓 🍉 🍕 🍪 🍵 ☕ 🎨 🎵 🎈 🎉 ✨ 💡 📚 🧠 💻 🛸 🛰️ 🧩 🏡 ⛵ 🏔️ 🌊".split())}
REFUSAL = re.compile(r"(?:only.{0,60}(?:opencode|coding agent|coding assistant)|(?:opencode|coding agent).{0,60}(?:only|restricted|required)|(?:cannot|can't|unable to|not allowed to|not permitted to).{0,35}(?:answer|respond|assist|fulfill|help with|comply)|(?:access|subscription|authentication).{0,25}(?:required|denied)|not supported for this)", re.I | re.S)
SENSITIVE = re.compile(r"(?:\bsk-[a-zA-Z0-9_-]{12,}|\bBearer\s+\S{12,}|Traceback \(most recent call last\)|litellm\.[A-Za-z]*Error)", re.I)
EXPECTED = {"denmark": re.compile(r"\bcopenhagen\b", re.I), "france": re.compile(r"\bparis\b", re.I), "math": re.compile(r"\b(?:4|four)\b", re.I)}


def settings(root=ROOT):
    env = dict(dotenv_values(Path(root) / ".env"))
    env.update({k: v for k, v in os.environ.items() if k.startswith("FREE_WEB_") and v})
    base = urlsplit(env.get("FREE_WEB_LITELLM_BASE_URL") or "")
    if base.scheme != "https" or not base.hostname or base.username or base.password:
        raise ValueError("HTTPS backend is not configured")
    port = base.port or int(env.get("FREE_WEB_LITELLM_PORT") or "443")
    host = f"[{base.hostname}]" if ":" in base.hostname else base.hostname
    endpoint = urlunsplit((base.scheme, f"{host}:{port}", base.path.rstrip("/"), "", ""))
    token = env.get("FREE_WEB_LITELLM_API_KEY") or ""
    guards = [s.strip() for s in (env.get("FREE_WEB_GUARDRAILS") or "").split(",") if s.strip()]
    if not token or not guards:
        raise ValueError("Demo credentials or filters are missing")
    return endpoint, token, guards


class Demo:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.state_file = self.root / "fire_state.json"
        self.log_file = self.root / "fire_events.jsonl"

    def states(self):
        return read_json(self.state_file) if self.state_file.exists() else {}

    def record(self, route_id, state):
        record = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "state": state}
        with self.state_file.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            states = self.states()
            states[route_id] = record
            write_json(self.state_file, states)
            fd = os.open(self.log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a") as log:
                log.write(json.dumps({"route_id": route_id, **record}) + "\n")
                log.flush()
                os.fsync(log.fileno())
        return record

    async def fire(self, route_id, question_id, emoji_id, catalog, *, transport=None):
        route = catalog["models"].get(route_id)
        # Never accept user text, an upstream URL, headers, or arbitrary model names.
        if (not route or not route["active"] or question_id not in QUESTIONS or emoji_id not in EMOJIS
                or route["details"]["duplicate_key"] in non_chat_identities(catalog)):
            return {"state": "yellow"}
        state, answer = "yellow", None
        try:
            endpoint, token, guards = settings(self.root)
            # Never fire endpoints with non-token charges as a chat test.
            if not route["details"].get("pricing_caveat"):
                payload = {
                    "model": route["snapshot"]["alias"],
                    "messages": [{"role": "user", "content": QUESTIONS[question_id] + "\nPlease give a short, friendly answer and include this emoji: " + EMOJIS[emoji_id]}],
                    "max_tokens": 4096, "stream": False, "guardrails": guards,
                    "no-log": True, "cache": {"no-cache": True, "no-store": True},
                }
                async with httpx.AsyncClient(verify=ssl.create_default_context(), timeout=httpx.Timeout(75, connect=8),
                                             follow_redirects=False, trust_env=False, transport=transport) as client:
                    response = await client.post(endpoint + "/v1/chat/completions", json=payload, headers={
                        "Authorization": "Bearer " + token, "x-litellm-enable-message-redaction": "true",
                        "Cache-Control": "no-store", "Content-Type": "application/json",
                    })
                applied = {s.strip() for s in response.headers.get("x-litellm-applied-guardrails", "").split(",")}
                if response.status_code == 200 and set(guards) <= applied:
                    data = response.json()
                    choice = (data.get("choices") or [{}])[0]
                    message = choice.get("message") or {}
                    content = message.get("content")
                    if isinstance(content, list):
                        content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") in ("text", "output_text"))
                    content = content.strip() if isinstance(content, str) else ""
                    expected = EXPECTED.get(question_id)
                    if (content and len(content) <= 12000 and not message.get("refusal")
                            and choice.get("finish_reason") not in ("content_filter", "length", "tool_calls")
                            and not REFUSAL.search(content) and not SENSITIVE.search(content)
                            and token not in content and endpoint not in content
                            and (expected is None or expected.search(content))):
                        state, answer = "green", content
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, OSError):
            pass  # Upstream error text must never reach a browser or our logs.
        last = self.record(route_id, state)
        return {**last, **({"answer": answer} if state == "green" else {})}
