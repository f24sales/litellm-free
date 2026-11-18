"""LiteLLM proxy adapter for managed LLM7 chat routes."""
from litellm.integrations.custom_logger import CustomLogger
from llm7_policy import filter_model_request


class LLM7Capabilities(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        updated = filter_model_request(data, data.get("model"))
        if updated != data:
            return updated


handler = LLM7Capabilities()
