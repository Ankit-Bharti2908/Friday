"""Outbound notifications to the owner (used by scheduled jobs).

Respects quiet hours (23:00–07:30 IST by default): non-urgent messages are
queued and flushed by the morning flush job.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from core import settings

log = logging.getLogger("friday.notify")

_bot = None
_owner: int = 0
_queue: list[str] = []

QUIET_START = (23, 0)
QUIET_END = (7, 30)
TG_LIMIT = 4096


def configure(bot, owner_id: int) -> None:
    global _bot, _owner
    _bot, _owner = bot, owner_id


def in_quiet_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now(ZoneInfo(settings.TIMEZONE))
    minutes = now.hour * 60 + now.minute
    start = QUIET_START[0] * 60 + QUIET_START[1]
    end = QUIET_END[0] * 60 + QUIET_END[1]
    return minutes >= start or minutes < end


async def send_to_owner(text: str, urgent: bool = False) -> None:
    text = (text or "").strip()
    if not text:
        return
    if _bot is None or not _owner:
        log.info("notify (no channel configured): %s", text[:200])
        return
    if in_quiet_hours() and not urgent:
        _queue.append(text)
        log.info("queued for morning (quiet hours): %s", text[:80])
        return
    for i in range(0, len(text), TG_LIMIT):
        await _bot.send_message(_owner, text[i : i + TG_LIMIT])


async def flush_queue() -> None:
    global _queue
    if not _queue:
        return
    pending, _queue = _queue, []
    await send_to_owner("While you were away:\n\n" + "\n\n---\n\n".join(pending))
