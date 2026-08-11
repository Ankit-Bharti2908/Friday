"""Diet planning data layer for the diet agent: the owner's nutrition
profile and per-day meal plans as human-editable markdown under
memory/nutrition/ (the files ARE the source of truth).

  PROFILE.md      intake record: eating pattern, allergies, schedule,
                  and the computed daily targets (calories/macros)
  days/<date>.md  one saved meal plan per calendar day — today's file
                  feeds the daily diet alert
  history/        archived copy of every replaced/deleted file (undo trail)

  nutrition_tools() -> LangChain tools for the agent
  todays_plan()     -> today's day file (feeds the daily alert job)

Unlike the weekly workout plan, a diet plan is a ONE-DAY document: it is
built fresh from the profile targets plus that day's training session, so
there is no weekly file to parse — the date IS the filename.

Approval posture (config/policies.json): get_* reads run free;
update_/save_/delete_ pause for the owner — the planner proposes, the
owner approves.
"""
from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from langchain_core.tools import tool

from core import settings

log = logging.getLogger("friday.nutrition")

NUTRITION_DIR = settings.MEMORY_DIR / "nutrition"
PROFILE_PATH = NUTRITION_DIR / "PROFILE.md"
DAYS_DIR = NUTRITION_DIR / "days"
HISTORY_DIR = NUTRITION_DIR / "history"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Sentinels double as NEXT-ACTION directives (same contract as core/fitness):
# they are the only signal that reaches a model mid-turn, including after an
# approval resume, so they must say exactly what to do next.
NO_DIET_PROFILE = (
    "(no diet profile yet) NEXT ACTION: read get_fitness_profile ONCE if you have "
    "not this turn (stats and training goal live there), then stop calling tools "
    "and start the diet intake in plain text — ask the first 2-4 questions from "
    "the diet_intake skill (eating pattern + allergies first). Do not compute "
    "targets or design meals yet."
)
NO_DAY_PLAN_NO_PROFILE = (
    "(no diet plan for {date} and no diet profile) NEXT ACTION: stop calling "
    "tools and start the diet intake in conversation, per the diet_intake skill."
)
NO_DAY_PLAN_PROFILE_READY = (
    "(no diet plan for {date} yet) The diet profile EXISTS — the daily targets "
    "are in it. NEXT ACTION: check that day's training ONCE (get_todays_workout "
    "for today, get_workout_plan otherwise) and get_recent_diet_plans ONCE, then "
    "stop calling tools and build that ONE day's menu per the diet_day_plan "
    "skill, presented in PLAIN TEXT for the owner to react to. Do NOT call "
    "save_diet_plan in this same turn — save only after the owner replies "
    "approving the menu, exactly as approved."
)


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.TIMEZONE))


def _read(path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _write(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")


def _archive(path) -> None:
    """Keep the previous version before overwriting/deleting (cheap undo trail)."""
    if not path.exists():
        return
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, HISTORY_DIR / f"{path.stem.lower()}-{stamp}.md")


def resolve_date(text: str) -> str | None:
    """'', 'today', 'tomorrow', 'yesterday', or 'YYYY-MM-DD' -> date key.
    None for anything else — the date becomes a filename, so junk is refused,
    never guessed."""
    norm = (text or "").strip().lower()
    if norm in ("", "today"):
        return _now().strftime("%Y-%m-%d")
    if norm == "tomorrow":
        return (_now() + timedelta(days=1)).strftime("%Y-%m-%d")
    if norm == "yesterday":
        return (_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    if _DATE_RE.match(norm):
        try:
            datetime.strptime(norm, "%Y-%m-%d")
            return norm
        except ValueError:
            return None
    return None


def _day_path(date_key: str):
    return DAYS_DIR / f"{date_key}.md"


def todays_plan(now: datetime | None = None) -> tuple[str, str | None]:
    """(date_key, today's saved meal plan or None). Used by the daily alert —
    pure file read, no LLM."""
    date_key = (now or _now()).strftime("%Y-%m-%d")
    return date_key, (_read(_day_path(date_key)) or None)


def _day_sentinel(date_key: str) -> str:
    template = NO_DAY_PLAN_PROFILE_READY if PROFILE_PATH.exists() else NO_DAY_PLAN_NO_PROFILE
    return template.format(date=date_key)


_BAD_DATE = (
    "ERROR: date {value!r} not understood — use 'today', 'tomorrow', "
    "'yesterday', or YYYY-MM-DD."
)


# ------------------------------------------------------------- agent tools
@tool
def get_diet_profile() -> str:
    """Read the owner's diet profile (eating pattern, allergies, meal schedule, cooking constraints, and the computed daily calorie/macro targets). Always check this before any nutrition advice."""
    return _read(PROFILE_PATH) or NO_DIET_PROFILE


@tool
def update_diet_profile(content: str) -> str:
    """Save the owner's complete diet profile as markdown (replaces the file; the old version is archived). Cover: eating pattern & exclusions, allergies/intolerances, meal schedule, cooking/budget constraints, food likes/dislikes, medical flags, and the computed daily targets (calories, protein, fat, carbs, fiber, water) with the reasoning."""
    _archive(PROFILE_PATH)
    _write(PROFILE_PATH, content)
    return f"Diet profile saved ({len(content.splitlines())} lines)."


@tool
def get_diet_plan(date: str = "") -> str:
    """Read the saved meal plan for one day (default today; also accepts 'tomorrow', 'yesterday', or YYYY-MM-DD). Diet plans are per-day documents — there is no weekly file."""
    date_key = resolve_date(date)
    if date_key is None:
        return _BAD_DATE.format(value=date)
    return _read(_day_path(date_key)) or _day_sentinel(date_key)


@tool
def save_diet_plan(content: str, date: str = "") -> str:
    """Save ONE day's meal plan as markdown (default today; also 'tomorrow' or YYYY-MM-DD; replaces that day's plan, archiving the old one). Use one '## <Meal>' section per meal (Breakfast, Lunch, Snack, Dinner…) and end with the day's totals vs targets — the morning alert sends the file exactly as saved."""
    date_key = resolve_date(date)
    if date_key is None:
        return _BAD_DATE.format(value=date)
    path = _day_path(date_key)
    _archive(path)
    _write(path, content)
    meals = len(re.findall(r"(?m)^##\s", content))
    note = (
        " WARNING: fewer than 2 '## <Meal>' sections found — list each meal "
        "under its own '## ' heading so the plan reads cleanly in the alert."
        if meals < 2
        else ""
    )
    return f"Diet plan for {date_key} saved ({len(content.splitlines())} lines).{note}"


@tool
def get_recent_diet_plans(days: int = 3) -> str:
    """The last N days of saved meal plans (default 3, max 7), newest first. Check this before designing a new day so meals rotate instead of repeating."""
    days = max(1, min(int(days), 7))
    if not DAYS_DIR.exists():
        return "(no diet plans saved yet)"
    files = sorted(
        (p for p in DAYS_DIR.glob("*.md") if _DATE_RE.match(p.stem)),
        key=lambda p: p.stem,
        reverse=True,
    )[:days]
    if not files:
        return "(no diet plans saved yet)"
    return "\n\n".join(f"# {p.stem}\n{_read(p)}" for p in files)


@tool
def delete_diet_data() -> str:
    """Archive and delete ALL diet data (profile + every saved day plan) — the full 'start over' reset for nutrition. Use only when the owner explicitly wants to start from scratch."""
    deleted = []
    day_files = sorted(DAYS_DIR.glob("*.md")) if DAYS_DIR.exists() else []
    for path in [PROFILE_PATH, *day_files]:
        if path.exists():
            _archive(path)
            path.unlink()
            deleted.append(path.name)
    if not deleted:
        return "(no diet data to delete)"
    return (
        f"Deleted {', '.join(deleted)} (archived to history/). NEXT ACTION: run the "
        "FULL diet intake from the very start — re-ask preferences and re-derive "
        "targets, reusing nothing."
    )


def nutrition_tools() -> list:
    return [
        get_diet_profile,
        update_diet_profile,
        get_diet_plan,
        save_diet_plan,
        get_recent_diet_plans,
        delete_diet_data,
    ]
