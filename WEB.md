# F24 SALES free AI directory

Live: **https://www.f24-sales.com/** · [Repository](https://github.com/f24sales/litellm-free)

FastAPI, Pydantic, HTTPX, Jinja2, Starlette forms and locally hosted HTMX.
No external frontend assets, analytics, app cookies or stored answers. Cloudflare
Access is a separate login layer and uses authentication cookies.

The responsive layout uses the original v031 brand assets: F24 SALES blue
`#1975D1`, navy/silver surfaces, unchanged SVG wordmark and slanted-bar motif.
Adwaita Sans is served locally with its license in `static/fonts/`; animations
respect reduced-motion preferences. No third-party font or image requests.

## Install and run — no virtual environment

```sh
python3 -m pip install --user --upgrade pip
python3 -m pip install --user --upgrade -r requirements-web.txt
python3 catalog_store.py
python3 setup_web_key.py
python3 -m uvicorn web:app --host 0.0.0.0 --port 8080 --no-access-log
```

Run the provisioner only against your configured LiteLLM instance. It creates
`litellm-free-web-pre_call`, `litellm-free-web-post_call` and a **separate** key
for the `litellm-free` access group. Existing shared keys remain unchanged.
Credentials stay in `.env` with mode `0600`; no secrets belong in Git.

## Deterministic data

- `model_probe_results.json`: stable availability SOT from the latest full scan;
  it contains all routes tested in that run, status, safe catalog facts and chronology,
  but no bearer. Dated `model_probe_results-*.json` files remain as run records.
- `free_models.json`: LiteLLM deployment SOT used by `free_sync.py`; the bearer
  itself stays only in `.env` as `CLIENT_KEY`.
- `model_metadata.json`: source-backed, LLM-curated descriptions and explicit
  `duplicate_key` model identities, with original sources, limits and thinking
  controls. AI review happens offline; page requests never call a model.
- `model_registry.json`: durable, credential-free history of all observed routes,
  including removed ones. Bootstrapped from `news.md`; retained after log rotation.
  Active membership always comes from the source catalog. Back up this file.
- `fire_state.json` / `fire_events.jsonl`: last status and append-only status
  history. Only route ID, UTC timestamp and green/yellow; never answer text.

The main timeline has **one card per curated model**, sorted by its
first observed **UTC calendar date**, newest first. Later gateways are appended
inside that card and never move it to the top or create a second model card.
The registry stores discovery dates without clock times; original technical logs
remain unchanged. Each model also shows its shared history across gateways.
`🏆` marks all gateways first observed on the earliest day, even in different scans;
`🥈` marks the next distinct discovery day. Models already present on the initial
scan day receive neither award: their earlier arrival order is unknown.
The initial inventory (2026-09-21 in this installation) is listed without a
discovery-date label, including in gateway details. Its raw dates remain intact
in the backend history. Test timestamps are independent and remain visible.
Initial-inventory cards show the known model provider followed by currently
available aggregators in a white box with a blue outline, without dates, awards
or ranking; failed/removed routes stay out of that box. Routers and undisclosed
stealth entries are not presented as model manufacturers inside the box.
A later gateway is an additional access route, not a new model. Unavailable and
archived gateways retain historical priority; awards are not availability badges.
Gateway-specific limits, Thinking controls and direct model IDs remain in the
expanded details. IDs are the exact `upstream_id` from the catalog: creator
namespaces and genuine suffixes are retained, never stripped by a heuristic.
Internal LiteLLM aliases and `base`/`fast`/`think` tags are omitted from model
details; the demo uses readable preset labels and keeps its existing route IDs.
The demo selector contains passing routes from every available gateway of that model;
changing routes preserves the full selector. A bold-italic last-scan heading sits
directly below “via API.” and uses the scan timestamp, never the page-load time.
It also reports the current catalog's distinct model, provider and aggregator
totals, using the same cards and tile groups as the directory. These scan totals
stay unchanged when a visitor narrows the selection.
Small “Aggregators” and “Providers” headings label the two tile groups. Each tile
shows its distinct passing model count as a bare number (not route variants).
Search, separate filter menus, tabs and explanatory navigation are omitted;
the overview tiles themselves provide the selection controls.

Creator and aggregator logos are curated in `model_metadata.json` and bundled
locally under `static/logos/`, with sources and license beside them. Overview
logos occupy a 36 × 36 px box; inline model-card logos remain 16 × 16 px.
Neither is stretched; unknown brands have no invented logo.
No third-party image request is made by the browser.
Below the aggregators, a compact provider tile list shows only creators of
currently displayed non-stealth models. Stealth model cards remain in the directory.
Research marks routing products with `entry_type: "router"` (for example Auto Free
and Free Models Router); their operators remain aggregators, not model providers.
Routing offers are hidden only from the main web page and its tile counts.
They remain in the research, catalog/probe data, LiteLLM configuration and virtual
key model lists. Kilo and OpenRouter still appear as aggregators of real models.
Both groups use the same compact, responsive square tile grid (roughly 108 px),
with the logo centered above the name and bare count. The smaller 10 px country
flag sits in the bottom-right corner, away from the centered count.
Long labels wrap without truncation.
Tiles are keyboard-accessible selection buttons, initially all selected (blue
border). The first click isolates its tile and the matching opposite group.
Later clicks add unselected tiles and their counterparts, or remove a selected
tile and prune incompatible counterparts. Thus the latest click determines the
cross-group adjustment. A top "Select all" tile appears only while narrowed;
reaching all selected again or pressing it restores the initial state. Counts
remain the total available models per tile; filtering never changes chronology,
scan results, LiteLLM or keys. Preferences are not stored and no requests are made.
Anonymous models follow aggregators without inventing a provider tile.

The F24 wordmark keeps its intrinsic SVG dimensions. The bar motif is displayed
at twice its original width and height on all screen sizes, as requested; its
proportions stay unchanged. Motion changes only position and whole-image opacity.
`static/brand/F24SALES_Balken.svg` contains the exact, unmodified bar group
from `F24SALES_blau.svg`; its canvas excludes the wordmark. Bar height (48 units),
widths, spacing, shear, color and transparency match the complete logo. Use this
asset beside the API heading, not the separate background variant with 240-unit
bars. Overflow is clipped at the viewport edge without widening the page; the
bar motif sits 16 px lower and extends past the right edge.

Brand reference: F24 SALES Brandpaket v031, `00_Brandbook/Brandbook.html`, sections
04 (bar progression), 05 (spacing) and 06 (size and cropping). The package's
`05_Web/F24SALES_Hintergrund.svg` is a deliberately extended background variant;
the user's choice for this website is the original proportions of the main logo.
Keep the supplied main logo and brand package unchanged.

Local `static/catalog.js` uses IntersectionObserver to assemble tiles/cards once
on viewport entry, with short staggered logo/text movement. Native scrolling is
unchanged; reduced-motion preferences disable it. Content stays readable without
JavaScript or IntersectionObserver, and controls remain disabled without JS.
Completed/interrupted animations are cleaned up so changing a selection does not
replay already viewed cards. Printing and reduced motion remain animation-free.
Run UI interaction tests with `node tests/catalog-ui.test.cjs`.
Country flags use curated ISO codes and a primary
source in the same JSON, never gateway geography or a guessed model name.
The flag describes the provider's origin/base, not inference location, ownership
or an individual researcher's nationality. Unknown origins remain unflagged.
Pending logo assets use neutral initials, not a fabricated brand logo.

Cards start with the model creator and model name, followed by a first-gateway
panel (trophies for first place, silver medals for second place,
both with dates but no ordinal labels), specifications, an optional official model-page button,
and the researched description. Model-specific gateway details live behind the
plus disclosure. Aggregator Info and Sign up links open one shared native dialog
per aggregator, styled in F24 blue. Each dialog contains signup, API documentation,
the public API base URL, access conditions and generic source references.
Registration continues through the Create account link inside the dialog.
Info links do not change filter selections. Escape, close button and backdrop
close the dialog; focus returns to its opener. Native modal focus handling,
reduced motion and fragment-link fallback without JavaScript are supported.
OpenCode has a qualified app/CLI notice because direct API results vary by model.
The 2026-09-29 scan passed Space Bunny Free while nine other OpenCode routes
returned HTTP 403; an app restriction must not be stated as universal.

Aggregator `api_base` and `sources` are curated in `model_metadata.json` and
published only after HTTPS URL validation. Base URLs were checked on 2026-09-29
against the linked official API docs; Nous Research's official Hermes source
confirms its endpoint. Generic sources are excluded from model cards by exact,
slash-normalized URL matching against aggregator website/signup/docs/base/sources.
Model-specific pages on the same domains remain in Model sources.
Thinking availability, effort levels, defaults, toggle and budget controls come
from each model/gateway's own metadata; they are not shared from the card's
reference gateway. Unknown is shown as Not documented; explicitly unadvertised
support suppresses inherited controls. These are documented capabilities, not
a claim that every Thinking parameter was exercised by the availability probe.
Only research entries with `model_page_is_official: true` supply the model-page
button; aggregator reports, unidentified stealth models and related base-model
pages stay in the expanded sources, never masquerading as an official model page.
Kilo Auto Free links directly to Kilo's routing explanation; ALLaM-2 links to the
SDAIA model card hosted by Microsoft Foundry. Curated `stealth/` identities without
an official page show a non-clickable “Stealth model 🥷” label in the button style.
Ling 3.0 Flash Sante links to Ant Ling's own launch post. The curated
`model_page_label: "Official announcement"` changes the button caption accordingly;
its linked source determines the label even when another gateway supplies specs.
`display_name` is the curated heading; the original catalog name remains in details.
The compact podium shows only the first two distinct discovery days, with shared
places for same-day arrivals. Dates appear once per place, prominently on the
podium, not as separate day headings. Later gateways and initial-scan entries
remain in the expanded details. A three-line legend below the directory explains
first discovery, the next discovery date and all later aggregators sharing third
place. This is listing chronology in our scans, not quality or exclusive access.
There is no public archive view. Removed models stay in the local registry; `/catalog.json` no longer
exports a separate archive collection.
Ranking accents use outlines only: `#1975D1` at 100% for first place, 50% for
second place, and 25% for the remaining gateway details. All interiors keep the
normal light card background and dark text. Alpha applies only to borders, never
to text or controls. All cards share one continuous responsive grid across
discovery dates, with no partial rows reserved for individual days.
Descriptions, limits and source links are not repeated; the expanded panel adds only further facts, per-gateway
differences, model IDs and an aggregator Info link. The initial history stays in the backend.

Only reviewed, explicit `duplicate_key` assignments merge model identities. An
LLM must review new aliases against sources and record the decision in the research
JSON; no fuzzy matching runs in the website. Different versions remain separate.
Stealth alias groups carry an uncertainty note, not a claim of identical weights.
Catalog limits take precedence over research; unknown facts remain unknown.
Only positive capabilities are displayed: green `✓ Reasoning` when support is
explicitly true and green `✓ Open weights` when manually reviewed metadata has
`open_weights: true` plus an HTTPS `open_weights_source`; the review date is saved
as `open_weights_checked_at`. False, missing or unverified values have no badge.
Weights evidence must identify the exact model, not merely its base or family.
Reviewed aliases share this model-level fact. Routers and stealth identities are
not inferred to have weights, and open weights does not imply an unrestricted license.
All decisions live in the research JSON; rendering does not research or fetch them.

`model_probe.py` refreshes the stable probe SOT after a scan. The web app joins
that file with `model_metadata.json` and refreshes on changed local files;
browsing makes no external API calls. The registry and test-state files are
local runtime data and deliberately excluded from Git.

Active model cards contain only 🟢 probe routes. Their historical gateway timeline
can name earlier, currently unavailable providers without enabling those routes. Yellow and red
routes remain in the dated/full probe JSON for diagnosis. `green_keys.py` keeps
both `litellm-free` and `litellm-free-web` virtual keys restricted to exactly
those green model aliases.

## Optional Fire demo

Only server-defined questions and emoji IDs are accepted. Arbitrary prompts,
model names, upstream URLs or guardrail settings cannot be supplied by browsers.
HTTPX sends requests to the configured HTTPS LiteLLM/Caddy endpoint. Responses
stay transient in the page; green displays an answer, yellow displays no answer
or error details. No-log/redaction and no-cache flags are sent to LiteLLM.
Upstream providers still have their own data-retention policies.
The demo shows the selected gateway's last test date/time (UTC), or “Not yet
tested”. This is persisted per model route in `fire_state.json`, including yellow
results; changing gateways restores that route's own timestamp and state.
During a Fire request the disabled button shows “Running” and a spinner with a
fixed black ring and F24-blue (`#1975D1`) / white quarters. A four-second cycle
alternates one rotation with a gentle crossfade that swaps the quarter colors
and returns them to their starting colors. It has no lettering. The pending
state ends on completion, network failure, timeout or abort; reduced-motion
preferences keep the status text and disable rotation and color animation.

Both pre-call and post-call LiteLLM content filters are mandatory in backend
requests. Missing confirmation in `x-litellm-applied-guardrails` fails closed.
Current filters are keyword/regex based; they are not exhaustive semantic
moderation. Key-bound guardrails require a LiteLLM Enterprise license here, so
the OSS installation enforces them in the backend, **not in the key itself**.
`web_guardrails.json` documents the installed policy. There are no additional
website RPM/TPM limits yet; upstream quotas still apply. Curated `entry_type`
values `music` and `guardrail` exclude a model identity across gateways from the
chat directory, counters, public catalog and demo form; research/history stay
intact. Lyria 3 Pro/Clip and Nemotron 3.5 Content Safety use these categories.
The Fire handler also rejects these categories. No upstream routes or credentials
are removed by this presentation filter.

## Host deployment

The user-level `litellm-free-web.service` serves FastAPI through
`%t/litellm-free/litellm-free.sock`. Caddy runs rootless as user `core`, mounts
that runtime directory and reverse-proxies the Unix socket while serving HTTPS
directly on 443. There is no intermediate 8080 listener, custom SELinux module
or migration helper.

The host permits all users to bind ports starting at 400 through
`net.ipv4.ip_unprivileged_port_start=400`, persisted in
`/etc/sysctl.d/99-ucore-unprivileged-ports.conf`. This does not change firewall
rules. Existing Caddy services and the website's backend remain unchanged.

Citadel publishes `https://www.f24-sales.com` and `https://f24-sales.com` through
the existing tunnel using its `www443` / `domain443` mapping. The port-443
whitelist is currently disabled; the Unix-socket backend is not published separately.
Use Citadel's ordinary `./scan.sh`; never replace its reconciler.
No changes to mail DNS or unrelated Cloudflare routes are needed.

```sh
systemctl --user restart litellm-free-web.service
curl --fail https://127.0.0.1/healthz
systemctl --user status caddy.service litellm-free-web.service
```

## Tests

```sh
python3 -m pip install --user --upgrade pytest
python3 -m pytest -q tests/test_web.py
```

The tests mock upstream calls. They cover chronology, history, duplicate routes,
secret redaction, HTML escaping, missing filters, green/yellow states and
non-persistence of answers. Live smoke tests should use only a few preset calls.

## Public configuration downloads and repository

The production website is **https://www.f24-sales.com/**. Its footer offers the
current `/litellm-config.json` and a GitHub button linking to
https://github.com/f24sales/litellm-free#import. The environment template is linked
in the repository documentation. The GitHub
Invertocat is the original black SVG from GitHub's official brand archive.

The JSON export is derived from passing, researched chat routes in the same
catalog used by the page. It contains standard LiteLLM `model_list` entries,
verified route presets and `os.environ/…` references, never server credentials.
It returns 503 if no verified export exists. Both downloads are uncached and
carry download filenames. The importer uses the fixed public www URL.
See [IMPORT.md](IMPORT.md) for configuration-file, management-API and direct-SQL
operation, including `.env` precedence, encryption, ownership and reload behavior.

New model metadata can be researched after a full scan by the agent workflow in
[AGENT_REVIEW.md](AGENT_REVIEW.md). Failed research never overwrites existing
metadata. Decision, embedding and speech models also count as non-chat entries.
The curated metadata remains the source for identities and presentation.
