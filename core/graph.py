"""The agent graph (full build):

  route -> agent <-> (tool_gate -> tools) -> END

route      classifies intent (fast tier) + recalls memories + matches skills,
           picking the agent profile for this turn.
agent      LLM on the profile's tier, sees SOUL/USER + memories + skills +
           the profile's tool subset. Reflects after repeated tool failures.
tool_gate  policy check. Risky calls: interrupt() for human approval —
           unless the graph was built autonomous=True (background jobs),
           where risky calls are auto-rejected instead of pausing.
tools      executes approved calls with timeouts + output truncation.

Checkpointed in friday.db: conversations AND pending approvals survive
restarts.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    ToolMessage,
    convert_to_openai_messages,
)
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from agents import registry
from core import approval, llm, memory, prompts, router, skills
from core.db import log_tool_audit

log = logging.getLogger("friday.graph")

MAX_HISTORY_MESSAGES = 40      # what we *send* to the model (full history stays in the checkpoint)
MAX_TOOL_RESULT_CHARS = 8000   # protect the context window from giant tool outputs
MAX_AGENT_LOOPS = 12           # circuit breaker for runaway tool loops


class FridayState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    agent_name: str
    context_block: str   # recalled memories + matched skills for this turn
    gate_decision: str   # "approved" | "rejected" | ""
    loops: int


def build_graph(checkpointer: Any, tools: list[BaseTool], *, autonomous: bool = False):
    tool_map: dict[str, BaseTool] = {t.name: t for t in tools}
    schema_cache: dict[str, list[dict]] = {}

    def _schemas(profile) -> list[dict] | None:
        if profile.name not in schema_cache:
            ptools = registry.filter_tools(profile, tools)
            schema_cache[profile.name] = [convert_to_openai_tool(t) for t in ptools]
        return schema_cache[profile.name] or None

    # ------------------------------------------------------------------ route
    async def route(state: FridayState) -> dict[str, Any]:
        user_text = next(
            (m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), ""
        )
        user_text = user_text if isinstance(user_text, str) else str(user_text)

        if autonomous:
            profile_name = "general"
            recalled: list[str] = []
            route_info = "autonomous"
        else:
            context = _recent_context(state["messages"])
            (_, route_obj), recalled = await asyncio.gather(
                router.classify(user_text, context),
                _recall_safe(user_text),
            )
            prev = state.get("agent_name", "")  # checkpointed from the previous turn
            profile_name = router.resolve_profile(route_obj, prev, user_text)
            route_info = (
                f"intent={route_obj.intent} conf={route_obj.confidence:.2f} prev={prev or '-'}"
                if route_obj
                else f"intent=? prev={prev or '-'}"
            )

        matched = skills.match(user_text, profile_name)
        parts = []
        if recalled:
            parts.append(
                "## Memories (things you know about the user — use naturally, never recite)\n"
                + "\n".join(f"- {m}" for m in recalled)
            )
        if matched:
            parts.append(skills.render(matched))

        log.info("route -> %s (%s; skills: %s)", profile_name, route_info,
                 [s.name for s in matched] or "-")
        return {
            "agent_name": profile_name,
            "context_block": "\n\n".join(parts),
            "loops": 0,
            "gate_decision": "",
        }

    # ------------------------------------------------------------------ agent
    async def agent(state: FridayState) -> dict[str, Any]:
        loops = state.get("loops", 0)
        if loops >= MAX_AGENT_LOOPS:
            return {
                "messages": [AIMessage(content=(
                    "I've hit my tool-step limit for this request, so I'm pausing before "
                    "I spin. Say 'continue' and I'll pick up where I left off, or tell "
                    "me what to change."
                ))],
                "loops": 0,
            }

        profile = registry.get(state.get("agent_name") or "general")
        ptools = registry.filter_tools(profile, tools)
        tier = "fast" if autonomous else profile.tier

        extras = [profile.instructions, state.get("context_block", "")]
        if autonomous:
            extras.append(
                "## Background mode\nYou are running unattended. Use READ-ONLY tools only; "
                "any write/send action will be auto-rejected. Be terse."
            )
        if loops >= MAX_AGENT_LOOPS - 4:
            extras.append(
                f"## Wrap up\n{MAX_AGENT_LOOPS - loops} tool steps remain this turn. "
                "Stop exploring — deliver your answer in text (plus at most one write call)."
            )
        reflection = _reflection_note(state["messages"])
        if reflection:
            extras.append(reflection)

        system = {"role": "system", "content": prompts.build_system_prompt(ptools, extras=extras)}
        history = convert_to_openai_messages(_trim(state["messages"]))
        resp = await llm.acomplete(tier, [system] + history, tools=_schemas(profile))
        msg = resp.choices[0].message

        tool_calls = [
            {
                "name": tc.function.name,
                "args": _safe_json(tc.function.arguments),
                "id": tc.id,
                "type": "tool_call",
            }
            for tc in (msg.tool_calls or [])
        ]
        ai = AIMessage(content=msg.content or "", tool_calls=tool_calls)
        return {"messages": [ai], "loops": loops + 1}

    # -------------------------------------------------------------- tool_gate
    def tool_gate(state: FridayState) -> dict[str, Any]:
        last = state["messages"][-1]
        calls = list(getattr(last, "tool_calls", []) or [])
        risky = [c for c in calls if approval.requires_approval(c["name"])]

        if not risky:
            for c in calls:
                log_tool_audit(c["name"], "auto_allowed", c["args"])
            return {"gate_decision": "approved"}

        if autonomous:
            for c in calls:
                log_tool_audit(c["name"], "rejected_autonomous", c["args"])
            rejections = [
                ToolMessage(
                    content="ACTION NOT EXECUTED — background mode allows read-only tools. "
                    "Report the finding instead of acting on it.",
                    tool_call_id=c["id"],
                    name=c["name"],
                )
                for c in calls
            ]
            return {"gate_decision": "rejected", "messages": rejections}

        # Pause for the human. On resume this node re-runs and interrupt()
        # returns the decision payload.
        decision = interrupt(
            {
                "type": "approval_request",
                "preview": approval.render_preview(risky),
                "actions": [{"tool": c["name"], "args": c["args"]} for c in risky],
            }
        )

        if isinstance(decision, dict) and decision.get("decision") == "approve":
            for c in calls:
                log_tool_audit(c["name"], "approved" if c in risky else "auto_allowed", c["args"])
            return {"gate_decision": "approved"}

        reason = (decision or {}).get("reason", "") if isinstance(decision, dict) else ""
        feedback = (
            f"User rejected this action. {('Feedback: ' + reason) if reason else 'No reason given.'} "
            "Do NOT retry unchanged — revise per feedback or ask what to do."
        )
        rejections = [
            ToolMessage(content=f"ACTION NOT EXECUTED — {feedback}", tool_call_id=c["id"], name=c["name"])
            for c in calls
        ]
        for c in calls:
            log_tool_audit(c["name"], "rejected", c["args"])
        return {"gate_decision": "rejected", "messages": rejections}

    # ------------------------------------------------------------------ tools
    async def run_tools(state: FridayState) -> dict[str, Any]:
        last_ai = next(m for m in reversed(state["messages"]) if isinstance(m, AIMessage))
        prior = _prior_tool_results(state["messages"])
        results: list[ToolMessage] = []
        for call in last_ai.tool_calls:
            tool = tool_map.get(call["name"])
            if tool is None:
                content = f"ERROR: tool '{call['name']}' does not exist."
                log_tool_audit(call["name"], "error", call["args"])
            else:
                try:
                    out = await asyncio.wait_for(tool.ainvoke(call["args"]), timeout=120)
                    content = _truncate(out)
                except Exception as exc:
                    content = f"ERROR running {call['name']}: {type(exc).__name__}: {exc}"
                    log_tool_audit(call["name"], "error", call["args"])
            # Repeated-call guard: the call ran (a write may legitimately need to),
            # but an identical result gets replaced by a stub so a looping model
            # sees an unmissable stop signal instead of the same data again.
            key = (call["name"], _canon_args(call["args"]))
            if prior.get(key) == content:
                if content.startswith("ERROR"):
                    content = (
                        "ERROR (unchanged): this exact call already failed the same way "
                        "this turn. Do not repeat it — change approach or tell the user "
                        "what is blocking you."
                    )
                else:
                    content = (
                        f"UNCHANGED — identical to your earlier {call['name']} result this "
                        "turn; the data has not changed (if this was a write, it did run "
                        "again — do NOT repeat it). Stop calling tools: answer the user in "
                        "text now, or make the one write call you were preparing."
                    )
            else:
                prior[key] = content  # catches duplicates within this same batch too
            results.append(ToolMessage(content=content, tool_call_id=call["id"], name=call["name"]))
        return {"messages": results, "gate_decision": ""}

    # ---------------------------------------------------------------- routing
    def after_agent(state: FridayState) -> Literal["tool_gate", "__end__"]:
        last = state["messages"][-1]
        return "tool_gate" if getattr(last, "tool_calls", None) else END

    def after_gate(state: FridayState) -> Literal["tools", "agent"]:
        return "tools" if state.get("gate_decision") == "approved" else "agent"

    g = StateGraph(FridayState)
    g.add_node("route", route)
    g.add_node("agent", agent)
    g.add_node("tool_gate", tool_gate)
    g.add_node("tools", run_tools)
    g.add_edge(START, "route")
    g.add_edge("route", "agent")
    g.add_conditional_edges("agent", after_agent)
    g.add_conditional_edges("tool_gate", after_gate)
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=checkpointer) if checkpointer else g.compile()


# ---------------------------------------------------------------- job helper
async def ainvoke_autoresolve(graph, prompt: str, thread_id: str) -> str:
    """Invoke for scheduled jobs: auto-rejects any approval pause (nobody is
    watching) and returns the final text."""
    from langgraph.types import Command  # local import to keep module load light

    config = {"configurable": {"thread_id": thread_id}}
    payload: Any = {"messages": [HumanMessage(prompt)]}
    for _ in range(5):
        result = await graph.ainvoke(payload, config)
        if not result.get("__interrupt__"):
            return last_ai_text(result)
        payload = Command(resume={"decision": "reject", "reason": "background job — no approvals available, report instead"})
    return last_ai_text(result)


def last_ai_text(result: dict) -> str:
    for msg in reversed(result.get("messages", [])):
        if isinstance(msg, AIMessage) and msg.content:
            return msg.content if isinstance(msg.content, str) else str(msg.content)
    return ""


# --------------------------------------------------------------------- utils
async def _recall_safe(text: str) -> list[str]:
    try:
        return await memory.recall(text, k=5)
    except Exception as exc:
        log.debug("recall skipped (%s)", exc)
        return []


def _recent_context(messages: list[AnyMessage]) -> str:
    texts = [
        m.content for m in messages[-6:-1] if isinstance(m, (HumanMessage, AIMessage)) and isinstance(m.content, str) and m.content
    ]
    return " | ".join(t[:80] for t in texts[-3:])


def _canon_args(args: Any) -> str:
    return json.dumps(args, sort_keys=True, default=str)


def _prior_tool_results(messages: list[AnyMessage]) -> dict[tuple[str, str], str]:
    """(tool name, canonical args) -> most recent REAL result content since the
    last HumanMessage. UNCHANGED/duplicate stubs are never stored, so a repeat
    is always compared against the data it would reproduce."""
    start = next(
        (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)), 0
    )
    calls: dict[str, tuple[str, str]] = {}
    out: dict[tuple[str, str], str] = {}
    for m in messages[start:]:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls or []:
                calls[tc["id"]] = (tc["name"], _canon_args(tc["args"]))
        elif isinstance(m, ToolMessage) and m.tool_call_id in calls:
            content = str(m.content)
            if not content.startswith(("UNCHANGED —", "ERROR (unchanged)")):
                out[calls[m.tool_call_id]] = content
    return out


def _reflection_note(messages: list[AnyMessage]) -> str:
    """If the last two tool results were errors, push the agent to change approach."""
    errors = []
    for m in reversed(messages):
        if isinstance(m, ToolMessage):
            if str(m.content).startswith(("ERROR", "ACTION NOT EXECUTED")):
                errors.append(f"{m.name}: {str(m.content)[:150]}")
                if len(errors) >= 2:
                    break
            else:
                return ""
        elif isinstance(m, (HumanMessage,)):
            break
        elif isinstance(m, AIMessage) and m.content and not m.tool_calls:
            break
    if len(errors) >= 2:
        return (
            "## Reflection required\nYour recent attempts failed:\n- "
            + "\n- ".join(reversed(errors))
            + "\nCritique your approach in one line, then try a DIFFERENT approach or ask the user."
        )
    return ""


def _trim(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Send at most the last N messages, cutting only at a Human boundary so
    we never orphan tool calls from their results."""
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages
    cut = len(messages) - MAX_HISTORY_MESSAGES
    while cut < len(messages) and not isinstance(messages[cut], HumanMessage):
        cut += 1
    return messages[cut:] if cut < len(messages) else messages[-MAX_HISTORY_MESSAGES:]


def _safe_json(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {"_raw": raw or ""}


def _truncate(out: Any) -> str:
    text = out if isinstance(out, str) else json.dumps(out, default=str, ensure_ascii=False)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        return text[:MAX_TOOL_RESULT_CHARS] + f"\n… (truncated, {len(text)} chars total)"
    return text
