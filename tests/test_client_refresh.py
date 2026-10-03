"""Model-only refresh contract: all clients, removals, retries and preservation."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

spec = importlib.util.spec_from_file_location("refresh_models", Path(__file__).resolve().parents[1] / "ops/refresh_models.py")
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)

CATALOG = {"id": "litellm-free", "base_url": "https://proxy.test:2001/v1",
           "key_env": "OPENAI_V1_KEY_3", "models": ["keep", "new"],
           "openclaw_models": [{"id": "keep", "name": "keep"}, {"id": "new", "name": "new"}]}


def configs():
    return {
        "openclaw": {"models": {"providers": {"litellm-free": {
            "baseUrl": CATALOG["base_url"], "apiKey": {"source": "env", "id": "OPENAI_V1_KEY_3"},
            "models": [{"id": "keep", "contextWindow": 8192}, {"id": "old"}]}}},
            "agents": {"defaults": {"model": {"primary": "litellm-free/keep"},
                       "models": {"litellm-free/keep": {"alias": "preferred"}, "litellm-free/old": {}, "other/model": {}}}},
            "plugins": {"entries": {"voice": {"enabled": True}}}},
        "hermes": {"providers": {"litellm-free": {
            "base_url": CATALOG["base_url"], "key_env": "OPENAI_V1_KEY_3",
            "models": {"keep": {"context_length": 8192}, "old": {}}}},
            "model": {"provider": "litellm-free", "default": "keep", "available": ["old"]},
            "tts": {"provider": "azure-speech"}, "stt": {"provider": "mai-transcribe"},
            "plugins": {"enabled": ["safrano-voice"]}, "mcp_servers": {"existing": {}}},
        "opencode": {"provider": {"litellm-free": {
            "options": {"baseURL": CATALOG["base_url"], "apiKey": "{env:OPENAI_V1_KEY_3}"},
            "models": {"keep": {"limit": {"context": 8192}}, "old": {}}}},
            "model": "other/native", "mcp": {"existing": {}}, "agent": {"custom": {}}},
    }


class RefreshTests(unittest.TestCase):
    def setUp(self):
        native = patch.object(refresh, "verify_hermes")
        self.hermes_verifier = native.start()
        self.addCleanup(native.stop)

    def test_add_remove_idempotence_and_unrelated_settings(self):
        for kind, original in configs().items():
            before = deepcopy(original)
            after = refresh.update_catalog(kind, original, CATALOG)
            self.assertEqual(original, before)
            block = after["models"]["providers"]["litellm-free"] if kind == "openclaw" else after[
                "providers" if kind == "hermes" else "provider"]["litellm-free"]
            actual = {r["id"] for r in block["models"]} if kind == "openclaw" else set(block["models"])
            self.assertEqual(actual, {"keep", "new"})
            self.assertEqual(refresh.update_catalog(kind, after, CATALOG), after)
            for field in ("plugins", "tts", "stt", "mcp", "mcp_servers", "agent"):
                self.assertEqual(after.get(field), before.get(field))
            if kind == "openclaw":
                self.assertEqual(block["models"][0]["contextWindow"], 8192)
                self.assertNotIn("litellm-free/old", after["agents"]["defaults"]["models"])
                self.assertEqual(after["agents"]["defaults"]["model"], before["agents"]["defaults"]["model"])
            elif kind == "hermes":
                self.assertEqual(after["model"]["available"], ["keep", "new"])
                self.assertEqual(after["model"]["default"], "keep")

    def test_empty_or_mismatched_catalog_is_rejected(self):
        for kind, original in configs().items():
            for change in ({"models": []}, {"base_url": "https://wrong.test/v1"}, {"id": "missing"}):
                with self.assertRaises(refresh.RefreshError):
                    refresh.update_catalog(kind, original, {**CATALOG, **change})

    def test_same_endpoint_other_credentials_are_untouched(self):
        original = configs()["opencode"]
        original["provider"]["litellm"] = deepcopy(original["provider"]["litellm-free"])
        original["provider"]["litellm"]["options"]["apiKey"] = "{env:OPENAI_V1_KEY}"
        after = refresh.update_catalog("opencode", original, CATALOG)
        self.assertEqual(after["provider"]["litellm"], original["provider"]["litellm"])

    def setup_files(self, root):
        env = {"HOME": str(root)}
        for kind, path in refresh.paths(env).items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(configs()[kind]) if kind == "hermes" else json.dumps(configs()[kind]))
            path.chmod(0o600)
        return env

    def test_all_files_validated_before_any_write(self):
        with tempfile.TemporaryDirectory() as raw:
            env = self.setup_files(Path(raw))
            paths = refresh.paths(env)
            before = paths["openclaw"].read_bytes()
            paths["opencode"].write_text('{"provider": {}}')
            with self.assertRaises(refresh.RefreshError):
                refresh.plan_configs(env, CATALOG)
            self.assertEqual(paths["openclaw"].read_bytes(), before)

    def test_concurrent_writer_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as raw:
            env = self.setup_files(Path(raw))
            plans, _ = refresh.plan_configs(env, CATALOG)
            plans[-1][0].write_text("newer data")
            with self.assertRaisesRegex(refresh.RefreshError, "changed"):
                refresh.commit_plans(plans)
            self.assertEqual(plans[0][0].read_bytes(), plans[0][1])

    def test_refresh_reloads_then_identical_refresh_does_not_restart(self):
        with tempfile.TemporaryDirectory() as raw:
            env = self.setup_files(Path(raw))
            status = Path(raw) / "status.json"
            with patch.object(refresh, "discover", return_value=CATALOG), \
                 patch.object(refresh, "running", return_value=True), \
                 patch.object(refresh, "verify_opencode") as verify, \
                 patch.object(refresh, "command") as command:
                result = refresh.refresh(env, status)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(command.call_count, 2)
                verify.assert_called_once()
                for path in refresh.paths(env).values():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                command.reset_mock()
                result = refresh.refresh(env, status)
                self.assertEqual(result["status"], "ok")
                command.assert_not_called()
                self.assertTrue(all(not c["changed"] for c in result["clients"].values()))

    def test_failed_reload_is_retried_with_identical_catalog(self):
        with tempfile.TemporaryDirectory() as raw:
            env = self.setup_files(Path(raw))
            status = Path(raw) / "status.json"
            with patch.object(refresh, "discover", return_value=CATALOG), \
                 patch.object(refresh, "running", return_value=True), \
                 patch.object(refresh, "verify_opencode"), \
                 patch.object(refresh, "command", side_effect=refresh.RefreshError("failed")):
                with self.assertRaises(refresh.RefreshError):
                    refresh.refresh(env, status)
            self.assertEqual(set(json.loads(status.read_text())["pending_services"]), {"hermes", "opencode"})
            with patch.object(refresh, "discover", return_value=CATALOG), \
                 patch.object(refresh, "running", return_value=True), \
                 patch.object(refresh, "verify_opencode"), \
                 patch.object(refresh, "command") as command:
                result = refresh.refresh(env, status)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(command.call_count, 2)

    def test_api_mismatch_cannot_report_success(self):
        with tempfile.TemporaryDirectory() as raw:
            env = self.setup_files(Path(raw))
            status = Path(raw) / "status.json"
            with patch.object(refresh, "discover", return_value=CATALOG), \
                 patch.object(refresh, "running", return_value=True), \
                 patch.object(refresh, "command"), \
                 patch.object(refresh, "verify_opencode", side_effect=refresh.RefreshError("stale API")):
                with self.assertRaisesRegex(refresh.RefreshError, "stale API"):
                    refresh.refresh(env, status)
            self.assertNotEqual(json.loads(status.read_text())["status"], "ok")

    def test_failed_discovery_retains_all_configs(self):
        with tempfile.TemporaryDirectory() as raw:
            env = self.setup_files(Path(raw))
            before = {p: p.read_bytes() for p in refresh.paths(env).values()}
            with patch.object(refresh, "discover", side_effect=refresh.RefreshError("offline")):
                with self.assertRaises(refresh.RefreshError):
                    refresh.refresh(env, Path(raw) / "status.json")
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
