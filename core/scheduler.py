"""Proactivity engine: APScheduler jobs defined in code at startup.

  06:30  daily workout alert (FRIDAY_WORKOUT_ALERT; pure file read, no LLM)
  07:00  daily diet alert (FRIDAY_DIET_ALERT; silent unless a meal plan
         was saved for today — pure file read, no LLM)
  08:00  morning briefing (through the main graph, reads only)
  07:35  flush messages queued during quiet hours
  */30 8-22  heartbeat (autonomous fast graph, read-only by construction)
  23:30  consolidate today's note

Jobs are deterministic from code/config, so no persistent jobstore needed.
"""
from __future__ import annotations

import logging
import re

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agents import briefing, diet, gym, heartbeat
from core import memory, notify, settings

log = logging.getLogger("friday.scheduler")


def parse_hhmm(text: str, default: tuple[int, int] = (6, 30), name: str = "alert time") -> tuple[int, int]:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", (text or "").strip())
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour, minute
    log.warning("bad %s %r — using %02d:%02d", name, text, *default)
    return default


def start(graph, autonomous_graph) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=settings.TIMEZONE)
    sched.add_job(briefing.run, CronTrigger(hour=8, minute=0), args=[graph], id="briefing",
                  misfire_grace_time=600)
    sched.add_job(notify.flush_queue, CronTrigger(hour=7, minute=35), id="flush",
                  misfire_grace_time=600)
    sched.add_job(heartbeat.run, CronTrigger(hour="8-22", minute="*/30"), args=[autonomous_graph],
                  id="heartbeat", misfire_grace_time=300, max_instances=1, coalesce=True)
    sched.add_job(memory.consolidate_today, CronTrigger(hour=23, minute=30), id="consolidate",
                  misfire_grace_time=1200)
    extra = ""
    if settings.WORKOUT_ALERT_TIME:
        hour, minute = parse_hhmm(settings.WORKOUT_ALERT_TIME, name="FRIDAY_WORKOUT_ALERT")
        sched.add_job(gym.daily_alert, CronTrigger(hour=hour, minute=minute), id="workout",
                      misfire_grace_time=3600, coalesce=True)
        extra += f", workout {hour:02d}:{minute:02d}"
    if settings.DIET_ALERT_TIME:
        hour, minute = parse_hhmm(settings.DIET_ALERT_TIME, default=(7, 0), name="FRIDAY_DIET_ALERT")
        sched.add_job(diet.daily_alert, CronTrigger(hour=hour, minute=minute), id="diet",
                      misfire_grace_time=3600, coalesce=True)
        extra += f", diet {hour:02d}:{minute:02d}"
    sched.start()
    log.info("scheduler up: briefing 08:00, heartbeat */30 8-22, consolidate 23:30%s (%s)",
             extra, settings.TIMEZONE)
    return sched
