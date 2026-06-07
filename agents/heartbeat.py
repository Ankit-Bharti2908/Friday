"""Heartbeat: every 30 minutes, run the HEARTBEAT.md checklist on the
autonomous fast graph (read-only by construction) and ping the owner ONLY
for new, actionable findings.

Discipline lives here:
  - findings must carry a stable item_key -> alerts_sent table dedupes,
    so the same unread email never pings twice
  - empty result = silence (most runs should be silent)
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from core import db, notify, settings
from core.graph import ainvoke_autoresolve

log = logging.getLogger("friday.heartbeat")

_PROMPT = """You are running a silent background check. Work through this checklist
using READ-ONLY tools (skip any item whose tool is unavailable):

{checklist}

Output format — STRICT: a JSON array, nothing else. Each finding:
  {{"key": "<stable id, e.g. email:<message_id> or pr:<repo>#<num>>",
    "message": "<one actionable line for the owner>"}}
If nothing is genuinely actionable RIGHT NOW, output exactly: []
Bias hard toward []."""


def _checklist() -> str:
    path = settings.IDENTITY_DIR / "HEARTBEAT.md"
    if not path.exists():
        return ""
    lines = [
        ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip().startswith("-")
    ]
    return "\n".join(lines)


def _parse_findings(text: str) -> list[dict]:
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
        return [i for i in items if isinstance(i, dict) and i.get("key") and i.get("message")]
    except json.JSONDecodeError:
        return []


async def run(autonomous_graph) -> None:
    checklist = _checklist()
    if not checklist:
        return
    try:
        stamp = datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y%m%d%H%M")
        text = await ainvoke_autoresolve(
            autonomous_graph, _PROMPT.format(checklist=checklist), thread_id=f"job:heartbeat:{stamp}"
        )
        fresh = [f for f in _parse_findings(text) if not db.alert_already_sent(f["key"])]
        if not fresh:
            log.info("heartbeat: quiet")
            return
        await notify.send_to_owner("🔔 " + "\n🔔 ".join(f["message"] for f in fresh[:5]))
        for f in fresh[:5]:
            db.mark_alert_sent(f["key"])
    except Exception as exc:
        log.warning("heartbeat skipped (%s)", exc)
