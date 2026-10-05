import copy
import unittest

from llm_model_metadata import merge_catalog, normalize, parse_catalog, render_model
from ops.refresh_models import RefreshError, update_catalog


class MetadataTests(unittest.TestCase):
    def test_limits_are_not_added(self):
        self.assertEqual(normalize({"max_input_tokens": 65536, "max_output_tokens": 65536}),
                         {"context": 65536, "output": 65536})

    def test_nested_and_explicit_context(self):
        self.assertEqual(normalize({"context_window": 131072,
                                   "model_info": {"max_input_tokens": 65536, "max_output_tokens": "8192"}}),
                         {"context": 131072, "output": 8192})

    def test_unknown_and_invalid_are_not_invented(self):
        self.assertEqual(normalize({"max_input_tokens": True, "max_output_tokens": -1}), {})
        self.assertEqual(normalize({"max_input_tokens": "unknown", "reasoning": "true"}), {})
        # Generic max_tokens is ambiguous; never guess whether it means context or output.
        self.assertEqual(normalize({"max_tokens": 131072}), {})
        for kind in ("openclaw", "hermes", "opencode"):
            result = render_model(kind, "m", {})
            self.assertFalse(set(result) & {"contextWindow", "maxTokens", "limit", "context_length", "reasoning"})

    def test_error_payload_is_not_a_catalog(self):
        for payload in ({"error": {"message": "not authorized"}}, None, {"data": {}}, []):
            self.assertFalse(parse_catalog(payload))

    def test_catalog_shapes(self):
        for payload in ([{"id": "m", "max_input_tokens": 123}],
                        {"data": [{"id": "m", "max_input_tokens": 123}]},
                        {"models": {"m": {"max_input_tokens": 123}}}):
            result = parse_catalog(payload)
            self.assertEqual(tuple(result), ("m",))
            self.assertEqual(result.metadata, {"m": {"context": 123}})

    def test_merge_keeps_metadata_and_selected_order(self):
        live = parse_catalog({"data": [{"id": "m", "max_input_tokens": 65536}]})
        result = merge_catalog(live, ("z",), prepend=True)
        self.assertEqual(tuple(result), ("z", "m"))
        self.assertEqual(result.metadata["m"]["context"], 65536)

    def test_all_clients_update_existing_limits_and_preserve_other_data(self):
        catalog = {"id": "free", "base_url": "http://example/v1", "models": ["m"],
                   "metadata": {"m": {"context": 65536, "output": 8192}}}
        configs = {
            "openclaw": {"models": {"providers": {"free": {"baseUrl": "http://example/v1",
                "apiKey": "test", "models": [{"id": "m", "contextWindow": 1048576,
                "maxTokens": 65536, "name": "Keep", "cost": {"input": 0}}]}}},
                "agents": {"defaults": {"model": {"primary": "free/removed"},
                "models": {"free/removed": {}, "other/x": {"alias": "Keep"}}}}},
            "hermes": {"providers": {"free": {"base_url": "http://example/v1", "api_key": "test",
                "models": {"m": {"context_length": 1048576}, "removed": {}}}},
                "model": {"provider": "free", "default": "removed"}},
            "opencode": {"provider": {"free": {"options": {"baseURL": "http://example/v1", "apiKey": "test"},
                "models": {"m": {"limit": {"context": 0, "output": 0}, "name": "Keep"}, "removed": {}}}},
                "model": "free/removed"},
        }
        for kind, original in configs.items():
            with self.subTest(kind=kind):
                original["mcp"] = {"unchanged": {"url": "local", "key": "test"}}
                snapshot = copy.deepcopy(original)
                updated = update_catalog(kind, original, catalog)
                self.assertEqual(original, snapshot)
                self.assertEqual(updated["mcp"], snapshot["mcp"])
                if kind == "openclaw":
                    self.assertEqual(updated["agents"]["defaults"]["model"], snapshot["agents"]["defaults"]["model"])
                    row = updated["models"]["providers"]["free"]["models"][0]
                    self.assertEqual((row["contextWindow"], row["maxTokens"]), (65536, 8192))
                    self.assertEqual(row["name"], "Keep")
                elif kind == "hermes":
                    self.assertEqual(updated["model"]["default"], "removed")
                    self.assertEqual(updated["providers"]["free"]["models"],
                                     {"m": {"context_length": 65536, "max_completion_tokens": 8192}})
                else:
                    self.assertEqual(updated["model"], "free/removed")
                    self.assertEqual(updated["provider"]["free"]["models"]["m"]["limit"],
                                     {"context": 65536, "output": 8192})
                self.assertEqual(update_catalog(kind, updated, catalog), updated)
                changed = copy.deepcopy(catalog)
                changed["metadata"]["m"]["context"] = 32768
                self.assertNotEqual(update_catalog(kind, updated, changed), updated)

    def test_empty_catalog_does_not_mutate_config(self):
        with self.assertRaises(RefreshError):
            update_catalog("openclaw", {}, {"id": "free", "models": []})


if __name__ == "__main__":
    unittest.main()
