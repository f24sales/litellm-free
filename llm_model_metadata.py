"""Reported model metadata and native config fields for the three clients."""
from collections.abc import Mapping
from copy import deepcopy


class ModelCatalog(tuple):
    def __new__(cls, ids=(), metadata=None):
        result = super().__new__(cls, ids)
        result.metadata = metadata or {}
        return result


def _positive(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    return value if isinstance(value, int) and value > 0 else None


def normalize(row):
    sources = [row]
    if isinstance(row.get("model_info"), Mapping):
        sources.append(row["model_info"])

    def number(*keys):
        for key in keys:
            for source in sources:
                value = _positive(source.get(key))
                if value is not None:
                    return value

    result = {}
    context = number("context_window", "context_length", "contextWindow", "max_context_tokens", "max_input_tokens")
    output = number("max_output_tokens", "max_completion_tokens", "maxTokens")
    if context:
        result["context"] = context
    if output:
        result["output"] = output
    for key in ("supports_reasoning", "reasoning"):
        value = next((source[key] for source in sources if key in source), None)
        if isinstance(value, bool):
            result["reasoning"] = value
            break
    modalities = row.get("input_modalities")
    if isinstance(modalities, list) and modalities and all(isinstance(v, str) for v in modalities):
        result["input"] = modalities
    return result


def parse_catalog(payload):
    rows = payload
    if isinstance(rows, Mapping):
        rows = rows.get("data", rows.get("models"))
    if isinstance(rows, Mapping):
        rows = [{"id": key, **(dict(value) if isinstance(value, Mapping) else {})}
                for key, value in rows.items()]
    if not isinstance(rows, list):
        return ModelCatalog()
    result = {}
    for row in rows:
        if isinstance(row, str):
            name, metadata = row.strip(), {}
        elif isinstance(row, Mapping):
            name = next((row[k].strip() for k in ("id", "model", "name")
                         if isinstance(row.get(k), str) and row[k].strip()), "")
            metadata = normalize(row)
        else:
            continue
        if name:
            result[name] = {**result.get(name, {}), **metadata}
    return ModelCatalog(sorted(result), result)


def merge_catalog(primary, extra=(), *, prepend=False):
    first, second = (extra, primary) if prepend else (primary, extra)
    ids = list(dict.fromkeys([*first, *second]))
    if not prepend:
        ids.sort()
    left, right = getattr(primary, "metadata", {}), getattr(extra, "metadata", {})
    metadata = {name: {**right.get(name, {}), **left.get(name, {})} for name in ids}
    return ModelCatalog(ids, metadata)


def render_model(kind, name, metadata, previous=None):
    """Replace owned discovery fields, preserving unrelated per-model settings."""
    result = deepcopy(previous or {})
    context, output = metadata.get("context"), metadata.get("output")
    if kind == "openclaw":
        for key in ("contextWindow", "maxTokens", "reasoning", "input"):
            result.pop(key, None)
        result.update(id=name)
        result.setdefault("name", name)
        if context:
            result["contextWindow"] = context
        if output:
            result["maxTokens"] = output
        if "reasoning" in metadata:
            result["reasoning"] = metadata["reasoning"]
        inputs = [v for v in metadata.get("input", []) if v in ("text", "image")]
        if inputs:
            result["input"] = inputs
    elif kind == "opencode":
        limits = result.pop("limit", {}).copy()
        for key in ("context", "output"):
            limits.pop(key, None)
        if context:
            limits["context"] = context
        if output:
            limits["output"] = output
        if limits:
            result["limit"] = limits
        result.pop("reasoning", None)
        if "reasoning" in metadata:
            result["reasoning"] = metadata["reasoning"]
    elif kind == "hermes":
        for key in ("context_length", "max_completion_tokens"):
            result.pop(key, None)
        if context:
            result["context_length"] = context
        if output:
            result["max_completion_tokens"] = output
    else:
        raise ValueError("Unknown client")
    return result
