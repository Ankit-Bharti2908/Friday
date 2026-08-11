"""Shared AgentProfile dataclass (avoids circular imports between
registry and the per-agent files)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentProfile:
    name: str
    description: str
    tier: str = "standard"
    tool_keywords: tuple[str, ...] | None = None
    # Exact tool names to drop AFTER keyword matching. Substring keywords are
    # coarse: the diet agent needs to read the fitness profile ("fitness") but
    # must never write it, and no prompt rule is as reliable as not having the
    # tool. Explicit exclusion wins over every other rule.
    tool_exclude: tuple[str, ...] = ()
    instructions: str = ""
