"""The agent graph: load_context -> agent <-> (tool_gate -> tools).

Flow per turn:
  1. agent: LLM (tier "standard" via LiteLLM) sees system prompt + history
     + tool schemas; replies with text and/or tool calls.
  2. tool_gate: every tool call is checked against policy.
       - all reads            -> straight to tools
       - anything risky       -> interrupt() pauses the WHOLE graph; the
         channel renders Approve/Reject buttons; graph resumes with the
         human's decision (Command(resume=...)).
       - rejected             -> ToolMessages explain the rejection and the
         agent gets to respond/revise (reject-with-feedback IS the edit flow).
  3. tools: execute approved calls, append ToolMessages, loop to agent.

Checkpointed in friday.db -> conversations survive restarts; interrupts
survive restarts too (that's what makes phone-approval reliable).
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

from core import approval, llm, prompts
from core.db import log_tool_audit

log = logging.getLogger("friday.graph")

MAX_HISTORY_MESSAGES = 40      # what we *send* to the model (full history stays in the checkpoint)
MAX_TOOL_RESULT_CHARS = 8000   # protect the context window from giant tool outputs
MAX_AGENT_LOOPS = 12           # circuit breaker for runaway tool loops


class FridayState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    gate_decision: str  # "approved" | "rejected" | ""
    loops: int


def build_graph(checkpointer: Any, tools: list[BaseTool]):
    tool_map: dict[str, BaseTool] = {t.name: t for t in tools}
    openai_tools = [convert_to_openai_tool(t) for t in tools] or None

    # ------------------------------------------------------------------ agent
    async def agent(state: FridayState) -> dict[str, Any]:
        if state.get("loops", 0) >= MAX_AGENT_LOOPS:
            return {
                "messages": [AIMessage(content="I hit my action limit for this request — stopping here. Tell me how to proceed.")],
                "loops": 0,
            }

        system = {"role": "system", "content": prompts.build_system_prompt(tools)}
        history = convert_to_openai_messages(_trim(state["messages"]))
        resp = await llm.acomplete("standard", [system] + history, tools=openai_tools)
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
        return {"messages": [ai], "loops": state.get("loops", 0) + 1}

    # -------------------------------------------------------------- tool_gate
    def tool_gate(state: FridayState) -> dict[str, Any]:
        last = state["messages"][-1]
        calls = list(getattr(last, "tool_calls", []) or [])
        risky = [c for c in calls if approval.requires_approval(c["name"])]

        if not risky:
            for c in calls:
                log_tool_audit(c["name"], "auto_allowed", c["args"])
            return {"gate_decision": "approved"}

        # Pause here. On resume, this node re-runs and interrupt() returns
        # the human's decision payload.
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
        feedback = f"User rejected this action. {('Feedback: ' + reason) if reason else 'No reason given.'} Do NOT retry unchanged — revise per feedback or ask what to do."
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
            results.append(ToolMessage(content=content, tool_call_id=call["id"], name=call["name"]))
        return {"messages": results, "gate_decision": ""}

    # ---------------------------------------------------------------- routing
    def after_agent(state: FridayState) -> Literal["tool_gate", "__end__"]:
        last = state["messages"][-1]
        return "tool_gate" if getattr(last, "tool_calls", None) else END

    def after_gate(state: FridayState) -> Literal["tools", "agent"]:
        return "tools" if state.get("gate_decision") == "approved" else "agent"

    g = StateGraph(FridayState)
    g.add_node("agent", agent)
    g.add_node("tool_gate", tool_gate)
    g.add_node("tools", run_tools)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", after_agent)
    g.add_conditional_edges("tool_gate", after_gate)
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=checkpointer)


# --------------------------------------------------------------------- utils
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
