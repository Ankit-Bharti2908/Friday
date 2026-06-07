"""Offline smoke test — no network, no API keys needed.

    uv run python -m tests.smoke

Covers: db (+vec round-trip, alert dedupe), policies, both graph modes,
history trim, prompt build, skills parsing/matching, agent registry tool
filtering, memory store/recall/forget with injected vectors, heartbeat
parsing, notify quiet-hours math.
"""
from __future__ import annotations

import asyncio
import struct
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from agents import registry
from agents.heartbeat import _parse_findings
from core import approval, db, memory, notify, prompts, settings, skills
from core.db import alert_already_sent, connect, init_db, mark_alert_sent
from core.graph import _reflection_note, _trim, build_graph


def test_policies() -> None:
    assert approval.requires_approval("gmail_send_email")
    assert approval.requires_approval("run_python")
    assert approval.requires_approval("totally_unknown_tool")  # default deny
    assert not approval.requires_approval("list_directory")
    assert not approval.requires_approval("remember")
    assert not approval.requires_approval("recall_memories")
    print("policies          OK")


def test_db_vec_alerts() -> None:
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
    finally:
        conn.close()
    assert not alert_already_sent("smoke:x")
    mark_alert_sent("smoke:x")
    assert alert_already_sent("smoke:x")
    print("db + vec + alerts OK")


def test_graphs_build() -> None:
    assert build_graph(MemorySaver(), tools=[]) is not None
    assert build_graph(None, tools=[], autonomous=True) is not None
    print("graphs compile    OK")


def test_trim_and_reflection() -> None:
    msgs = []
    for i in range(60):
        msgs.append(HumanMessage(f"q{i}"))
        msgs.append(AIMessage("", tool_calls=[{"name": "t", "args": {}, "id": f"c{i}", "type": "tool_call"}]))
        msgs.append(ToolMessage("r", tool_call_id=f"c{i}", name="t"))
        msgs.append(AIMessage(f"a{i}"))
    assert isinstance(_trim(msgs)[0], HumanMessage)

    err_msgs = [
        HumanMessage("do thing"),
        AIMessage("", tool_calls=[{"name": "x", "args": {}, "id": "1", "type": "tool_call"}]),
        ToolMessage("ERROR running x: boom", tool_call_id="1", name="x"),
        AIMessage("", tool_calls=[{"name": "x", "args": {}, "id": "2", "type": "tool_call"}]),
        ToolMessage("ERROR running x: boom again", tool_call_id="2", name="x"),
    ]
    assert "Reflection required" in _reflection_note(err_msgs)
    assert _reflection_note(msgs) == ""
    print("trim + reflection OK")


def test_prompt_and_skills() -> None:
    text = prompts.build_system_prompt([], extras=["## Extra\nhello"])
    assert "Friday" in text and "## Extra" in text

    all_skills = skills.load_all()
    assert {s.name for s in all_skills} >= {"email_style", "rca_summary", "daily_note_format"}
    hit = skills.match("write the RCA summary for yesterday's escalation", "general")
    assert any(s.name == "rca_summary" for s in hit)
    by_agent = skills.match("anything at all", "email")
    assert any(s.name == "email_style" for s in by_agent)
    rendered = skills.render(hit)
    assert "Skill: rca_summary" in rendered
    print("prompt + skills   OK")


def test_registry_filtering() -> None:
    fake = lambda name: SimpleNamespace(name=name, description="d")
    tools = [fake("gmail_search_emails"), fake("gmail_send_email"), fake("github_list_prs"),
             fake("remember"), fake("run_python")]
    email_tools = registry.filter_tools(registry.get("email"), tools)
    names = {t.name for t in email_tools}
    assert "gmail_send_email" in names and "github_list_prs" not in names
    assert "remember" in names  # ALWAYS_INCLUDE survives filtering
    # filter that matches nothing domain-specific -> falls back to all tools
    cal_tools = registry.filter_tools(registry.get("calendar"), tools)
    assert len(cal_tools) == len(tools)
    print("registry filter   OK")


def test_memory_roundtrip() -> None:
    dim = settings.MODELS["embeddings"]["dim"]
    v1 = [0.5] * dim
    v2 = [0.5] * (dim - 1) + [0.51]   # nearly identical -> dedupe
    v3 = [-0.5] * dim                  # opposite -> distinct

    async def flow():
        r1 = await memory.add_memory("Prefers uv over pip for Python projects", "preference", _vec=v1)
        assert r1.startswith("Remembered")
        r2 = await memory.add_memory("Prefers uv over pip for python projects.", "preference", _vec=v2)
        assert r2.startswith("Already known")
        await memory.add_memory("Working on Friday assistant project", "project", _vec=v3)
        hits = await memory.recall("python tooling preference", _vec=v1)
        assert any("uv over pip" in h for h in hits)
        gone = await memory.forget_matching("uv over pip")
        assert gone.startswith("Forgot")
        hits_after = await memory.recall("python tooling preference", _vec=v1)
        assert not any("uv over pip" in h for h in hits_after)

    asyncio.run(flow())
    md = (settings.MEMORY_DIR / "MEMORY.md").read_text(encoding="utf-8")
    assert "auto-generated" in md and "Friday assistant project" in md
    print("memory roundtrip  OK")


def test_heartbeat_parse_and_notify() -> None:
    text = 'Here you go:\n[{"key": "pr:friday#1", "message": "PR #1 awaits your review"}]'
    items = _parse_findings(text)
    assert items and items[0]["key"] == "pr:friday#1"
    assert _parse_findings("nothing actionable") == []
    assert _parse_findings("[]") == []

    tz = ZoneInfo(settings.TIMEZONE)
    assert notify.in_quiet_hours(datetime(2026, 6, 6, 23, 30, tzinfo=tz))
    assert notify.in_quiet_hours(datetime(2026, 6, 6, 6, 0, tzinfo=tz))
    assert not notify.in_quiet_hours(datetime(2026, 6, 6, 12, 0, tzinfo=tz))
    print("heartbeat+notify  OK")


if __name__ == "__main__":
    test_policies()
    test_db_vec_alerts()
    test_graphs_build()
    test_trim_and_reflection()
    test_prompt_and_skills()
    test_registry_filtering()
    test_memory_roundtrip()
    test_heartbeat_parse_and_notify()
    print("\nall smoke tests passed ✔")
