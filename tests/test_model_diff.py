import json

from model_diff import model_diff, public_row


def test_diff_redacts_nested_credentials_but_keeps_useful_model_metadata():
    row = {
        "model_name": "example/model",
        "litellm_params": {
            "model": "upstream/model", "api_key": "hidden-one",
            "api_base": "https://user:hidden-two@example.com/v1?key=hidden-three",
            "extra_headers": {"Authorization": "Bearer hidden-four"},
            "extra_body": {"clientSecret": "hidden-five", "max_tokens": 4096},
            "aws_access_key_id": "hidden-six",
        },
        "model_info": {
            "context_tokens": 131072, "input_cost_per_token": 0,
            "credentials": {"anything": "hidden-seven"},
            "nested": {"accessToken": "hidden-eight", "X-API-Key": "hidden-nine"},
            "f24_metadata": {"description": "A useful model", "reasoning": {"supported": True}},
        },
    }
    clean = public_row(row)
    assert "hidden-" not in json.dumps(clean)
    assert clean["litellm_params"]["api_base"] == "https://example.com/v1"
    assert clean["litellm_params"]["extra_body"]["max_tokens"] == 4096
    assert clean["model_info"]["context_tokens"] == 131072
    assert clean["model_info"]["input_cost_per_token"] == 0
    assert clean["model_info"]["f24_metadata"]["reasoning"]["supported"] is True
    assert row["litellm_params"]["api_key"] == "hidden-one"


def test_list_order_and_credential_rotation_do_not_change_diff_fingerprint():
    rows = [{"model_name": name, "litellm_params": {"api_key": "old"}} for name in ("b", "a")]
    changed_keys = [{"model_name": name, "litellm_params": {"api_key": "new"}} for name in ("a", "b")]
    diff = model_diff(rows, changed_keys)
    assert diff["changed"] is False
    assert diff["before_sha256"] == diff["after_sha256"]
