"""SQL worker, also executable inside an existing LiteLLM container over stdin."""
from __future__ import annotations

import json
import os
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

OWNER = "f24-sales-import"


class ImportFailure(Exception):
    pass


def as_object(value):
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def import_models(models, dry_run=False):
    import psycopg
    from psycopg.types.json import Jsonb
    from litellm.proxy.common_utils.encrypt_decrypt_utils import encrypt_value_helper, decrypt_value_helper

    raw = os.environ.get("DATABASE_URL", "")
    if not raw:
        raise ImportFailure("DATABASE_URL is missing in the SQL environment")
    if not (os.environ.get("LITELLM_SALT_KEY") or os.environ.get("LITELLM_MASTER_KEY")):
        raise ImportFailure("The proxy's existing LITELLM_SALT_KEY or LITELLM_MASTER_KEY is required")
    parsed = urlsplit(raw)
    query = dict(parse_qsl(parsed.query))
    if query.pop("schema", "public") != "public":
        raise ImportFailure("This importer supports the public LiteLLM schema")
    dsn = urlunsplit(parsed._replace(query=urlencode(query)))
    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "skipped_existing": 0, "dry_run": dry_run}
    with psycopg.connect(dsn, connect_timeout=15) as db:
        if dry_run:
            db.execute("SET TRANSACTION READ ONLY")
        db.execute("SET LOCAL statement_timeout = '15s'")
        columns = dict(db.execute("SELECT column_name, data_type FROM information_schema.columns "
                                  "WHERE table_schema = 'public' AND table_name = 'LiteLLM_ProxyModelTable'").fetchall())
        expected = {"model_id": "text", "model_name": "text", "litellm_params": "jsonb", "model_info": "jsonb",
                    "created_by": "text", "updated_by": "text", "created_at": "timestamp without time zone",
                    "updated_at": "timestamp without time zone", "blocked": "boolean"}
        if any(columns.get(k) != v for k, v in expected.items()):
            raise ImportFailure("Unsupported LiteLLM database schema; no models were written")
        if not dry_run:
            db.execute('LOCK TABLE public."LiteLLM_ProxyModelTable" IN SHARE ROW EXCLUSIVE MODE')
        rows = db.execute('SELECT model_id, model_name, model_info, litellm_params '
                          'FROM public."LiteLLM_ProxyModelTable"').fetchall()
        existing = {row[0]: row for row in rows}
        names = {}
        for ident, name, info, params in rows:
            names.setdefault(name, []).append(ident)
        for model in models:
            info, params = model["model_info"], model["litellm_params"]
            ident, name = info["id"], model["model_name"]
            old = existing.get(ident)
            if old and as_object(old[2]).get("managed_by") != OWNER:
                raise ImportFailure("Model ID belongs to another manager; transaction cancelled")
            if any(other != ident for other in names.get(name, [])):
                counts["skipped_existing"] += 1
                continue
            if old:
                old_params = as_object(old[3])
                plain = {k: decrypt_value_helper(v, key=k, exception_type="debug") if isinstance(v, str) else v
                         for k, v in old_params.items()}
                if any(value is None for key, value in plain.items() if isinstance(old_params.get(key), str)):
                    raise ImportFailure("Existing imported parameters cannot be decrypted with this proxy's key")
                if old[1] == name and as_object(old[2]) == info and plain == params:
                    counts["unchanged"] += 1
                    continue
            counts["updated" if old else "inserted"] += 1
            if dry_run:
                continue
            encrypted = {key: encrypt_value_helper(value) if isinstance(value, str) else value for key, value in params.items()}
            if old:
                db.execute('UPDATE public."LiteLLM_ProxyModelTable" SET model_name=%s, litellm_params=%s, '
                           'model_info=%s, updated_at=CURRENT_TIMESTAMP, updated_by=%s WHERE model_id=%s',
                           (name, Jsonb(encrypted), Jsonb(info), OWNER, ident))
            else:
                db.execute('INSERT INTO public."LiteLLM_ProxyModelTable" '
                           '(model_id,model_name,litellm_params,model_info,created_by,updated_by,created_at,updated_at,blocked) '
                           'VALUES (%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,false)',
                           (ident, name, Jsonb(encrypted), Jsonb(info), OWNER, OWNER))
        if dry_run:
            db.rollback()
    return counts


def main():
    try:
        payload = json.load(sys.stdin)
        print(json.dumps(import_models(payload["models"], dry_run=payload.get("dry_run", False))))
        return 0
    except ImportFailure as exc:
        print(json.dumps({"error": str(exc)}))
    except Exception as exc:
        print(json.dumps({"error": "SQL import failed; transaction rolled back", "type": type(exc).__name__}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
