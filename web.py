"""f24-sales: local catalog HTML and an explicitly triggered backend-only demo."""
from collections import Counter
from datetime import datetime
import logging
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from catalog_store import CatalogStore, ROOT, model_cards
from demo import Demo, QUESTIONS, EMOJIS
from litellm_export import export_config, env_template


class FireInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    route_id: str = Field(min_length=1, max_length=100)
    question_id: str = Field(max_length=30)
    emoji_id: str = Field(max_length=3)

    @field_validator("question_id", "emoji_id")
    @classmethod
    def allowed_selection(cls, value, info):
        if value not in (QUESTIONS if info.field_name == "question_id" else EMOJIS):
            raise ValueError("Unknown preset")
        return value


def compact_tokens(value):
    if value is None:
        return "—"
    for divisor, suffix in ((1_000_000, "M"), (1_000, "K")):
        if value >= divisor:
            return f"{value / divisor:.2f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def date_label(value, full=False):
    if not value:
        return "Not recorded"
    date = datetime.fromisoformat(value)
    return date.strftime("%d %b %Y · %H:%M UTC" if full and "T" in value else "%d %b %Y")


def create_app(data_dir=None):
    app = FastAPI(title="f24-sales · Free AI directory", docs_url=None, redoc_url=None, openapi_url=None)
    root = Path(data_dir or os.environ.get("FREE_WEB_DATA_DIR", ROOT)).expanduser()
    source_value = os.environ.get("FREE_WEB_SOURCE", "model_probe_results.json")
    source = Path(source_value).expanduser()
    if not source.is_absolute():
        source = root / source
    # Test fixtures and older installations may not have a promoted probe SOT
    # yet; keep the deployment catalog as a safe compatibility fallback.
    if not source.exists():
        source = root / "free_models.json"
    store = CatalogStore(root, source=source)
    app.state.store = store
    demo = Demo(store.root)
    app.state.demo = demo
    templates = Jinja2Templates(directory=ROOT / "templates")
    templates.env.filters.update(tokens=compact_tokens, date=date_label)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.middleware("http")
    async def privacy_headers(request, call_next):
        response = await call_next(request)
        response.headers.update({
            "Content-Security-Policy": "default-src 'none'; style-src 'self'; img-src 'self'; font-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
            "Cache-Control": "public, max-age=300" if request.url.path.startswith("/static/") else "no-store",
        })
        return response

    def load():
        try:
            return store.refresh()
        except (OSError, ValueError, KeyError, TypeError):
            logging.getLogger(__name__).exception("Local catalog unavailable")
            return None

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        data = load()
        if data is None:
            return templates.TemplateResponse(request=request, name="unavailable.html", context={}, status_code=503)
        green_only = data.get("source_type") == "model_probe"
        cards = [card for card in model_cards(data, probe_status="green" if green_only else None)
                 if card["entry_type"] != "router"]
        last_states = demo.states()
        for card in cards:
            for route in card["routes"]:
                route["last_fire"] = last_states.get(route["id"], {})

        return templates.TemplateResponse(request=request, name="index.html", context={
            "data": data, "cards": cards,
            "gateway_counts": Counter(g for card in cards for g in card["available_gateways"]),
            "creator_counts": Counter(card["author"] for card in cards
                                      if card["author"] and card["entry_type"] != "router"
                                      and not card["duplicate_key"].startswith("stealth/")),
            "questions": QUESTIONS, "emojis": EMOJIS,
        })

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exception):
        return JSONResponse({"state": "yellow"}, status_code=400)

    @app.post("/api/fire")
    async def fire(request: Request):
        origin = request.headers.get("origin")
        if (request.headers.get("x-f24-demo") != "1" or
                request.headers.get("content-type", "").split(";")[0] != "application/json" or
                (origin and urlsplit(origin).netloc != request.headers.get("host"))):
            return JSONResponse({"state": "yellow"}, status_code=403)
        try:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 2048:
                    return JSONResponse({"state": "yellow"}, status_code=413)
            fields = json.loads(body)
            fields = FireInput.model_validate(fields).model_dump()
            data = load()
            if data is None:
                return {"state": "yellow"}
            return await demo.fire(catalog=data, **fields)
        except (OSError, ValueError, TypeError):
            return {"state": "yellow"}

    def render_demo(request, fields, data, result):
        if data is None:
            raise ValueError("Catalog unavailable")
        green_only = data.get("source_type") == "model_probe"
        card = next((c for c in model_cards(data, probe_status="green" if green_only else None)
                     if any(r["id"] == fields.route_id for r in c["routes"])), None)
        if not card:
            return HTMLResponse('<button class="fire-button" data-state="yellow" type="button">Fire ↗</button>')
        return templates.TemplateResponse(request=request, name="demo_form.html", context={
            "card": card, "questions": QUESTIONS, "emojis": EMOJIS, "result": result,
            "selected_route": fields.route_id, "selected_question": fields.question_id, "selected_emoji": fields.emoji_id})

    @app.get("/demo/form", response_class=HTMLResponse)
    def demo_form(request: Request, route_id: str, question_id: str = "howdy", emoji_id: str = "0"):
        try:
            fields = FireInput(route_id=route_id, question_id=question_id, emoji_id=emoji_id)
            return render_demo(request, fields, load(), demo.states().get(route_id, {}))
        except (ValueError, KeyError, TypeError, OSError):
            return HTMLResponse('<button class="fire-button" data-state="yellow" type="button">Fire ↗</button>')

    @app.post("/demo/fire", response_class=HTMLResponse)
    async def demo_fire(request: Request):
        yellow = HTMLResponse('<button class="fire-button" data-state="yellow" type="button">Fire ↗</button>')
        origin = request.headers.get("origin")
        if (request.headers.get("hx-request") != "true" or
                request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded" or
                (origin and urlsplit(origin).netloc != request.headers.get("host"))):
            return yellow
        try:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 2048:
                    return yellow
            # Starlette's tested form parser; cache the bounded body for it.
            request._body = bytes(body)
            form = await request.form(max_fields=3, max_files=0)
            if len(form.multi_items()) != 3:
                return yellow
            fields = FireInput.model_validate(dict(form))
            data = load()
            if data is None:
                return yellow
            result = await demo.fire(catalog=data, **fields.model_dump())
            return render_demo(request, fields, data, result)
        except (ValueError, TypeError, OSError):
            return yellow

    @app.get("/catalog.json")
    def catalog():
        data = load()
        if data is None:
            return JSONResponse({"error": "Local catalog unavailable"}, status_code=503)
        green_only = data.get("source_type") == "model_probe"
        models = model_cards(data, probe_status="green" if green_only else None)
        visible_keys = {g for c in models for g in c["available_gateways"]}
        return {"schema_version": 1, "catalog_updated_at": data["catalog_updated_at"], "checked_at": data["checked_at"],
                "models": models,
                "aggregators": {key: value for key, value in data["aggregators"].items() if key in visible_keys}}

    @app.get("/litellm-config.json")
    def litellm_config():
        data = load()
        try:
            if data is None:
                raise ValueError("Catalog unavailable")
            result = export_config(data)
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "No verified LiteLLM configuration available"}, status_code=503)
        return JSONResponse(result, headers={"Content-Disposition": 'attachment; filename="litellm-free.json"'})

    @app.get("/litellm.env.example")
    def litellm_env():
        return PlainTextResponse(env_template(), headers={"Content-Disposition": 'attachment; filename=".env.example"'})

    @app.get("/healthz")
    def health():
        return JSONResponse({"status": "ok"}) if load() else JSONResponse({"status": "unavailable"}, status_code=503)

    return app


app = create_app()
