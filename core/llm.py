"""LLM layer: provider-agnostic tiers with ordered fallbacks, via LiteLLM.

Code never names a model — it names a *tier* ("fast" | "standard" | "deep").
config/models.json maps each tier to an ordered [primary, ...fallbacks] list.
Switching providers = editing JSON, zero code changes.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Type, TypeVar

import litellm
from pydantic import BaseModel

from core import settings

log = logging.getLogger("friday.llm")

litellm.drop_params = True  # silently drop params a given provider doesn't support
litellm.suppress_debug_info = True

_TIERS: dict[str, list[dict[str, Any]]] = settings.MODELS["tiers"]
_PARAMS: dict[str, Any] = settings.MODELS.get("params", {})
_EMBED = settings.MODELS.get("embeddings", {})

T = TypeVar("T", bound=BaseModel)


class AllModelsFailed(RuntimeError):
    pass


async def acomplete(
    tier: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    **overrides: Any,
):
    """Chat completion against a tier, walking the fallback chain in order.

    `messages` are OpenAI-format dicts. Returns the raw LiteLLM ModelResponse
    (resp.choices[0].message has .content and .tool_calls).
    """
    if tier not in _TIERS:
        raise KeyError(f"unknown tier '{tier}' (have: {list(_TIERS)})")

    errors: list[str] = []
    for entry in _TIERS[tier]:
        kwargs: dict[str, Any] = {**_PARAMS, **entry, **overrides}
        model = kwargs.pop("model")
        if tools:
            kwargs["tools"] = tools
        try:
            resp = await litellm.acompletion(model=model, messages=messages, **kwargs)
            log.debug("tier=%s served by %s", tier, model)
            return resp
        except Exception as exc:
            errors.append(f"{model}: {type(exc).__name__}: {exc}")
            log.warning("tier=%s model=%s failed (%s) — trying next", tier, model, exc)

    raise AllModelsFailed(f"tier '{tier}' exhausted all models:\n" + "\n".join(errors))


async def astructured(tier: str, messages: list[dict[str, Any]], schema: Type[T]) -> T:
    """Structured output: try native response_format, fall back to prompted JSON."""
    try:
        resp = await acomplete(tier, messages, response_format=schema)
        raw = resp.choices[0].message.content or ""
        return schema.model_validate_json(_strip_fences(raw))
    except AllModelsFailed:
        raise  # the whole tier is down — a second walk of the same chain can't succeed
    except Exception as exc:
        log.debug("native structured output failed (%s) — falling back to prompted JSON", exc)

    prompted = messages + [
        {
            "role": "user",
            "content": (
                "Respond with ONLY a JSON object matching this schema, no prose, no fences:\n"
                + json.dumps(schema.model_json_schema())
            ),
        }
    ]
    resp = await acomplete(tier, prompted)
    raw = resp.choices[0].message.content or ""
    return schema.model_validate_json(_strip_fences(raw))


async def aembed(text: str) -> list[float]:
    """Embed one string with the configured embedding model."""
    kwargs = {k: v for k, v in _EMBED.items() if k not in ("model", "dim")}
    resp = await litellm.aembedding(model=_EMBED["model"], input=[text], **kwargs)
    vec = resp.data[0]["embedding"]
    expected = _EMBED.get("dim")
    if expected and len(vec) != expected:
        raise ValueError(
            f"embedding dim {len(vec)} != configured {expected} — "
            "never mix embedding models in one vec table"
        )
    return vec


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.rsplit("```", 1)[0]
    return raw.strip()
