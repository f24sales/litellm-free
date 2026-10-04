from copy import deepcopy
import json

import pytest

import import_litellm as importer
from litellm_export import CONFIG_URL, OWNER, model_config_fingerprint, model_id, route_params


def row(upstream='openai/gpt-oss-20b', variant='base'):
    provider = 'groq'
    return {
        'model_name': provider + '/' + upstream + ('-' + variant if variant != 'base' else ''),
        'litellm_params': route_params(provider, upstream, variant),
        'model_info': {
            'id': model_id(provider, upstream, variant), 'managed_by': OWNER,
            'f24_provider': provider, 'f24_upstream_id': upstream, 'f24_variant': variant,
            'source': CONFIG_URL, 'checked_at': '2026-09-29T12:00:00Z',
            'access_groups': ['litellm-free'],
            'f24_metadata': {
                'model': {'name': 'GPT-OSS 20B', 'creator': 'OpenAI', 'open_weights': True,
                          'researched_at': '2026-09-29'},
                'route': {'context_tokens': 131072, 'reasoning': {'supported': True},
                          'probe_status': 'green', 'probe_assessment': 'Passing answer'},
            },
        },
    }


@pytest.fixture
def config():
    return {'model_list': [row(), row('openai/gpt-oss-120b')]}


def test_scan_dates_and_probe_diagnostics_do_not_change_model_fingerprint(config):
    changed = deepcopy(config)
    info = changed['model_list'][0]['model_info']
    info['checked_at'] = '2026-09-30T00:00:00Z'
    facts = info['f24_metadata']
    for field in ['created_at', 'updated_at', 'researched_at', 'identity_reviewed_at', 'open_weights_checked_at']:
        facts['model'][field] = '2026-09-30'
    facts['route'].update(probe_status='green', probe_assessment='Different passing answer',
                          probe_latency_ms=4000, probe_checked_at='2026-09-30T00:00:00Z')
    facts['route']['reasoning']['probe_internal_diagnostic'] = {'arbitrary': 'diagnostic data'}
    assert model_config_fingerprint(changed) == model_config_fingerprint(config)


def test_row_and_object_key_order_are_irrelevant(config):
    original = deepcopy(config)
    changed = {'model_list': list(reversed(config['model_list']))}
    changed['model_list'] = [{key: item[key] for key in reversed(item)} for item in changed['model_list']]
    assert model_config_fingerprint(changed) == model_config_fingerprint(config)
    assert config == original
    assert len(model_config_fingerprint(config)) == 64


@pytest.mark.parametrize('change', ['add', 'remove', 'preset', 'context', 'thinking', 'creator', 'source'])
def test_route_membership_parameters_and_curated_facts_change_fingerprint(config, change):
    altered = deepcopy(config)
    if change == 'add':
        altered['model_list'].append(row('openai/gpt-oss-20b', 'think'))
    elif change == 'remove':
        altered['model_list'].pop()
    elif change == 'preset':
        altered['model_list'][0] = row('openai/gpt-oss-20b', 'fast')
        assert altered['model_list'][0]['litellm_params'] != config['model_list'][0]['litellm_params']
    else:
        facts = altered['model_list'][0]['model_info']['f24_metadata']
        if change == 'context':
            facts['route']['context_tokens'] = 262144
        elif change == 'thinking':
            facts['route']['reasoning']['supported'] = False
        elif change == 'creator':
            facts['model']['creator'] = None
        else:
            facts['model']['sources'] = [{'label': 'New supported evidence', 'url': 'https://example.org/model'}]
    assert model_config_fingerprint(altered) != model_config_fingerprint(config)


def test_fingerprint_rejects_resolved_bearers(config):
    config['model_list'][0]['litellm_params']['api_key'] = 'a-test-secret-bearer'
    with pytest.raises(ValueError):
        model_config_fingerprint(config)


def test_published_catalog_wrapper_hashes_only_its_importable_model_list(config):
    catalog = {
        'schema_version': 1, 'catalog_updated_at': '2026-09-29T12:00:00Z',
        'checked_at': '2026-09-29T12:00:00Z', 'models': [], 'aggregators': {},
        'model_list': config['model_list'], 'research': {'unrelated_catalog_fact': 'not imported'},
    }
    assert model_config_fingerprint(catalog) == model_config_fingerprint(config)


def test_api_receipt_hashes_the_exact_original_http_document_before_key_resolution(config, monkeypatch, capsys):
    document = deepcopy(config)
    received = []
    monkeypatch.setattr(importer, 'load_env', lambda *args, **kwargs: {'GROQ_API_KEY': 'fake-provider-bearer'})
    monkeypatch.setattr(importer, 'load_document', lambda *args, **kwargs: document)
    monkeypatch.setattr(importer.time, 'sleep', lambda *args: None)

    def imported(models, env, dry_run, **kwargs):
        assert models[0]['litellm_params']['api_key'] == 'fake-provider-bearer'
        assert document['model_list'][0]['litellm_params']['api_key'] == 'os.environ/GROQ_API_KEY'
        received.append(models)
        return {'inserted': 2, 'updated': 0, 'dry_run': dry_run}

    monkeypatch.setattr(importer, 'import_api', imported)
    assert importer.main(['api', '--env-file', '/unused-test.env']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['ok'] is True and status['mode'] == 'api'
    assert status['fingerprint'] == model_config_fingerprint(config)
    assert 'fake-provider-bearer' not in json.dumps(status)
    assert len(received) == 1


def test_failed_api_does_not_emit_a_success_fingerprint(config, monkeypatch, capsys):
    monkeypatch.setattr(importer, 'load_env', lambda *args, **kwargs: {'GROQ_API_KEY': 'fake-provider-bearer'})
    monkeypatch.setattr(importer, 'load_document', lambda *args, **kwargs: config)
    monkeypatch.setattr(importer.time, 'sleep', lambda *args: None)
    def fail(*args, **kwargs):
        raise ValueError('test import failed')
    monkeypatch.setattr(importer, 'import_api', fail)
    assert importer.main(['api', '--env-file', '/unused-test.env']) == 1
    status = json.loads(capsys.readouterr().out)
    assert status['ok'] is False and 'fingerprint' not in status


@pytest.mark.parametrize('mode', ['pull', 'file', 'sql'])
def test_other_modes_keep_their_status_without_fingerprint(config, tmp_path, monkeypatch, capsys, mode):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(importer, 'load_env', lambda *args, **kwargs: {'GROQ_API_KEY': 'fake-provider-bearer'})
    monkeypatch.setattr(importer, 'load_document', lambda *args, **kwargs: config)
    monkeypatch.setattr(importer, 'import_sql', lambda *args, **kwargs: {'inserted': 2})
    monkeypatch.setattr(importer.time, 'sleep', lambda *args: None)
    assert importer.main([mode, '--dry-run', '--env-file', '/unused-test.env']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['ok'] is True and 'fingerprint' not in status
