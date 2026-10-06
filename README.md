# litellm-free

Free LLM routes come and go. I run a couple of LiteLLM proxies and got tired of hand-editing configs every time Groq, OpenRouter or Kilo changed their free tier. So I built a scanner that checks which free routes actually answer, publishes the result on **[f24-sales.com](https://www.f24-sales.com/)**, and this repo pulls that list into your own LiteLLM. Maybe it saves you the same chore.

Currently covered: Groq, Kilo, Nous Portal, NVIDIA, OpenCode Zen and OpenRouter. Any OpenAI-compatible `/v1/models` endpoint can be added.

**[LiteLLM YAML](https://www.f24-sales.com/litellm-config.yaml)** · **[.env template](env.example)** · **[Import guide](IMPORT.md)**

## Quickstart

```bash
git clone https://github.com/f24sales/litellm-free.git ~/litellm-free
cd ~/litellm-free
python3 -m pip install --target ~/.local/share/litellm-free/python -r requirements-import.txt
export PYTHONPATH=~/.local/share/litellm-free/python${PYTHONPATH:+:$PYTHONPATH}
cp env.example .env && cp config.conf_example config.conf && chmod 600 .env config.conf
```

Put your gateway keys and your LiteLLM URL + bearer token into `.env`. The bearer needs model-management rights; a plain chat key won't do.

Then see what would happen:

```bash
python3 import_litellm.py api --env-file .env --dry-run
```

Happy with the diff? Drop `--dry-run`.

## What you can do with it

Just want the YAML? No keys needed:

```bash
python3 import_litellm.py pull --output config.yaml --force
```

Only the routes you actually have keys for:

```bash
python3 import_litellm.py file --format yaml --output config.yaml --force
```

Write into a running LiteLLM:

| Goal | Command |
| --- | --- |
| Push via HTTPS API into LiteLLM's database | `python3 import_litellm.py api --env-file .env` |
| Take over routes an earlier free-sync created | `… api --env-file .env --adopt-managed-by free-sync` |
| Only update metadata on existing routes | `… api --env-file .env --patch-managed-by free-sync` |
| Write straight into the Postgres inside the LiteLLM container | `… sql --env-file .env --container litellm-database` |

`config.yaml` is a symlink to the newest `config_DATETIME.yaml`. Set `LITELLM_FREE_ARCHIVE=1` in `config.conf` if you want old versions kept under `archiv/`. Identical content doesn't create a new file.

## What it won't do

- Won't touch models owned by anyone other than this importer.
- Won't delete anything unless you pass `--prune` or set `IMPORT_PRUNE=1`.
- Won't invent a context window. If the gateway doesn't report a limit, the field stays empty.
- Won't add input and output limits together — they're kept separate, the way the clients expect them.
- Won't change your configured defaults or fallbacks. If one of them disappears upstream, the status report tells you.

## Running it on a schedule

Container (Podman shown, Docker works the same):

```bash
podman build -f Containerfile -t localhost/litellm-free-import:latest .
podman run --rm --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --env-file config.conf --env-file .env localhost/litellm-free-import:latest api --dry-run
```

systemd user timer, twice a day at 00:10 and 12:10 UTC:

```bash
install -d -m 700 ~/.config/litellm-free
install -m 600 .env config.conf ~/.config/litellm-free/
install -d ~/.config/systemd/user
cp deploy/litellm-free-import.service deploy/litellm-free-import.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now litellm-free-import.timer
```

The unit expects the checkout at `~/litellm-free` and the packages in the directory from the quickstart. No venv needed. Details and the container drop-in are in [IMPORT.md](IMPORT.md).

## Telling other tools about changes

If `IMPORT_HOOK_URL` and `IMPORT_HOOK_BEARER` are set, the importer posts the result to that endpoint after LiteLLM has confirmed the new state — `import_succeeded` or `import_failed`, with the model diff in the body and no credentials in it.

The [OpenClaw plugin](litellm-free-hook) on the receiving side refreshes the model lists in OpenCode, Hermes and OpenClaw and sends a Telegram note about what changed. `ops/refresh-models.sh` does the same reconciliation by hand. How that reload works internally is documented in [ops/](ops/).

## Tests

```bash
python3 -m pip install --target ~/.local/share/litellm-free/python pytest
python3 -m pytest -q tests
```

## Honest caveats

The SQL path has only been exercised against my own LiteLLM/Postgres setup. The API path is what I use daily. Free tiers are promotional by nature — expect routes to vanish between scans; that's the whole reason this thing exists.

Issues and PRs welcome.
