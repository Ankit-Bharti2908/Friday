# Friday — 10 Interview Questions & Detailed Answers

Questions an interviewer could reasonably ask about this project, with answers
grounded in the actual code. File references point at the implementation so you
can re-derive every claim.

---

## 1. Walk me through the high-level architecture. What happens end-to-end when a Telegram message arrives?

**What they're probing:** can you explain the whole system in one pass, and do
you actually understand the control flow rather than just the buzzwords.

**Answer.** Friday is a single Python process (`main.py`) backed by one SQLite
file (`friday.db`) plus editable markdown. There is no web server and no
webhook — Telegram long-polling means it runs behind NAT with no public IP.

The heart is a LangGraph state machine (`core/graph.py`) with four nodes:

```
route → agent → tool_gate → tools ─┐
  ▲       │ (no tool calls → END)  │
  └───────┴────────────────────────┘ (loop back to agent)
```

End-to-end, for *"Reply to Priya's email confirming the 3 PM demo"*:

1. `channels/telegram.py` receives the update, drops it unless the sender is
   the hardcoded owner id, and calls
   `graph.ainvoke({messages: [HumanMessage]}, {thread_id: "tg:<chat>"})`.
2. **route** runs `router.classify()` (fast tier) and `memory.recall()`
   *concurrently* via `asyncio.gather`, matches skills (here `email_style`),
   and packs recalled memories + rendered skills into `context_block`.
3. **agent** looks up the `email` profile, filters the tool list to that
   profile's subset, assembles the system prompt (SOUL + USER + clock + tool
   digest + profile instructions + context block), trims history to the last
   40 messages, and calls the LLM through the tier system. The model returns
   tool calls, e.g. `gmail_search_emails`.
4. **tool_gate** checks each call against the approval policy. `search_`
   matches an auto-allow pattern → runs immediately. Later the model calls
   `gmail_send_email`, which matches `send_` → the graph `interrupt()`s and
   Telegram shows the draft with ✅ / ❌ / ✏️ buttons. Execution pauses.
5. On approve, the channel resumes with
   `Command(resume={"decision": "approve"})`; **tools** executes each call with
   a 120 s timeout and 8000-char output truncation, and results loop back to
   **agent**.
6. When the model finally answers without tool calls, the graph hits END. The
   channel shows the text and fires `memory.after_turn()` in the background to
   extract durable facts.

Conversation history **and** pending approvals are checkpointed per
`thread_id` by `AsyncSqliteSaver`, so a restart mid-conversation — even
mid-approval — is lossless.

---

## 2. How does the human-in-the-loop approval work under the hood, and how does a pending approval survive a process restart?

**What they're probing:** understanding of LangGraph interrupts and
checkpointing — the trickiest mechanism in the codebase.

**Answer.** The `tool_gate` node (`core/graph.py`) splits the model's requested
tool calls into risky and safe using `approval.requires_approval()`. If risky
calls exist in interactive mode, it calls LangGraph's
`interrupt({type, preview, actions})`. That *suspends the graph*: the pending
state is persisted by the checkpointer, and `graph.ainvoke()` returns with an
`__interrupt__` marker.

The channel then renders `approval.render_preview()` (args pretty-printed,
truncated at 1200 chars) and collects a decision in whatever UX fits the
channel — inline buttons on Telegram, `y/n/feedback` in the CLI, a plain text
reply on WhatsApp. Every channel runs the same loop:

```python
while True:
    result = await graph.ainvoke(payload, config)
    if result.get("__interrupt__"):
        # show preview, collect decision
        payload = Command(resume={"decision": ..., "reason": ...})
        continue
    break
```

On resume, the `tool_gate` node **re-runs from the top**, and this time
`interrupt()` *returns* the decision payload instead of pausing (which is why
the code before `interrupt()` must be idempotent — a subtle point worth
mentioning). Approve → `gate_decision: "approved"` → the `tools` node runs.
Anything else → rejection `ToolMessage`s are injected carrying the user's
feedback plus *"Do NOT retry unchanged — revise per feedback or ask what to
do"*, and control routes back to the agent.

Restart-survival falls out of the design: the interrupt is just a checkpoint
row in `friday.db` keyed by `thread_id`. The process can die between the pause
and the human's tap; re-invoking with `Command(resume=...)` on the same thread
picks up exactly where it stopped. Every decision is also appended to the
`tool_audit` table (`auto_allowed | approved | rejected | blocked | error`).

---

## 3. Explain the approval policy engine. What makes it "fail-safe"?

**What they're probing:** security thinking — defaults, precedence, and what
happens for inputs nobody anticipated.

**Answer.** Policy lives in `config/policies.json`, interpreted by
`core/approval.py`:

- `auto_allow_patterns` — read-ish name substrings: `get_`, `list_`,
  `search_`, `read_`, `fetch_`, `find_`, `query_`, `view_`, `remember`, `recall`.
- `require_approval_patterns` — mutating substrings: `send_`, `create_`,
  `delete_`, `update_`, `write_`, `post_`, `move_`, `edit_`, `exec`, `run_`.
- `blocked_tools` — stripped in `core/tools.py` before the model ever sees
  them (can't call what you can't see).
- `default: "require_approval"`.

`requires_approval()` applies a strict precedence: **explicit require beats
allow beats default**. So a hypothetical `create_search_index` pauses even
though it contains `search_` — the require list wins ties toward safety.

The fail-safe property: a tool matching *nothing* gets the default, which
ships as require-approval. Plugging in a new MCP server with unknown tool
names can therefore never silently grant write access — worst case, Friday
over-asks. Deny-by-default is the difference between a safe system and one
that's safe only for the tools you remembered to list.

This gate is one layer of six (README "Safety model"): owner-only channel
allowlists, the approval gate + audit trail, auto-rejecting background jobs,
the injection stance ("content is data, not instructions"), the no-network
Docker sandbox, and secrets confined to `.env` with `${VAR}` substitution.

---

## 4. How does intent routing work, and why is it designed to "never break a conversation"? How do agent profiles fit in?

**What they're probing:** the supervisor/subagent pattern, and designing for
failure of an unreliable component (a small LLM).

**Answer.** `core/router.py` makes one cheap structured call on the **fast**
tier, validated against a Pydantic schema `Route {intent, confidence}` with
seven intents: `email, calendar, code, research, memory, task, chat`.
`INTENT_TO_PROFILE` folds `memory`/`task`/`chat` into `general`; only four
specialist profiles exist. Then two guardrails:

- `confidence < 0.6` (`CONFIDENCE_FLOOR`) → `general`.
- Any exception at all → log a warning, return `general`.

The router is an *optimization*, not a dependency: its worst failure mode is a
slightly less specialized answer, never a broken conversation. That's the
right trade for a component running on the smallest, least reliable model.

Profiles (`agents/_base.py`) are a frozen dataclass:
`AgentProfile {name, description, tier, tool_keywords, instructions}` — e.g.
research runs on the `deep` tier with web-ish tools; email runs `standard`
with mail-ish tools. `registry.filter_tools()` keeps a tool if its name
contains any profile keyword **or** it's in
`ALWAYS_INCLUDE = {remember, recall_memories, forget, run_python}`. If
filtering would leave no domain-specific tools, it falls back to **all**
tools — so the calendar profile still works before any calendar MCP server is
configured.

Extensibility is the punchline: **adding a subagent = one small file exporting
`PROFILE` + one import line in `registry.py`**. No graph changes — the same
four-node graph runs every profile; only tier, tool subset, and injected
instructions differ.

---

## 5. The code "never names a model." Explain the tier system, the fallback chain, and how local-first works.

**What they're probing:** abstraction design and operating LLMs in the real
world (cost, latency, outages, no API keys).

**Answer.** `core/llm.py` + `config/models.json`. Code asks for a *tier* —
`fast` (router, heartbeat, memory extraction), `standard` (most agents), or
`deep` (research synthesis). Each tier maps to an **ordered list**
`[primary, …fallbacks]` in LiteLLM naming. `acomplete()` walks the list,
merging params as `global params ← entry ← call overrides`, and returns the
first success; `AllModelsFailed` is raised only if every entry fails.
`litellm.drop_params = True` silently drops parameters a given provider
doesn't support, so one call site works across providers.

**Local-first:** every tier leads with llama.cpp serving Gemma 3n E4B on
`:8080`. LiteLLM has no llama.cpp provider, so the entry is plain
`openai/<name>` + `api_base` + a dummy key against llama.cpp's
OpenAI-compatible server — the model string is cosmetic; only the port
matters. Next comes Ollama, then optional cloud models. An *unstarted* local
server just throws and the chain moves on, so it's safe to keep local first
even when it isn't running, and the whole assistant works keyless.

Two supporting pieces:

- `astructured()` — tries native `response_format`; on any failure appends
  *"Respond with ONLY a JSON object matching this schema"* and validates with
  Pydantic, stripping code fences first. The router and memory extraction
  depend on this working even on small local models.
- `aembed()` — guards the returned vector's dimension against the configured
  `dim` (768). Mixing embedding models in one vector table doesn't error — it
  silently makes nearest-neighbor search meaningless, which is why the guard
  raises loudly instead.

Switching providers, reordering fallbacks, or putting a cloud model first on
`deep` is a JSON edit with zero code changes.

---

## 6. Describe the memory system: storage, recall, deduplication, and the automatic extraction pipeline.

**What they're probing:** hybrid vector+keyword retrieval, threshold
reasoning, and pipelines that must never take down the main flow.

**Answer.** Source of truth is SQLite (`core/db.py`): a `memories` table
(kind constrained by `CHECK (kind IN ('fact','preference','project','event'))`,
soft-delete flag) plus `vec_memories`, a sqlite-vec `vec0` virtual table
(`float[768]`, cosine distance). `memory/MEMORY.md` is an auto-regenerated
human-readable **mirror** — every write rewrites it; you edit it via
"remember/forget", not by hand.

- **Write** (`add_memory`): normalize whitespace → embed → nearest-neighbor
  check; if the closest existing memory is at cosine distance **< 0.08** it
  returns *"Already known"* and skips the insert. Otherwise insert row +
  vector, rewrite the mirror.
- **Read** (`recall`): vector search over-fetches `k*2`, keeps hits at
  distance **≤ 0.55**, returns top `k` as `[kind] text` lines that `route`
  injects into the system prompt each turn. Two thresholds, two jobs: 0.08
  answers "is this the *same* fact?", 0.55 answers "is this even *relevant*?".
- **Delete** (`forget_matching`): soft-deletes (`deleted=1`) the top 3 matches
  and removes their vectors — the row survives as an audit trail.
- **Implicit capture** (`after_turn`): a fire-and-forget post-turn hook that
  *never raises*. It appends the turn to `memory/notes/YYYY-MM-DD.md`, then
  asks the fast tier to extract durable facts from the (truncated) exchange.
  The prompt is biased hard toward extracting nothing ("Usually 0 items is
  correct"), and only candidates with **confidence ≥ 0.8** are stored — both
  filters exist because false memories are worse than missed ones.
- **Nightly** (`consolidate_today`, 23:30): compresses the day's raw log into
  5–8 bullets at the top of the note.

Everything degrades gracefully offline: no embedding backend → memories are
stored without vectors and recall falls back to a keyword `LIKE` search over
words longer than 3 chars; no sqlite-vec extension → same fallback. Memory
gets worse, never broken.

---

## 7. Friday messages you proactively. How do the scheduled jobs work, and how does the design prevent (a) unsafe autonomous actions and (b) alert spam?

**What they're probing:** autonomous agents are where safety incidents
happen; alert fatigue is where proactive assistants die.

**Answer.** `core/scheduler.py` registers APScheduler cron jobs in code at
startup (deterministic, so no persistent jobstore): **08:00** briefing through
the main graph, **07:35** quiet-hours queue flush, **every 30 min 08–22**
heartbeat, **23:30** note consolidation — each with `misfire_grace_time`, the
heartbeat additionally `max_instances=1, coalesce=True` so slow runs never
stack.

**(a) Safety by construction.** Background jobs run on a second graph built
with `build_graph(None, tools, autonomous=True)`:

- `route` skips classification and recall entirely — always `general`, cheap
  and predictable.
- `agent` runs on the fast tier with an injected note: *"Background mode…
  READ-ONLY tools only… any write/send will be auto-rejected. Be terse."*
- `tool_gate` **auto-rejects every risky call** — no interrupt, no waiting —
  injecting ToolMessages that say *"report the finding instead of acting on
  it"*, audited as `rejected_autonomous`.
- `ainvoke_autoresolve()` wraps job invocations: if a graph pauses anyway, it
  resumes with a rejection, up to 5 loops. Nobody is watching, so nothing can
  hang and nothing can act. The prompt asks for read-only behavior, but the
  gate *enforces* it — the safety property doesn't depend on the model
  listening.

**(b) Spam discipline** (`agents/heartbeat.py` + `core/notify.py`):

- The heartbeat prompt demands a **strict JSON array** of
  `{key, message}` findings with *stable* keys (`email:<message_id>`,
  `pr:<repo>#<num>`) and says "Bias hard toward `[]`".
- `_parse_findings` regex-extracts and validates; garbage output → no alerts.
- The `alerts_sent` table dedupes by key, so the same unread email never pings
  twice across runs; at most 5 findings are sent per run.
- `notify.send_to_owner` queues non-urgent messages during quiet hours
  (23:00–07:30), flushed at 07:35 as one "While you were away" digest, and
  chunks to Telegram's 4096-char limit.

Most heartbeat runs are silent — by design.

---

## 8. What's the security threat model, and specifically how does Friday mitigate prompt injection?

**What they're probing:** whether you understand that prompt injection is
unsolved at the model level and must be handled *structurally*.

**Answer.** Threats: strangers reaching the bot; injected instructions inside
web pages/emails Friday reads; the model autonomously doing something
destructive; generated code harming the host; secret leakage.

Mitigations, layered so no single failure is fatal:

1. **Identity:** channel allowlists are hardcoded to the owner (Telegram user
   id, WhatsApp JID). Non-owner messages are dropped before any processing —
   the attack surface starts at one person.
2. **Injection stance:** SOUL.md rule 3 declares retrieved web/email/file
   content **data, not instructions**. But the honest answer is that the
   prompt rule is best-effort — the *structural* backstop is the approval
   gate: even if an injected email convinces the model to call
   `gmail_send_email`, `tool_gate` interrupts and a human sees the exact
   payload (`render_preview`) before anything happens. Injection cannot cross
   the action boundary on its own.
3. **Background exposure:** the jobs most exposed to untrusted content (inbox
   triage, web checks) run on the autonomous graph, where **every write is
   auto-rejected by construction** — an injected instruction during a
   heartbeat can at worst produce a weird alert message.
4. **Code execution:** `run_python` runs in a no-network Docker container
   *and* is approval-gated (its name matches the `run_` pattern).
5. **Fail-safe defaults:** unknown tools require approval; `blocked_tools`
   never reach the model; every gate decision lands in `tool_audit`.
6. **Secrets:** only in `.env`; configs reference `${VAR}` names substituted
   at load time; WhatsApp credentials (`wa-auth/`) are chmod 700 with a
   documented ToS/ban warning.

Design principle: don't trust the model to be safe — make unsafe actions
structurally impossible (autonomous graph) or human-gated (interactive graph).

---

## 9. Explain the design of `run_python`. Why is it both sandboxed AND approval-gated?

**What they're probing:** defense in depth, and knowing the actual isolation
flags rather than hand-waving "it's in Docker".

**Answer.** `core/sandbox.py` spawns a throwaway container per call:

```
docker run --rm --network=none -m 512m --cpus 1 -i python:3.12-slim python -c <code>
```

- `--network=none` — no exfiltration, no downloading payloads.
- No volume mounts — the host filesystem simply isn't there.
- `-m 512m --cpus 1` — memory leaks and CPU spins are contained.
- 30 s `asyncio.wait_for` timeout, then `proc.kill()` — infinite loops die.
- `--rm` — nothing persists between runs; each execution is stateless.
- Output (stdout + stderr) is truncated to 6000 chars; if Docker isn't
  installed, the tool returns a clear `ERROR` string instead of crashing.

Why *both* layers: they protect different things. The sandbox protects the
**machine** (even hostile code can't reach the network or host). The approval
gate protects **intent** (the human reads the code before it runs — the model
can't quietly compute something you'd object to). Layers fail independently:
a container-escape CVE is caught by the human gate; a rubber-stamped approval
is caught by the sandbox.

A nice detail: the gating comes free from naming. `run_python` matches the
`run_` require-approval pattern in `policies.json` — no special-case code
anywhere. Policy-by-naming-convention keeps the whole mechanism in one place.

---

## 10. What reliability engineering keeps a long-running agent loop stable? Cover context-window management, failure handling, and graceful degradation.

**What they're probing:** production maturity — agents fail in loops, in
overflowing contexts, and in half-broken environments.

**Answer.** Three families of defenses:

**Context-window management** (`core/graph.py`):

- `MAX_HISTORY_MESSAGES = 40` — only the recent window is *sent* to the model
  (full history stays in the checkpoint). The trim cuts **only at a
  HumanMessage boundary**, because splitting an AIMessage's tool calls from
  their ToolMessage results produces hard API errors on most providers.
- `MAX_TOOL_RESULT_CHARS = 8000` — tool output is truncated with an explicit
  `… (truncated, N chars total)` marker so the model knows it's partial.
- `core/tools.py` warns when > 40 MCP tools load — oversized tool lists burn
  context and degrade tool selection.

**Loop control:**

- `MAX_AGENT_LOOPS = 12` circuit breaker: the agent stops with "I hit my
  action limit — tell me how to proceed" instead of burning tokens forever.
- Reflection: `_reflection_note` scans the recent transcript; **two
  consecutive tool errors** inject *"Critique your approach in one line, then
  try a DIFFERENT approach or ask the user"* — breaking the classic
  retry-the-same-call death spiral.

**Failure handling & graceful degradation:**

- The `tools` node never crashes the graph: unknown tool → `ERROR` message;
  exception → `ERROR running X: …`; 120 s timeout per call. Errors become
  ToolMessages the model can react to.
- Router exception → `general`; recall failure → empty memories
  (`_recall_safe`); `after_turn` and consolidation never raise.
- The LLM layer walks the fallback chain; `_safe_json` absorbs malformed
  tool-call arguments from weak models (`{"_raw": …}` instead of a crash).
- Each MCP server loads in isolation — one broken server (say, expired Google
  OAuth) never blocks the others.
- Missing pieces fail soft: no sqlite-vec → keyword recall; no embeddings →
  vectorless storage; no Docker → clear error; Phoenix down → warning, not a
  boot failure.
- SQLite runs in WAL mode with `busy_timeout=5000` so the checkpointer's
  async connection and the sync connections coexist in one process.

Ops story: `tests/smoke.py` verifies every core module offline;
`tests/eval_routing.py` is a routing regression gate (< 90 % = fix the
prompt); Phoenix traces every LLM span; `scripts/backup.sh` zips the folder —
the whole brain is one directory.

---

## Bonus: five more they might ask

- **Skills:** how does the markdown skill system give hot-reloadable behavior?
  (Frontmatter triggers + agent filter, re-read on every match — edit a file,
  next message changes, no restart. `core/skills.py`.)
- **Channels:** why can four UIs share one brain? (All channels are thin
  adapters over the same graph + the same resume loop; they differ only in
  approval UX and thread-id prefix.)
- **Why SQLite over Postgres?** (Single user, single process; WAL covers the
  concurrency needed; backup = zip; one less server to run. Trade-off:
  no horizontal scaling — acceptable by design.)
- **WhatsApp bridge:** why a Node sidecar, and what are the risks? (Baileys is
  the mature library; localhost-only WebSocket protocol; unofficial automation
  violates WhatsApp ToS → secondary number, `wa-auth/` chmod 700.)
- **How would you make this multi-user?** (Today identity is a hardcoded
  allowlist and `USER.md` is singular — you'd need per-user threads, per-user
  memory namespaces, per-user policies, and a real auth story; good
  open-ended design discussion.)
