from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from catalog_store import CatalogStore
import import_litellm as importer
from litellm_export import CONFIG_URL, OWNER, env_template, export_config, validate_config
import review_models as review
from web import create_app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def snapshot(tmp_path):
    (tmp_path / 'model_probe_results.json').write_bytes((ROOT / 'examples/model_probe_results.json').read_bytes())
    (tmp_path / 'model_metadata.json').write_bytes((ROOT / 'model_metadata.json').read_bytes())
    return tmp_path


@pytest.fixture
def config(snapshot):
    return export_config(CatalogStore(snapshot, source=snapshot / 'model_probe_results.json').refresh())


def test_download_is_native_config_without_non_chat_or_secrets(snapshot):
    with TestClient(create_app(snapshot)) as client:
        response = client.get('/litellm-config.json')
        assert response.status_code == 200
        assert 'attachment' in response.headers['content-disposition']
        assert response.headers['cache-control'] == 'no-store'
        rows = validate_config(response.json())
        assert {r['model_info']['f24_provider'] for r in rows} == {'groq', 'openrouter', 'kilo', 'nous', 'opencode', 'nvidia'}
        for row in rows:
            assert row['litellm_params']['api_key'].startswith('os.environ/')
            assert not any(term in row['model_name'].lower() for term in ['lyria', 'content-safety', 'jev-', 'auto:free', 'free-models-router'])
        template = client.get('/litellm.env.example')
        assert template.status_code == 200 and template.text == env_template()
        page = client.get('/').text
        assert 'github-button' in page and '/static/logos/github.svg' in page
        assert CONFIG_URL in page


def test_unavailable_download_never_returns_empty_config(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/litellm-config.json').status_code == 503


def test_unreviewed_routes_are_not_exported(snapshot):
    meta = json.loads((snapshot / 'model_metadata.json').read_text())
    for value in meta['models'].values():
        value['research_status'] = 'pending'
    (snapshot / 'model_metadata.json').write_text(json.dumps(meta))
    with pytest.raises(ValueError, match='No passing'):
        export_config(CatalogStore(snapshot, source=snapshot / 'model_probe_results.json').refresh())


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
        return httpx.Response(200, json={'data': existing} if request.method == 'GET' else {})
    real_client = httpx.Client
    monkeypatch.setattr(importer.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    env = {'LITELLM_BASE_URL': 'http://127.0.0.1', 'LITELLM_PORT': '4000', 'LITELLM_ADMIN_KEY': 'test'}
    result = importer.import_api(models, env)
    assert result == {'inserted': 1, 'updated': 1, 'skipped_existing': 1, 'dry_run': False}
    assert [r.method for r in requests] == ['GET', 'PATCH', 'POST']
    assert all(r.url.port == 4000 for r in requests)
    requests.clear()
    importer.import_api(models, env, dry_run=True)
    assert [r.method for r in requests] == ['GET']


def test_sql_container_payload_uses_stdin_not_process_args(config, monkeypatch):
    models, _ = importer.select_models(config, {'GROQ_API_KEY': 'secret-test'}, resolve=True)
    def run(command, **kwargs):
        assert 'secret-test' not in '\n'.join(command)
        assert 'secret-test' in kwargs['input']
        assert json.loads(kwargs['input'])['dry_run'] is True
        return SimpleNamespace(returncode=0, stdout='{"dry_run":true}', stderr='')
    monkeypatch.setattr(importer.subprocess, 'run', run)
    assert importer.import_sql(models, {}, 'litellm-database', dry_run=True)['dry_run']


def review_fixture(tmp_path):
    (tmp_path / 'model_metadata.json').write_text(json.dumps({'schema_version': 1, 'models': {}, 'aggregators': {}}))
    document = {'generated_at': '2026-09-29T08:00:00Z', 'models': {
        'route': {'aggregator': 'groq', 'model': 'groq/creator/new', 'status': 'green',
                  'catalog': {'upstream_id': 'creator/new'}}}}
    proposal = {'models': {'groq|creator/new': {'name': 'New', 'display_name': 'New', 'creator': 'Creator',
                    'entry_type': 'model', 'description': 'A researched model.', 'duplicate_key': 'creator/new',
                    'research_status': 'reviewed', 'researched_at': '2026-09-29',
                    'sources': [{'label': 'Official model card', 'url': 'https://example.com/model'}]}}}
    return document, proposal


def test_generic_agent_applies_only_pending_and_does_not_receive_provider_keys(tmp_path, monkeypatch):
    document, proposal = review_fixture(tmp_path)
    monkeypatch.setenv('GROQ_API_KEY', 'never-forward')
    monkeypatch.setenv('DATABASE_URL', 'never-forward')
    def run(command, **kwargs):
        assert command[0] == '/test/research-agent'
        assert 'GROQ_API_KEY' not in kwargs['env'] and 'DATABASE_URL' not in kwargs['env']
        assert 'never-forward' not in kwargs['input']
        return SimpleNamespace(returncode=0, stdout=json.dumps(proposal), stderr='')
    monkeypatch.setattr(review.subprocess, 'run', run)
    result = review.run_review(tmp_path, document, {'MODEL_REVIEW_COMMAND': '/test/research-agent {input}'})
    assert result['updated'] == 1
    assert (tmp_path / result['job'] / 'metadata.before.json').is_file()
    assert review.run_review(tmp_path, document, {}) == {'status': 'up_to_date', 'pending': 0}


def test_unconfigured_agent_queues_without_mutating_metadata(tmp_path):
    document, _ = review_fixture(tmp_path)
    before = (tmp_path / 'model_metadata.json').read_bytes()
    result = review.run_review(tmp_path, document, {})
    assert result['status'] == 'queued'
    assert (tmp_path / result['job'] / 'TASK.md').is_file()
    assert (tmp_path / 'model_metadata.json').read_bytes() == before


@pytest.mark.parametrize('invalid', ['foreign', 'source', 'weights', 'merge', 'field', 'type'])
def test_invalid_agent_proposal_never_overwrites_metadata(tmp_path, invalid):
    document, proposal = review_fixture(tmp_path)
    job = review.prepare_review(tmp_path, document)
    item = proposal['models']['groq|creator/new']
    if invalid == 'foreign':
        proposal['models']['foreign|id'] = deepcopy(item)
    elif invalid == 'source':
        item['sources'][0]['url'] = 'javascript:alert(1)'
    elif invalid == 'weights':
        item['open_weights'] = True
    elif invalid == 'merge':
        request = json.loads((job / 'input.json').read_text())
        request['pending']['groq|creator/other'] = deepcopy(request['pending']['groq|creator/new'])
        (job / 'input.json').write_text(json.dumps(request))
        proposal['models']['groq|creator/other'] = deepcopy(item)
    elif invalid == 'field':
        item['api_key'] = 'forbidden'
    else:
        item['entry_type'] = 'unknown'
    (job / 'proposal.json').write_text(json.dumps(proposal))
    before = (tmp_path / 'model_metadata.json').read_bytes()
    with pytest.raises(ValueError):
        review.apply_review(tmp_path, job)
    assert (tmp_path / 'model_metadata.json').read_bytes() == before


def test_concurrent_metadata_edits_abort_agent_apply(tmp_path):
    document, proposal = review_fixture(tmp_path)
    job = review.prepare_review(tmp_path, document)
    (job / 'proposal.json').write_text(json.dumps(proposal))
    path = tmp_path / 'model_metadata.json'
    path.write_text(path.read_text() + '\n')
    with pytest.raises(ValueError, match='changed during research'):
        review.apply_review(tmp_path, job)
