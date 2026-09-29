import asyncio
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from catalog_store import CatalogStore, listings, model_cards
from demo import Demo
from web import create_app

T1, T2, T3 = [f"2026-09-{n}T12:00:00+00:00" for n in (21, 24, 28)]


def entry(ident="one", when=T1, aggregator="test", model="creator/model"):
    return {"aggregator": aggregator, "created_at": when, "updated_at": when,
            "model_author": "creator", "provider": None, "supported_parameters": ["reasoning"],
            "deployment": {"model_name": aggregator + "/" + model, "litellm_params": {"api_key": "SECRET-NEVER-EXPOSE"},
                           "model_info": {"id": ident, "free_sync_model_id": model, "free_sync_variant": "base",
                                          "max_input_tokens": 12345, "max_output_tokens": 1200}}}


def source(root, models, at=T1):
    (root / "free_models.json").write_text(json.dumps({"schema_version": 1, "updated_at": at, "models": models}))


def event(root, item, at, action):
    with (root / "news.md").open("a") as f:
        f.write(f"\n## {at} — {action}\n\n```json\n" + json.dumps({"action": action, **item}) + "\n```\n")


@pytest.fixture
def catalog(tmp_path):
    source(tmp_path, {"one": entry()})
    (tmp_path / "model_metadata.json").write_text(json.dumps({"schema_version": 1, "aggregators": {}, "models": {
        "test|creator/model": {"name": "A researched model", "description": "A short model description.",
                               "research_status": "reviewed", "duplicate_key": "creator/model",
                               "context_tokens": 99999, "model_page": "https://example.org/model", "sources": []}}}))
    (tmp_path / ".env").write_text("FREE_WEB_LITELLM_BASE_URL=https://backend.invalid\nFREE_WEB_LITELLM_PORT=2001\nFREE_WEB_LITELLM_API_KEY=sk-test-backend-secret\nFREE_WEB_GUARDRAILS=test-pre,test-post\n")
    return tmp_path


def test_sort_by_first_added_not_last_changed(catalog):
    old = entry()
    old["updated_at"] = T3
    source(catalog, {"one": old, "two": entry("two", T2, model="creator/new")}, T3)
    cards = listings(CatalogStore(catalog).refresh())
    assert [c["upstream_id"] for c in cards] == ["creator/new", "creator/model"]


def test_history_keeps_removed_and_returned(catalog):
    store = CatalogStore(catalog)
    event(catalog, entry(), T1, "AUFGENOMMEN")
    store.refresh()
    event(catalog, entry(), T2, "ENTFERNT")
    source(catalog, {}, T2)
    result = store.refresh()
    assert result["models"]["one"]["removed_at"] == T2[:10]
    assert len(listings(result, True)) == 1
    source(catalog, {"one": entry(when=T3)}, T3)
    restored = store.refresh()["models"]["one"]
    assert restored["active"] and restored["first_seen"] == T1[:10]
    assert any(e["action"] == "returned" for e in restored["events"])
    assert store.refresh() == store.refresh()


def test_logs_backfill_and_dont_need_to_remain(catalog):
    event(catalog, entry("historic", model="old/model"), T1, "AUFGENOMMEN")
    event(catalog, entry("historic", model="old/model"), T2, "ENTFERNT")
    store = CatalogStore(catalog)
    data = store.refresh()
    assert data["models"]["historic"]["removed_at"] == T2[:10]
    (catalog / "news.md").unlink()
    assert "historic" in store.refresh()["models"]


def test_bad_source_never_clobbers_history(catalog):
    store = CatalogStore(catalog)
    store.refresh()
    before = store.registry.read_bytes()
    (catalog / "free_models.json").write_text('{"bad":true}')
    with pytest.raises(ValueError):
        store.refresh()
    assert store.registry.read_bytes() == before


def test_variants_and_duplicate_gateways_are_all_retained(catalog):
    path = catalog / "model_metadata.json"
    meta = json.loads(path.read_text())
    meta["models"]["other|creator/model"] = {"research_status": "reviewed", "duplicate_key": "creator/model"}
    path.write_text(json.dumps(meta))
    variant = entry("two")
    variant["deployment"]["model_info"]["free_sync_variant"] = "think"
    variant["deployment"]["model_name"] += "-think"
    source(catalog, {"one": entry(), "two": variant, "three": entry("three", aggregator="other")})
    cards = listings(CatalogStore(catalog).refresh())
    assert len(cards) == 2 and sum(len(c["routes"]) for c in cards) == 3
    assert all(len(c["duplicates"]) == 1 for c in cards)


def test_catalog_limits_win_and_no_secret_fields(catalog):
    data = CatalogStore(catalog).refresh()
    assert listings(data)[0]["context_tokens"] == 12345
    assert "SECRET-NEVER-EXPOSE" not in json.dumps(data)
    assert "api_key" not in json.dumps(data)


def test_research_does_not_create_models(catalog):
    source(catalog, {})
    assert listings(CatalogStore(catalog).refresh()) == []


def test_new_models_appear_without_research(catalog):
    store = CatalogStore(catalog)
    store.refresh()
    source(catalog, {"one": entry(), "new": entry("new", T2, model="new/unknown")}, T2)
    cards = listings(store.refresh())
    assert cards[0]["upstream_id"] == "new/unknown"
    assert cards[0]["research_status"] == "pending"


def test_server_rendered_privacy_and_escaping(catalog):
    client = TestClient(create_app(catalog))
    response = client.get("/")
    assert response.status_code == 200
    assert "Set-Cookie" not in response.headers
    assert "SECRET-NEVER-EXPOSE" not in response.text
    assert "sk-test-backend-secret" not in response.text and "backend.invalid" not in response.text
    assert 'src="https://' not in response.text
    assert 'name="question_id"' in response.text
    assert 'hx-post="/demo/fire"' in response.text
    assert "A researched model" in client.get("/?q=absent-model").text
    assert "A researched model" in client.get("/?q=researched").text
    assert "api_key" not in client.get("/catalog.json").text
    for path in ("/.env", "/free_models.json", "/runs.log", "/fire_events.jsonl", "/static/../.env"):
        assert client.get(path).status_code == 404


def test_brand_assets_are_local_and_atom_art_is_removed(catalog):
    client = TestClient(create_app(catalog))
    html = client.get("/").text
    assert 'alt="F24 SALES"' in html
    assert 'class="brand-rhythm"' in html
    assert 'core-object' not in html and 'orbital' not in html
    for path in ("/static/brand/F24SALES_blau.svg",
                 "/static/brand/F24SALES_Balken.svg",
                 "/static/brand/F24SALES_Hintergrund.svg",
                 "/static/fonts/AdwaitaSans-Regular.ttf"):
        assert path in html
        response = client.get(path)
        assert response.status_code == 200
        assert "set-cookie" not in response.headers
    css = client.get("/static/style.css").text
    assert "#1975d1" in css
    assert "prefers-reduced-motion:reduce" in css


def test_unsafe_source_url_is_not_rendered(catalog):
    path = catalog / "model_metadata.json"
    meta = json.loads(path.read_text())
    detail = meta["models"]["test|creator/model"]
    detail.update(name="<script>alert(1)</script>", model_page="javascript:alert(1)", sources=[{"url": "javascript:alert(1)"}])
    path.write_text(json.dumps(meta))
    html = TestClient(create_app(catalog)).get("/").text
    assert "<script>alert" not in html and "javascript:alert" not in html
    assert "&lt;script&gt;" in html


def run_demo(catalog, status=200, text="Copenhagen 👋", **kwargs):
    def backend(request):
        body = json.loads(request.content)
        assert request.url.host == "backend.invalid"
        assert body["guardrails"] == ["test-pre", "test-post"]
        assert body["no-log"] is True and body["cache"]["no-store"] is True
        assert request.headers["x-litellm-enable-message-redaction"] == "true"
        return httpx.Response(status, headers=kwargs.get("headers", {"x-litellm-applied-guardrails": "test-pre,test-post"}),
                              json=kwargs.get("response", {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}))
    return asyncio.run(Demo(catalog).fire("one", "denmark", "0", CatalogStore(catalog).refresh(), transport=httpx.MockTransport(backend)))


def test_green_has_answer_but_never_persists_it(catalog):
    result = run_demo(catalog)
    assert result["state"] == "green" and result["answer"] == "Copenhagen 👋"
    for path in (catalog / "fire_state.json", catalog / "fire_events.jsonl"):
        assert "Copenhagen" not in path.read_text()
        assert "sk-" not in path.read_text()
    assert set(json.loads((catalog / "fire_events.jsonl").read_text())) == {"route_id", "at", "state"}


@pytest.mark.parametrize("headers", [{}, {"x-litellm-applied-guardrails": "test-pre"}])
def test_missing_guardrail_confirmation_fails_closed(catalog, headers):
    result = run_demo(catalog, headers=headers)
    assert result["state"] == "yellow" and "answer" not in result


@pytest.mark.parametrize("reply", ["", "The capital is Paris.", "This endpoint is only available in OpenCode.", "sk-FAKEsecret123456789 Copenhagen", "I cannot answer this request."])
def test_yellow_has_no_answer_or_error(catalog, reply):
    result = run_demo(catalog, text=reply)
    assert result["state"] == "yellow"
    assert set(result) == {"state", "at"}


@pytest.mark.parametrize("code", [400, 401, 403, 429, 500, 503])
def test_provider_errors_never_escape(catalog, code):
    result = run_demo(catalog, status=code, response={"error": {"message": "private traceback sk-secret-internal"}})
    assert result["state"] == "yellow" and "answer" not in result
    assert "private traceback" not in (catalog / "fire_events.jsonl").read_text()


def test_no_client_prompts_or_ssrf(catalog):
    client = TestClient(create_app(catalog))
    headers = {"x-f24-demo": "1", "origin": "http://testserver"}
    for body in ({"route_id": "one", "question_id": "arbitrary unsafe prompt", "emoji_id": "0"},
                 {"route_id": "one", "question_id": "howdy", "emoji_id": "0", "api_base": "http://internal"}):
        r = client.post("/api/fire", json=body, headers=headers)
        assert r.json() == {"state": "yellow"}
    assert client.post("/api/fire", json={}, headers={"origin": "https://elsewhere.example"}).status_code == 403


def test_html_fire_green_escaping_yellow_and_no_stored_answer(catalog):
    app = create_app(catalog)
    async def fake(**kwargs):
        return {"state": "green", "at": T3, "answer": "Copenhagen <script>evil</script>"}
    app.state.demo.fire = fake
    client = TestClient(app)
    headers = {"HX-Request": "true", "Origin": "http://testserver"}
    data = {"route_id": "one", "question_id": "denmark", "emoji_id": "0"}
    response = client.post("/demo/fire", data=data, headers=headers)
    assert response.status_code == 200 and 'data-state="green"' in response.text
    assert "&lt;script&gt;" in response.text and "<script>evil" not in response.text
    assert "no-store" == response.headers["cache-control"]
    assert "Copenhagen &lt;" not in client.get("/").text
    async def yellow(**kwargs):
        return {"state": "yellow", "at": T3}
    app.state.demo.fire = yellow
    response = client.post("/demo/fire", data=data, headers=headers)
    assert 'data-state="yellow"' in response.text
    assert 'aria-live="polite" hidden></p>' in response.text
    assert "error" not in response.text.lower()


def test_changed_route_loads_its_own_status_without_firing(catalog):
    Demo(catalog).record("one", "green")
    client = TestClient(create_app(catalog))
    html = client.get("/demo/form?route_id=one").text
    assert 'data-state="green"' in html
    assert 'aria-live="polite" hidden></p>' in html


def review_alias(catalog, alias, identity="creator/model"):
    path = catalog / "model_metadata.json"
    meta = json.loads(path.read_text())
    meta["models"][alias] = {"research_status": "reviewed", "duplicate_key": identity}
    path.write_text(json.dumps(meta))


def test_same_day_different_times_share_first_star(catalog):
    review_alias(catalog, "other|renamed-preview-free")
    source(catalog, {"one": entry(when="2026-09-21T01:00:00Z"),
                     "two": entry("two", "2026-09-21T23:59:00Z", "other", "renamed-preview-free")}, T2)
    cards = listings(CatalogStore(catalog).refresh())
    assert all(c["is_first"] for c in cards)
    assert all(len(c["first_aggregators"]) == 2 for c in cards)
    assert {c["first_seen"] for c in cards} == {"2026-09-21"}
    assert len({c["duplicate_key"] for c in cards}) == 1


def test_later_gateway_is_not_a_new_model(catalog):
    review_alias(catalog, "other|renamed-preview-free")
    source(catalog, {"one": entry(), "two": entry("two", T2, "other", "renamed-preview-free")}, T2)
    cards = listings(CatalogStore(catalog).refresh())
    assert cards[0]["aggregator"] == "other" and not cards[0]["is_first"]
    assert cards[0]["first_seen"] == T2[:10]
    assert cards[0]["model_first_seen"] == T1[:10]
    assert cards[0]["first_aggregators"][0]["aggregator"] == "test"


def test_unknown_aliases_are_not_guessed_from_names(catalog):
    source(catalog, {"one": entry(), "two": entry("two", T2, "other")}, T2)
    cards = listings(CatalogStore(catalog).refresh())
    assert len({c["duplicate_key"] for c in cards}) == 2
    assert all(c["duplicates"] == [] for c in cards)


def test_first_gateway_survives_availability_filter_and_archive(catalog):
    review_alias(catalog, "other|renamed-preview-free")
    store = CatalogStore(catalog)
    source(catalog, {"one": entry(), "two": entry("two", T2, "other", "renamed-preview-free")}, T2)
    data = store.refresh()
    data["models"]["one"]["details"]["probe_status"] = "red"
    data["models"]["two"]["details"]["probe_status"] = "green"
    cards = listings(data, probe_status="green")
    assert len(cards) == 1 and not cards[0]["is_first"]
    assert cards[0]["first_aggregators"][0]["aggregator"] == "test"
    source(catalog, {"two": entry("two", T2, "other", "renamed-preview-free")}, T3)
    card = listings(store.refresh())[0]
    assert not card["is_first"]
    assert card["first_aggregators"][0]["active"] is False


def test_older_snapshot_cannot_remove_or_backdate_future_discovery(catalog):
    event(catalog, entry("future", T3, model="creator/new"), T3, "AUFGENOMMEN")
    source(catalog, {"one": entry(), "future": entry("future", T3, model="creator/new")}, T3)
    store = CatalogStore(catalog)
    store.refresh()
    source(catalog, {"one": entry()}, T2)
    result = store.refresh()["models"]["future"]
    assert result["first_seen"] == T3[:10] and result["removed_at"] is None
    assert not any(e["action"] == "removed" for e in result["events"])
    assert not listings(store.refresh(), archive=True)
    source(catalog, {"future": entry("future", T3, model="creator/new")}, T3)
    assert not any(e["action"] == "returned" for e in store.refresh()["models"]["future"]["events"])


def test_legacy_bad_date_is_repaired_and_chronology_stores_only_days(catalog):
    source(catalog, {"one": entry(when=T3)}, T3)
    store = CatalogStore(catalog)
    store.refresh()
    saved = json.loads(store.registry.read_text())
    record = saved["models"]["one"]
    record["first_seen"] = T2
    record["events"].extend([{"at": T2, "action": "removed", "source": "catalog"},
                             {"at": T3, "action": "returned", "source": "catalog"}])
    store.registry.write_text(json.dumps(saved))
    result = store.refresh()
    fixed = result["models"]["one"]
    assert fixed["first_seen"] == T3[:10]
    assert {e["action"] for e in fixed["events"]} == {"added"}
    assert len(fixed["first_seen"]) == len(fixed["last_seen"]) == 10
    assert all(len(e["at"]) == 10 for e in fixed["events"])
    assert result["chronology"]["creator/model"]["first_seen"] == T3[:10]


def test_chronology_survives_log_rotation(catalog):
    review_alias(catalog, "other|renamed-preview-free")
    event(catalog, entry(), T1, "AUFGENOMMEN")
    source(catalog, {"one": entry(), "two": entry("two", T2, "other", "renamed-preview-free")}, T2)
    store = CatalogStore(catalog)
    before = store.refresh()["chronology"]
    (catalog / "news.md").unlink()
    assert store.refresh()["chronology"] == before


def test_timeline_markup_and_single_privacy_sentence(catalog):
    html = render_page(catalog)
    sentence = "No advertising or analytics cookies. Cloudflare may use security cookies to protect this website."
    assert html.count(sentence) == 1
    for old in ("NO COOKIES", "No cookies", "No tracking", "NOTHING HIDDEN", "Quiet footprint", "about-section"):
        assert old not in html
    assert 'class="model-grid"' in html and 'class="timeline-day"' not in html
    assert 'class="day-heading"' not in html and 'class="timeline-legend"' not in html
    assert 'class="arrival-panel"' not in html  # Initial-scan models are not ranked.
    assert "12:00" not in html


def test_model_bundle_keeps_original_position_when_gateway_arrives(catalog):
    review_alias(catalog, "other|renamed-preview-free")
    source(catalog, {"one": entry(), "newer": entry("newer", T2, model="creator/newer"),
                     "late": entry("late", T3, "other", "renamed-preview-free")}, T3)
    cards = model_cards(CatalogStore(catalog).refresh())
    assert len(cards) == 2
    assert cards[0]["upstream_id"] == "creator/newer"
    old = cards[1]
    assert old["first_seen"] == T1[:10] and len(old["routes"]) == 2
    assert [(g["aggregator"], g["first_seen"], g["is_first"]) for g in old["gateway_timeline"]] == [
        ("test", T1[:10], True), ("other", T3[:10], False)]


def test_model_bundle_keeps_unavailable_first_but_only_tests_green(catalog):
    review_alias(catalog, "other|renamed-preview-free")
    source(catalog, {"one": entry(), "late": entry("late", T3, "other", "renamed-preview-free")}, T3)
    data = CatalogStore(catalog).refresh()
    data["models"]["one"]["details"]["probe_status"] = "red"
    data["models"]["late"]["details"]["probe_status"] = "green"
    card = model_cards(data, probe_status="green")[0]
    assert card["first_seen"] == T1[:10]
    assert card["first_aggregators"][0]["aggregator"] == "test"
    assert [r["id"] for r in card["routes"]] == ["late"]
    assert len(card["gateway_details"]) == 2
    assert card["available_gateways"] == ["other"]


def test_one_card_with_shared_first_day_stars(catalog):
    review_alias(catalog, "other|renamed-preview-free")
    source(catalog, {"one": entry(), "two": entry("two", "2026-09-21T23:59:00Z", "other", "renamed-preview-free")}, T2)
    cards = model_cards(CatalogStore(catalog).refresh())
    assert len(cards) == 1 and len(cards[0]["first_aggregators"]) == 2
    assert len(cards[0]["gateway_details"]) == 2


def test_removed_gateway_stays_in_model_not_duplicate_archive(catalog):
    review_alias(catalog, "other|renamed-preview-free")
    source(catalog, {"one": entry(), "two": entry("two", T2, "other", "renamed-preview-free")}, T2)
    store = CatalogStore(catalog)
    store.refresh()
    source(catalog, {"two": entry("two", T2, "other", "renamed-preview-free")}, T3)
    data = store.refresh()
    assert len(model_cards(data)) == 1 and model_cards(data, archive=True) == []
    assert len(model_cards(data)[0]["gateway_details"]) == 2
    source(catalog, {}, T3)
    assert len(model_cards(store.refresh(), archive=True)) == 1


def test_removed_filters_do_not_change_model_sort_date(catalog):
    review_alias(catalog, "other|renamed-preview-free")
    source(catalog, {"one": entry(), "late": entry("late", T3, "other", "renamed-preview-free")}, T3)
    app = create_app(catalog)
    html = render_page(catalog)
    index = next(route for route in app.routes if route.path == '/')
    assert index.dependant.query_params == []
    assert html.count('<article class="model-card') == 1
    data = next(route.endpoint for route in app.routes if route.path == '/catalog.json')()
    assert len(data['models']) == 1 and data['models'][0]['first_seen'] == T1[:10]


def test_demo_switch_keeps_all_passing_gateways_in_same_model(catalog):
    review_alias(catalog, "other|renamed-preview-free")
    source(catalog, {"one": entry(), "two": entry("two", T2, "other", "renamed-preview-free")}, T2)
    client = TestClient(create_app(catalog))
    html = client.get("/demo/form?route_id=two").text
    assert 'value="one"' in html and 'value="two" selected' in html
    assert "other · Default settings" in html and "test · Default settings" in html


def test_bundle_preserves_gateway_specific_limits(catalog):
    review_alias(catalog, "other|renamed-preview-free")
    later = entry("two", T2, "other", "renamed-preview-free")
    later["deployment"]["model_info"]["max_input_tokens"] = 5000
    source(catalog, {"one": entry(), "two": later}, T2)
    card = model_cards(CatalogStore(catalog).refresh())[0]
    assert {g["aggregator"]: g["context_tokens"] for g in card["gateway_details"]} == {"test": 12345, "other": 5000}


def render_page(catalog):
    """Exercise the real page endpoint synchronously, without a client socket."""
    from starlette.requests import Request
    app = create_app(catalog)
    route = next(route for route in app.routes if route.path == "/")
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                       "query_string": b"", "scheme": "http", "server": ("testserver", 80), "app": app})
    response = route.endpoint(request)
    assert response.status_code == 200
    return response.body.decode()


def update_research(catalog, **fields):
    path = catalog / "model_metadata.json"
    meta = json.loads(path.read_text())
    meta["models"]["test|creator/model"].update(fields)
    path.write_text(json.dumps(meta))


def record_prior_scan(catalog):
    at = "2026-09-20T12:00:00+00:00"
    baseline = entry("baseline", at, model="old/baseline")
    event(catalog, baseline, at, "AUFGENOMMEN")
    event(catalog, baseline, at, "ENTFERNT")


def test_card_layout_provider_model_first_and_demo_unchanged(catalog):
    record_prior_scan(catalog)
    update_research(catalog, creator="Alibaba", display_name="Qwen example", model_page_is_official=True)
    html = render_page(catalog)
    card = html.split('<article class="model-card', 1)[1].split('</article>', 1)[0]
    markers = ['class="model-author">', '<h3>Qwen example</h3>', 'class="arrival-panel"',
               'class="spec-strip"', 'class="capabilities"', 'class="model-provider-link"',
               'class="model-description"', 'class="model-details"', 'class="demo-panel"']
    assert [card.index(marker) for marker in markers] == sorted(card.index(marker) for marker in markers)
    assert 'FREE TIER' not in card and 'free-badge' not in card and 'card-top' not in card
    assert 'Try this model' in card and 'hx-post="/demo/fire"' in card
    assert 'name="question_id"' in card and 'name="emoji_id"' in card
    assert card.count('A short model description.') == 1
    assert 'creator/model' in card.split('class="model-details"', 1)[1]


def test_card_layout_podium_honors_first_two_days_and_hides_later_arrivals(catalog):
    record_prior_scan(catalog)
    review_alias(catalog, "later|renamed-preview-free")
    review_alias(catalog, "tie|same-model")
    review_alias(catalog, "third|third-arrival")
    source(catalog, {"one": entry(), "late": entry("late", T2, "later", "renamed-preview-free"),
                     "third": entry("third", T3, "third", "third-arrival"),
                     "tie": entry("tie", "2026-09-21T23:59:00Z", "tie", "same-model")}, T3)
    html = render_page(catalog)
    podium = html.split('class="arrival-panel"', 1)[1].split('</section>', 1)[0]
    first, second = podium.split('class="podium-place second-place"')
    assert first.count('class="first-trophy"') == 2 and '🏆' in first
    assert 'test' in first and 'tie' in first and 'later' not in first
    assert 'later' in second and '🥈' in second and T2[:10] in second
    assert 'third' not in podium and T3[:10] not in podium
    assert podium.count(T1[:10]) == 1  # One date per place, even for tied gateways.
    assert 'class="day-heading"' not in html and 'class="timeline-legend"' not in html
    details = html.split('class="gateway-specs"', 1)[1].split('Research &amp;', 1)[0]
    assert 'later' in details and 'third' in details and T3[:10] in details


def test_card_layout_podium_shares_second_place_by_day(catalog):
    record_prior_scan(catalog)
    review_alias(catalog, "second|alias")
    review_alias(catalog, "tie|alias")
    source(catalog, {"one": entry(), "second": entry("second", T2, "second", "alias"),
                     "tie": entry("tie", "2026-09-24T23:59:00Z", "tie", "alias")}, T3)
    html = render_page(catalog)
    second = html.split('class="podium-place second-place"', 1)[1].split('</section>', 1)[0]
    assert second.count('class="second-medal"') == 2 and second.count(T2[:10]) == 1


def test_card_layout_podium_omits_empty_second_place(catalog):
    record_prior_scan(catalog)
    html = render_page(catalog)
    assert 'class="podium-place first-place"' in html
    assert 'class="podium-place second-place"' not in html


def test_card_layout_initial_scan_is_unranked_even_after_later_gateway(catalog):
    review_alias(catalog, "other|renamed-preview-free")
    source(catalog, {"one": entry(), "later": entry("later", T3, "other", "renamed-preview-free")}, T3)
    html = render_page(catalog)
    card_html = html.split('class="model-grid"', 1)[1].split('class="discovery-legend"', 1)[0]
    assert 'class="arrival-panel"' not in card_html and '🏆' not in card_html and '🥈' not in card_html
    assert 'renamed-preview-free' in html and T3[:10] in html
    assert '<dt>Model first observed</dt>' not in html
    assert f'<time datetime="{T1[:10]}">' not in html
    box = html.split('class="available-gateways"', 1)[1].split('</ul>', 1)[0]
    assert '<li>other</li>' in box and '<li>test</li>' in box
    assert '<time' not in box and '🏆' not in box and '🥈' not in box
    assert T1[:10] not in box and T3[:10] not in box


def test_card_layout_archive_is_backend_only(catalog):
    record_prior_scan(catalog)
    app = create_app(catalog)
    public = next(route.endpoint for route in app.routes if route.path == '/catalog.json')()
    assert 'archive' not in public
    assert 'old/baseline' not in json.dumps(public)
    assert len(model_cards(app.state.store.refresh(), archive=True)) == 1
    index = next(route for route in app.routes if route.path == '/')
    assert 'view' not in {param.name for param in index.dependant.query_params}
    html = render_page(catalog)
    assert 'Archive' not in html and 'name="view"' not in html and 'view=archive' not in html


@pytest.mark.parametrize("official", [True, False, None, "true"])
def test_card_layout_official_button_requires_explicit_true(catalog, official):
    update_research(catalog, model_page_is_official=official)
    html = render_page(catalog)
    assert ('class="model-provider-link"' in html) == (official is True)
    assert 'https://example.org/model' in html  # Other sources remain in expanded details.


def test_card_layout_stealth_has_no_aggregator_as_official_button(catalog):
    update_research(catalog, creator="Not disclosed (stealth)", display_name="Space Bunny Alpha",
                    duplicate_key="stealth/space-bunny-alpha",
                    model_page="https://openrouter.ai/stealth/space-bunny-alpha", model_page_is_official=False)
    html = render_page(catalog)
    before_details = html.split('class="model-details"', 1)[0]
    assert '<span class="button stealth-label">Stealth model' in before_details
    assert '🥷' in before_details and '<a class="button"' not in before_details
    assert 'https://openrouter.ai/stealth/space-bunny-alpha' not in before_details
    assert 'https://openrouter.ai/stealth/space-bunny-alpha' in html


@pytest.mark.parametrize('identity, url', [
    ('kilo|kilo-auto/free', 'https://kilo.ai/docs/code-with-ai/agents/auto-model'),
    ('groq|allam-2-7b', 'https://ai.azure.com/catalog/models/ALLaM-2-7b-instruct?publisher=SDAIA'),
])
def test_card_layout_verified_kilo_and_allam_buttons(catalog, identity, url):
    from pathlib import Path
    metadata = json.loads((Path(__file__).resolve().parents[1] / 'model_metadata.json').read_text())
    update_research(catalog, **metadata['models'][identity])
    assert model_cards(CatalogStore(catalog).refresh())[0]['official_model_page'] == url
    html = render_page(catalog)
    if metadata['models'][identity].get('entry_type') == 'router':
        assert '<article class="model-card ' not in html  # Research is kept, offer hidden.
        return
    before_details = html.split('class="model-details"', 1)[0]
    assert f'<a class="button" href="{url}"' in before_details
    assert html.count(f'href="{url}"') == 1  # Don't repeat the button in sources.
    assert 'class="button stealth-label"' not in html


def test_card_layout_unknown_model_does_not_invent_stealth_label(catalog):
    update_research(catalog, creator="Not disclosed", model_page_is_official=False)
    assert 'class="button stealth-label"' not in render_page(catalog)


@pytest.mark.parametrize('gateway', ['nous', 'openrouter', 'kilo'])
def test_card_layout_sante_has_official_announcement(catalog, gateway):
    from pathlib import Path
    metadata = json.loads((Path(__file__).resolve().parents[1] / 'model_metadata.json').read_text())
    detail = metadata['models'][gateway + '|inclusionai/ling-3.0-flash-sante:free']
    update_research(catalog, **detail)
    html = render_page(catalog)
    before_details = html.split('class="model-details"', 1)[0]
    url = 'https://www.linkedin.com/posts/ant-ling_antling-healthai-llm-activity-7501721483279278080-dDAk'
    assert f'<a class="button" href="{url}"' in before_details
    assert 'Official announcement <span' in before_details
    assert html.count(f'href="{url}"') == 1
    assert 'Official model page' not in before_details and 'Stealth model' not in html
    assert 'https://huggingface.co/inclusionAI/Ling-3.0-flash' not in before_details
    assert 'https://huggingface.co/inclusionAI/Ling-3.0-flash' in html


def test_card_layout_announcement_caption_follows_linked_source(catalog):
    review_alias(catalog, 'other|renamed-preview-free')
    path = catalog / 'model_metadata.json'
    metadata = json.loads(path.read_text())
    metadata['models']['other|renamed-preview-free'].update(
        model_page='https://creator.example/launch', model_page_is_official=True,
        model_page_label='Official announcement')
    path.write_text(json.dumps(metadata))
    source(catalog, {'one': entry(), 'later': entry('later', T3, 'other', 'renamed-preview-free')}, T3)
    html = render_page(catalog).split('class="model-details"', 1)[0]
    assert 'href="https://creator.example/launch"' in html
    assert 'Official announcement <span' in html


def test_card_layout_official_page_can_come_from_other_gateway(catalog):
    review_alias(catalog, "other|renamed-preview-free")
    path = catalog / "model_metadata.json"
    meta = json.loads(path.read_text())
    meta["models"]["other|renamed-preview-free"].update(
        model_page="https://creator.example/model", model_page_is_official=True)
    path.write_text(json.dumps(meta))
    source(catalog, {"one": entry(), "later": entry("later", T3, "other", "renamed-preview-free")}, T3)
    card = model_cards(CatalogStore(catalog).refresh())[0]
    assert card["official_model_page"] == "https://creator.example/model"
    assert card["first_seen"] == T1[:10]


def test_aggregator_dialog_owns_signup_api_and_opencode_warning(catalog):
    source(catalog, {"one": entry(aggregator="opencode")})
    path = catalog / "model_metadata.json"
    meta = json.loads(path.read_text())
    meta["aggregators"]["opencode"] = {"name": "OpenCode Zen", "signup": "https://opencode.ai/zen",
                                      "docs": "https://opencode.ai/docs/zen/"}
    path.write_text(json.dumps(meta))
    html = render_page(catalog)
    card = html.split('<article class="model-card', 1)[1].split('</article>', 1)[0]
    assert 'href="https://opencode.ai/zen"' not in card
    assert 'href="https://opencode.ai/docs/zen/"' not in card
    assert 'software-warning' not in card
    assert 'data-aggregator-info="aggregator-opencode"' in card
    popup = html.split('<dialog class="aggregator-dialog"', 1)[1].split('</dialog>', 1)[0]
    assert 'href="https://opencode.ai/zen"' in popup
    assert 'href="https://opencode.ai/docs/zen/"' in popup
    assert popup.count('class="software-warning"') == 1
    assert '<em>Some free routes require the OpenCode app or CLI.</em>' in popup


def test_card_layout_demo_time_is_persistent_and_selected_gateway_specific(catalog, monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace
    from starlette.requests import Request
    review_alias(catalog, "other|renamed-preview-free")
    review_alias(catalog, "untested|renamed-preview-free")
    source(catalog, {"one": entry(), "two": entry("two", T2, "other", "renamed-preview-free"),
                     "three": entry("three", T3, "untested", "renamed-preview-free")}, T3)
    clock = iter([datetime.fromisoformat(T1), datetime.fromisoformat(T2)])
    monkeypatch.setattr('demo.datetime', SimpleNamespace(now=lambda zone: next(clock)))
    demo = Demo(catalog)
    demo.record('one', 'green')
    demo.record('two', 'yellow')
    assert Demo(catalog).states() == {'one': {'at': T1, 'state': 'green'},
                                      'two': {'at': T2, 'state': 'yellow'}}
    app = create_app(catalog)
    endpoint = next(route.endpoint for route in app.routes if route.path == '/demo/form')
    request = Request({'type': 'http', 'method': 'GET', 'path': '/demo/form', 'headers': [],
                       'query_string': b'', 'scheme': 'http', 'server': ('testserver', 80), 'app': app})
    for route, gateway, when, state in [('one', 'test', T1, 'green'), ('two', 'other', T2, 'yellow')]:
        html = endpoint(request, route_id=route, question_id='howdy', emoji_id='0').body.decode()
        assert f'class="last-fire">{gateway} · Last tested' in html
        assert f'<time datetime="{when}">' in html and '12:00 UTC' in html
        assert f'data-state="{state}"' in html
        assert 'aria-live="polite" hidden></p>' in html
    html = endpoint(request, route_id='three', question_id='howdy', emoji_id='0').body.decode()
    assert 'untested · Not yet tested' in html and '<time' not in html
    assert 'data-state="idle"' in html
    for record in (json.loads(line) for line in (catalog / 'fire_events.jsonl').read_text().splitlines()):
        assert set(record) == {'route_id', 'state', 'at'}


def test_card_layout_plain_directory_without_menus_or_ordinal_labels(catalog):
    record_prior_scan(catalog)
    html = render_page(catalog)
    for removed in ('1ST', '2ND', 'FIRST OBSERVED', 'search-form', 'gateway-filters', 'stats-bar',
                    'view-tabs', 'timeline-legend', 'hero-actions', 'day-heading', 'gateways-section',
                    'What arrived.', 'Catalog JSON', 'Explore the timeline', 'Choose a gateway',
                    'F24 SALES / OPEN ACCESS', 'OPEN MODELS. NEW POSSIBILITIES.'):
        assert removed not in html
    assert 'type="search"' not in html and 'name="reasoning"' not in html
    assert '🏆' in html and 'class="arrival-date"' in html
    assert html.count('class="model-grid"') == 1
    assert html.count('<form') == 1  # Only the explicit model test remains.


def test_card_layout_description_limits_sources_and_dates_are_not_repeated(catalog):
    record_prior_scan(catalog)
    update_research(catalog, model_page_is_official=True, sources=[
        {'url': 'https://example.org/model', 'label': 'Official page'},
        {'url': 'https://example.org/paper', 'label': 'Paper'},
        {'url': 'https://example.org/paper', 'label': 'Same paper again'}])
    html = render_page(catalog)
    assert html.count('A short model description.') == 1
    assert html.count('href="https://example.org/model"') == 1
    assert html.count('href="https://example.org/paper"') == 1
    assert html.count(f'<time class="arrival-date" datetime="{T1[:10]}">') == 1
    assert f'<time datetime="{T1[:10]}">' not in html
    assert 'Context here' not in html and 'Max output here' not in html
    assert 'Model creator</dt>' not in html and 'Catalog history' not in html


def test_card_layout_dates_do_not_reserve_empty_grid_slots(catalog):
    # Four new models must flow straight into the baseline models, not leave
    # two empty desktop slots after the fourth card in a separate day grid.
    models = {'one': entry()}
    models.update({str(n): entry(str(n), T3 if n < 5 else T1, model=f'creator/model-{n}')
                   for n in range(1, 9)})
    source(catalog, models, T3)
    html = render_page(catalog)
    assert html.count('class="model-grid"') == 1
    assert html.count('<article class="model-card ') == 9
    assert html.count('class="arrival-panel"') == 4
    assert 'class="timeline-day"' not in html
    grid = html.split('class="model-grid"', 1)[1].split('id="selection-empty"', 1)[0]
    assert '>No models available right now.</p>' not in grid
    assert 'id="selection-empty" hidden' in html
    assert html.index('<h3>creator/model-4</h3>') < html.index('<h3>A researched model</h3>')


def test_card_layout_different_gateway_limits_remain_visible(catalog):
    review_alias(catalog, 'other|renamed-preview-free')
    later = entry('two', T2, 'other', 'renamed-preview-free')
    later['deployment']['model_info']['max_input_tokens'] = 5000
    source(catalog, {'one': entry(), 'two': later}, T2)
    html = render_page(catalog)
    assert html.count('Context here') == 1 and '<dd>5,000</dd>' in html
    assert 'Max output here' not in html


def test_aggregator_sources_are_shared_and_model_sources_stay_with_models(catalog):
    path = catalog / 'model_metadata.json'
    meta = json.loads(path.read_text())
    generic = 'https://gateway.example/models'
    specific = 'https://gateway.example/models/one'
    meta['aggregators']['test'] = {
        'signup': 'https://gateway.example/signup', 'docs': 'https://gateway.example/docs',
        'api_base': 'https://gateway.example/v1',
        'sources': [{'label': 'Catalog', 'url': generic},
                    {'label': 'Unsafe', 'url': 'javascript:alert(1)'},
                    {'label': 'Credentials', 'url': 'https://secret@gateway.example/'}]}
    meta['models']['test|creator/model'].update(
        model_page=generic + '/', sources=[{'label': 'Catalog', 'url': generic},
                                           {'label': 'Model reference', 'url': specific}])
    path.write_text(json.dumps(meta))
    source(catalog, {'one': entry(), 'two': entry('two', T2, model='creator/another')}, T2)
    html = render_page(catalog)
    cards = html.split('class="model-grid"', 1)[1].split('</main>', 1)[0]
    assert f'href="{generic}"' not in cards and f'href="{generic}/"' not in cards
    assert f'href="{specific}"' in cards
    assert 'https://gateway.example/signup' not in cards and 'https://gateway.example/docs' not in cards
    assert html.count('<dialog class="aggregator-dialog"') == 1
    assert html.count(f'href="{generic}"') == 1
    assert 'https://gateway.example/v1' not in cards
    assert '<code class="api-endpoint">https://gateway.example/v1</code>' in html
    assert 'javascript:alert' not in html and 'secret@gateway' not in html
    assert 'data-aggregator-info="aggregator-test"' in html


def test_thinking_controls_stay_specific_to_each_aggregator(catalog):
    review_alias(catalog, 'other|renamed-preview-free')
    path = catalog / 'model_metadata.json'
    meta = json.loads(path.read_text())
    meta['models']['test|creator/model']['reasoning'] = {
        'supported': True, 'effort_levels': ['low', 'high'], 'default_effort': 'high',
        'mandatory': False, 'default_enabled': False, 'budget_supported': True}
    meta['models']['other|renamed-preview-free']['reasoning'] = {
        'supported': True, 'effort_levels': ['medium'], 'default_effort': 'medium',
        'mandatory': True, 'default_enabled': True, 'budget_supported': False}
    path.write_text(json.dumps(meta))
    source(catalog, {'one': entry(), 'two': entry('two', T2, 'other', 'renamed-preview-free')}, T2)
    html = render_page(catalog)
    blocks = [part.split('</section>', 1)[0] for part in html.split('<section class="gateway-spec">')[1:]]
    assert len(blocks) == 2
    first = next(block for block in blocks if 'aggregator-test' in block)
    other = next(block for block in blocks if 'aggregator-other' in block)
    assert 'low · high' in first and '<dd>high</dd>' in first
    assert 'Can be toggled' in first and '<dd>Disabled</dd>' in first and 'medium' not in first
    assert '<dd>medium</dd>' in other and 'Always enabled' in other
    assert '<dd>Enabled</dd>' in other and 'low · high' not in other
    common = html.split('class="gateway-specs"', 1)[0].rsplit('class="details-content"', 1)[1]
    assert 'effort' not in common.lower() and 'Thinking' not in common


def test_unadvertised_thinking_does_not_inherit_model_controls(catalog):
    item = entry()
    item['supported_parameters'] = []
    source(catalog, {'one': item})
    update_research(catalog, reasoning={'supported': False, 'effort_levels': ['low', 'high'],
                                      'default_effort': 'high', 'budget_supported': True})
    html = render_page(catalog)
    thinking = html.split('class="gateway-thinking"', 1)[1].split('</dl>', 1)[0]
    assert 'Not advertised by this API' in thinking
    assert 'low' not in thinking and 'Token budget' not in thinking
    update_research(catalog, reasoning={'supported': None})
    assert 'Not documented</span>' in render_page(catalog)


@pytest.mark.parametrize('aggregator,model', [
    ('openrouter', 'nvidia/nemotron-3-super-120b-a12b:free'),
    ('groq', 'openai/gpt-oss-120b'),
    ('nvidia', 'nvidia/model-fast'),  # A genuine creator prefix/suffix must survive.
])
def test_direct_model_ids_preserve_catalog_id_without_local_aliases(catalog, aggregator, model):
    variants = {}
    for variant in ('base', 'fast', 'think'):
        item = entry(variant, aggregator=aggregator, model=model)
        item['deployment']['model_info']['free_sync_variant'] = variant
        item['deployment']['model_name'] = aggregator + '/' + model + '-' + variant
        variants[variant] = item
    source(catalog, variants)
    html = render_page(catalog)
    assert f'<code>{model}</code>' in html
    for item in variants.values():
        assert item['deployment']['model_name'] not in html
    assert 'Default settings' in html and 'Thinking preset' in html and 'Fast preset' in html
    assert 'class="routes"' not in html


@pytest.mark.parametrize('entry_type', ['music', 'guardrail'])
def test_non_chat_models_are_removed_from_directory_and_demo_but_keep_history(catalog, entry_type):
    review_alias(catalog, 'other|renamed-preview-free')
    update_research(catalog, entry_type=entry_type)
    source(catalog, {'one': entry(), 'two': entry('two', T2, 'other', 'renamed-preview-free')}, T2)
    (catalog / 'runs.log').write_text(json.dumps(
        {'status': 'OK', 'finished_at': T2, 'providers': {'test': {}, 'other': {}}}) + '\n')
    app = create_app(catalog)
    data = app.state.store.refresh()
    assert data['models']['one']['details']['entry_type'] == entry_type
    assert len(data['models']) == 2 and model_cards(data) == []
    client = TestClient(app)
    assert client.get('/catalog.json').json()['models'] == []
    html = client.get('/').text
    assert '<article class="model-card ' not in html
    assert 'discovering 0 free Models from 0 Providers via 0 Aggregators' in html
    assert '<select' not in client.get('/demo/form?route_id=one').text
    def no_request(request):
        pytest.fail('A non-chat model must not receive a demo request')
    for route_id in ('one', 'two'):
        result = asyncio.run(Demo(catalog).fire(route_id, 'howdy', '0', data,
                                               transport=httpx.MockTransport(no_request)))
        assert result == {'state': 'yellow'}


def test_card_layout_uses_brand_bar_opacities_not_transparent_text():
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1] / 'static/style.css').read_text()
    assert '--brand-step-1:#1975d1' in css
    assert '--brand-step-2:rgb(25 117 209 / 50%)' in css
    assert '--brand-step-3:rgb(25 117 209 / 25%)' in css
    for selector, token in [('.first-place', 'var(--brand-step-1)'),
                            ('.second-place', 'var(--brand-step-2)'),
                            ('.gateway-spec', 'var(--brand-step-3)')]:
        rule = css.split(selector + '{', 1)[1].split('}', 1)[0]
        assert 'border:1px solid ' + token in rule and 'opacity:' not in rule
        assert 'background:transparent' in rule
    assert '#b98e30' not in css and '.timeline-day' not in css
    for selector in ('.first-place', '.first-place .gateway-timeline li',
                     '.second-place', '.second-place .arrival-date',
                     '.second-place .gateway-timeline li'):
        assert 'color:var(--ink)' in css.split(selector + '{', 1)[1].split('}', 1)[0]


@pytest.mark.parametrize('value', [False, None, 'true', 1, 0])
def test_card_layout_open_weights_requires_explicit_boolean(catalog, value):
    update_research(catalog, open_weights=value, open_weights_source='https://example.org/weights')
    assert 'data-feature="open-weights"' not in render_page(catalog)


@pytest.mark.parametrize('url', [None, 'http://example.org/weights', 'javascript:alert(1)',
                                  'https://secret@example.org/weights'])
def test_card_layout_open_weights_requires_safe_evidence(catalog, url):
    update_research(catalog, open_weights=True, open_weights_source=url)
    assert 'data-feature="open-weights"' not in render_page(catalog)


def test_card_layout_positive_capabilities_only(catalog):
    update_research(catalog, open_weights=True, open_weights_source='https://example.org/weights')
    html = render_page(catalog)
    assert html.count('data-feature="open-weights"') == 1
    assert html.count('data-feature="reasoning"') == 1
    item = entry()
    item['supported_parameters'] = []
    source(catalog, {'one': item})
    for supported in (False, None, 'true', 1):
        update_research(catalog, reasoning={'supported': supported})
        html = render_page(catalog)
        assert 'data-feature="reasoning"' not in html
        assert 'Reasoning here' not in html and 'reasoning-value' not in html
    assert 'data-feature="open-weights"' in html


def test_card_layout_open_weights_shared_across_reviewed_aliases(catalog):
    review_alias(catalog, 'other|renamed-preview-free')
    path = catalog / 'model_metadata.json'
    meta = json.loads(path.read_text())
    meta['models']['other|renamed-preview-free'].update(
        open_weights=True, open_weights_source='https://example.org/weights')
    path.write_text(json.dumps(meta))
    source(catalog, {'one': entry(), 'two': entry('two', T2, 'other', 'renamed-preview-free')}, T2)
    assert render_page(catalog).count('data-feature="open-weights"') == 1


def test_card_layout_overview_counts_models_not_variants(catalog):
    review_alias(catalog, 'other|renamed-preview-free')
    variant = entry('think')
    variant['deployment']['model_info']['free_sync_variant'] = 'think'
    source(catalog, {'one': entry(), 'think': variant,
                    'other': entry('other', T2, 'other', 'renamed-preview-free')}, T2)
    (catalog / 'runs.log').write_text(json.dumps(
        {'status': 'OK', 'finished_at': T3, 'providers': {'test': {}, 'other': {}}}) + '\n')
    html = render_page(catalog)
    overview = html.split('class="catalog-overview"', 1)[1].split('class="model-grid"', 1)[0]
    assert html.count('<article class="model-card ') == 1
    assert '<span class="tile-name">other</span><strong>1</strong>' in overview
    assert '<span class="tile-name">test</span><strong>1</strong>' in overview
    assert '<span class="tile-name">creator</span><strong>1</strong>' in overview
    assert f'<time datetime="{T3}">28 Sep 2026 · 12:00 UTC</time>' in html
    assert 'Last scan' not in overview and 'models' not in overview.lower()


def test_card_layout_overview_filters_failed_models_and_old_providers(catalog, monkeypatch):
    source(catalog, {'one': entry(), 'red': entry('red', T2, 'other', 'old/model')}, T2)
    data = CatalogStore(catalog).refresh()
    data['source_type'] = 'model_probe'
    data['models']['one']['details']['probe_status'] = 'green'
    data['models']['red']['details']['probe_status'] = 'red'
    monkeypatch.setattr(CatalogStore, 'refresh', lambda self: data)
    html = render_page(catalog)
    overview = html.split('class="catalog-overview"', 1)[1].split('class="model-grid"', 1)[0]
    assert html.count('<article class="model-card ') == 1
    assert '<span>other</span>' not in overview
    assert 'Last scan' not in html  # Never invent a timestamp.


def test_card_layout_local_logos_and_curated_flags(catalog):
    path = catalog / 'model_metadata.json'
    meta = json.loads(path.read_text())
    meta['creators'] = {'creator': {'logo': '/static/logos/openai.svg', 'country': 'US',
        'country_name': 'United States', 'country_source': 'https://example.org/about'}}
    meta['aggregators'] = {'test': {'logo': '/static/logos/groq.svg'}}
    path.write_text(json.dumps(meta))
    html = render_page(catalog)
    assert html.count('src="/static/logos/openai.svg?v=brand-v20"') == 2  # Tile and model card.
    assert html.count('src="/static/logos/groq.svg?v=brand-v20"') == 2  # Overview and aggregator dialog.
    assert html.count('🇺🇸') == 1
    assert html.index('class="gateway-counts"') < html.index('class="provider-tiles"') < html.index('class="model-grid"')
    card = html.split('<article class="model-card', 1)[1].split('</article>', 1)[0]
    assert 'country-flag' not in card and '/static/logos/groq.svg' not in card
    overview = html.split('class="catalog-overview"', 1)[1].split('class="model-grid"', 1)[0]
    assert overview.count('width="36" height="36"') == 2
    assert 'width="16" height="16"' in card  # Inline logo stays small.


@pytest.mark.parametrize('path', ['https://example.org/logo.svg', '/static/logos/../secret.svg',
                                   '/static/logos/nonexistent.svg', '/static/logos/logo.svg?token=x'])
def test_card_layout_logos_reject_remote_missing_and_traversal_paths(path):
    from catalog_store import local_logo
    assert local_logo(path) is None


def test_card_layout_unknown_country_is_not_guessed(catalog):
    path = catalog / 'model_metadata.json'
    meta = json.loads(path.read_text())
    meta['creators'] = {'creator': {'country': 'US', 'country_name': 'United States'}}
    path.write_text(json.dumps(meta))
    assert 'country-flag' not in render_page(catalog)


def test_card_layout_bundled_logos_are_small_passive_svg():
    from pathlib import Path
    from xml.etree import ElementTree as ET
    root = Path(__file__).resolve().parents[1]
    for path in (root / 'static/logos').glob('*.svg'):
        svg = ET.parse(path).getroot()
        assert float(svg.get('width')) > 0 and float(svg.get('height')) > 0
        assert svg.get('viewBox')
        ids = {element.get('id') for element in svg.iter() if element.get('id')}
        for element in svg.iter():
            assert element.tag.rsplit('}', 1)[-1] in {'svg', 'path', 'g', 'circle', 'rect', 'polygon',
                                                     'defs', 'linearGradient', 'stop', 'clipPath'}
            assert not any(key.lower().startswith('on') for key in element.attrib)
            for key, value in element.attrib.items():
                if 'href' in key:
                    assert element.tag.rsplit('}', 1)[-1] == 'linearGradient'
                    assert value.startswith('#') and value[1:] in ids
    css = (root / 'static/style.css').read_text()
    rule = css.split('.brand-icon{', 1)[1].split('}', 1)[0]
    assert 'width:16px;height:16px;object-fit:contain' in rule


def test_card_layout_brand_colors_and_intrinsic_header(catalog):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for name, colors in {'nvidia': ['#74B71B'], 'alibaba': ['#FF6003'],
                         'groq': ['#F55036'], 'cohere': ['#39594D', '#D18EE2', '#FF7759']}.items():
        asset = (root / 'static/logos' / (name + '.svg')).read_text()
        assert all(color in asset for color in colors)
        assert '#142235' not in asset
    css = (root / 'static/style.css').read_text()
    logo = css.split('.wordmark img{', 1)[1].split('}', 1)[0]
    wrapper = css.split('.wordmark{', 1)[1].split('}', 1)[0]
    assert 'height:auto;width:auto;max-width:none' in logo
    assert 'overflow:hidden' not in wrapper and 'min-width:0' in wrapper
    assert 'grayscale(' not in css
    assert 'prefers-reduced-motion:no-preference' in css
    assert '[hidden]{display:none!important}' in css
    html = render_page(catalog)
    assert '/static/catalog.js?v=interactive-brand-v20' in html
    assert 'src="https://' not in html


def test_card_layout_aggregators_and_providers_share_compact_tile_grid(catalog):
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1] / 'static/style.css').read_text()
    grid = css.split('.gateway-counts,.provider-tiles{', 1)[1].split('}', 1)[0]
    assert 'display:grid' in grid and 'gap:10px' in grid
    assert 'repeat(auto-fill,minmax(min(100%,108px),1fr))' in grid
    tile = css.split('.tile-choice,.select-all-tile{', 1)[1].split('}', 1)[0]
    assert 'aspect-ratio:1' in tile and 'font-size:11px' in tile
    assert 'position:relative' in tile and 'grid-template-rows:36px 1fr 18px' in tile
    assert 'border:2px solid var(--line)' in tile and 'background:var(--panel)' in tile
    assert '.tile-choice[aria-pressed="true"]{border-color:var(--blue)}' in css
    assert 'overflow-wrap:anywhere' in css
    flag = css.split('.country-flag{', 1)[1].split('}', 1)[0]
    assert 'width:12px;height:10px;font-size:10px' in flag
    assert 'position:absolute' in flag and 'right:8px;bottom:8px' in flag
    logo = css.split('.catalog-overview .brand-icon{', 1)[1].split('}', 1)[0]
    assert 'width:36px;height:36px' in logo
    html = render_page(catalog)
    overview = html.split('class="catalog-overview"', 1)[1].split('class="model-grid"', 1)[0]
    assert 'class="gateway-counts"' in overview and 'class="provider-tiles"' in overview
    assert 'class="tile-choice"' in overview and '<input' not in overview
    assert 'aria-pressed="true"' in overview and 'aria-controls="model-grid"' in overview
    assert overview.index('Select all') < overview.index('>Aggregators</h3>')
    assert 'id="reset-choices" type="button" hidden' in overview


def test_card_layout_stealth_omitted_from_providers_but_model_retained(catalog):
    update_research(catalog, creator='Not disclosed (stealth)',
                    duplicate_key='stealth/space-bunny', display_name='Space Bunny')
    source(catalog, {'one': entry(), 'known': entry('known', T2, model='creator/known')}, T2)
    html = render_page(catalog)
    providers = html.split('class="provider-tiles"', 1)[1].split('</ul>', 1)[0]
    assert 'Not disclosed' not in providers and 'stealth' not in providers.lower()
    assert '<span class="tile-name">creator</span>' in providers
    assert '<h3>Space Bunny</h3>' in html and 'Stealth model' in html
    assert html.count('<article class="model-card ') == 2


def test_card_layout_scan_heading_below_title_and_small_group_headings(catalog):
    from pathlib import Path
    (catalog / 'runs.log').write_text(json.dumps(
        {'status': 'OK', 'finished_at': T3, 'providers': {'test': {}}}) + '\n')
    html = render_page(catalog)
    assert html.count('Last scan') == 1
    assert '<h2 class="last-scan">Last scan <time' in html
    markers = ['via API.</span></h1>', 'class="last-scan"', '>Aggregators</h3>',
               'class="gateway-counts"', '>Providers</h3>', 'class="provider-tiles"',
               'class="model-grid"']
    positions = [html.index(marker) for marker in markers]
    assert positions == sorted(positions)
    css = (Path(__file__).resolve().parents[1] / 'static/style.css').read_text()
    scan = css.split('.last-scan{', 1)[1].split('}', 1)[0]
    assert 'font-style:italic' in scan and 'font-weight:700' in scan
    title = css.split('.overview-title{', 1)[1].split('}', 1)[0]
    assert 'font-size:12px' in title


def test_card_layout_provider_count_covers_distinct_models(catalog):
    variant = entry('think')
    variant['deployment']['model_info']['free_sync_variant'] = 'think'
    source(catalog, {'one': entry(), 'think': variant,
                    'two': entry('two', T2, model='creator/another')}, T2)
    html = render_page(catalog)
    providers = html.split('class="provider-tiles"', 1)[1].split('</ul>', 1)[0]
    assert '<span class="tile-name">creator</span><strong>2</strong>' in providers
    assert 'models' not in providers.lower()


def test_card_layout_baseline_box_only_lists_passing_unique_gateways(catalog, monkeypatch):
    for gateway in ('failed', 'removed'):
        review_alias(catalog, gateway + '|alias')
    variant = entry('think')
    variant['deployment']['model_info']['free_sync_variant'] = 'think'
    source(catalog, {'one': entry(), 'think': variant,
                    'bad': entry('bad', T2, 'failed', 'alias'),
                    'gone': entry('gone', T2, 'removed', 'alias')}, T2)
    data = CatalogStore(catalog).refresh()
    data['source_type'] = 'model_probe'
    for ident, record in data['models'].items():
        record['details']['probe_status'] = 'green' if ident in ('one', 'think') else 'red'
    data['models']['gone']['active'] = False
    monkeypatch.setattr(CatalogStore, 'refresh', lambda self: data)
    html = render_page(catalog)
    box = html.split('class="available-gateways"', 1)[1].split('</ul>', 1)[0]
    assert box.count('<li>test</li>') == 1
    assert 'failed' not in box and 'removed' not in box
    assert '<time' not in box and '🏆' not in box and '🥈' not in box
    assert '<img' not in box  # Only the creator logo appears in a model card.
    assert 'class="duplicate-tag"' not in html


def test_card_layout_new_models_keep_podium_not_baseline_box(catalog):
    record_prior_scan(catalog)
    html = render_page(catalog)
    assert 'class="arrival-panel"' in html and '🏆' in html
    assert 'class="available-gateways"' not in html


@pytest.mark.parametrize('identity', ['openrouter|openrouter/free', 'kilo|kilo-auto/free',
                                      'kilo|openrouter/free'])
def test_card_layout_routing_offers_hidden_only_from_web_page(catalog, identity):
    from pathlib import Path
    metadata = json.loads((Path(__file__).resolve().parents[1] / 'model_metadata.json').read_text())
    detail = metadata['models'][identity]
    assert detail['entry_type'] == 'router'
    update_research(catalog, **detail)
    html = render_page(catalog)
    providers = html.split('class="provider-tiles"', 1)[1].split('</ul>', 1)[0]
    assert detail['creator'] not in providers and '<li>' not in providers
    assert '<article class="model-card ' not in html
    assert '<span class="tile-name">test</span><strong>' not in html
    cards = model_cards(CatalogStore(catalog).refresh())
    assert len(cards) == 1 and cards[0]['entry_type'] == 'router'
    assert cards[0]['routes'][0]['id'] == 'one'
    assert 'one' in json.loads((catalog / 'free_models.json').read_text())['models']


def test_card_layout_dual_role_aggregator_can_still_be_model_provider(catalog):
    update_research(catalog, creator='test', entry_type='model')
    html = render_page(catalog)
    providers = html.split('class="provider-tiles"', 1)[1].split('</ul>', 1)[0]
    assert '<span class="tile-name">test</span><strong>1</strong>' in providers


def test_card_layout_baseline_box_keeps_gateways_without_repeating_provider(catalog):
    from pathlib import Path
    update_research(catalog, creator='Alibaba')
    html = render_page(catalog)
    box = html.split('class="available-gateways"', 1)[1].split('</ul>', 1)[0]
    assert 'class="available-provider"' not in box and 'Alibaba</li>' not in box
    assert box.count('<li>test</li>') == 1
    assert '<time' not in box and '🏆' not in box and '🥈' not in box
    css = (Path(__file__).resolve().parents[1] / 'static/style.css').read_text()
    rule = css.split('.available-gateways{', 1)[1].split('}', 1)[0]
    assert 'background:#fff' in rule and 'border:1px solid var(--brand-step-3)' in rule


def test_card_layout_baseline_box_does_not_invent_manufacturer(catalog):
    update_research(catalog, creator='Undisclosed or router operator',
                    entry_type='model', duplicate_key='stealth/anonymous')
    html = render_page(catalog)
    box = html.split('class="available-gateways"', 1)[1].split('</ul>', 1)[0]
    assert 'class="available-provider"' not in box
    assert 'Undisclosed or router operator' not in box
    assert '<li>test</li>' in box


@pytest.mark.parametrize('aggregator,router', [('kilo', 'kilo-auto/free'),
                                             ('openrouter', 'openrouter/free')])
def test_card_layout_router_hidden_but_its_aggregator_stays(catalog, aggregator, router):
    path = catalog / 'model_metadata.json'
    meta = json.loads(path.read_text())
    meta['models'][aggregator + '|' + router] = {
        'name': 'Routing offer', 'creator': aggregator, 'entry_type': 'router',
        'research_status': 'reviewed', 'duplicate_key': router}
    path.write_text(json.dumps(meta))
    source(catalog, {'routing': entry('routing', T1, aggregator, router),
                    'real': entry('real', T2, aggregator, 'creator/real')}, T2)
    before = (catalog / 'free_models.json').read_bytes()
    html = render_page(catalog)
    assert '<h3>Routing offer</h3>' not in html and '<h3>creator/real</h3>' in html
    assert f'<span class="tile-name">{aggregator}</span><strong>1</strong>' in html
    assert len(model_cards(CatalogStore(catalog).refresh())) == 2
    assert (catalog / 'free_models.json').read_bytes() == before
