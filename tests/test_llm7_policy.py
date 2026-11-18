import json

from llm7_policy import enrich_catalog, filter_request, update_deployments


def test_selective_tools_and_forced_choice():
    vision = {"type": "function", "function": {"name": "look", "parameters": {
        "type": "object", "properties": {"image_url": {"type": "string"}}}}}
    text = {"type": "function", "function": {"name": "lookup"}}
    request = {"tools": [vision, text], "tool_choice": {"type": "function", "function": {"name": "look"}},
               "messages": [{"role": "user", "content": "Hi"}], "parallel_tool_calls": True}
    filtered = filter_request(request, {"tools": True, "vision": False})
    assert filtered == {"tools": [text], "messages": request["messages"], "parallel_tool_calls": True}
    assert filter_request(request, {"tools": False, "vision": False}) == {"messages": request["messages"]}
    assert filter_request(request, None) is request
    assert request["tools"] == [vision, text]


def test_import_and_refresh_agree_on_new_llm7_ids(tmp_path, monkeypatch):
    policy = {"llm7/future": {"tools": False, "vision": False}}
    cache = tmp_path / "capabilities.json"
    cache.write_text(json.dumps(policy))
    monkeypatch.setenv("LITELLM_FREE_LLM7_CACHE", str(cache))
    names = ["llm7/future", "unrelated/future"]
    rows = [{"model_name": name, "model_info": {}, "litellm_params": {}} for name in names]
    update_deployments(rows)
    assert rows[0]["model_info"] == {"supports_vision": False, "supports_function_calling": False}
    assert "tools" in rows[0]["litellm_params"]["additional_drop_params"]
    assert rows[1]["litellm_params"] == rows[1]["model_info"] == {}
    catalog = enrich_catalog({"models": names, "metadata": {}})
    assert catalog["metadata"] == {"llm7/future": {"llm7": policy[names[0]]}}
