# litellm-free · F24 SALES

**Website: [www.f24-sales.com](https://www.f24-sales.com/)**

Freely accessible AI chat models, their aggregators, and verified API routes in
one place. This repository contains the website, LiteLLM synchronization,
model checks, an agent integration for researching new models, and an importer
for your own LiteLLM installation.

OpenAI-compatible `/v1/models` services can be integrated. As of September 2026,
**OpenRouter, Groq, Kilo, Nous Portal, OpenCode Zen, and NVIDIA Build** are
preconfigured. Model developers and aggregators are listed separately.

## Website

- One shared entry per model, showing its available aggregators.
- Context and output limits, modalities, open weights, and thinking per API route.
- Information dialogs with signup links, API base URLs, documentation, and sources.
- Original model IDs, official model pages, and locally hosted logos.
- Aggregator and developer filters, plus a Fire demo with predefined questions.
- LiteLLM downloads and a GitHub button using the original GitHub icon.

Music, guardrails, decision models, embeddings, speech, and pure routers are
excluded from the regular chat model catalog. Cards use routes that passed the
scan; historical notes may mention earlier access. A successful scan is a
snapshot, not a guarantee of continued availability.

**🏆**: first listed in our scans. **🥈**: the next distinct discovery date.
Later aggregators share third place. This order indicates neither quality nor
exclusive access. Earlier discovery order is unknown for models in the initial
catalog.

## Import

**[Download the LiteLLM configuration](https://www.f24-sales.com/litellm-config.json)**
· **[Download the .env template](https://www.f24-sales.com/litellm.env.example)**

By default, the importer downloads directly from the address above on
**www.f24-sales.com**. The JSON file is also valid YAML and can serve as a
LiteLLM startup configuration. API keys appear only as environment variable
references.

```sh
git clone https://github.com/f24sales/litellm-free.git
cd litellm-free
python3 -m pip install --user -r requirements-import.txt
cp import.env.example .env
chmod 600 .env
# Edit .env and enter your own provider keys.
```

| Method | Command | Result |
| --- | --- | --- |
| Configuration file | `python3 import_litellm.py file --env-file .env` | `litellm-free.json` for `litellm --config` |
| HTTP API → database | `python3 import_litellm.py api --env-file .env` | Store models through the LiteLLM management API |
| Direct SQL | `python3 import_litellm.py sql --env-file .env --container litellm-database` | Transaction in the running LiteLLM container's PostgreSQL database |

All methods support `--dry-run`, repeated `--env-file` arguments, and `--input`
for an already downloaded JSON file. Providers without a local key are skipped.
Models managed elsewhere are not overwritten; the importer does not delete
models. SQL uses the installed LiteLLM version's encryption. Existing database
and salt keys are preserved.

**Complete examples, database prerequisites, Docker/Podman, and reload behavior:
[IMPORT.md](IMPORT.md).**

## Scanning and agent research

### AI-container client refresh

`ops/refresh-models.sh` queries the configured `litellm-free` OpenAI-v1 group
once and reconciles **additions and removals** in OpenClaw, Hermes and OpenCode.
It does not run the ephemeral generators: Voice, MCP, credentials, model
preferences and other providers remain intact. An empty/unreachable catalog
fails without replacing the existing lists. Failed service reloads are retried
on the next invocation even if the upstream catalog has not changed.

OpenCode and Hermes are reloaded after changes. Success requires the running
OpenCode API and Hermes' native provider reader to contain the live catalog;
OpenClaw uses its existing configuration watcher. The refresh is locked against
overlapping hook/cron runs and writes a credential-free status receipt under
`/var/lib/litellm-free`.

The Fedora Core integration installs this repository at
`/opt/safrano9999/litellm-free` and the `image/runtime` files. The cron job runs
at **00:10 and 12:10 UTC** through `litellm-free-refresh.service`. Its environment
comes from the container's systemd environment injection, not cron's empty
environment. `config.conf_example` follows the shared `config.sh` presets:
`IMPORT_SOURCE_URL=https://www.f24-sales.com/litellm-config.yaml` and
`LITELLM_FREE_PROVIDER=litellm-free`.

The scheduled task downloads and validates the public YAML catalog, then always
refreshes client lists from their authorized live `/v1/models` endpoint. The
public download is not itself an authorized model list: the existing LiteLLM
import/key-management pipeline still owns database imports and key restrictions.
This task never changes those restrictions or adds routes to the database.
An unchanged public file therefore does **not** skip client reconciliation.

`tests/live_catalog_roundtrip.py` is an explicit opt-in integration test. It adds
one isolated, temporary mock model to LiteLLM and the existing client key's
allowlist, verifies all three clients, and removes only its own test entry in a
`finally` block. It does not make inference calls or rotate credentials.

```text
Provider catalogs → LiteLLM sync → API scan → research new IDs
                                                  ↓
                                      validated model metadata
                                                  ↓
                                    website and LiteLLM download
```

The API scan checks reachability. An LLM agent researches only new or unreviewed
IDs: developers, model types, duplicates, capabilities, thinking support through
each aggregator, and reliable sources. Mappings are structurally validated
before being accepted. Previously reviewed entries are not sent to an LLM again
on every scan.

For the built-in Codex CLI adapter, set these values in the local `.env`:

```env
MODEL_REVIEW_AGENT=codex
MODEL_REVIEW_TIMEOUT=900
```

Codex CLI must be installed and authenticated. Other agents, such as Claude Code,
Hermes, or OpenClaw, can be connected through `MODEL_REVIEW_COMMAND`.
Without an agent, a research task is saved for review. The complete workflow,
output format, and error handling are documented in **[AGENT_REVIEW.md](AGENT_REVIEW.md)**.

## Local preview

Requires Python 3.11 or newer; Node.js is needed for UI tests. The preview needs
neither API keys nor a LiteLLM server.

```sh
python3 -m pip install --user -r requirements-web.txt
cp examples/model_probe_results.json model_probe_results.json
python3 -m uvicorn web:app --host 127.0.0.1 --port 8080 --no-access-log
```

Then open **http://127.0.0.1:8080/**. The sanitized example catalog is dated
September 29, 2026. Fire requires its own backend configuration.

## Running your own synchronization

Requires an existing LiteLLM proxy with PostgreSQL and `STORE_MODEL_IN_DB=True`.
Fill in `.env` using [.env.example](.env.example).

```sh
python3 free_sync.py --dry-run
python3 free_sync.py
python3 model_probe.py
```

`free_sync.py` updates managed deployments. `model_probe.py` sends real test
requests, starts research when needed, and restricts existing client/web keys
to passing routes. Provider limits still apply. `python3 setup_web_key.py`
configures the separate key and pre/post filters for the optional Fire demo.
Keys remain in the backend; the web application does not store responses.

## Model IDs

Direct aggregator requests use that aggregator's base URL and original model ID,
for example `https://api.groq.com/openai/v1` with `openai/gpt-oss-120b`.
By contrast, `groq/openai/gpt-oss-120b` is a local LiteLLM routing name.
Additional `-fast`/`-think` aliases are local presets. Actual developer prefixes
and documented suffixes such as `:free` are preserved.
A catalog entry alone does not prove working inference. Some OpenCode free
routes require the app or CLI; this does not apply to every model.

## Documentation and tests

| File | Contents |
| --- | --- |
| [IMPORT.md](IMPORT.md) | JSON download, `.env`, file, API, and SQL import |
| [AGENT_REVIEW.md](AGENT_REVIEW.md) | Post-scan research agent, adapters, and validation |
| [MODEL_REVIEW_TASK.md](MODEL_REVIEW_TASK.md) | Complete research task for the agent |
| [WEB.md](WEB.md) | Presentation, data model, branding, Fire, and deployment |
| [SYNC.md](SYNC.md) | Synchronization, credentials, virtual keys, and provider rules |
| [model_metadata.json](model_metadata.json) | Researched mappings and sources |

```sh
python3 -m pip install --user -r requirements-web.txt pytest
python3 -m pytest -q tests
node --test tests/catalog-ui.test.cjs
```

Local credentials, current scan files, research tasks, and run logs stay outside
Git. Sources and licenses for bundled logos, the font, and HTMX are listed under
[static/logos](static/logos/SOURCES.md),
[static/fonts](static/fonts/Adwaita-LICENSE.txt), and
[static/vendor](static/vendor/htmx-LICENSE.txt). F24 SALES and GitHub vectors
retain their original proportions.
