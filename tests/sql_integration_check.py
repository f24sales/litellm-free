"""Manual integration check for a disposable DB named f24_import_test only.

Run inside the target LiteLLM Python environment with sql_import.py importable.
Input on stdin: a JSON array of at least three exported model entries, using
dummy API keys. DATABASE_URL must point to a separate disposable PostgreSQL.
"""
from copy import deepcopy
import json
import os
import sys

import psycopg
from psycopg.types.json import Jsonb
from litellm.proxy.common_utils.encrypt_decrypt_utils import decrypt_value_helper
from sql_import import ImportFailure, import_models


def check(models):
    dsn = os.environ['DATABASE_URL']
    with psycopg.connect(dsn) as db:
        assert db.execute('select current_database()').fetchone()[0] == 'f24_import_test'
        db.execute('CREATE TABLE public."LiteLLM_ProxyModelTable" ('
                   'model_id text PRIMARY KEY, model_name text NOT NULL, litellm_params jsonb NOT NULL, '
                   'model_info jsonb, blocked boolean NOT NULL DEFAULT false, '
                   'created_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP, created_by text NOT NULL, '
                   'updated_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_by text NOT NULL)')
    first = models[:1]
    assert import_models(first, dry_run=True)['inserted'] == 1
    with psycopg.connect(dsn) as db:
        assert db.execute('SELECT count(*) FROM public."LiteLLM_ProxyModelTable"').fetchone()[0] == 0
    assert import_models(first)['inserted'] == 1
    assert import_models(first)['unchanged'] == 1
    with psycopg.connect(dsn) as db:
        row = db.execute('SELECT litellm_params FROM public."LiteLLM_ProxyModelTable"').fetchone()[0]
        assert row['api_key'] != first[0]['litellm_params']['api_key']
        assert decrypt_value_helper(row['api_key'], key='api_key') == first[0]['litellm_params']['api_key']
        db.execute('UPDATE public."LiteLLM_ProxyModelTable" SET blocked=true')
    changed = deepcopy(first)
    changed[0]['litellm_params']['api_key'] = 'changed-dummy-key'
    assert import_models(changed)['updated'] == 1
    with psycopg.connect(dsn) as db:
        assert db.execute('SELECT blocked FROM public."LiteLLM_ProxyModelTable"').fetchone()[0] is True
        db.execute('INSERT INTO public."LiteLLM_ProxyModelTable" '
                   '(model_id,model_name,litellm_params,model_info,created_by,updated_by) '
                   'VALUES (%s,%s,%s,%s,%s,%s)',
                   ('foreign-id', models[1]['model_name'], Jsonb({}), Jsonb({'managed_by': 'other'}), 'other', 'other'))
    assert import_models(models[1:2])['skipped_existing'] == 1
    with psycopg.connect(dsn) as db:
        db.execute('UPDATE public."LiteLLM_ProxyModelTable" SET model_info=%s WHERE model_id=%s',
                   (Jsonb({'managed_by': 'other'}), models[0]['model_info']['id']))
    try:
        import_models([models[2], models[0]])
        raise AssertionError('Foreign ID should abort the transaction')
    except ImportFailure:
        pass
    with psycopg.connect(dsn) as db:
        assert db.execute('SELECT count(*) FROM public."LiteLLM_ProxyModelTable"').fetchone()[0] == 2
        assert not db.execute('SELECT model_id FROM public."LiteLLM_ProxyModelTable" WHERE model_id=%s',
                              (models[2]['model_info']['id'],)).fetchone()
    print('SQL integration: read-only dry-run, insert, encrypted roundtrip, repeat, update, blocked flag, foreign alias and rollback passed.')


if __name__ == '__main__':
    check(json.load(sys.stdin))
