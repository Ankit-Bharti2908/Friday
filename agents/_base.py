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
    instructions: str = ""
