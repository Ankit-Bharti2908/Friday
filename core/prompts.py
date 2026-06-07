"""System prompt assembly: SOUL.md + USER.md + clock + tool digest + extras.

Identity lives in markdown so you can change Friday's personality with a
text editor — no code, no redeploy. `extras` carries per-turn sections:
agent-profile instructions, recalled memories, matched skills, reflection.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from zoneinfo import ZoneInfo

from core import settings


def _read(name: str) -> str:
    path = settings.IDENTITY_DIR / name
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def tools_digest(tools: list) -> str:
    """One line per tool: 'name — first sentence of description'."""
    lines = []
    for t in tools:
        desc = (getattr(t, "description", "") or "").strip().split("\n")[0][:120]
        lines.append(f"- {t.name}: {desc}")
    return "\n".join(lines) if lines else "(no external tools loaded)"


def build_system_prompt(tools: list, extras: Iterable[str] = ()) -> str:
    now = datetime.now(ZoneInfo(settings.TIMEZONE))
    sections = [
        _read("SOUL.md"),
        _read("USER.md"),
        f"## Now\nCurrent datetime: {now.strftime('%A, %d %B %Y, %H:%M')} ({settings.TIMEZONE})",
        "## Available tools\n" + tools_digest(tools),
        (
            "## Tool etiquette\n"
            "Use tools to answer questions about the real world instead of guessing. "
            "Actions that send/create/delete/modify anything will pause for the user's "
            "approval — prepare them confidently, the user gets the final say."
        ),
        *[e for e in extras if e and e.strip()],
    ]
    return "\n\n".join(s for s in sections if s)
