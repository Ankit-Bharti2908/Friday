"""Intent router: cheap (`fast` tier) structured classification of each
incoming message, deciding which agent profile handles the turn.

Low confidence -> 'general'. Any failure -> 'general'. The router must never
be the thing that breaks a conversation.
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from core import llm

log = logging.getLogger("friday.router")

Intent = Literal["email", "calendar", "code", "research", "fitness", "memory", "task", "chat"]

INTENT_TO_PROFILE = {
    "email": "email",
    "calendar": "calendar",
    "code": "coder",
    "research": "research",
    "fitness": "gym",
    "memory": "general",
    "task": "general",
    "chat": "general",
}

CONFIDENCE_FLOOR = 0.6


class Route(BaseModel):
    intent: Intent
    confidence: float = Field(ge=0, le=1)


_PROMPT = """Classify the user's message for a personal assistant. Intents:
- email: reading, triaging, drafting, sending email
- calendar: events, meetings, scheduling, availability
- code: GitHub, PRs, issues, repos, programming help, running code
- research: questions needing web search / reading sources / comparisons
- fitness: gym training, workout plans, exercises, logging a workout, body/weight goals, cardio
- memory: asking the assistant to remember/forget/recall things about the user
- task: file operations, reminders, todos, misc actions
- chat: everything else (small talk, opinions, quick answers)

Recent context: {context}
Message: {text}"""


async def classify(text: str, context: str = "") -> tuple[str, Route | None]:
    """Returns (profile_name, route)."""
    try:
        route = await llm.astructured(
            "fast",
            [{"role": "user", "content": _PROMPT.format(context=context[:400] or "(none)", text=text[:600])}],
            Route,
        )
        if route.confidence < CONFIDENCE_FLOOR:
            return "general", route
        return INTENT_TO_PROFILE.get(route.intent, "general"), route
    except Exception as exc:
        log.warning("router failed (%s) — using general", exc)
        return "general", None
