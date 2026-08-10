"""Agent profiles: the modular 'subagent' system.

A profile = tier + tool subset + extra instructions. The supervisor (router)
picks one per turn; the same graph runs it. Adding a subagent = one small
file exporting PROFILE + one import line below.
"""
from __future__ import annotations

from agents._base import AgentProfile
from agents.calendar_agent import PROFILE as CALENDAR
from agents.coder import PROFILE as CODER
from agents.email import PROFILE as EMAIL
from agents.gym import PROFILE as GYM
from agents.research import PROFILE as RESEARCH

# Tools every profile keeps regardless of filtering (memory + sandbox).
ALWAYS_INCLUDE = {"remember", "recall_memories", "forget", "run_python"}

GENERAL = AgentProfile(
    name="general",
    description="Default assistant for chat, tasks, files, reminders, memory.",
    instructions=(
        "You are in general mode. Handle the request directly. "
        "For multi-step tasks, briefly state your plan, then execute. "
        "If the owner asks to forget everything / start from scratch, confirm once, "
        "then call forget(query='all') AND delete_fitness_data — both together are "
        "the full reset."
    ),
)

PROFILES: dict[str, AgentProfile] = {
    p.name: p for p in (GENERAL, EMAIL, RESEARCH, CODER, CALENDAR, GYM)
}


def get(name: str) -> AgentProfile:
    return PROFILES.get(name, GENERAL)


def filter_tools(profile: AgentProfile, tools: list) -> list:
    """Tool subset for a profile; falls back to all tools if the filter
    would leave the agent with nothing domain-specific."""
    if profile.tool_keywords is None:
        return tools
    kept = [
        t
        for t in tools
        if t.name in ALWAYS_INCLUDE or any(k in t.name.lower() for k in profile.tool_keywords)
    ]
    domain_tools = [t for t in kept if t.name not in ALWAYS_INCLUDE]
    return kept if domain_tools else tools
