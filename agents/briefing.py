"""Morning briefing: one agentic pass through the main graph.

Rather than hand-coding tool calls per integration, we ask the agent to
assemble the briefing with whatever read tools exist (calendar, email,
GitHub...). Missing integrations degrade to skipped sections automatically.
Risky calls are auto-rejected by ainvoke_autoresolve — briefings only read.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from core import memory, notify, settings
from core.graph import ainvoke_autoresolve

log = logging.getLogger("friday.briefing")

_PROMPT = """Generate my morning briefing. Use read-only tools to check, in this order
(silently skip any section whose tool is unavailable or empty):
1. Today's calendar events.
2. Unread email — triage to: urgent / needs-reply (max 5, one line each: sender — gist). Skip newsletters.
3. GitHub PRs or issues waiting on me.
4. Yesterday's note (below) — carry over open loops in one line.

Yesterday's note:
{yesterday}

Format: <= 15 lines total, plain text, sections only if non-empty, no preamble,
no filler like "no urgent emails" unless EVERYTHING is empty (then one calm line)."""


async def run(graph) -> None:
    try:
        date = datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m-%d")
        yesterday = memory.read_note(days_ago=1) or "(no note)"
        text = await ainvoke_autoresolve(
            graph, _PROMPT.format(yesterday=yesterday[:1500]), thread_id=f"job:briefing:{date}"
        )
        await notify.send_to_owner(f"☀️ Briefing — {date}\n\n{text}" if text else "☀️ Briefing: nothing needs you today.")
    except Exception as exc:
        log.exception("briefing failed")
        await notify.send_to_owner(f"Briefing failed: {type(exc).__name__}: {exc}")
