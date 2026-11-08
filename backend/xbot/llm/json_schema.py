"""Translate a Pydantic model into a strict JSON schema that OpenAI-style structured-output
backends (OpenRouter → Gemini, OpenAI, others) accept: no $ref/$defs, no titles or defaults,
`additionalProperties: false` and every property required on every object."""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

_DROP_KEYS = {"title", "default", "examples", "description"}


def _inline(node: Any, defs: dict[str, Any], seen: tuple[str, ...] = ()) -> Any:
    if isinstance(node, list):
        return [_inline(n, defs, seen) for n in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = node["$ref"].split("/")[-1]
        if name in seen:
            raise ValueError(f"recursive schema reference {name!r} is not supported")
        return _inline(copy.deepcopy(defs[name]), defs, (*seen, name))
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROP_KEYS or key == "$defs":
            continue
        out[key] = _inline(value, defs, seen)
    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"].keys())
    return out


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    defs = raw.get("$defs", {})
    result = _inline(raw, defs)
    assert isinstance(result, dict)
    return result
