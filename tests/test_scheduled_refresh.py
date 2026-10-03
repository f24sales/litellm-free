from io import BytesIO
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
import pull_and_refresh as scheduled

VALID = b"model_list:\n  - model_name: keep\n    litellm_params:\n      model: openai/example\n"


class PullTests(unittest.TestCase):
    def test_valid_download_and_idempotence(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "published.yaml"
            env = {"IMPORT_SOURCE_URL": "https://catalog.example.test/models.yaml"}
            first = scheduled.pull(env, target, opener=lambda *args, **kwargs: BytesIO(VALID))
            self.assertTrue(first["changed"])
            self.assertEqual(first["models"], 1)
            stamp = target.stat().st_mtime_ns
            second = scheduled.pull(env, target, opener=lambda *args, **kwargs: BytesIO(VALID))
            self.assertFalse(second["changed"])
            self.assertEqual(target.stat().st_mtime_ns, stamp)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_invalid_download_never_overwrites_previous_catalog(self):
        for payload in (b"model_list: []", b"model_list: [null]", b"model_list: [1]", b"oops"):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as raw:
                target = Path(raw) / "published.yaml"
                target.write_bytes(VALID)
                with self.assertRaises(scheduled.RefreshError):
                    scheduled.pull({"IMPORT_SOURCE_URL": "https://catalog.test/file"}, target,
                                   opener=lambda *args, **kwargs: BytesIO(payload))
                self.assertEqual(target.read_bytes(), VALID)

    def test_source_does_not_allow_credentials_or_insecure_transport(self):
        for url in ("http://catalog.test/file", "https://user:secret@catalog.test/file",
                    "https://catalog.test/file?secret=1", "file:///tmp/catalog"):
            with self.subTest(url=url), self.assertRaises(scheduled.RefreshError):
                scheduled.pull({"IMPORT_SOURCE_URL": url}, Path("/unused"))

    def test_unchanged_public_catalog_still_refreshes_clients(self):
        with patch.dict(os.environ, {"IMPORT_SOURCE_URL": "https://injected.test/catalog"}, clear=True), \
             patch.object(scheduled, "dotenv_values", return_value={"IMPORT_SOURCE_URL": "https://preset.test/catalog"}), \
             patch.object(scheduled, "pull", return_value={"changed": False}) as pull, \
             patch.object(scheduled, "refresh_main", return_value=0) as refresh:
            self.assertEqual(scheduled.main(), 0)
            self.assertEqual(pull.call_args.args[0]["IMPORT_SOURCE_URL"], "https://injected.test/catalog")
            refresh.assert_called_once_with([])

    def test_failed_download_is_not_reported_as_success(self):
        with patch.object(scheduled, "dotenv_values", return_value={}), \
             patch.object(scheduled, "pull", side_effect=ValueError("private error")), \
             patch.object(scheduled, "refresh_main") as refresh:
            self.assertEqual(scheduled.main(), 1)
            refresh.assert_not_called()

    def test_cron_uses_systemd_environment_twice_daily(self):
        root = Path(__file__).resolve().parents[1] / "image/runtime"
        cron = (root / "etc/cron.d/litellm-free").read_text()
        self.assertIn("10 0,12 * * * root /usr/bin/systemctl start --no-block litellm-free-refresh.service", cron)
        unit = (root / "etc/systemd/system/litellm-free-refresh.service").read_text()
        self.assertIn("ops/pull_and_refresh.py", unit)
        self.assertIn("Requisite=openclaw.service hermes.service opencode.service", unit)


if __name__ == "__main__":
    unittest.main()
