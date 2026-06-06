"""Friday entrypoint — one process.

    uv run python main.py

Boot order: tracing -> db -> MCP tools -> graph (SQLite-checkpointed)
-> Telegram long polling. Ctrl+C shuts down cleanly.

Observability: start `uv run phoenix serve` in another terminal first
(UI at http://localhost:6006). If Phoenix isn't running, Friday still
works — traces just have nowhere to land. Disable with FRIDAY_TRACING=0.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from core import settings
from core.db import init_db
from core.graph import build_graph
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

    problems = settings.validate()
    for p in problems:
        log.warning("config: %s", p)
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_OWNER_ID:
        log.error("Telegram not configured — fix .env, or use the CLI: uv run python -m channels.cli")
        return

    tools = await load_mcp_tools()

    async with AsyncSqliteSaver.from_conn_string(str(settings.DB_PATH)) as saver:
        graph = build_graph(saver, tools)
        graph._friday_tools = tools  # for /status

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
        log.info("Friday is up — %d tools, talking to owner %s", len(tools), settings.TELEGRAM_OWNER_ID)

        # Phase 2 will start the APScheduler here (briefing, nightly notes).
        await stop.wait()

        log.info("shutting down…")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
