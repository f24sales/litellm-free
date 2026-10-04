# litellm-free

Download the curated model catalog from **[www.f24-sales.com](https://www.f24-sales.com/)** and import it into your own LiteLLM installation. Model information is preserved when importing through files, the API or SQL.

OpenAI-compatible `/v1/models` services can be integrated. As of September 2026, Groq, Kilo, Nous Portal, NVIDIA, OpenCode Zen and OpenRouter are preconfigured. The importer validates routes against explicit endpoint and credential presets.

**[LiteLLM YAML](https://www.f24-sales.com/litellm-config.yaml)** · **[.env template](env.example)** · **[Import guide](IMPORT.md)**

## Setup

```sh
git clone https://github.com/f24sales/litellm-free.git "$HOME/litellm-free"
cd "$HOME/litellm-free"
python3 -m pip install --target "$HOME/.local/share/litellm-free/python" -r requirements-import.txt
export PYTHONPATH="$HOME/.local/share/litellm-free/python${PYTHONPATH:+:$PYTHONPATH}"
cp env.example .env
cp config.conf_example config.conf
chmod 600 .env config.conf
# Enter your gateway keys and LiteLLM address/bearer token.
```

Store your keys in `.env`; `config.conf` contains the source, target address and configurable delay before online downloads (`IMPORT_DELAY_SECONDS=10`). The shared `python_header.py` loads these files; injected process environment variables take precedence. Both files stay local. The LiteLLM bearer token needs model-management permissions; an ordinary chat key is insufficient.

## Catalog and configuration

```sh
# Native LiteLLM YAML; downloading requires no keys.
python3 import_litellm.py pull --output config.yaml --force
# Select routes using locally available gateway keys:
python3 import_litellm.py file --format yaml --output config.yaml --force
```

The fixed filename is a symlink to the latest `config_DATETIME.yaml`. Set `LITELLM_FREE_ARCHIVE=1` in `config.conf` to retain earlier versions under `archiv/`; the default is `0`. Repeating the operation with identical content does not create another version. Downloads, local configurations and the archive are excluded from Git.

| Target | Command with `python3 import_litellm.py` |
| --- | --- |
| HTTPS API → LiteLLM database | `api --env-file .env` |
| Adopt matching existing free-sync routes | `api --env-file .env --adopt-managed-by free-sync` |
| Add model information to existing routes only | `api --env-file .env --patch-managed-by free-sync` |
| Direct SQL inside the existing LiteLLM container | `sql --env-file .env --container litellm-database` |

Before writing to the database, check the same command with `--dry-run`. Metadata mode preserves existing IDs, ownership, routes and keys. Explicit adoption with `--adopt-managed-by` also preserves existing IDs and checks the previous route identity. Without these options, models managed by other owners remain unchanged. `--prune` removes obsolete routes owned by this importer after a successful import; it is disabled by default. Use `--input catalog.json` or `--input config.yaml` to read a local file.

## Container and systemd

```sh
podman build -f Containerfile -t localhost/litellm-free-import:latest .
podman run --rm --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --env-file config.conf --env-file .env localhost/litellm-free-import:latest api --dry-run
```

The image contains the importer and its libraries. Your LiteLLM URL must be reachable from the container. Docker can replace Podman.

```sh
install -d -m 700 "$HOME/.config/litellm-free"
install -m 600 .env config.conf "$HOME/.config/litellm-free/"
install -d "$HOME/.config/systemd/user"
cp deploy/litellm-free-import.service deploy/litellm-free-import.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user start litellm-free-import.service
# Optional: twice daily at 00:10 and 12:10 UTC:
systemctl --user enable --now litellm-free-import.timer
```

The unit expects the checkout at `~/litellm-free` and the libraries installed above in the separate Python package directory. No virtual environment is needed. [deploy/container.conf](deploy/container.conf) is the optional drop-in for container operation. See [IMPORT.md](IMPORT.md) for details.

When `IMPORT_HOOK_URL` and `IMPORT_HOOK_BEARER` are set in the local `.env`,
the importer posts the final result to that OpenClaw-compatible endpoint with
`curl`. It sends `import_succeeded` only after LiteLLM readback succeeds and
`import_failed` when the import or readback fails. The body contains the
credential-free model diff; the bearer is sent only as an HTTP header.

```sh
python3 -m pip install --target "$HOME/.local/share/litellm-free/python" pytest
python3 -m pytest -q tests
```

The [OpenClaw plugin](litellm-free-hook/) receives LiteLLM-Free update webhooks.
It refreshes the model catalogs in OpenCode, Hermes, and OpenClaw.
It sends update notifications and model changes to Telegram.
