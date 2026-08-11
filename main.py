"""Friday entrypoint — one process, all phases.

    uv run python main.py

Boot: tracing -> db -> MCP tools (+ memory & sandbox tools) -> graphs
(interactive + autonomous) -> scheduler (briefing/heartbeat/notes)
-> Telegram polling -> optional WhatsApp bridge client.

Observability: start `uv run phoenix serve` in another terminal
(UI at http://localhost:6006). Disable with FRIDAY_TRACING=0.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from core import notify, scheduler, settings
from core.db import init_db
from core.fitness import fitness_tools
from core.graph import build_graph
from core.memory import memory_tools
from core.nutrition import nutrition_tools
from core.sandbox import sandbox_tools
from core.tools import load_mcp_tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("friday")


def setup_tracing() -> None:
    if not settings.TRACING_ENABLED:
        log.info("tracing disabled (FRIDAY_TRACING=0)")
        return
    try:
        from phoenix.otel import register

        register(project_name="friday", auto_instrument=True, batch=True)
        log.info("tracing -> Phoenix at http://localhost:6006")
    except Exception as exc:
        log.warning("tracing not active (%s) — run `uv run phoenix serve` for traces", exc)


async def amain() -> None:
    setup_tracing()
    init_db()

    for p in settings.validate():
        log.warning("config: %s", p)
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_OWNER_ID:
        log.error("Telegram not configured — fix .env, or use the CLI: uv run python -m channels.cli")
        return

    tools = await load_mcp_tools() + memory_tools() + sandbox_tools() + fitness_tools() + nutrition_tools()

    async with AsyncSqliteSaver.from_conn_string(str(settings.DB_PATH)) as saver:
        graph = build_graph(saver, tools)
        graph._friday_tools = tools  # for /status
        autonomous_graph = build_graph(None, tools, autonomous=True)

        from channels.telegram import build_application

        app = build_application(graph)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # windows
                pass

        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        notify.configure(app.bot, settings.TELEGRAM_OWNER_ID)
        sched = scheduler.start(graph, autonomous_graph)

        wa_task = None
        if settings.WHATSAPP_ENABLED:
            from channels import whatsapp

            wa_task = asyncio.create_task(whatsapp.run(graph))
            log.info("whatsapp channel enabled (bridge expected at %s)", settings.WHATSAPP_WS_URL)

        log.info("Friday is up — %d tools, owner %s", len(tools), settings.TELEGRAM_OWNER_ID)
        await stop.wait()

        log.info("shutting down…")
        if wa_task:
            wa_task.cancel()
        sched.shutdown(wait=False)
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
