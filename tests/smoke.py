"""Offline smoke test — no network, no API keys needed.

    uv run python -m tests.smoke

Verifies: db init (+ vector round-trip), policy matching, prompt build,
graph compiles, history trimming never orphans tool messages.
"""
from __future__ import annotations

import asyncio
import struct

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from core import approval, prompts, settings
from core.db import connect, init_db
from core.graph import _trim, build_graph


def test_policies() -> None:
    assert approval.requires_approval("gmail_send_email")
    assert approval.requires_approval("delete_file")
    assert approval.requires_approval("totally_unknown_tool")  # default deny
    assert not approval.requires_approval("list_directory")
    assert not approval.requires_approval("search_emails")
    print("policies        OK")


def test_db_and_vec() -> None:
    init_db()
    conn = connect()
    try:
        dim = settings.MODELS["embeddings"]["dim"]
        vec = struct.pack(f"{dim}f", *([0.1] * dim))
        conn.execute("INSERT INTO vec_memories(rowid, embedding) VALUES (?, ?)", (999999, vec))
        row = conn.execute(
            "SELECT rowid, distance FROM vec_memories WHERE embedding MATCH ? AND k = 1", (vec,)
        ).fetchone()
        assert row and row[0] == 999999
        conn.execute("DELETE FROM vec_memories WHERE rowid = 999999")
        conn.commit()
        print("db + sqlite-vec OK")
    finally:
        conn.close()


def test_graph_builds() -> None:
    graph = build_graph(MemorySaver(), tools=[])
    assert graph is not None
    print("graph compile   OK")


def test_trim_keeps_tool_pairs() -> None:
    msgs = []
    for i in range(60):
        msgs.append(HumanMessage(f"q{i}"))
        msgs.append(AIMessage("", tool_calls=[{"name": "t", "args": {}, "id": f"c{i}", "type": "tool_call"}]))
        msgs.append(ToolMessage("r", tool_call_id=f"c{i}", name="t"))
        msgs.append(AIMessage(f"a{i}"))
    trimmed = _trim(msgs)
    assert isinstance(trimmed[0], HumanMessage), "trim must cut at a human boundary"
    print("history trim    OK")


def test_prompt_builds() -> None:
    text = prompts.build_system_prompt([])
    assert "Friday" in text and "Now" in text
    print("system prompt   OK")


if __name__ == "__main__":
    test_policies()
    test_db_and_vec()
    test_graph_builds()
    test_trim_keeps_tool_pairs()
    test_prompt_builds()
    print("\nall smoke tests passed ✔")
