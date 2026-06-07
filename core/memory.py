"""Long-term memory: SQLite (+sqlite-vec) is the source of truth,
memory/MEMORY.md is an auto-generated human-readable mirror.

Pieces:
  add_memory / recall / forget   -> core operations (embedding-deduped)
  memory_tools()                 -> LangChain tools the agent can call
                                    (remember / recall_memories / forget)
  after_turn(messages)           -> fire-and-forget post-turn hook: logs the
                                    turn to today's note and extracts durable
                                    memories with the `fast` tier
  consolidate_today()            -> nightly: compress today's note to bullets

Everything degrades gracefully offline: no embeddings -> store without
vector, recall falls back to keyword LIKE search.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from core import db, llm, settings

log = logging.getLogger("friday.memory")

KINDS = ("fact", "preference", "project", "event")
DUP_COSINE_DISTANCE = 0.08  # distance < this == "we already know that"
RECALL_MAX_DISTANCE = 0.55  # ignore matches less related than this


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.TIMEZONE))


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


async def _embed_safe(text: str) -> list[float] | None:
    try:
        return await llm.aembed(text)
    except Exception as exc:
        log.warning("embedding unavailable (%s) — storing/searching without vectors", exc)
        return None


# ----------------------------------------------------------------- core ops
async def add_memory(
    text: str, kind: str = "fact", source: str | None = None, _vec: list[float] | None = None
) -> str:
    text = " ".join(text.split()).strip()
    if not text:
        return "Nothing to remember."
    if kind not in KINDS:
        kind = "fact"

    vec = _vec if _vec is not None else await _embed_safe(text)
    conn = db.connect()
    try:
        if vec is not None:
            try:
                row = conn.execute(
                    "SELECT rowid, distance FROM vec_memories WHERE embedding MATCH ? AND k = 1",
                    (_pack(vec),),
                ).fetchone()
                if row and row[1] is not None and row[1] < DUP_COSINE_DISTANCE:
                    existing = conn.execute(
                        "SELECT text FROM memories WHERE id = ? AND deleted = 0", (row[0],)
                    ).fetchone()
                    if existing:
                        return f"Already known: “{existing[0]}”"
            except Exception as exc:
                log.debug("dedupe check skipped (%s)", exc)

        cur = conn.execute(
            "INSERT INTO memories(created_at, kind, text, source) VALUES (?,?,?,?)",
            (_now().isoformat(), kind, text, source),
        )
        mem_id = cur.lastrowid
        if vec is not None:
            try:
                conn.execute(
                    "INSERT INTO vec_memories(rowid, embedding) VALUES (?, ?)", (mem_id, _pack(vec))
                )
            except Exception as exc:
                log.debug("vector insert skipped (%s)", exc)
        conn.commit()
        _rewrite_markdown(conn)
        return f"Remembered ({kind}): {text}"
    finally:
        conn.close()


async def recall(query: str, k: int = 5, _vec: list[float] | None = None) -> list[str]:
    vec = _vec if _vec is not None else await _embed_safe(query)
    conn = db.connect()
    try:
        if vec is not None:
            try:
                rows = conn.execute(
                    """
                    SELECT m.kind, m.text, v.distance
                    FROM vec_memories v JOIN memories m ON m.id = v.rowid
                    WHERE v.embedding MATCH ? AND k = ? AND m.deleted = 0
                    ORDER BY v.distance
                    """,
                    (_pack(vec), k * 2),
                ).fetchall()
                hits = [f"[{r[0]}] {r[1]}" for r in rows if r[2] is None or r[2] <= RECALL_MAX_DISTANCE]
                if hits:
                    return hits[:k]
            except Exception as exc:
                log.debug("vector recall failed (%s) — falling back to keywords", exc)

        words = [w for w in query.lower().split() if len(w) > 3][:5]
        if not words:
            return []
        clause = " OR ".join("lower(text) LIKE ?" for _ in words)
        rows = conn.execute(
            f"SELECT kind, text FROM memories WHERE deleted = 0 AND ({clause}) "
            "ORDER BY created_at DESC LIMIT ?",
            [f"%{w}%" for w in words] + [k],
        ).fetchall()
        return [f"[{r[0]}] {r[1]}" for r in rows]
    finally:
        conn.close()


async def forget_matching(query: str) -> str:
    vec = await _embed_safe(query)
    conn = db.connect()
    try:
        ids: list[tuple[int, str]] = []
        if vec is not None:
            try:
                rows = conn.execute(
                    """
                    SELECT m.id, m.text FROM vec_memories v JOIN memories m ON m.id = v.rowid
                    WHERE v.embedding MATCH ? AND k = 3 AND m.deleted = 0 AND v.distance <= ?
                    """,
                    (_pack(vec), RECALL_MAX_DISTANCE),
                ).fetchall()
                ids = [(r[0], r[1]) for r in rows]
            except Exception:
                pass
        if not ids:
            rows = conn.execute(
                "SELECT id, text FROM memories WHERE deleted = 0 AND lower(text) LIKE ? LIMIT 3",
                (f"%{query.lower()}%",),
            ).fetchall()
            ids = [(r[0], r[1]) for r in rows]
        if not ids:
            return "No matching memory found."
        for mem_id, _ in ids:
            conn.execute("UPDATE memories SET deleted = 1 WHERE id = ?", (mem_id,))
            try:
                conn.execute("DELETE FROM vec_memories WHERE rowid = ?", (mem_id,))
            except Exception:
                pass
        conn.commit()
        _rewrite_markdown(conn)
        return "Forgot: " + "; ".join(t for _, t in ids)
    finally:
        conn.close()


def _rewrite_markdown(conn) -> None:
    """Regenerate memory/MEMORY.md from the DB (the file is a mirror)."""
    lines = [
        "# MEMORY — Friday's long-term notes",
        "<!-- auto-generated from friday.db — edit via 'remember/forget', not by hand -->",
        "",
    ]
    for kind in KINDS:
        rows = conn.execute(
            "SELECT created_at, text FROM memories WHERE kind = ? AND deleted = 0 ORDER BY created_at",
            (kind,),
        ).fetchall()
        lines.append(f"## {kind.capitalize()}s")
        lines += [f"- {r[1]}  _({r[0][:10]})_" for r in rows] or ["(none yet)"]
        lines.append("")
    (settings.MEMORY_DIR / "MEMORY.md").write_text("\n".join(lines), encoding="utf-8")


# -------------------------------------------------------------- daily notes
def _note_path(day: datetime) -> Any:
    return settings.NOTES_DIR / f"{day.strftime('%Y-%m-%d')}.md"


def log_turn(user_text: str) -> None:
    now = _now()
    path = _note_path(now)
    if not path.exists():
        path.write_text(f"# {now.strftime('%Y-%m-%d')}\n\n## Log\n", encoding="utf-8")
    snippet = " ".join(user_text.split())[:140]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"- {now.strftime('%H:%M')} {snippet}\n")


def read_note(days_ago: int = 0) -> str:
    path = _note_path(_now() - timedelta(days=days_ago))
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


async def consolidate_today() -> None:
    """Nightly: compress today's raw log into a short summary at the top."""
    raw = read_note(0)
    if not raw or raw.count("\n") < 6:
        return
    try:
        resp = await llm.acomplete(
            "fast",
            [
                {
                    "role": "user",
                    "content": "Summarize this daily activity log into 5-8 terse bullets: "
                    "what was worked on, decisions made, open loops. Output only bullets.\n\n" + raw,
                }
            ],
        )
        summary = (resp.choices[0].message.content or "").strip()
        if summary:
            day = _now().strftime("%Y-%m-%d")
            _note_path(_now()).write_text(
                f"# {day}\n\n## Summary\n{summary}\n\n{raw.split('## Log', 1)[-1] and '## Log' + raw.split('## Log', 1)[-1]}",
                encoding="utf-8",
            )
            log.info("daily note consolidated")
    except Exception as exc:
        log.warning("consolidation skipped (%s)", exc)


# ------------------------------------------------- post-turn extraction hook
class _Candidate(BaseModel):
    text: str
    kind: str = "fact"
    confidence: float = Field(ge=0, le=1)


class _Candidates(BaseModel):
    items: list[_Candidate] = []


_EXTRACT_PROMPT = """You maintain long-term memory for a personal assistant.
From the exchange below, extract ONLY durable facts worth remembering for months
(stable preferences, personal/work facts, ongoing projects, future events).
Do NOT extract: one-off requests, transient tasks, anything already obvious.
kind must be one of: fact, preference, project, event. Usually 0 items is correct.

Exchange:
{exchange}"""


async def after_turn(messages: list[Any]) -> None:
    """Fire-and-forget after a completed turn. Never raises."""
    try:
        user_text = next(
            (m.content for m in reversed(messages) if isinstance(m, HumanMessage)), None
        )
        ai_text = next(
            (m.content for m in reversed(messages) if isinstance(m, AIMessage) and m.content), ""
        )
        if not user_text or not isinstance(user_text, str):
            return
        log_turn(user_text)

        exchange = f"USER: {user_text[:800]}\nASSISTANT: {str(ai_text)[:800]}"
        cands = await llm.astructured(
            "fast", [{"role": "user", "content": _EXTRACT_PROMPT.format(exchange=exchange)}], _Candidates
        )
        for c in cands.items:
            if c.confidence >= 0.8:
                result = await add_memory(c.text, c.kind, source="auto")
                log.info("memory: %s", result)
    except Exception as exc:
        log.debug("after_turn hook skipped (%s)", exc)


# ------------------------------------------------------------- agent tools
@tool
async def remember(text: str, kind: str = "fact") -> str:
    """Save a durable long-term memory about the user. kind: fact | preference | project | event."""
    return await add_memory(text, kind, source="explicit")


@tool
async def recall_memories(query: str) -> str:
    """Search Friday's long-term memories about the user."""
    hits = await recall(query, k=6)
    return "\n".join(hits) if hits else "No relevant memories."


@tool
async def forget(query: str) -> str:
    """Delete long-term memories matching the query (use when the user says to forget something)."""
    return await forget_matching(query)


def memory_tools() -> list:
    return [remember, recall_memories, forget]
