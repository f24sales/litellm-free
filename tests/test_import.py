from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest
import yaml

import import_litellm as importer
from config_files import publish_file
from litellm_export import CATALOG_URL, CONFIG_URL, OWNER, PROVIDERS, config_template, env_template, model_id, route_params, validate_config

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("command_exit", [0, 7])
def test_confirmed_import_runs_prerequisite_before_notification(config, tmp_path, monkeypatch, capsys, command_exit):
    source = tmp_path / "input.json"
    source.write_text(json.dumps(config))
    settings = tmp_path / "settings.conf"
    settings.write_text('GROQ_API_KEY=test\nIMPORT_HOOK_ON_NO_CHANGE=true\n'
                        'IMPORT_PRE_SUCCESS_COMMAND_JSON=["/local/prerequisite", "$literal"]\n')
    order = []

    def imported(*args, **kwargs):
        order.append("readback")
        return {"inserted": 0, "unchanged": 3, "dry_run": False}

    def prerequisite(argv, **kwargs):
        order.append("prerequisite")
        assert argv == ["/local/prerequisite", "$literal"]
        assert json.loads(kwargs["input"])["unchanged"] == 3
        names = sorted(row["model_name"] for row in config["model_list"] if row["model_info"]["f24_provider"] == "groq")
        assert json.loads(kwargs["env"]["IMPORT_MODEL_NAMES_JSON"]) == names
        return SimpleNamespace(returncode=command_exit)

    def notify(event, payload, env):
        order.append(event)
        assert payload["run_id"] is None
        if command_exit:
            assert payload["import_ok"] is True and payload["phase"] == "post_import"
        return {"sent": True}

    monkeypatch.setattr(importer, "import_api", imported)
    monkeypatch.setattr(importer.subprocess, "run", prerequisite)
    monkeypatch.setattr("hook_client.post_hook", notify)
    assert importer.main(["api", "--input", str(source), "--config", str(settings), "--env-file", str(settings)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["post_import"]["ok"] is (command_exit == 0)
    assert order == ["readback", "prerequisite", "import_failed" if command_exit else "import_succeeded"]


@pytest.fixture
def config():
    pairs = [('groq', 'allam-2-7b'), ('groq', 'openai/gpt-oss-120b'), ('groq', 'openai/gpt-oss-20b')]
    pairs.extend((provider, 'example/model') for provider in PROVIDERS if provider != 'groq')
    return {'model_list': [{
        'model_name': provider + '/' + upstream,
        'litellm_params': route_params(provider, upstream, 'base'),
        'model_info': {'id': model_id(provider, upstream, 'base'), 'managed_by': OWNER,
                       'f24_provider': provider, 'f24_upstream_id': upstream, 'f24_variant': 'base',
                       'source': CONFIG_URL, 'checked_at': '2026-09-29T17:33:47+00:00',
                       'access_groups': ['litellm-free'],
                       'f24_metadata': {'name': 'Example model', 'context_tokens': 131072,
                                        'reasoning': {'supported': True, 'effort_levels': ['low', 'high']},
                                        'sources': [{'url': 'https://example.com/model', 'label': 'Official model'}]}}
    } for provider, upstream in pairs]}


@pytest.fixture
def catalog(config):
    return {'schema_version': 1, 'catalog_updated_at': '2026-09-29T17:33:47+00:00',
            'checked_at': '2026-09-29T17:33:47+00:00', 'models': [{'name': 'Example model'}],
            'aggregators': {}, **deepcopy(config)}


@pytest.mark.parametrize('change', ['endpoint', 'key', 'model', 'owner', 'duplicate', 'extra'])
def test_download_cannot_redirect_keys_or_take_over_models(config, change):
    bad = deepcopy(config)
    row = bad['model_list'][0]
    if change == 'endpoint':
        row['litellm_params']['api_base'] = 'https://attacker.invalid'
    elif change == 'key':
        row['litellm_params']['api_key'] = 'os.environ/LITELLM_ADMIN_KEY'
    elif change == 'model':
        row['litellm_params']['model'] = 'changed'
    elif change == 'owner':
        row['model_info']['managed_by'] = 'free-sync'
    elif change == 'duplicate':
        bad['model_list'].append(deepcopy(row))
    else:
        row['litellm_params']['callback'] = 'evil.module'
    with pytest.raises(ValueError):
        validate_config(bad)


def test_file_refs_and_sql_secrets_are_separate(config, tmp_path):
    env = {'GROQ_API_KEY': 'private-test-value'}
    models, skipped = importer.select_models(config, env)
    assert skipped and all(m['model_info']['f24_provider'] == 'groq' for m in models)
    path = tmp_path / 'config.json'
    importer.write_config(path, models)
    assert 'private-test-value' not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    before = path.read_bytes()
    with pytest.raises(ValueError, match='Output exists'):
        importer.write_config(path, [])
    assert path.read_bytes() == before
    resolved, _ = importer.select_models(config, env, resolve=True)
    assert resolved[0]['litellm_params']['api_key'] == 'private-test-value'
    assert config['model_list'][0]['litellm_params']['api_key'].startswith('os.environ/')


def test_layered_env_no_interpolation(tmp_path, monkeypatch):
    a, b = tmp_path / 'a.env', tmp_path / 'b.env'
    a.write_text('GROQ_API_KEY=one\nNOUS_API_KEY=${GROQ_API_KEY}\n')
    b.write_text('GROQ_API_KEY=two\n')
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    assert importer.load_env([a, b])['GROQ_API_KEY'] == 'two'
    assert importer.load_env([a, b])['NOUS_API_KEY'] == '${GROQ_API_KEY}'
    monkeypatch.setenv('GROQ_API_KEY', 'process')
    assert importer.load_env([a, b])['GROQ_API_KEY'] == 'process'


def test_api_skip_foreign_update_owned_and_create(config, monkeypatch):
    models = deepcopy(config['model_list'][:3])
    existing = [deepcopy(models[0]), deepcopy(models[1])]
    existing[0]['model_info']['id'] = 'someone-elses-id'
    existing[0]['model_info']['managed_by'] = 'someone-else'
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == 'PATCH':
            updated = json.loads(request.content)
            existing[:] = [updated if row['model_info']['id'] == updated['model_info']['id'] else row for row in existing]
        elif request.method == 'POST':
            existing.append(json.loads(request.content))
        return httpx.Response(200, json={'data': existing} if request.method == 'GET' else {})
    real_client = httpx.Client
    monkeypatch.setattr(importer.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    env = {'LITELLM_BASE_URL': 'http://127.0.0.1', 'LITELLM_PORT': '4000', 'LITELLM_ADMIN_KEY': 'test'}
    result = importer.import_api(models, env)
    assert result == {'inserted': 1, 'updated': 1, 'adopted': 0, 'unchanged': 0, 'pruned': 0, 'skipped_existing': 1, 'dry_run': False}
    assert [r.method for r in requests] == ['GET', 'PATCH', 'POST', 'GET']
    assert all(r.url.port == 4000 for r in requests)
    requests.clear()
    importer.import_api(models, env, dry_run=True)
    assert [r.method for r in requests] == ['GET']


def test_api_diff_contains_only_owned_routing_rows(config, monkeypatch):
    current = deepcopy(config['model_list'][0])
    new = deepcopy(config['model_list'][1])
    foreign = deepcopy(config['model_list'][2])
    foreign['model_info']['managed_by'] = 'other-manager'
    rows = [current, foreign]
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == 'PATCH':
            body = json.loads(request.content)
            rows[:] = [body if row['model_info']['id'] == body['model_info']['id'] else row for row in rows]
        elif request.method == 'POST':
            rows.append(json.loads(request.content))
        return httpx.Response(200, json={'data': rows})

    real_client = httpx.Client
    monkeypatch.setattr(importer.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    env = {'LITELLM_BASE_URL': 'http://127.0.0.1', 'LITELLM_PORT': '4000', 'LITELLM_ADMIN_KEY': 'test'}
    result = importer.import_api([current, new], env, capture_diff=True)
    assert [row['model_name'] for row in result['diff']['added']] == [new['model_name']]
    assert result['diff']['added'][0]['provider'] == new['model_info']['f24_upstream_id']
    assert result['diff']['added'][0]['aggregator'] == new['model_info']['f24_provider']
    assert set(result['diff']['added'][0]) == {'model_name', 'provider', 'aggregator', 'variant'}


def test_sql_container_payload_uses_stdin_not_process_args(config, monkeypatch):
    models, _ = importer.select_models(config, {'GROQ_API_KEY': 'secret-test'}, resolve=True)
    def run(command, **kwargs):
        assert 'secret-test' not in '\n'.join(command)
        assert 'secret-test' in kwargs['input']
        payload = json.loads(kwargs['input'])
        assert payload['dry_run'] is True
        assert all('f24_metadata' not in row['model_info'] for row in payload['models'])
        return SimpleNamespace(returncode=0, stdout='{"dry_run":true}', stderr='')
    monkeypatch.setattr(importer.subprocess, 'run', run)
    assert importer.import_sql(models, {}, 'litellm-database', dry_run=True)['dry_run']


def test_complete_catalog_and_native_config_validate(config, catalog):
    assert validate_config(catalog) == config['model_list']
    plain = deepcopy(config)
    for row in plain['model_list']:
        del row['model_info']['f24_metadata']
    assert validate_config(plain)
    assert validate_config({**catalog, 'research': {'models': {'example/model': {'description': 'Full research data'}}}})
    for change in ({'schema_version': 2}, {'extra': {}}, {'models': 'invalid'}):
        with pytest.raises(ValueError):
            validate_config({**catalog, **change})


@pytest.mark.parametrize('metadata', [None, {'bad': float('nan')}, {'bad': float('inf')},
                                    {'bad': 'x' * 32769}, {'bad': [0] * 10001}])
def test_descriptive_metadata_is_bounded(config, metadata):
    config['model_list'][0]['model_info']['f24_metadata'] = metadata
    with pytest.raises(ValueError):
        validate_config(config)


def test_metadata_nesting_and_size_limits(config):
    value = {}
    for _ in range(14):
        value = {'nested': value}
    for metadata in (value, {str(n): 'x' * 30000 for n in range(5)}):
        config['model_list'][0]['model_info']['f24_metadata'] = metadata
        with pytest.raises(ValueError):
            validate_config(config)


def test_source_download_uses_default_catalog_and_sends_no_keys(catalog, monkeypatch):
    captured = []
    real_client = httpx.Client
    def handler(request):
        captured.append(request)
        assert 'authorization' not in request.headers
        assert 'local-provider-secret' not in str(request.url)
        return httpx.Response(200, json=catalog)
    monkeypatch.setattr(importer.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    assert importer.load_document(env={'GROQ_API_KEY': 'local-provider-secret'}) == catalog
    assert str(captured[0].url) == CATALOG_URL
    importer.load_document(env={'IMPORT_SOURCE_URL': 'http://127.0.0.2:18080/catalog.json'})
    assert captured[1].url.host == '127.0.0.2'
    importer.load_document(env={'IMPORT_SOURCE_URL': 'https://other.example/catalog.json'}, source_url='https://selected.example/catalog.json')
    assert captured[2].url.host == 'selected.example'


@pytest.mark.parametrize('source', ['http://remote.example/catalog.json', 'ftp://localhost/catalog.json',
                                  'https://user:secret@remote.example/catalog.json', 'https://remote.example/#fragment'])
def test_remote_source_restrictions(source):
    with pytest.raises(ValueError):
        importer.load_document(env={'IMPORT_SOURCE_URL': source})


def test_uds_source_is_local_and_uses_socket(catalog, monkeypatch):
    with pytest.raises(ValueError, match='localhost'):
        importer.load_document(env={'IMPORT_SOURCE_UDS': '/tmp/example.sock'})
    socket_options = []
    real_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=catalog))
    monkeypatch.setattr(importer.httpx, 'HTTPTransport', lambda **kw: socket_options.append(kw) or transport)
    assert importer.load_document(env={'IMPORT_SOURCE_URL': 'http://localhost/catalog.json', 'IMPORT_SOURCE_UDS': '/tmp/example.sock'}) == catalog
    assert socket_options[0]['uds'] == '/tmp/example.sock'


def test_pull_without_credentials_defaults_to_native_yaml(catalog, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(catalog))
    assert importer.main(['pull', '--input', str(source)]) == 0
    yaml_path = tmp_path / 'litellm-free.yaml'
    assert yaml_path.is_symlink() and yaml.safe_load(yaml_path.read_text()) == {'model_list': catalog['model_list']}
    assert importer.load_document(yaml_path)['model_list'] == catalog['model_list']
    assert yaml_path.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / 'catalog.json').exists()


def test_default_publication_replaces_only_owned_current_file(tmp_path):
    path = tmp_path / 'litellm-free.yaml'
    first = publish_file(path, 'first')
    second = publish_file(path, 'second', force=True)
    assert path.read_text() == 'second' and second.exists() and not first.exists()
    assert not (tmp_path / 'archiv').exists()
    # An arbitrary alias target is user-owned and must never be removed.
    external = tmp_path / 'custom.yaml'
    external.write_text('custom')
    path.unlink()
    path.symlink_to(external.name)
    publish_file(path, 'third', force=True)
    assert external.read_text() == 'custom'


def test_archive_links_retain_versions_and_migrate_regular_file(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('old regular file')
    with pytest.raises(ValueError, match='Output exists'):
        publish_file(path, 'new')
    first = publish_file(path, 'first', force=True, archive=True)
    assert path.is_symlink() and path.read_text() == 'first'
    second = publish_file(path, 'second', force=True, archive=True)
    assert path.read_text() == 'second'
    assert (tmp_path / 'archiv' / first.name).read_text() == 'first' and second.read_text() == 'second'
    assert any(item.read_text() == 'old regular file' for item in (tmp_path / 'archiv').iterdir())
    before = sorted((tmp_path / 'archiv').iterdir())
    assert publish_file(path, 'second', force=True, archive=True) == second.resolve()
    assert sorted((tmp_path / 'archiv').iterdir()) == before


def test_failed_link_switch_preserves_current_and_history(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    first = publish_file(path, 'first')
    monkeypatch.setattr('config_files.os.replace', lambda *args: (_ for _ in ()).throw(OSError('test failure')))
    with pytest.raises(OSError):
        publish_file(path, 'second', force=True)
    assert path.read_text() == 'first' and first.read_text() == 'first'


def test_file_yaml_and_config_settings(config, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(config))
    (tmp_path / 'config.conf').write_text('GROQ_API_KEY=test-only-key\n')
    assert importer.main(['file', '--input', str(source), '--format', 'yaml']) == 0
    result = yaml.safe_load((tmp_path / 'litellm-free.yaml').read_text())
    assert all(row['model_info']['f24_provider'] == 'groq' for row in result['model_list'])
    assert all(row['litellm_params']['api_key'] == 'os.environ/GROQ_API_KEY' for row in result['model_list'])


def test_metadata_patch_only_updates_info_and_is_idempotent(config, monkeypatch):
    model = deepcopy(config['model_list'][0])
    existing = {'model_name': model['model_name'], 'litellm_params': {'model': 'existing-routing', 'api_key': 'hidden-key'},
                'model_info': {'id': 'existing-private-id', 'managed_by': 'free-sync', 'custom': 'preserved'}}
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == 'PATCH':
            body = json.loads(request.content)
            assert set(body) == {'model_info'}
            assert body['model_info']['id'] == 'existing-private-id'
            assert body['model_info']['managed_by'] == 'free-sync'
            assert body['model_info']['custom'] == 'preserved'
            assert 'hidden-key' not in request.content.decode()
            existing['model_info'] = body['model_info']
            return httpx.Response(200, json={})
        return httpx.Response(200, json={'data': [existing]})
    real_client = httpx.Client
    monkeypatch.setattr(importer.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    env = {'LITELLM_BASE_URL': 'http://localhost', 'LITELLM_VIRTUAL_KEY': 'management-only-key'}
    result = importer.import_api([model], env, patch_managed_by='free-sync')
    assert result == {'patched': 1, 'unchanged': 0, 'skipped_existing': 0, 'dry_run': False}
    assert [request.method for request in requests] == ['GET', 'PATCH', 'GET']
    requests.clear()
    assert importer.import_api([model], env, patch_managed_by='free-sync')['unchanged'] == 1
    assert [request.method for request in requests] == ['GET']


def test_patch_requires_exact_owner_name_and_readback(config, monkeypatch):
    model = deepcopy(config['model_list'][0])
    row = {'model_name': model['model_name'], 'model_info': {'id': 'foreign-id', 'managed_by': 'other'}}
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'data': [row]})
    real_client = httpx.Client
    monkeypatch.setattr(importer.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    env = {'LITELLM_BASE_URL': 'http://localhost', 'LITELLM_ADMIN_KEY': 'test'}
    assert importer.import_api([model], env, patch_managed_by='free-sync')['skipped_existing'] == 1
    assert all(request.method == 'GET' for request in requests)
    row['model_info']['managed_by'] = 'free-sync'
    with pytest.raises(ValueError, match='readback'):
        importer.import_api([model], env, patch_managed_by='free-sync')


def test_patch_dry_run_needs_no_provider_keys_and_no_writes(config, tmp_path, monkeypatch):
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(config))
    monkeypatch.chdir(tmp_path)
    (tmp_path / '.env').write_text('LITELLM_BASE_URL=http://localhost\nLITELLM_ADMIN_KEY=test\nIMPORT_PATCH_MANAGED_BY=free-sync\n')
    received = []
    monkeypatch.setattr(importer, 'import_api', lambda models, env, dry_run, patch_managed_by, adopt_managed_by, prune: received.append((models, dry_run, patch_managed_by)) or {'patched': 0})
    assert importer.main(['api', '--input', str(source), '--dry-run']) == 0
    assert received[0][1:] == (True, 'free-sync')
    assert len(received[0][0]) == len(config['model_list'])
    assert all(row['litellm_params']['api_key'].startswith('os.environ/') for row in received[0][0])


def test_adoption_preserves_id_and_metadata_then_becomes_idempotent(config, monkeypatch):
    model = deepcopy(config['model_list'][0])
    model['litellm_params']['api_key'] = 'local-test-secret'
    info = model['model_info']
    row = {'model_name': model['model_name'], 'litellm_params': {'model': 'existing'},
        'model_info': {'id': 'previous-stable-id', 'managed_by': 'free-sync',
                          'free_sync_provider': info['f24_provider'], 'free_sync_model_id': info['f24_upstream_id'],
                          'free_sync_variant': info['f24_variant'], 'custom': 'preserve-me',
                          'f24_metadata': {'legacy': True}}}
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == 'PATCH':
            body = json.loads(request.content)
            assert body['model_info']['id'] == 'previous-stable-id'
            assert body['model_info']['managed_by'] == OWNER
            assert body['model_info']['custom'] == 'preserve-me'
            assert 'f24_metadata' not in body['model_info']
            assert body['litellm_params']['api_key'] == 'local-test-secret'
            row.update(body)
        return httpx.Response(200, json={'data': [row]})
    real_client = httpx.Client
    monkeypatch.setattr(importer.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    env = {'LITELLM_BASE_URL': 'http://localhost', 'LITELLM_ADMIN_KEY': 'test'}
    assert importer.import_api([model], env, adopt_managed_by='free-sync')['adopted'] == 1
    assert [request.method for request in requests] == ['GET', 'PATCH', 'GET']
    assert row['model_info']['id'] != model['model_info']['id']
    requests.clear()
    assert importer.import_api([model], env)['unchanged'] == 1
    assert [request.method for request in requests] == ['GET']
    requests.clear()
    model['model_info']['f24_metadata']['context_tokens'] = 262144
    assert importer.import_api([model], env)['unchanged'] == 1
    assert row['model_info']['id'] == 'previous-stable-id'
    assert 'f24_metadata' not in row['model_info']


@pytest.mark.parametrize('change', ['provider', 'upstream', 'variant', 'ambiguous'])
def test_adoption_rejects_identity_mismatch_or_ambiguity(config, monkeypatch, change):
    model = deepcopy(config['model_list'][0])
    info = model['model_info']
    row = {'model_name': model['model_name'], 'model_info': {'id': 'old-id', 'managed_by': 'free-sync',
           'free_sync_provider': info['f24_provider'], 'free_sync_model_id': info['f24_upstream_id'],
           'free_sync_variant': info['f24_variant']}}
    if change != 'ambiguous':
        key = {'provider': 'free_sync_provider', 'upstream': 'free_sync_model_id', 'variant': 'free_sync_variant'}[change]
        row['model_info'][key] = 'different'
    rows = [row, deepcopy(row)] if change == 'ambiguous' else [row]
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'data': rows})
    real_client = httpx.Client
    monkeypatch.setattr(importer.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(ValueError):
        importer.import_api([model], {'LITELLM_BASE_URL': 'http://localhost', 'LITELLM_ADMIN_KEY': 'test'}, adopt_managed_by='free-sync')
    assert [request.method for request in requests] == ['GET']


def test_adoption_and_metadata_patch_are_mutually_exclusive(config):
    with pytest.raises(ValueError, match='exclusive'):
        importer.import_api(config['model_list'], {}, patch_managed_by='free-sync', adopt_managed_by='free-sync')


def test_core_network_http_requires_explicit_opt_in(config, monkeypatch):
    env = {'LITELLM_BASE_URL': 'http://litellm-database:4000', 'LITELLM_ADMIN_KEY': 'test'}
    with pytest.raises(ValueError, match='LITELLM_ALLOW_HTTP'):
        importer.import_api(config['model_list'], env, dry_run=True)
    real_client = httpx.Client
    monkeypatch.setattr(importer.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'data': []})), **kw))
    assert importer.import_api(config['model_list'], {**env, 'LITELLM_ALLOW_HTTP': 'true'}, dry_run=True)['inserted'] == len(config['model_list'])


def test_online_delay_is_configurable_and_dry_run_waits_none(catalog, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'config.conf').write_text('IMPORT_DELAY_SECONDS=7\n')
    monkeypatch.setattr(importer, 'load_document', lambda *args: catalog)
    delays = []
    monkeypatch.setattr(importer.time, 'sleep', delays.append)
    assert importer.main(['pull', '--dry-run']) == 0
    assert not delays
    assert importer.main(['pull']) == 0
    assert delays == [7]
    assert importer.main(['pull', '--force', '--delay-seconds', '0']) == 0
    assert delays == [7]


def test_cli_header_loads_defaults_but_explicit_env_stays_isolated(config, tmp_path):
    for name in ('import_litellm.py', 'litellm_export.py', 'config_files.py', 'python_header.py'):
        shutil.copyfile(ROOT / name, tmp_path / name)
    (tmp_path / 'config.conf_example').write_text('IMPORT_DELAY_SECONDS=0\n')
    (tmp_path / 'env.example').write_text('GROQ_API_KEY=default-example-only\n')
    selected = tmp_path / 'selected.credentials'
    selected.write_text('OPENROUTER_API_KEY=selected-example-only\n')
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(config))
    environment = {'PATH': os.environ['PATH']}
    command = [sys.executable, str(tmp_path / 'import_litellm.py'), 'file', '--input', str(source), '--dry-run']
    default = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True, check=True)
    assert json.loads(default.stdout)['models'] == 3
    explicit = subprocess.run(command + ['--env-file', str(selected)], cwd=tmp_path, env=environment,
                              capture_output=True, text=True, check=True)
    assert json.loads(explicit.stdout)['models'] == 1
    process = subprocess.run(command + ['--env-file', str(selected)], cwd=tmp_path,
                             env={**environment, 'GROQ_API_KEY': 'injected-example-only'},
                             capture_output=True, text=True, check=True)
    assert json.loads(process.stdout)['models'] == 4
    library = subprocess.run([sys.executable, '-c', "import sys; import import_litellm; assert 'python_header' not in sys.modules"],
                             cwd=tmp_path, env=environment, capture_output=True, text=True, check=True)
    assert not library.stdout


def test_prune_removes_only_obsolete_owned_routes_after_readback(config, monkeypatch):
    current = deepcopy(config['model_list'][0])
    stale = deepcopy(config['model_list'][1])
    foreign = deepcopy(config['model_list'][2])
    foreign['model_info']['managed_by'] = 'free-sync'
    without_key = deepcopy(config['model_list'][3])
    wrong_source = deepcopy(stale)
    wrong_source['model_name'] = 'groq/example/source-guard'
    wrong_source['model_info'].update(id='source-guard', f24_upstream_id='example/source-guard', source='https://other.example/')
    wrong_group = deepcopy(stale)
    wrong_group['model_name'] = 'groq/example/group-guard'
    wrong_group['model_info'].update(id='group-guard', f24_upstream_id='example/group-guard', access_groups=['other'])
    rows = [current, stale, foreign, without_key, wrong_source, wrong_group]
    requests = []
    def handler(request):
        requests.append(request)
        body = json.loads(request.content) if request.content else {}
        if request.method == 'PATCH':
            rows[:] = [body if row['model_info']['id'] == body['model_info']['id'] else row for row in rows]
        elif request.method == 'POST':
            assert request.url.path == '/model/delete'
            assert body == {'id': stale['model_info']['id']}
            rows[:] = [row for row in rows if row['model_info']['id'] != body['id']]
        return httpx.Response(200, json={'data': rows})
    real_client = httpx.Client
    monkeypatch.setattr(importer.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    env = {'LITELLM_BASE_URL': 'http://localhost', 'LITELLM_ADMIN_KEY': 'test', 'GROQ_API_KEY': 'test-only'}
    result = importer.import_api([current], env, prune=True)
    assert result['updated'] == 1 and result['pruned'] == 1
    assert [request.method for request in requests] == ['GET', 'PATCH', 'GET', 'POST', 'GET']
    assert len(rows) == 5 and foreign in rows and without_key in rows and wrong_source in rows and wrong_group in rows
    requests.clear()
    assert importer.import_api([current], env, prune=True)['pruned'] == 0
    assert [request.method for request in requests] == ['GET']


def test_prune_never_runs_after_failed_upsert_readback(config, monkeypatch):
    current, stale = deepcopy(config['model_list'][:2])
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'data': [current, stale]})
    real_client = httpx.Client
    monkeypatch.setattr(importer.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    env = {'LITELLM_BASE_URL': 'http://localhost', 'LITELLM_ADMIN_KEY': 'test', 'GROQ_API_KEY': 'test-only'}
    with pytest.raises(ValueError, match='readback'):
        importer.import_api([current], env, prune=True)
    assert [request.method for request in requests] == ['GET', 'PATCH', 'GET']
    with pytest.raises(ValueError, match='nonempty'):
        importer.import_api([], env, prune=True)


def test_prune_dry_run_keeps_routes_and_metadata_mode_cannot_prune(config, monkeypatch):
    current, stale = deepcopy(config['model_list'][:2])
    requests = []
    real_client = httpx.Client
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'data': [current, stale]})
    monkeypatch.setattr(importer.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    env = {'LITELLM_BASE_URL': 'http://localhost', 'LITELLM_ADMIN_KEY': 'test', 'GROQ_API_KEY': 'test-only'}
    assert importer.import_api([current], env, prune=True, dry_run=True)['pruned'] == 1
    assert [request.method for request in requests] == ['GET']
    with pytest.raises(ValueError):
        importer.import_api([current], env, prune=True, patch_managed_by='free-sync')


def test_downloadable_templates_match_generic_sot():
    assert env_template() == (ROOT / 'env.example').read_text()
    assert config_template() == (ROOT / 'config.conf_example').read_text()
    assert (ROOT / '.env.example').read_text() == env_template()
    assert (ROOT / 'import.env.example').read_text() == env_template()
    assert 'LITELLM_BASE_URL=' not in env_template()
    assert 'LITELLM_CA_FILE=' not in env_template()
    assert 'LITELLM_BASE_URL=' in config_template()


def test_prune_adopted_legacy_routes_matches_yaml_and_is_idempotent(config, monkeypatch):
    current, stale = deepcopy(config['model_list'][:2])
    current = importer.routing_model(current)
    model = deepcopy(current)
    current['model_info']['f24_import_hash'] = importer.source_hash(current)
    info = stale['model_info']
    legacy = {'model_name': stale['model_name'], 'model_info': {
        'id': 'legacy-stale', 'managed_by': 'free-sync', 'access_groups': ['litellm-free'],
        'free_sync_provider': info['f24_provider'], 'free_sync_model_id': info['f24_upstream_id'],
        'free_sync_variant': info['f24_variant']}}
    guards = []
    for field, value in [('managed_by', 'other-manager'), ('access_groups', ['other']),
                         ('access_groups', ['litellm-free', 'other']), ('free_sync_provider', 'unknown'),
                         ('free_sync_variant', 'invalid')]:
        guarded = deepcopy(legacy)
        guarded['model_info'].update({field: value, 'id': 'guard-' + str(len(guards))})
        guards.append(guarded)
    mismatched = deepcopy(legacy)
    mismatched['model_info']['id'] = 'guard-name'
    mismatched['model_name'] = 'opencode/not-this-route'
    guards.append(mismatched)
    rows, requests = [current, legacy, *guards], []

    def handler(request):
        requests.append(request)
        if request.method == 'POST':
            assert request.url.path == '/model/delete'
            assert json.loads(request.content) == {'id': 'legacy-stale'}
            rows.remove(legacy)
        return httpx.Response(200, json={'data': rows})

    real = httpx.Client
    monkeypatch.setattr(importer.httpx, 'Client', lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    env = {'LITELLM_BASE_URL': 'http://localhost', 'LITELLM_ADMIN_KEY': 'test', 'GROQ_API_KEY': 'test'}
    assert importer.import_api([model], env, prune=True, adopt_managed_by='free-sync')['pruned'] == 1
    assert [r.method for r in requests] == ['GET', 'POST', 'GET']
    assert all(row in rows for row in guards)
    requests.clear()
    assert importer.import_api([model], env, prune=True, adopt_managed_by='free-sync')['pruned'] == 0
    assert [r.method for r in requests] == ['GET']


def test_legacy_prune_dry_run_requires_explicit_adoption(config, monkeypatch):
    current, stale = deepcopy(config['model_list'][:2])
    info = stale['model_info']
    stale['model_info'] = {'id': 'legacy', 'managed_by': 'free-sync', 'access_groups': ['litellm-free'],
                           'free_sync_provider': info['f24_provider'], 'free_sync_model_id': info['f24_upstream_id'],
                           'free_sync_variant': info['f24_variant']}
    requests = []
    real = httpx.Client

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'data': [current, stale]})

    monkeypatch.setattr(importer.httpx, 'Client', lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    env = {'LITELLM_BASE_URL': 'http://localhost', 'LITELLM_ADMIN_KEY': 'test', 'GROQ_API_KEY': 'test'}
    assert importer.import_api([current], env, prune=True, dry_run=True)['pruned'] == 0
    assert importer.import_api([current], env, prune=True, dry_run=True, adopt_managed_by='free-sync')['pruned'] == 1
    assert all(request.method == 'GET' for request in requests)


def test_prune_readback_failure_is_not_a_success(config, monkeypatch):
    current, stale = deepcopy(config['model_list'][:2])
    current = importer.routing_model(current)
    model = deepcopy(current)
    current['model_info']['f24_import_hash'] = importer.source_hash(current)
    real = httpx.Client
    monkeypatch.setattr(importer.httpx, 'Client', lambda **kw: real(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'data': [current, stale]})), **kw))
    env = {'LITELLM_BASE_URL': 'http://localhost', 'LITELLM_ADMIN_KEY': 'test', 'GROQ_API_KEY': 'test'}
    with pytest.raises(ValueError, match='removed route'):
        importer.import_api([model], env, prune=True)


def test_unchanged_source_hash_does_not_hide_live_routing_drift(config, monkeypatch):
    model = importer.routing_model(config['model_list'][0])
    row = deepcopy(model)
    row['model_info']['f24_import_hash'] = importer.source_hash(model)
    row['litellm_params']['api_base'] = 'https://wrong.example'
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == 'PATCH':
            row.update(json.loads(request.content))
        return httpx.Response(200, json={'data': [row]})

    real = httpx.Client
    monkeypatch.setattr(importer.httpx, 'Client', lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    env = {'LITELLM_BASE_URL': 'http://localhost', 'LITELLM_ADMIN_KEY': 'test'}
    assert importer.import_api([model], env)['updated'] == 1
    requests.clear()
    assert importer.import_api([model], env)['unchanged'] == 1
    assert [r.method for r in requests] == ['GET']


def test_exact_reconciliation_refuses_foreign_name_without_deleting(config, monkeypatch):
    model = deepcopy(config['model_list'][0])
    foreign = deepcopy(model)
    foreign['model_info'].update(id='foreign', managed_by='other-manager')
    requests = []
    real = httpx.Client

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'data': [foreign]})

    monkeypatch.setattr(importer.httpx, 'Client', lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(ValueError, match='exact reconciliation'):
        importer.import_api([model], {'LITELLM_BASE_URL': 'http://localhost', 'LITELLM_ADMIN_KEY': 'test'}, prune=True)
    assert [r.method for r in requests] == ['GET']
