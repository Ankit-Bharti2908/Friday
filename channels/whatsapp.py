"""WhatsApp channel via the Baileys Node sidecar (bridge/whatsapp).

Friday connects as a WebSocket client to the sidecar on localhost. Only
messages from WHATSAPP_OWNER_JID are processed — everything else is dropped
at the door. Approvals degrade to text replies (yes / no / anything else =
revision feedback), since WhatsApp has no inline buttons here.

⚠ Unofficial WhatsApp libraries violate WhatsApp ToS — number-ban risk.
Run it on a secondary number, keep Telegram primary. The wa-auth/ session
directory is credential material: chmod 700, encrypted disk, never in git.
"""
from __future__ import annotations

import asyncio
import json
import logging

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from core import memory, settings
from core.graph import last_ai_text

log = logging.getLogger("friday.whatsapp")

WA_LIMIT = 3500
PENDING: dict[str, bool] = {}  # jid -> approval pending


async def run(graph) -> None:
    try:
        import websockets
    except ImportError:
        log.error("whatsapp channel needs `websockets` (uv sync) — not starting")
        return
    if not settings.WHATSAPP_OWNER_JID:
        log.error("WHATSAPP_OWNER_JID not set — not starting whatsapp channel")
        return

    url = settings.WHATSAPP_WS_URL
    while True:
        try:
            async with websockets.connect(url) as ws:
                log.info("connected to whatsapp bridge at %s", url)
                async for raw in ws:
                    try:
                        await _handle(graph, ws, json.loads(raw))
                    except Exception:
                        log.exception("whatsapp message handling failed")
        except (OSError, Exception) as exc:  # bridge down / restarting
            log.warning("bridge unavailable (%s) — retrying in 10s", exc)
            await asyncio.sleep(10)


async def _handle(graph, ws, msg: dict) -> None:
    jid, text = msg.get("from", ""), (msg.get("text") or "").strip()
    if jid != settings.WHATSAPP_OWNER_JID:
        log.warning("dropped message from non-owner jid=%s", jid)
        return
    if not text:
        return

    config = {"configurable": {"thread_id": f"wa:{jid}"}}

    if PENDING.get(jid):
        PENDING[jid] = False
        low = text.lower()
        if low in ("yes", "y", "approve", "ok", "haan"):
            payload = Command(resume={"decision": "approve"})
        elif low in ("no", "n", "reject", "nahi"):
            payload = Command(resume={"decision": "reject", "reason": ""})
        else:
            payload = Command(resume={"decision": "reject", "reason": text})
    else:
        payload = {"messages": [HumanMessage(text)]}

    result = await graph.ainvoke(payload, config)

    interrupts = result.get("__interrupt__")
    if interrupts:
        PENDING[jid] = True
        preview = interrupts[0].value.get("preview", "")
        await _send(ws, jid, f"Approval needed:\n\n{preview}\n\nReply: yes / no / or describe changes.")
        return

    await _send(ws, jid, last_ai_text(result) or "(no reply)")
    asyncio.create_task(memory.after_turn(result.get("messages", []), result.get("agent_name", "")))


async def _send(ws, jid: str, text: str) -> None:
    for i in range(0, len(text), WA_LIMIT):
        await ws.send(json.dumps({"to": jid, "text": text[i : i + WA_LIMIT]}))
