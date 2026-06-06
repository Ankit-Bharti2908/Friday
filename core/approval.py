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


def render_preview(tool_calls: list[dict[str, Any]]) -> str:
    """Human-readable preview of pending actions for the approval message."""
    blocks: list[str] = []
    for i, call in enumerate(tool_calls, 1):
        args = json.dumps(call.get("args", {}), indent=2, ensure_ascii=False, default=str)
        if len(args) > 1200:
            args = args[:1200] + "\n… (truncated)"
        blocks.append(f"{i}. {call['name']}\n{args}")
    return "\n\n".join(blocks)
