"""SQLite layer: one file (friday.db) holds everything.

- LangGraph's AsyncSqliteSaver creates its own checkpoint tables here.
- We add: memories (+ vec_memories, cosine distance), tool_audit, alerts_sent.

WAL mode lets the checkpointer's aiosqlite connection and our sync
connections coexist in one process.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from core import settings

log = logging.getLogger("friday.db")

_EMBED_DIM = settings.MODELS.get("embeddings", {}).get("dim", 768)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories(
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('fact','preference','project','event')),
    text        TEXT NOT NULL,
    source      TEXT,
    deleted     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tool_audit(
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    tool        TEXT NOT NULL,
    decision    TEXT NOT NULL,          -- auto_allowed | approved | rejected | blocked | error
    args_preview TEXT
);

CREATE TABLE IF NOT EXISTS alerts_sent(
    item_key    TEXT PRIMARY KEY,
    ts          TEXT NOT NULL
);
"""

_VEC_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0(
    embedding float[{_EMBED_DIM}] distance_metric=cosine
);
"""


def connect() -> sqlite3.Connection:
    """Open a sync connection with WAL + sqlite-vec loaded."""
    conn = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception as exc:  # pragma: no cover - environment dependent
        log.warning("sqlite-vec unavailable (%s) — vector recall disabled", exc)
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        try:
            conn.executescript(_VEC_SCHEMA)
        except sqlite3.OperationalError as exc:
            log.warning("vec_memories not created (%s)", exc)
        conn.commit()
    finally:
        conn.close()
    log.info("database ready at %s", settings.DB_PATH)


def log_tool_audit(tool: str, decision: str, args: Any = None) -> None:
    """Append-only audit trail of every gated tool decision."""
    preview = json.dumps(args, default=str)[:500] if args is not None else None
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO tool_audit(ts, tool, decision, args_preview) VALUES (?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), tool, decision, preview),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------- heartbeat dedupe
def alert_already_sent(item_key: str) -> bool:
    conn = connect()
    try:
        row = conn.execute("SELECT 1 FROM alerts_sent WHERE item_key = ?", (item_key,)).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_alert_sent(item_key: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO alerts_sent(item_key, ts) VALUES (?, ?)",
            (item_key, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
