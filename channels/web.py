"""Optional web UI (Chainlit): `uv sync --extra web`, then
    uv run chainlit run channels/web.py

A first-class connector, equal to Telegram and CLI: same graph, same tools,
and the SAME persistent friday.db. Conversations are checkpointed to SQLite
(thread 'web:default', survives restarts) and every completed turn runs the
post-turn memory hook — so what you tell Friday on the web teaches the same
long-term memory the bot and CLI read. Run it alongside `main.py`; both share
friday.db.
"""
from __future__ import annotations

import asyncio

try:
    import chainlit as cl
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Web UI needs chainlit: uv sync --extra web") from exc

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from core import memory, settings
from core.db import init_db
from core.fitness import fitness_tools
from core.graph import build_graph, last_ai_text
from core.memory import memory_tools
from core.sandbox import sandbox_tools
from core.tools import load_mcp_tools

WEB_THREAD = "web:default"  # stable thread → web conversation persists across sessions

# Process-wide singletons. The graph and its SQLite checkpointer are built once
# and shared by every web session, so the web UI reads/writes the same friday.db
# as Telegram and CLI. The saver context manager is entered once and kept open
# for the process lifetime (Chainlit owns the event loop, so we can't wrap the
# app in `async with`); the module-level refs keep it from being garbage-collected.
_graph = None
_saver_cm = None
_init_lock = asyncio.Lock()


async def _get_graph():
    global _graph, _saver_cm
    if _graph is not None:
        return _graph
    async with _init_lock:
        if _graph is None:
            init_db()
            tools = await load_mcp_tools() + memory_tools() + sandbox_tools() + fitness_tools()
            _saver_cm = AsyncSqliteSaver.from_conn_string(str(settings.DB_PATH))
            saver = await _saver_cm.__aenter__()
            _graph = build_graph(saver, tools)
            _graph._friday_tools = tools
    return _graph


@cl.on_chat_start
async def on_start() -> None:
    graph = await _get_graph()
    n_tools = len(getattr(graph, "_friday_tools", []) or [])
    await cl.Message(content=f"Friday web — {n_tools} tools, shared memory (friday.db).").send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    graph = await _get_graph()
    config = {"configurable": {"thread_id": WEB_THREAD}}
    payload = {"messages": [HumanMessage(message.content)]}

    while True:
        result = await graph.ainvoke(payload, config)
        interrupts = result.get("__interrupt__")
        if not interrupts:
            await cl.Message(content=last_ai_text(result) or "(no reply)").send()
            # Same fire-and-forget memory hook Telegram & CLI use — teach the shared brain.
            asyncio.create_task(memory.after_turn(result.get("messages", [])))
            return

        preview = interrupts[0].value.get("preview", "")
        action = await cl.AskActionMessage(
            content=f"Approval needed:\n\n```\n{preview}\n```",
            actions=[
                cl.Action(name="approve", payload={"d": "approve"}, label="✅ Approve"),
                cl.Action(name="reject", payload={"d": "reject"}, label="❌ Reject"),
            ],
            timeout=300,
        ).send()
        decision = (action or {}).get("payload", {}).get("d", "reject")
        payload = Command(resume={"decision": decision, "reason": ""})
