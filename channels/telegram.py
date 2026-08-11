"""Telegram channel: long polling (no webhook, no public IP needed).

Security: hard allowlist — anyone who isn't TELEGRAM_OWNER_ID is silently
ignored before any processing.

Approvals: graph interrupt -> [Approve]/[Reject]/[Edit] inline buttons.
Edit = send free-text feedback; the agent revises and asks again.

Voice notes: transcribed locally with faster-whisper if installed
(`uv sync --extra voice`), then handled like text.

After each completed turn, a fire-and-forget memory hook logs the turn to
today's note and extracts durable memories (fast tier).
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.types import Command
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from core import memory, settings
from core.graph import last_ai_text

log = logging.getLogger("friday.telegram")

TG_LIMIT = 4096

_APPROVAL_KB = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✅ Approve", callback_data="approve"),
            InlineKeyboardButton("❌ Reject", callback_data="reject"),
            InlineKeyboardButton("✏️ Edit", callback_data="edit"),
        ]
    ]
)


def build_application(graph) -> Application:
    app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    app.bot_data["graph"] = graph
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("new", _cmd_new))
    app.add_handler(CommandHandler("status", _cmd_status))
    app.add_handler(CommandHandler("briefing", _cmd_briefing))
    app.add_handler(CallbackQueryHandler(_on_approval_button))
    app.add_handler(MessageHandler(filters.VOICE, _on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    return app


# ----------------------------------------------------------------- guards
def _is_owner(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == settings.TELEGRAM_OWNER_ID)


def _thread_id(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str:
    return context.chat_data.setdefault("thread_id", f"tg:{chat_id}")


# ---------------------------------------------------------------- commands
async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    await update.message.reply_text(
        "Friday online. Talk to me — text or voice.\n/new resets the thread, /briefing on demand, /status for vitals."
    )


async def _cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    context.chat_data["thread_id"] = f"tg:{update.effective_chat.id}:{uuid.uuid4().hex[:8]}"
    context.chat_data.pop("awaiting_edit", None)
    await update.message.reply_text("Fresh thread started.")


async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    graph = context.bot_data["graph"]
    n_tools = len(getattr(graph, "_friday_tools", []) or [])
    await update.message.reply_text(
        f"thread: {_thread_id(context, update.effective_chat.id)}\ntools: {n_tools}\ndb: {settings.DB_PATH.name}"
    )


async def _cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    from agents import briefing

    await update.message.reply_text("On it — assembling your briefing…")
    await briefing.run(context.bot_data["graph"])


# ---------------------------------------------------------------- messages
async def _on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        log.warning("ignored message from non-owner user_id=%s", getattr(update.effective_user, "id", "?"))
        return
    await _handle_text(update, context, update.message.text)


async def _on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    text = await _transcribe_voice(update, context)
    if text is None:
        await update.message.reply_text(
            "Voice support needs faster-whisper: `uv sync --extra voice`, then restart me."
        )
        return
    if not text.strip():
        await update.message.reply_text("Couldn't make out any speech in that.")
        return
    await update.message.reply_text(f"🎤 “{text}”")
    await _handle_text(update, context, text)


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    chat_id = update.effective_chat.id

    # Edit flow step 2: this message is feedback for a pending approval.
    if context.chat_data.pop("awaiting_edit", False):
        await _resume(update, context, {"decision": "reject", "reason": text})
        return

    config = {"configurable": {"thread_id": _thread_id(context, chat_id)}}
    await _run_graph(update, context, {"messages": [HumanMessage(text)]}, config)


async def _on_approval_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    query = update.callback_query
    await query.answer()

    if query.data == "approve":
        await query.edit_message_reply_markup(None)
        await query.message.reply_text("Approved — executing.")
        await _resume(update, context, {"decision": "approve"})
    elif query.data == "reject":
        await query.edit_message_reply_markup(None)
        await _resume(update, context, {"decision": "reject", "reason": ""})
    elif query.data == "edit":
        context.chat_data["awaiting_edit"] = True
        await query.message.reply_text("Send your changes as a message — I'll revise and ask again.")


# ---------------------------------------------------------------- execution
async def _resume(update: Update, context: ContextTypes.DEFAULT_TYPE, decision: dict) -> None:
    chat_id = update.effective_chat.id
    config = {"configurable": {"thread_id": _thread_id(context, chat_id)}}
    await _run_graph(update, context, Command(resume=decision), config)


async def _run_graph(update: Update, context: ContextTypes.DEFAULT_TYPE, payload, config) -> None:
    graph = context.bot_data["graph"]
    chat_id = update.effective_chat.id
    typing = asyncio.create_task(_keep_typing(context, chat_id))
    try:
        result = await graph.ainvoke(payload, config)
    except Exception as exc:
        log.exception("graph error")
        await context.bot.send_message(chat_id, f"Something broke: {type(exc).__name__}: {exc}")
        return
    finally:
        typing.cancel()

    interrupts = result.get("__interrupt__")
    if interrupts:
        req = interrupts[0].value
        preview = req.get("preview", str(req))
        await _send_long(context, chat_id, f"Approval needed:\n\n{preview}", reply_markup=_APPROVAL_KB)
        return

    await _send_long(context, chat_id, last_ai_text(result) or "(no reply)")
    # Memory hook: never blocks the reply, never raises.
    asyncio.create_task(memory.after_turn(result.get("messages", []), result.get("agent_name", "")))


async def _keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    try:
        while True:
            await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(4.5)
    except asyncio.CancelledError:
        pass


async def _send_long(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, **kwargs) -> None:
    text = text or "(empty reply)"
    chunks = [text[i : i + TG_LIMIT] for i in range(0, len(text), TG_LIMIT)]
    for i, chunk in enumerate(chunks):
        # plain text on purpose: Telegram's markdown parser rejects too much
        await context.bot.send_message(chat_id, chunk, **(kwargs if i == len(chunks) - 1 else {}))


# ------------------------------------------------------------------- voice
_whisper_model = None


def _load_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel  # raises ImportError if extra not installed

        _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
    return _whisper_model


def _transcribe_file(path: str) -> str:
    model = _load_whisper()
    segments, _info = model.transcribe(path, vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()


async def _transcribe_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    try:
        _load_whisper()
    except ImportError:
        return None
    voice_file = await update.message.voice.get_file()
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "note.oga")
        await voice_file.download_to_drive(path)
        return await asyncio.to_thread(_transcribe_file, path)
