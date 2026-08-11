"""Skills: teach Friday workflows by dropping markdown files in skills/.

Format (tiny frontmatter, no YAML dependency):

    ---
    name: email_style
    description: How Ankit writes emails
    triggers: draft, reply, email, mail
    agents: email
    ---
    <body injected into the system prompt when matched>

A skill is injected when (a) any trigger substring appears in the user's
message, or (b) the active agent profile is listed in `agents`
(`agents: all` = every profile). Files are re-read on every match, so
editing a skill changes behavior on the very next message — no restart.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from core import settings

log = logging.getLogger("friday.skills")


@dataclass
class Skill:
    name: str
    description: str = ""
    triggers: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    body: str = ""
    path: Path | None = None


def _parse(path: Path) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("skill %s unreadable (%s)", path.name, exc)
        return None

    meta: dict[str, str] = {}
    body = text
    if text.lstrip().startswith("---"):
        parts = text.lstrip().split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    meta[key.strip().lower()] = value.strip()
            body = parts[2].strip()

    split = lambda s: [x.strip().lower() for x in s.split(",") if x.strip()]
    return Skill(
        name=meta.get("name", path.stem),
        description=meta.get("description", ""),
        triggers=split(meta.get("triggers", "")),
        agents=split(meta.get("agents", "")),
        body=body,
        path=path,
    )


def load_all() -> list[Skill]:
    if not settings.SKILLS_DIR.exists():
        return []
    skills = [_parse(p) for p in sorted(settings.SKILLS_DIR.glob("*.md"))]
    return [s for s in skills if s]


def match(user_text: str, agent_name: str) -> list[Skill]:
    low = (user_text or "").lower()
    out = []
    for skill in load_all():
        by_trigger = any(t in low for t in skill.triggers)
        by_agent = "all" in skill.agents or agent_name.lower() in skill.agents
        if by_trigger or by_agent:
            out.append(skill)
    return out


def render(skills: list[Skill]) -> str:
    if not skills:
        return ""
    blocks = [f"### Skill: {s.name}\n{s.body}" for s in skills]
    return "## Skills (follow these exactly when relevant)\n" + "\n\n".join(blocks)
