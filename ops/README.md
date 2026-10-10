# Client catalog refresh

`refresh-models.sh` discovers the live `litellm-free` catalog once, validates all
three client configurations, and launches these sibling scripts in parallel:

- `openclaw`: updates its catalog and runs
  `openclaw models list --provider litellm-free --refresh`.
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

No script restarts a gateway or container. The OpenClaw CLI refresh is not a
guarantee that an already-running gateway has replaced its in-memory catalog.
Individual status/lock files and the aggregate status are stored under
`REFRESH_RUNTIME_DIR` (default `/var/lib/litellm-free`). Failed client updates
remain pending for a retry; the other client scripts still run.
