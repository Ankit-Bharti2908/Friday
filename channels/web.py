"""Optional web UI (Chainlit): `uv sync --extra web`, then
    uv run chainlit run channels/web.py

Same brain, ephemeral per-session memory checkpointing (web sessions are
scratchpads; Telegram/CLI threads persist in friday.db).
"""
from __future__ import annotations

import uuid

try:
    import chainlit as cl
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Web UI needs chainlit: uv sync --extra web") from exc

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from core.db import init_db
from core.graph import build_graph, last_ai_text
from core.tools import load_mcp_tools


@cl.on_chat_start
async def on_start() -> None:
    init_db()
    tools = await load_mcp_tools()
    graph = build_graph(MemorySaver(), tools)
    cl.user_session.set("graph", graph)
    cl.user_session.set("config", {"configurable": {"thread_id": f"web:{uuid.uuid4().hex[:8]}"}})
    await cl.Message(content=f"Friday web — {len(tools)} tools loaded.").send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    graph = cl.user_session.get("graph")
    config = cl.user_session.get("config")
    payload = {"messages": [HumanMessage(message.content)]}

    while True:
        result = await graph.ainvoke(payload, config)
        interrupts = result.get("__interrupt__")
        if not interrupts:
            await cl.Message(content=last_ai_text(result) or "(no reply)").send()
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
