"""Fitness coaching data layer for the gym agent: the owner's training
profile, weekly workout plan, and workout log as human-editable markdown
under memory/fitness/ (the files ARE the source of truth).

  PROFILE.md   intake record: stats, goal, schedule, equipment, level
  PLAN.md      current weekly plan — one '## <Weekday>' section per day
  LOG.md       append-only workout log ('- YYYY-MM-DD HH:MM — entry')
  history/     archived copy of every replaced profile/plan (undo trail)

  fitness_tools()   -> LangChain tools for the agent
  todays_session()  -> today's PLAN.md section (feeds the daily alert job)

Approval posture (config/policies.json): get_* reads run free;
update_/save_ pause for the owner — the trainer proposes, the owner
approves. record_workout is auto-allowed like `remember` (an internal
log append with no external effect).
"""
from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from langchain_core.tools import tool

from core import settings

log = logging.getLogger("friday.fitness")

FITNESS_DIR = settings.MEMORY_DIR / "fitness"
PROFILE_PATH = FITNESS_DIR / "PROFILE.md"
PLAN_PATH = FITNESS_DIR / "PLAN.md"
LOG_PATH = FITNESS_DIR / "LOG.md"
HISTORY_DIR = FITNESS_DIR / "history"

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Sentinels double as NEXT-ACTION directives: they are the only signal that
# reaches a model mid-turn (including after an approval resume, where the
# route node does not re-run), so they must say exactly what to do next —
# a weak model will otherwise loop re-reading files until the circuit breaker.
NO_PROFILE = (
    "(no fitness profile yet) NEXT ACTION: stop calling tools this turn. Start the "
    "intake interview in plain text — ask the first 2-4 questions from the "
    "fitness_intake skill (goal + safety screen first). Do not design anything yet."
)
NO_PLAN_NO_PROFILE = (
    "(no workout plan and no profile) NEXT ACTION: stop calling tools and start the "
    "intake interview in conversation, per the fitness_intake skill."
)
NO_PLAN_PROFILE_READY = (
    "(no workout plan yet) The profile EXISTS — you already have everything you need. "
    "NEXT ACTION: no more read calls. Design the full 7-day week per the "
    "gym_program_design skill, present it in text, then call save_workout_plan once."
)


def _no_plan_sentinel() -> str:
    return NO_PLAN_PROFILE_READY if PROFILE_PATH.exists() else NO_PLAN_NO_PROFILE


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.TIMEZONE))


def _read(path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _write(path, content: str) -> None:
    FITNESS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")


def _archive(path) -> None:
    """Keep the previous version before overwriting (cheap undo trail)."""
    if not path.exists():
        return
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, HISTORY_DIR / f"{path.stem.lower()}-{stamp}.md")


# ------------------------------------------------------------- plan parsing
def day_section(plan_text: str, weekday: str) -> str | None:
    """Extract one '## <Weekday> …' section (heading through the line before
    the next level-2 heading). Case-insensitive; the weekday may appear
    anywhere in the heading ('## Monday — Push', '## Day 1 · Monday')."""
    lines = plan_text.splitlines()
    start = next(
        (i for i, ln in enumerate(lines)
         if re.match(r"^##\s", ln) and weekday.lower() in ln.lower()),
        None,
    )
    if start is None:
        return None
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"^##\s", lines[i])),
        len(lines),
    )
    section = "\n".join(lines[start:end]).strip()
    return section or None


def missing_days(plan_text: str) -> list[str]:
    return [wd.capitalize() for wd in WEEKDAYS if day_section(plan_text, wd) is None]


def todays_session(now: datetime | None = None) -> tuple[str, str | None]:
    """(weekday_name, today's plan section or None). Used by the daily alert
    and the get_todays_workout tool — pure file read, no LLM."""
    now = now or _now()
    weekday = WEEKDAYS[now.weekday()]
    plan = _read(PLAN_PATH)
    if not plan:
        return weekday.capitalize(), None
    return weekday.capitalize(), day_section(plan, weekday)


# ------------------------------------------------------------- agent tools
@tool
def get_fitness_profile() -> str:
    """Read the owner's fitness profile (stats, goal, schedule, equipment, injuries, assessed level). Always check this before giving training advice."""
    return _read(PROFILE_PATH) or NO_PROFILE


@tool
def update_fitness_profile(content: str) -> str:
    """Save the owner's complete fitness profile as markdown (replaces the file; the old version is archived). Cover: personal stats, goal, training history, schedule, equipment, health flags, and the assessed level with reasoning."""
    _archive(PROFILE_PATH)
    _write(PROFILE_PATH, content)
    return f"Fitness profile saved ({len(content.splitlines())} lines)."


@tool
def get_workout_plan() -> str:
    """Read the current weekly workout plan."""
    return _read(PLAN_PATH) or _no_plan_sentinel()


@tool
def save_workout_plan(content: str) -> str:
    """Save the weekly workout plan as markdown (replaces the current plan; the old one is archived). It MUST have one '## <Weekday> — <focus>' section for each of the 7 days, rest days included ('## Sunday — Rest'), because the daily alert sends exactly that day's section each morning."""
    _archive(PLAN_PATH)
    _write(PLAN_PATH, content)
    gaps = missing_days(content)
    note = (
        f" WARNING: no section found for {', '.join(gaps)} — the daily alert stays"
        " silent on those days. Add '## <Weekday> — Rest' sections if that's intended."
        if gaps
        else ""
    )
    return f"Workout plan saved ({len(content.splitlines())} lines).{note}"


@tool
def get_todays_workout() -> str:
    """Today's session from the weekly plan — exactly what the morning alert sends."""
    weekday, section = todays_session()
    if section is None:
        if PLAN_PATH.exists():
            return f"(the plan has no section for {weekday})"
        return _no_plan_sentinel()
    return section


@tool
def record_workout(entry: str) -> str:
    """Append one line to the workout log when the owner reports training: what was done, key loads/reps or PRs, how it felt, any pain. Example: 'Push day done — bench 4x8 @ 52.5kg (RIR 2), shoulder fine'."""
    entry = " ".join(entry.split()).strip()
    if not entry:
        return "Nothing to log."
    if not LOG_PATH.exists():
        _write(LOG_PATH, "# Workout log\n<!-- appended by Friday's gym agent; newest last -->")
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(f"- {_now().strftime('%Y-%m-%d %H:%M')} — {entry}\n")
    return f"Logged: {entry}"


@tool
def get_workout_log(days: int = 14) -> str:
    """Workout-log entries from the last N days (default 14). Check this before progression or plan changes — never guess adherence."""
    text = _read(LOG_PATH)
    if not text:
        return "(workout log is empty)"
    cutoff = (_now() - timedelta(days=max(1, days))).strftime("%Y-%m-%d")
    entries = [
        ln for ln in text.splitlines()
        if (m := re.match(r"^- (\d{4}-\d{2}-\d{2})", ln)) and m.group(1) >= cutoff
    ]
    return "\n".join(entries) if entries else f"(no workouts logged in the last {days} days)"


def fitness_tools() -> list:
    return [
        get_fitness_profile,
        update_fitness_profile,
        get_workout_plan,
        save_workout_plan,
        get_todays_workout,
        record_workout,
        get_workout_log,
    ]
