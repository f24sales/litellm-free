# Import the LiteLLM catalog

The published source is directly importable **[LiteLLM YAML](https://www.f24-sales.com/litellm-config.yaml)** containing `model_list`. It carries compact routing, stable model identity, provider and aggregator fields only. The full researched JSON catalog remains in the private backend; the public importer downloads only the compact YAML.

## Local files and credentials

Follow the [README setup instructions](README.md#setup). Copy [env.example](env.example) to `.env` and [config.conf_example](config.conf_example) to `config.conf`. The shared `python_header.py` loads the project configuration and additional `*.env` files; `.env` is loaded last. Local configuration and credential files are excluded from version control. `.env.example` and `import.env.example` point to the same environment template.

- `.env`: your own `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `KILO_API_KEY`, `NOUS_API_KEY`, `OPENCODE_API_KEY`, `NVIDIA_API_KEY`, `CLINE_API_KEY`, `INFRON_API_KEY` and LiteLLM bearer token. These gateways are preconfigured as of September 2026; OpenAI-compatible `/v1/models` services can be integrated through explicit validated presets.
- `config.conf`: `IMPORT_SOURCE_URL`, `LITELLM_BASE_URL`, optional ports/CA paths, `IMPORT_DELAY_SECONDS=10` and optional `IMPORT_PATCH_MANAGED_BY`, `IMPORT_ADOPT_MANAGED_BY` and `IMPORT_PRUNE`.

`LITELLM_ADMIN_KEY` can be the existing master key or an admin virtual key with model-management permissions. `LITELLM_VIRTUAL_KEY` is the fallback when `LITELLM_ADMIN_KEY` is empty. This does not grant additional permissions to an ordinary chat bearer token.

`--config /path/config.conf` selects the configuration explicitly. Multiple `--env-file` arguments are allowed; later nonempty values win. Nonempty injected process environment variables take precedence. Explicitly selected files are read without `${...}` interpolation. No shell code is executed.

```sh
python3 import_litellm.py api --config /etc/litellm-free/config.conf \
  --env-file /etc/litellm-free/providers.env --env-file /etc/litellm-free/proxy.env --dry-run
```

## Download YAML

```sh
python3 import_litellm.py pull --output config.yaml --force
python3 import_litellm.py file --format yaml --output config.yaml --force
```

`pull` requires no provider keys and saves native LiteLLM YAML with all routes by default. `file` selects routes with a locally available provider key; `--all-providers` produces the complete template. Configuration files contain `os.environ/…` references rather than resolved keys. The LiteLLM process that later loads the file needs the same provider environment variables. `model_name` is the local gateway alias; `f24_upstream_id` contains the original API model ID. An additional `openai/` in `litellm_params.model` selects LiteLLM's OpenAI-compatible adapter. Local `-think`/`-fast` aliases are not new upstream IDs:

```sh
litellm --config /path/to/config.yaml
```

Every output has a fixed filename, atomically updated as a symlink to a dated file alongside it. The default is `LITELLM_FREE_ARCHIVE=0`: the previous version created by the importer is removed after the switch. With `LITELLM_FREE_ARCHIVE=1` in `config.conf` or the injected environment, older versions move to `./archiv/`; an existing regular file is also archived. External symlink targets and existing archive history are preserved. Without `--force`, an existing output is not replaced. Identical content retains its existing version.

`--input config.yaml` or an explicitly selected JSON file works offline. `--source-url` overrides `IMPORT_SOURCE_URL`; remote sources require HTTPS, and HTTP is allowed only for localhost/loopback. For a local web service, set `IMPORT_SOURCE_UDS=/run/user/1000/litellm-free/litellm-free.sock` together with `IMPORT_SOURCE_URL=http://localhost/litellm-config.yaml`. `IMPORT_SOURCE_CA_FILE` adds trusted certificates when needed; otherwise, `LITELLM_CA_FILE` is respected. Redirects are not followed, and downloads include no provider or LiteLLM keys.

Before actual online runs, the importer waits ten seconds by default. `IMPORT_DELAY_SECONDS` or `--delay-seconds` changes this delay; offline files and `--dry-run` do not wait. Use `--delay-seconds 0` to disable the delay.

## API import and metadata patch

```sh
python3 import_litellm.py api --env-file .env --dry-run
python3 import_litellm.py api --env-file .env
```

LiteLLM requires a database connection and `STORE_MODEL_IN_DB=True`. New models are created through `/model/new`; models owned by this importer are updated through `/model/{id}/update`. IDs are stable. Models managed by other owners are skipped by default. No models are deleted by default. Routes without a provider key are skipped during regular imports.

For existing routes with a specific owner, an explicit mode adds the compact
`f24_*` route identity only:

```sh
python3 import_litellm.py api --env-file .env --patch-managed-by free-sync --dry-run
python3 import_litellm.py api --env-file .env --patch-managed-by free-sync
```

Alternatively, set `IMPORT_PATCH_MANAGED_BY=free-sync` in `config.conf`. This mode requires no provider keys. The model name and `managed_by` must match exactly; ambiguous matches abort the operation. IDs, owners, deployment parameters and keys are preserved. The PATCH body contains only `model_info`. A readback confirms the stored fields. No PATCH is sent when they already match. No new routes are created.

A separate mode fully adopts existing free-sync routes:

```sh
python3 import_litellm.py api --env-file .env --adopt-managed-by free-sync --dry-run
python3 import_litellm.py api --env-file .env --adopt-managed-by free-sync
```

Alternatively, set `IMPORT_ADOPT_MANAGED_BY=free-sync`. The exact name, previous owner, provider, upstream ID and variant must match. Existing IDs and metadata are preserved; the new owner is `f24-sales-import`. New routes receive a stable import ID. Later updates use the name and verified identity to find adopted routes with their original IDs. A stored `f24_import_hash` detects identical incoming parameters/model information and avoids repeated writes. The two owner modes are mutually exclusive.

Optionally, `--prune` or `IMPORT_PRUNE=true` removes obsolete routes owned by this importer. Checks cover the owner, fixed source, access group, route identity and local provider configuration. Obsolete owned routes are deleted only after successful writes and readback. Routes managed by other owners remain unchanged. An empty model selection aborts the operation. The generic default is `IMPORT_PRUNE=false`.

Remote targets require HTTPS. Set `LITELLM_ALLOW_HTTP=true` for an explicitly configured internal container endpoint such as `http://litellm-database:4000`. Downloaded data does not control this setting.

API calls do not form a single transaction. If a call fails, earlier changes may already be stored; rerunning reconciles the same model set. Allowed provider URLs, key references, presets and identities are validated before local keys are used. Descriptive metadata does not control commands, API targets or credentials.

## Direct SQL

```sh
python3 import_litellm.py sql --env-file .env --container litellm-database --dry-run
python3 import_litellm.py sql --env-file .env --container litellm-database
```

Use `--engine docker` for the same workflow with Docker. The worker receives models through stdin; database and encryption settings come from the existing LiteLLM container. It requires `psycopg` version 3, LiteLLM, `DATABASE_URL` and the existing `LITELLM_SALT_KEY` or `LITELLM_MASTER_KEY`. Keep the existing encryption key. Without `--container`, the worker runs in the local LiteLLM Python environment.

The SQL importer validates the ProxyModelTable schema, executes parameterized statements in a transaction, preserves lock flags and updates only models with `managed_by=f24-sales-import`. Aliases belonging to other owners are preserved, and conflicting IDs owned by others abort the transaction. An error rolls back the entire transaction; dry-run uses a read-only transaction. Metadata patches for other owners are available only through the API.

Direct SQL bypasses LiteLLM's API cache. LiteLLM workers must be reloaded or restarted afterwards. The importer does not restart services or extend virtual-key model permissions.

## Container and systemd

The container includes the importer for pull, file and API operations. Your own configuration and credentials are supplied at startup:

```sh
podman run --rm --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --env-file config.conf --env-file .env localhost/litellm-free-import:latest api --dry-run
```

The LiteLLM address must be reachable from the container; localhost refers to the container itself. Mount an internal CA certificate as PEM and set `LITELLM_CA_FILE` or `IMPORT_SOURCE_CA_FILE` as needed. For file output, mount a writable directory at `/data`:

```sh
mkdir -p output
podman run --rm --userns=keep-id --user "$(id -u):$(id -g)" \
  --env-file config.conf --env-file .env -v "$PWD/output:/data:Z" \
  localhost/litellm-free-import:latest pull --output config.yaml --force
```

Omit `--userns=keep-id` with Docker. Direct SQL runs from the host or an existing LiteLLM Python environment.

Host libraries are installed without a virtual environment using `pip --target ~/.local/share/litellm-free/python`. The service sets `PYTHONPATH` only for that package directory and uses `/usr/bin/python3`.

The optional systemd files use a separate `.env` and `config.conf` under `~/.config/litellm-free/`. The timer runs at **00:10 and 12:10 UTC**. An independent calendar timer cannot detect when an external scan finishes. When operating a scanner alongside the importer, the same service can instead start after a successful scan. The delay remains configurable through `IMPORT_DELAY_SECONDS`. Missed timer runs are caught up. The container drop-in is [deploy/container.conf](deploy/container.conf).

## Verification

Regular tests use generated model information without credentials and cover pull, source validation, JSON/YAML, archive switching, API ownership, metadata patches, repeated runs and SQL transport. The integration test [tests/sql_integration_check.py](tests/sql_integration_check.py) requires a separate PostgreSQL database named `f24_import_test` and a matching LiteLLM environment. A real SQL test against LiteLLM 1.101.0/PostgreSQL 18 on September 29, 2026 verified encryption, dry-run, insert/update, repeated runs, models managed by other owners and rollback.
### Optional OpenClaw webhook

The importer can send the final result to an OpenClaw-compatible HTTP hook after
LiteLLM accepts the models and the readback succeeds. Configure
`IMPORT_HOOK_URL`, `IMPORT_HOOK_BEARER`, and optionally
`IMPORT_HOOK_TIMEOUT_SECONDS` and `IMPORT_HOOK_CA_FILE` in the local `.env`.
The credentials are never part of the repository or the event body. A changed
import emits `import_succeeded` with the sanitized `added`, `removed`, and
`updated` model diff. A failed import or readback emits `import_failed`.
Diffs retain model, provider and aggregator identity, while removing credential fields,
HTTP headers/cookies, and URL credentials, query strings and fragments.
An unchanged import is skipped by default (`IMPORT_HOOK_ON_NO_CHANGE=false`).
Set it to `true` when another service has announced an upcoming import and needs
a completion event even if LiteLLM already has the desired models. A caller may
set `IMPORT_HOOK_RUN_ID` to correlate that completion with its scan.

Each event includes an `event_id`, preserved across up to three attempts on
transient HTTP or transport failures. Receivers should deduplicate that ID before
running actions. Authentication failures are not retried. Delivery failure is
reported in the import result; there is no durable delivery queue.

An optional `IMPORT_PRE_SUCCESS_COMMAND_JSON` in `config.conf` runs a local
prerequisite after successful import/readback and before the success event:

```dotenv
IMPORT_PRE_SUCCESS_COMMAND_JSON=["/absolute/path/sync-access", "--configured-target"]
IMPORT_PRE_SUCCESS_TIMEOUT_SECONDS=180
```

The command runs directly without a shell and receives the import status JSON on
stdin. Use it, for example, to update model access before a receiver refreshes
client catalogs. A nonzero exit or timeout sets `post_import.ok=false` and emits
`import_failed` with `phase="post_import"` and `import_ok=true`; it does not undo
the confirmed LiteLLM import. No success event is sent in that case. The command
is never run during dry runs or file exports. Its output is not forwarded.
