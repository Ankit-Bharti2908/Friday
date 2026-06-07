"""CLI channel: a dev REPL against the same graph the bot uses.

Run:  uv run python -m channels.cli
Approvals render as a y/n/feedback prompt. Conversation persists in
friday.db under thread 'cli:default' (/new to rotate). The post-turn
memory hook runs here too, so the CLI also teaches Friday about you.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from core import memory, settings
from core.db import init_db
from core.graph import build_graph, last_ai_text
from core.memory import memory_tools
from core.sandbox import sandbox_tools
from core.tools import load_mcp_tools

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


async def main() -> None:
    init_db()
    for problem in settings.validate():
        print(f"[warn] {problem}")

    tools = await load_mcp_tools() + memory_tools() + sandbox_tools()
    print(f"[friday] {len(tools)} tools loaded. /new = fresh thread, /quit = exit.\n")

    async with AsyncSqliteSaver.from_conn_string(str(settings.DB_PATH)) as saver:
        graph = build_graph(saver, tools)
        thread = "cli:default"

        while True:
            try:
                user_input = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user_input:
                continue
            if user_input == "/quit":
                break
            if user_input == "/new":
                thread = f"cli:{uuid.uuid4().hex[:8]}"
                print(f"[friday] new thread {thread}")
                continue

            config = {"configurable": {"thread_id": thread}}
            payload: object = {"messages": [HumanMessage(user_input)]}

            # Loop because one turn can hit several approval pauses.
            while True:
                result = await graph.ainvoke(payload, config)
                interrupts = result.get("__interrupt__")
                if not interrupts:
                    print(f"\nfriday> {last_ai_text(result) or '(no reply)'}\n")
                    asyncio.get_running_loop().create_task(memory.after_turn(result.get("messages", [])))
                    await asyncio.sleep(0)  # let the hook start
                    break

                req = interrupts[0].value
                print("\n[approval needed]\n" + req.get("preview", str(req)))
                answer = input("approve? [y]es / [n]o / or type feedback: ").strip()
                if answer.lower() in ("y", "yes"):
                    payload = Command(resume={"decision": "approve"})
                elif answer.lower() in ("n", "no", ""):
                    payload = Command(resume={"decision": "reject", "reason": ""})
                else:
                    payload = Command(resume={"decision": "reject", "reason": answer})


if __name__ == "__main__":
    asyncio.run(main())
