"""Approval policy: which tool calls run freely vs. pause for the human.

Rules live in config/policies.json:
  - auto_allow_patterns      -> run without asking (reads)
  - require_approval_patterns -> always pause (writes/sends/deletes)
  - blocked_tools            -> never even exposed to the model
  - default                  -> what to do with tools matching nothing
                                 (ships as "require_approval" — fail safe)
"""
from __future__ import annotations

import json
from typing import Any

from core import settings

_POL = settings.POLICIES
_ALLOW: list[str] = _POL.get("auto_allow_patterns", [])
_REQUIRE: list[str] = _POL.get("require_approval_patterns", [])
_BLOCKED: set[str] = set(_POL.get("blocked_tools", []))
_DEFAULT: str = _POL.get("default", "require_approval")


def _matches(name: str, patterns: list[str]) -> bool:
    low = name.lower()
    return any(p.lower() in low for p in patterns)


def is_blocked(tool_name: str) -> bool:
    return tool_name in _BLOCKED


def requires_approval(tool_name: str) -> bool:
    """Order matters: explicit require beats allow beats default."""
    if _matches(tool_name, _REQUIRE):
        return True
    if _matches(tool_name, _ALLOW):
        return False
    return _DEFAULT == "require_approval"


# Tools whose single content arg is a document the human must actually read
# before approving — rendered as plain text with a roomier limit. 3500 stays
# under Telegram's 4096/message even with the "Approval needed" header.
_CONTENT_PREVIEW_TOOLS = {"save_workout_plan", "update_fitness_profile"}
_PREVIEW_LIMIT = 1200
_PREVIEW_LIMIT_CONTENT = 3500


def render_preview(tool_calls: list[dict[str, Any]]) -> str:
    """Human-readable preview of pending actions for the approval message.

    A single string arg containing a document (newlines / long text) renders
    as plain text — json.dumps would escape every newline into an unreadable
    one-liner, which is worse than truncation."""
    blocks: list[str] = []
    for i, call in enumerate(tool_calls, 1):
        name, args = call["name"], call.get("args", {})
        limit = _PREVIEW_LIMIT_CONTENT if name in _CONTENT_PREVIEW_TOOLS else _PREVIEW_LIMIT
        values = list(args.values()) if isinstance(args, dict) else []
        if len(values) == 1 and isinstance(values[0], str) and ("\n" in values[0] or len(values[0]) > 200):
            key, body = next(iter(args)), values[0]
            if len(body) > limit:
                body = body[:limit] + f"\n… (truncated — {len(values[0])} chars total; the full text is saved on approve)"
            blocks.append(f"{i}. {name} · {key} ({len(values[0])} chars)\n{body}")
        else:
            rendered = json.dumps(args, indent=2, ensure_ascii=False, default=str)
            if len(rendered) > limit:
                rendered = rendered[:limit] + f"\n… (truncated — {len(rendered)} chars total)"
            blocks.append(f"{i}. {name}\n{rendered}")
    return "\n\n".join(blocks)
