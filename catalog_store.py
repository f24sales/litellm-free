"""Local, credential-free catalog projection and durable history. No network I/O."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from threading import RLock
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
NEWS_BLOCK = re.compile(r"^## ([^\n ]+)[^\n]*\n+```json\n(.*?)\n```", re.M | re.S)
ACTIONS = {"AUFGENOMMEN": "added", "GEÄNDERT": "updated", "ENTFERNT": "removed"}


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing catalog timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Catalog timestamps must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def number(value):
    return value if type(value) is int and value > 0 else None


def day(value):
    """Discovery chronology has calendar-day precision; source logs retain UTC times."""
    return value.split("T", 1)[0]


def strings(value):
    return [s for s in value if isinstance(s, str)] if isinstance(value, list) else []


def safe_url(value):
    if not isinstance(value, str):
        return None
    url = urlsplit(value)
    return value if url.scheme == "https" and url.hostname and not url.username and not url.password else None


def local_logo(value):
    """Research can select bundled SVG/PNG assets, never third-party images."""
    if isinstance(value, str) and re.fullmatch(r"/static/logos/[a-z0-9-]+\.(?:svg|png)", value):
        return value if (ROOT / value.lstrip("/")).is_file() else None
    return None


def creator_info(name, metadata):
    info = metadata.get(name, {})
    country = info.get("country")
    source = safe_url(info.get("country_source"))
    confirmed = isinstance(country, str) and re.fullmatch(r"[A-Z]{2}", country) and source
    return {"name": name, "logo": local_logo(info.get("logo")),
            "initials": info.get("initials") or "".join(part[0] for part in name.split()[:2]),
            "country": country if confirmed else None,
            "country_name": info.get("country_name") if confirmed else None,
            "country_flag": "".join(chr(127397 + ord(c)) for c in country) if confirmed else None,
            "country_source": source if confirmed else None}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, data):
    """Atomic replace; retain the old registry if writing fails."""
    encoded = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == encoded:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def snapshot(entry):
    """Explicit allowlist: never publish deployment credentials or arbitrary fields."""
    deployment = entry["deployment"]
    info = deployment["model_info"]
    aggregator = entry["aggregator"]
    ident, upstream, alias = info["id"], info["free_sync_model_id"], deployment["model_name"]
    if not all(isinstance(x, str) and x for x in (aggregator, ident, upstream, alias)):
        raise ValueError("Invalid catalog identity")
    catalog = info.get("free_sync_catalog") or {}
    reason = catalog.get("reasoning") or {}
    architecture = catalog.get("architecture") or {}
    params = strings(entry.get("supported_parameters"))
    variant = info.get("free_sync_variant", "base")
    supported = True if reason or "reasoning" in params or "reasoning_effort" in params or variant == "think" else None
    provider = entry.get("provider")
    return {
        "id": ident, "aggregator": aggregator, "upstream_id": upstream,
        "alias": alias, "variant": variant if isinstance(variant, str) else "base",
        "author": entry.get("model_author") if isinstance(entry.get("model_author"), str) else None,
        "reported_provider": provider if isinstance(provider, str) else None,
        "reported_providers": strings(entry.get("providers")), "parameters": params,
        "context_tokens": number(info.get("max_input_tokens")),
        "max_output_tokens": number(info.get("max_output_tokens")),
        "reasoning": {"supported": supported,
                      "mandatory": reason.get("mandatory") if type(reason.get("mandatory")) is bool else None,
                      "default_enabled": reason.get("default_enabled") if type(reason.get("default_enabled")) is bool else None,
                      "budget_supported": reason.get("supports_max_tokens") if type(reason.get("supports_max_tokens")) is bool else None,
                      "effort_levels": strings(reason.get("supported_efforts")),
                      "default_effort": reason.get("default_effort") if isinstance(reason.get("default_effort"), str) else None},
        "input_modalities": strings(architecture.get("input_modalities")),
        "output_modalities": strings(architecture.get("output_modalities")),
        "created_at": timestamp(entry["created_at"]), "updated_at": timestamp(entry["updated_at"]),
    }


def probe_snapshot(entry, revision):
    """Normalize one sanitized model-probe row to the directory route shape."""
    if not isinstance(entry, dict):
        raise ValueError("Invalid model-probe row")
    aggregator = entry.get("aggregator")
    model = entry.get("model")
    catalog = entry.get("catalog") if isinstance(entry.get("catalog"), dict) else {}
    ident = catalog.get("id")
    upstream = catalog.get("upstream_id") or model
    alias = catalog.get("alias") or model
    if not all(isinstance(value, str) and value for value in (aggregator, model, upstream, alias)):
        raise ValueError("Invalid model-probe identity")
    if not isinstance(ident, str) or not ident:
        ident = "probe-" + hashlib.sha256((aggregator + "\0" + model).encode()).hexdigest()[:32]
    created = catalog.get("created_at") or entry.get("checked_at") or revision
    updated = catalog.get("updated_at") or entry.get("checked_at") or revision
    reason = catalog.get("reasoning") if isinstance(catalog.get("reasoning"), dict) else {}
    route = {
        "id": ident, "aggregator": aggregator, "upstream_id": upstream,
        "alias": alias, "variant": catalog.get("variant") if isinstance(catalog.get("variant"), str) else "base",
        "author": catalog.get("author") if isinstance(catalog.get("author"), str) else None,
        "reported_provider": catalog.get("reported_provider") if isinstance(catalog.get("reported_provider"), str) else None,
        "reported_providers": strings(catalog.get("reported_providers")),
        "parameters": strings(catalog.get("parameters")),
        "context_tokens": number(catalog.get("context_tokens")),
        "max_output_tokens": number(catalog.get("max_output_tokens")),
        "reasoning": {
            "supported": reason.get("supported") if type(reason.get("supported")) is bool else None,
            "mandatory": reason.get("mandatory") if type(reason.get("mandatory")) is bool else None,
            "default_enabled": reason.get("default_enabled") if type(reason.get("default_enabled")) is bool else None,
            "budget_supported": reason.get("budget_supported") if type(reason.get("budget_supported")) is bool else None,
            "effort_levels": strings(reason.get("effort_levels")),
            "default_effort": reason.get("default_effort") if isinstance(reason.get("default_effort"), str) else None,
        },
        "input_modalities": strings(catalog.get("input_modalities")),
        "output_modalities": strings(catalog.get("output_modalities")),
        "created_at": timestamp(created), "updated_at": timestamp(updated),
        "probe_status": entry.get("status") if entry.get("status") in {"green", "yellow", "red"} else None,
        "probe_assessment": entry.get("assessment") if isinstance(entry.get("assessment"), str) else None,
        "probe_http_status": entry.get("http_status") if type(entry.get("http_status")) is int else None,
        "probe_checked_at": timestamp(entry.get("checked_at") or revision),
        "probe_listed_in_proxy": entry.get("listed_in_proxy") if type(entry.get("listed_in_proxy")) is bool else None,
        "access_note": entry.get("access_note") if isinstance(entry.get("access_note"), str) else None,
    }
    return route


def enrich(route, metadata, creators=None):
    """Catalog limits win; manually researched facts fill only catalog gaps."""
    detail = metadata.get(route["aggregator"] + "|" + route["upstream_id"], {})
    result = dict(route)
    result["name"] = detail.get("name") or route["upstream_id"]
    result["display_name"] = detail.get("display_name") or result["name"]
    result["entry_type"] = (detail["entry_type"] if detail.get("entry_type") in {"router", "music", "guardrail", "decision", "embedding", "speech"}
                            else "model")
    result["author"] = detail.get("creator") or route["author"]
    result["creator_logo"] = local_logo((creators or {}).get(result["author"], {}).get("logo"))
    result["creator_initials"] = creator_info(result["author"] or "?", creators or {})["initials"]
    result["research_status"] = detail.get("research_status", "pending")
    result["researched_at"] = detail.get("researched_at")
    result["notes"] = detail.get("notes", "")
    result["description"] = detail.get("description", "This model is listed in our local catalog. Its technical description has not been researched yet.")
    result["identity_reviewed"] = detail.get("research_status") == "reviewed" and bool(detail.get("duplicate_key"))
    result["duplicate_key"] = (detail["duplicate_key"] if result["identity_reviewed"]
                               else route["aggregator"] + "|" + route["upstream_id"])
    result["identity_note"] = detail.get("identity_note", "")
    result["open_weights_source"] = safe_url(detail.get("open_weights_source"))
    result["open_weights"] = (True if detail.get("open_weights") is True
                              and result["open_weights_source"] else None)
    result["pricing_caveat"] = detail.get("pricing_caveat")
    result["limit_sources"] = {}
    for field in ("context_tokens", "max_output_tokens"):
        result[field] = route[field] or number(detail.get(field))
        result["limit_sources"][field] = "catalog" if route[field] else "research" if result[field] else None
    reason = dict(detail.get("reasoning") or {})
    reason.update({k: v for k, v in route["reasoning"].items() if v is not None and v != []})
    result["reasoning"] = {"supported": reason.get("supported"), "mandatory": reason.get("mandatory"),
                           "default_enabled": reason.get("default_enabled"), "budget_supported": reason.get("budget_supported"),
                           "effort_levels": strings(reason.get("effort_levels")),
                           "effort_range": reason.get("effort_range"), "default_effort": reason.get("default_effort")}
    for field in ("input_modalities", "output_modalities"):
        result[field] = route[field] or strings(detail.get(field))
    result["sources"] = [{"label": s.get("label", "Source"), "url": safe_url(s.get("url"))}
                         for s in detail.get("sources", []) if safe_url(s.get("url"))]
    result["model_page"] = safe_url(detail.get("model_page"))
    result["model_page_label"] = detail.get("model_page_label", "Model page")
    result["official_model_page"] = (result["model_page"]
                                     if detail.get("model_page_is_official") is True else None)
    result["probe_status"] = route.get("probe_status")
    result["probe_assessment"] = route.get("probe_assessment")
    result["probe_http_status"] = route.get("probe_http_status")
    result["probe_checked_at"] = route.get("probe_checked_at")
    result["probe_listed_in_proxy"] = route.get("probe_listed_in_proxy")
    result["access_note"] = route.get("access_note")
    if route["aggregator"] == "opencode":
        result["access_note"] = (
            "OpenCode free routes must be used through the OpenCode software; "
            "direct LiteLLM/API calls are not accepted."
        )
    return result


class CatalogStore:
    def __init__(self, root=ROOT, *, source=None, news=None, runs=None):
        self.root = Path(root)
        self.source = Path(source) if source else self.root / "free_models.json"
        self.news = Path(news) if news else self.root / "news.md"
        self.runs = Path(runs) if runs else self.root / "runs.log"
        self.metadata = self.root / "model_metadata.json"
        self.registry = self.root / "model_registry.json"
        self._mutex = RLock()
        self._stamp = None
        self._cached = None

    def signature(self):
        return tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
                     for p in (self.source, self.news, self.runs, self.metadata, self.registry))

    def refresh(self):
        with self._mutex:
            if self._cached is not None and self._stamp == self.signature():
                return self._cached
            with self.registry.with_suffix(".json.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                result = self._refresh()
                self._cached = result
                return result

    def _refresh(self):
        signature_before = self.signature()
        source = read_json(self.source)
        if source.get("schema_version") != 1 or not isinstance(source.get("models"), dict):
            raise ValueError("Invalid source catalog; history has not been changed")
        probe_source = source.get("source_type") == "model_probe"
        revision = timestamp(source.get("generated_at") if probe_source else source["updated_at"])
        if probe_source:
            current = {}
            for entry in source["models"].values():
                route = probe_snapshot(entry, revision)
                current[route["id"]] = route
        else:
            current = {ident: snapshot(e) for ident, e in source["models"].items()}
        if any(ident != r["id"] for ident, r in current.items()):
            raise ValueError("Catalog ID mismatch")
        meta = read_json(self.metadata) if self.metadata.exists() else {"models": {}, "aggregators": {}}
        if not isinstance(meta.get("models"), dict) or not isinstance(meta.get("aggregators"), dict):
            raise ValueError("Invalid research database")
        old = read_json(self.registry) if self.registry.exists() else {"schema_version": 1, "models": {}}
        if old.get("schema_version") != 1 or not isinstance(old.get("models"), dict):
            raise ValueError("Invalid history; refusing to overwrite it")
        records = old["models"]

        # Migrate chronology to dates and repair pre-introduction removals caused
        # by refreshing an old probe snapshot after a newer discovery run.
        for record in records.values():
            for event in record["events"]:
                event["at"] = day(event["at"])
            additions = [e["at"] for e in record["events"] if e["action"] == "added"]
            record["first_seen"] = min([day(record["snapshot"]["created_at"]), *additions])
            record["last_seen"] = day(record["last_seen"])
            record["events"] = [e for e in record["events"] if e["at"] >= record["first_seen"]]
            removals = [e["at"] for e in record["events"] if e["action"] == "removed"]
            record["events"] = [e for e in record["events"] if e["action"] != "returned"
                                or any(removed <= e["at"] for removed in removals)]
            record["events"] = list({(e["at"], e["action"], e["source"]): e for e in record["events"]}.values())
            record["removed_at"] = max(removals, default=None) if not record["active"] else None

        def observe(route, when, action, provenance):
            when = day(when)
            ident = route["id"]
            record = records.setdefault(ident, {"first_seen": day(route["created_at"]), "last_seen": day(route["updated_at"]),
                                                "active": False, "removed_at": None, "events": []})
            record["first_seen"] = min(record["first_seen"], day(route["created_at"]))
            if action != "removed":
                record["last_seen"] = max(record["last_seen"], when)
            # Do not replace a newer preserved snapshot by replaying an older log.
            if "snapshot" not in record or route["updated_at"] >= record["snapshot"]["updated_at"]:
                record["snapshot"] = route
            event = {"at": when, "action": action, "source": provenance}
            if event not in record["events"]:
                record["events"].append(event)
            return record

        if self.news.exists():
            for when, block in NEWS_BLOCK.findall(self.news.read_text(encoding="utf-8")):
                entry = json.loads(block)
                action = ACTIONS.get(entry.get("action"))
                if not action:
                    continue
                if entry.get("previous"):
                    prev = snapshot(entry["previous"])
                    observe(prev, prev["created_at"], "added", "log")
                route = snapshot(entry)
                observe(route, timestamp(when), action, "log")

        for ident, route in current.items():
            if ident not in records:
                observe(route, route["created_at"], "added", "catalog")
            record = records[ident]
            if not record["active"] and record.get("removed_at"):
                observe(route, max(route["updated_at"], revision), "returned", "catalog")
            record.update(active=True, removed_at=None, snapshot=route)
            record["first_seen"] = min(record["first_seen"], day(route["created_at"]))
            record["last_seen"] = max(record["last_seen"], day(route["updated_at"]))

        for ident, record in records.items():
            if ident not in current:
                removals = [e["at"] for e in record["events"] if e["action"] == "removed"]
                if record["active"] and day(revision) >= record["first_seen"] and (not removals or max(removals) < record["last_seen"]):
                    observe(record["snapshot"], revision, "removed", "catalog")
                    removals.append(day(revision))
                record.update(active=False, removed_at=max(removals) if removals else record.get("removed_at"))
            record["events"].sort(key=lambda e: (e["at"], e["action"]))
            record["details"] = enrich(record["snapshot"], meta["models"], meta.get("creators"))

        checked = {}
        if probe_source:
            for entry in source["models"].values():
                aggregator = entry.get("aggregator")
                when = entry.get("checked_at")
                if isinstance(aggregator, str) and isinstance(when, str):
                    checked[aggregator] = max(checked.get(aggregator, when), timestamp(when))
        elif self.runs.exists():
            for line in self.runs.read_text(encoding="utf-8").splitlines():
                try:
                    run = json.loads(line)
                    if run.get("status") == "OK" and not run.get("dry_run"):
                        when = timestamp(run["finished_at"])
                        for aggregator in run.get("providers", {}):
                            checked[aggregator] = max(checked.get(aggregator, when), when)
                except (ValueError, KeyError, TypeError):
                    continue  # A concurrent run may still be appending its final line.
        for record in records.values():
            if record["active"] and not probe_source:
                record["last_seen"] = max(record["last_seen"], day(checked.get(record["snapshot"]["aggregator"], record["last_seen"])))
        data = {"schema_version": 1, "catalog_updated_at": revision,
                "checked_at": revision if probe_source else max(checked.values(), default=None),
                "checks_by_aggregator": checked, "models": records}
        aggregators = {}
        for key in sorted({r["snapshot"]["aggregator"] for r in records.values()}):
            info = meta["aggregators"].get(key, {})
            access_note = info.get("access_note", "Provider terms and limits apply.")
            if key == "opencode":
                access_note = (access_note.rstrip(".") + ". Direct API availability varies by model; "
                               "the directory shows each route's result from the latest scan.")
            aggregators[key] = {"id": key, "name": info.get("name", key), "initials": info.get("initials", key[:2].upper()),
                                "logo": local_logo(info.get("logo")),
                                "description": info.get("description", ""), "access_note": access_note,
                                "website": safe_url(info.get("website")), "signup": safe_url(info.get("signup")),
                                "api_base": safe_url(info.get("api_base")),
                                "docs": safe_url(info.get("docs")), "checked_at": checked.get(key),
                                "sources": [{"label": s.get("label", "Source"), "url": safe_url(s.get("url"))}
                                            for s in info.get("sources", []) if safe_url(s.get("url"))]}
        data["chronology"] = model_chronology(records, aggregators)
        write_json(self.registry, data)
        after = self.signature()
        # Inputs can change while parsing. Do not cache an inconsistent revision.
        self._stamp = after if signature_before[:-1] == after[:-1] else None
        creators = {name: creator_info(name, meta.get("creators", {}))
                    for name in sorted({r["details"]["author"] for r in records.values() if r["details"]["author"]})}
        return {**data, "source_type": "model_probe" if probe_source else "deployment",
                "aggregators": aggregators, "creators": creators}


def model_chronology(records, aggregators):
    """Join only explicitly curated identities, including unavailable first gateways."""
    groups = {}
    for record in records.values():
        route = record["details"]
        group = groups.setdefault(route["duplicate_key"], {})
        key = route["aggregator"]
        gateway = group.setdefault(key, {
            "aggregator": key, "name": aggregators[key]["name"],
            "website": aggregators[key]["website"], "first_seen": record["first_seen"],
            "active": False, "software_required": key == "opencode",
        })
        gateway["first_seen"] = min(gateway["first_seen"], record["first_seen"])
        gateway["active"] |= record["active"]
    result = {}
    for key, gateways in groups.items():
        first = min(g["first_seen"] for g in gateways.values())
        result[key] = {"first_seen": first, "gateways": sorted(
            ({**g, "is_first": g["first_seen"] == first} for g in gateways.values()),
            key=lambda g: (g["first_seen"], g["name"]))}
    return result


def listings(data, archive=False, probe_status=None):
    grouped = {}
    for record in data["models"].values():
        if record["active"] == archive or (archive and not record["removed_at"]):
            continue
        route = record["details"]
        if probe_status and route.get("probe_status") != probe_status:
            continue
        key = route["aggregator"] + "|" + route["upstream_id"]
        card = grouped.setdefault(key, {**route, "key": hashlib.sha256(key.encode()).hexdigest()[:12],
                                       "first_seen": record["first_seen"], "last_seen": record["last_seen"],
                                       "removed_at": record["removed_at"], "routes": [], "events": [],
                                       "aggregator_info": data["aggregators"][route["aggregator"]]})
        card["first_seen"] = min(card["first_seen"], record["first_seen"])
        card["last_seen"] = max(card["last_seen"], record["last_seen"])
        card["routes"].append({"id": route["id"], "alias": route["alias"], "variant": route["variant"],
                               "probe_status": route.get("probe_status"),
                               "probe_assessment": route.get("probe_assessment"),
                               "probe_checked_at": route.get("probe_checked_at")})
        for event in record["events"]:
            if event not in card["events"]:
                card["events"].append(event)
        # Reasoning may only be declared on one of a pair of local routes.
        if route["reasoning"].get("supported"):
            card["reasoning"] = route["reasoning"]
    for card in grouped.values():
        history = data["chronology"][card["duplicate_key"]]
        card["model_first_seen"] = history["first_seen"]
        card["gateway_timeline"] = history["gateways"]
        card["first_aggregators"] = [g for g in history["gateways"] if g["is_first"]]
        card["is_first"] = any(g["aggregator"] == card["aggregator"] for g in card["first_aggregators"])
        card["routes"].sort(key=lambda r: r["variant"])
        card["events"].sort(key=lambda e: (e["at"], e["action"]), reverse=True)
        statuses = [route["probe_status"] for route in card["routes"] if route.get("probe_status")]
        card["probe_status"] = ("red" if "red" in statuses else "yellow" if "yellow" in statuses
                                 else "green" if statuses and all(status == "green" for status in statuses) else None)
        card["probe_summary"] = {status: statuses.count(status) for status in ("green", "yellow", "red")}
        card["access_note"] = card["aggregator_info"].get("access_note")
        card["duplicates"] = [{"key": other["key"], "aggregator": other["aggregator_info"]["name"], "upstream_id": other["upstream_id"]}
                              for other in grouped.values() if other["key"] != card["key"] and other["duplicate_key"] == card["duplicate_key"]]
    return sorted(sorted(grouped.values(), key=lambda c: (c["name"].casefold(), c["aggregator"])),
                  key=lambda c: c["first_seen"], reverse=True)


def non_chat_identities(data):
    return {r["details"]["duplicate_key"] for r in data["models"].values()
            if r["details"].get("entry_type") in {"music", "guardrail", "decision", "embedding", "speech"}}


def model_cards(data, archive=False, probe_status=None):
    """One card per curated model; gateway arrivals never change its sort date."""
    # Generic references belong to the aggregator, including cross-gateway sources.
    # Match explicitly curated URLs only; model pages on the same domain stay local.
    aggregator_urls = {url.rstrip("/") for info in data["aggregators"].values()
                       for url in [info.get("website"), info.get("signup"), info.get("docs"), info.get("api_base"),
                                   *(s["url"] for s in info.get("sources", []))] if url}
    all_listings = listings(data) + listings(data, archive=True)
    excluded_identities = non_chat_identities(data)
    active_identities = {r["details"]["duplicate_key"] for r in data["models"].values() if r["active"]}
    grouped = {}
    for gateway in listings(data, archive=archive, probe_status=probe_status):
        identity = gateway["duplicate_key"]
        if identity in excluded_identities:
            continue  # Retain research/history, but omit non-chat models from the public directory.
        if archive and identity in active_identities:
            continue  # Removed gateways live inside the model's shared history.
        grouped.setdefault(identity, []).append(gateway)
    cards = []
    for identity, members in grouped.items():
        members.sort(key=lambda g: (g["first_seen"], g["aggregator"], g["upstream_id"]))
        reference = members[0]
        details = sorted((g for g in all_listings if g["duplicate_key"] == identity),
                         key=lambda g: (g["first_seen"], g["aggregator"], g["upstream_id"]))
        card = {**reference, "key": hashlib.sha256(("model|" + identity).encode()).hexdigest()[:12],
                "first_seen": data["chronology"][identity]["first_seen"],
                "last_seen": max(g["last_seen"] for g in details),
                "available_gateways": sorted({g["aggregator"] for g in members}),
                "gateway_details": details, "spec_reference": reference["aggregator_info"]["name"],
                "reference_first_seen": reference["first_seen"],
                "routes": [{**route, "aggregator": g["aggregator"], "gateway_name": g["aggregator_info"]["name"]}
                           for g in members for route in g["routes"]],
                "events": sorted([dict(e, aggregator=g["aggregator_info"]["name"]) for g in details for e in g["events"]],
                                 key=lambda e: (e["at"], e["aggregator"], e["action"]), reverse=True),
                "duplicates": []}
        card["sources"] = list({(s["url"], s["label"]): s for g in details for s in g["sources"]
                                if s["url"].rstrip("/") not in aggregator_urls}.values())
        if card["model_page"] and card["model_page"].rstrip("/") in aggregator_urls:
            card["model_page"] = None
        card["official_model_page"] = next((g["official_model_page"] for g in [reference, *details]
                                             if g["official_model_page"]), None)
        card["open_weights_source"] = next((g["open_weights_source"] for g in details
                                             if g["open_weights"] is True), None)
        card["open_weights"] = True if card["open_weights_source"] else None
        cards.append(card)
    return sorted(sorted(cards, key=lambda c: c["name"].casefold()), key=lambda c: c["first_seen"], reverse=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT)
    args = parser.parse_args()
    result = CatalogStore(args.data_dir).refresh()
    counts = Counter("active" if r["active"] else "archived" for r in result["models"].values())
    print(json.dumps(dict(counts), sort_keys=True))
