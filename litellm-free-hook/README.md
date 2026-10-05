# LiteLLM-Free OpenClaw receiver

Authenticated `POST /plugins/litellm-free` processes the two LiteLLM-Free event
sources without running an LLM. Install the complete repository at
`/opt/safrano9999/litellm-free`. The canonical refresh entrypoint is
`ops/refresh-models.sh`. Copy `litellm-free-hook/config.json_example` to
`litellm-free-hook/config.json`, then set `OPENCLAW_PLUGIN_ROOTS` to
`/opt/safrano9999/litellm-free/litellm-free-hook` in the container environment.
Regenerate the ephemeral configuration and reload the gateway.
The plugin verifies the existing injected `OPENCLAW_GATEWAY_TOKEN` using a
constant-time comparison. Its route uses OpenClaw's plugin-owned authentication:
forwarded client-IP headers do not grant access and are not used. This also works
when rootless Caddy forwards a host-local request with a loopback client address.
Other Gateway routes retain their existing authentication and proxy checks.

Scan results send one fixed status line. Every successful import event runs the
fixed refresh script before confirming that models have reloaded, including an
already-current import. Unchanged scans still do not schedule an import.
The refresh changes model lists only, preserving runtime Voice/MCP settings.
It verifies OpenCode's running API and Hermes' native provider catalog, and
retries previously failed reloads even when the downloaded catalog is unchanged.
When no local `config.json` exists, the receiver uses `config.json_example`;
an explicit plugin `configPath` still selects the instance configuration.
Successful updates list added and removed models as one CSV-style line per model:
`➕ Kilo, Liquid, lfm-2.5-2.6b:free` or
`➖ OpenRouter, Poolside, laguna-s-2.1:free-think`. They can also attach the
sanitized diff. Import, prerequisite and refresh failures send only the failure
line.

Telegram status text is fixed:

- `🟢 LiteLLM-Free: Scan valid. No model changes.`
- `🟢 LiteLLM-Free: Scan valid. Model changes ...`
- `🔴 LiteLLM-Free: Scan not valid.`
- `🟢 LiteLLM-Free: Update valid, models reloaded.`
- `🔴 LiteLLM-Free: Update not valid.`

Run IDs and delivery receipts remain internal. Notifications contain no test
prefixes, route counts or diagnostic text.

Configuration is read for every event. Persistent runtime data is grouped under
`worker/receipts/` (processing and delivery records) and `worker/diffs/` (sanitized
JSON attachments). These directories and the live configuration are excluded
from Git. A repeated successful event does not rerun a recorded refresh;
failed refreshes and failed delivery can be retried. A crash between an external side effect and its
receipt can still repeat that step, so the refresh script must stay idempotent.
Refresh failure is a processed event with red Telegram feedback, while delivery
failure returns HTTP 503. Neither event depends on receiving the other first.

The general Telegram helper owns route-to-chat mapping. This plugin owns event
interpretation, the green/red status circles and model-refresh decisions.
