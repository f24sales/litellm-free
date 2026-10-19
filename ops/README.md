# Client catalog refresh

`refresh-models.sh` discovers the live `litellm-free` catalog once, validates all
three client configurations, and launches these sibling scripts in parallel:

- `openclaw`: updates its catalog, lets the gateway hot-reload the configuration,
  then checks `openclaw models list --provider litellm-free --json`. The running
  gateway must return the exact model IDs and the context sizes supplied by the
  catalog. Up to three bounded attempts allow hot-reload to finish; stale or local fallback results fail.
  It does not trigger global provider discovery with `--refresh`.
- `opencode`: updates its catalog, invalidates the running API's configuration
  cache and instances when needed, then checks the loaded catalog.
- `hermes-agent`: updates its catalog and checks Hermes' native provider
  normalization. Hermes rereads the configuration on its next `/model` call.

Each script is also directly executable, for example:

```sh
/opt/safrano9999/litellm-free/ops/openclaw
/opt/safrano9999/litellm-free/ops/opencode
/opt/safrano9999/litellm-free/ops/hermes-agent
```

A standalone invocation discovers the catalog itself and needs only its own
client configuration and service. Run these scripts inside the client container
with its existing provider environment. Keep `refresh_common.py` beside them and
the repository's `llm_model_metadata.py` in the parent directory. Invoke scripts
by their path; do not put this directory ahead of the actual client CLIs in PATH.

No script restarts a gateway or container. OpenClaw success confirms the running
gateway catalog, not merely a successful configuration write or CLI exit.
Individual status/lock files and the aggregate status are stored under
`REFRESH_RUNTIME_DIR` (default `/var/lib/litellm-free`). Failed client updates
remain pending for a retry; the other client scripts still run.
