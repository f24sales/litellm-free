import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import hook_client


@pytest.mark.parametrize("codes, expected_attempts", [([503, 200], 2), ([401], 1), ([503, 503, 503], 3)])
def test_delivery_retries_only_transient_errors_with_same_event(codes, expected_attempts, monkeypatch):
    bodies = []

    def run(argv, **kwargs):
        assert "test-bearer" not in str(argv)
        curl_config = Path(argv[argv.index("--config") + 1])
        bodies.append(json.loads(curl_config.with_name("payload.json").read_text()))
        status = codes[len(bodies) - 1]
        return SimpleNamespace(returncode=0 if status == 200 else 22, stdout=str(status), stderr="")

    monkeypatch.setattr(hook_client.subprocess, "run", run)
    monkeypatch.setattr(hook_client.time, "sleep", lambda _: None)
    result = hook_client.post_hook("import_succeeded", {"run_id": "scan-1"}, {
        "IMPORT_HOOK_URL": "https://localhost/example", "IMPORT_HOOK_BEARER": "test-bearer",
    })
    assert result["attempts"] == expected_attempts
    assert result["sent"] is (codes[-1] == 200)
    assert all(body == bodies[0] for body in bodies)
    assert bodies[0]["event_id"] and bodies[0]["run_id"] == "scan-1"
    assert "test-bearer" not in json.dumps(bodies)


def test_missing_credentials_do_not_start_curl(monkeypatch):
    monkeypatch.setattr(hook_client.subprocess, "run", lambda *a, **kw: pytest.fail("curl started"))
    assert hook_client.post_hook("import_succeeded", {}, {}) is None
