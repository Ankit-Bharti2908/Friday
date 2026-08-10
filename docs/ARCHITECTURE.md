# Friday — Architecture & Code Walkthrough

A personal AI assistant that runs as **one Python process**, backed by **SQLite +
markdown** (no Postgres, no Docker except the optional code sandbox, no proxy
servers). This document explains how every part fits together so you can navigate
the codebase quickly.

---

## 1. The 60-second mental model

```
                    ┌──────────────────────────────────────────────┐
   Telegram ─┐      │                  main.py                      │
   WhatsApp ─┼──────▶  builds tools, builds graphs, starts polling  │
   CLI / Web ┘      │       + APScheduler proactive jobs            │
                    └───────────────────┬──────────────────────────┘
                                        │ graph.ainvoke(msg, thread)
                                        ▼
        ┌─────────────────── LangGraph state machine ───────────────────┐
        │                                                               │
        │   route ──▶ agent ──▶ tool_gate ──▶ tools ──┐                 │
        │     ▲          │  (after_agent)   (approval)  │                │
        │     │          └────── END (no tool call)     │                │
        │     └────────────────────────────────────────┘ (loop back)    │
        └───────────────────────────────────────────────────────────────┘
                                        │
        recall memories · match skills · pick agent profile · pick tier
                                        │
                                        ▼
                core/llm.py  →  LiteLLM  →  fast/standard/deep tiers
                                        │
                          MCP tools + memory tools + run_python
```

**One sentence:** a message comes in on a channel → the graph *routes* it to a
specialist agent profile, *recalls* relevant memories and *matches* skills → the
LLM (chosen by tier) decides on tool calls → every risky call *pauses for your
approval* → tools run → the loop repeats until the agent answers → a post-turn
hook *extracts durable memories*.

Everything (conversation history **and** pending approvals) is checkpointed in
`friday.db`, so restarts are lossless.

---

## 2. Repository layout

```
main.py            Single entrypoint — boots everything, runs the event loop.

core/              The engine. No business logic about "email" or "calendar" lives here.
  settings.py        Paths, env vars, JSON config loading with ${VAR} substitution.
  db.py              SQLite: memories, vec_memories (sqlite-vec), tool_audit, alerts_sent.
  llm.py             Provider-agnostic tiers (fast/standard/deep) via LiteLLM + fallbacks.
  prompts.py         System-prompt assembly (SOUL + USER + clock + tool digest + extras).
  router.py          Fast-tier intent classification → agent profile.
  graph.py           The LangGraph state machine (route → agent → tool_gate → tools).
  approval.py        Policy: which tools auto-run vs. pause vs. are blocked.
  tools.py           Loads MCP servers from config/mcp.json into LangChain tools.
  memory.py          Long-term memory (vector + keyword), daily notes, extraction hook.
  fitness.py         Gym-trainer data layer: profile/plan/log markdown + tools + today's session.
  skills.py          Markdown "skills" — workflow snippets injected when matched.
  notify.py          Outbound owner notifications with quiet-hours queueing.
  scheduler.py       APScheduler cron jobs (briefing, heartbeat, consolidation).
  sandbox.py         run_python in a throwaway no-network Docker container.

agents/            The modular "subagent" system. Each profile = tier + tools + instructions.
  _base.py           AgentProfile dataclass (frozen).
  registry.py        PROFILES map + filter_tools() + ALWAYS_INCLUDE set.
  email.py / research.py / coder.py / calendar_agent.py / gym.py   The five specialists.
  briefing.py        08:00 morning briefing (agentic pass, read-only).
  heartbeat.py       Every 30 min proactive check (autonomous graph, dedup'd alerts).
                     (gym.py also carries the daily workout alert job.)

channels/          Gateways. All share the same graph; they differ only in I/O + approval UX.
  telegram.py        Primary. Long-poll, inline Approve/Reject/Edit buttons, voice notes.
  cli.py             Dev REPL against the same graph.
  whatsapp.py        WebSocket client to the Node bridge; text-based approvals.
  web.py             Optional Chainlit UI (persistent friday.db, shared memory).

bridge/whatsapp/   Node "Baileys" sidecar that bridges WhatsApp <-> a localhost WebSocket.

identity/          Friday's personality & knowledge, as editable markdown (no redeploy).
  SOUL.md            Who Friday is + hard safety rules.
  USER.md            Who Friday works for (the owner's facts/preferences).
  HEARTBEAT.md       The proactive checklist the heartbeat job runs.

skills/            *.md workflow snippets (email_style, daily_note_format, fitness_intake, gym_program_design).
config/            models.json (tiers), policies.json (approval rules), mcp.json (servers).
memory/            MEMORY.md (human-readable mirror of the DB) + notes/ (daily logs).
tests/             smoke.py (offline) + eval_routing.py (needs a live fast tier).
scripts/           backup.sh — the whole brain is this folder; zip it nightly.
```

**Design philosophy:** `core/` is a generic engine; capabilities are added by
*data*, not code — drop an MCP server in `mcp.json`, a profile file in `agents/`,
or a markdown file in `skills/`.

---

## 3. Boot sequence (`main.py`)

`amain()` runs once and then blocks on a stop event:

1. **`setup_tracing()`** — registers Phoenix/OpenTelemetry auto-instrumentation
   (LiteLLM spans). Disabled with `FRIDAY_TRACING=0`; failure is non-fatal.
2. **`init_db()`** — creates tables + the `vec_memories` virtual table.
3. **`settings.validate()`** — warns about missing tokens / LLM keys. Hard-stops
   only if Telegram isn't configured (suggests the CLI instead).
4. **`load_mcp_tools() + memory_tools() + sandbox_tools()`** — assembles the full
   tool list once, shared by every graph and channel.
5. **`AsyncSqliteSaver`** opens the checkpointer over `friday.db`, then:
   - `build_graph(saver, tools)` → the **interactive** graph.
   - `build_graph(None, tools, autonomous=True)` → the **background** graph
     (no checkpointer needed, auto-rejects writes).
   - `graph._friday_tools = tools` is stashed so `/status` can count tools.
6. Telegram `Application` is built and starts **long polling**
   (`drop_pending_updates=True` so it ignores messages sent while offline).
7. `notify.configure(bot, owner)` wires outbound notifications to the bot.
8. `scheduler.start(graph, autonomous_graph)` registers the cron jobs.
9. Optional WhatsApp client task starts if `WHATSAPP_ENABLED=1`.
10. Blocks on `stop.wait()` (SIGINT/SIGTERM) → graceful shutdown of every piece.

There is **no web server and no webhook** — Telegram long-polling means it runs
behind NAT with no public IP.

---

## 4. The agent graph (`core/graph.py`) — the heart

A LangGraph `StateGraph` over `FridayState`:

```python
class FridayState(TypedDict, total=False):
    messages:      list[AnyMessage]   # full transcript (add_messages reducer)
    agent_name:    str                # which profile handles this turn
    context_block: str                # recalled memories + matched skills
    gate_decision: str                # "approved" | "rejected" | ""
    loops:         int                # circuit breaker counter
```

### Nodes & edges

```
START → route → agent → (after_agent?) → tool_gate → (after_gate?) → tools → agent → …
                  │                                                              ▲
                  └── after_agent: no tool_calls → END                          │
                                                       after_gate: approved ────┘
                                                       after_gate: rejected → agent
```

**`route`** (async):
- Extracts the latest human message.
- **Interactive mode:** runs `router.classify()` (fast tier) *and* `memory.recall()`
  concurrently via `asyncio.gather`. **Autonomous mode:** skips both — always
  `general`, no recall (background jobs stay cheap and predictable).
- Matches skills with `skills.match(text, profile)`.
- Packs recalled memories + rendered skills into `context_block`.
- Resets `loops=0` and `gate_decision=""`.

**`agent`** (async) — the LLM call:
- Circuit breaker: if `loops >= MAX_AGENT_LOOPS (12)`, it stops and asks the user.
- Looks up the profile, filters tools to that profile's subset, picks the tier
  (`fast` if autonomous, else the profile's tier).
- Builds `extras`: profile instructions + context_block + (if autonomous) a
  "background mode, read-only, be terse" note + a **reflection note** if the last
  two tool results were errors.
- Assembles the system prompt (`prompts.build_system_prompt`), trims history to
  the last 40 messages (cutting only at a Human boundary so tool calls aren't
  orphaned), and calls `llm.acomplete(tier, …, tools=schemas)`.
- Converts the response into an `AIMessage` with structured `tool_calls`,
  increments `loops`.

**`tool_gate`** (sync) — the safety checkpoint:
- Splits the requested calls into risky (per `approval.requires_approval`) and safe.
- No risky calls → log all as `auto_allowed`, return `approved`.
- **Autonomous mode** → auto-reject *everything*, injecting `ToolMessage`s telling
  the agent to "report the finding instead of acting on it". (Background jobs
  never write.)
- **Interactive + risky** → `interrupt({type, preview, actions})`. This *pauses the
  graph* and surfaces to the channel. On resume, the node re-runs and `interrupt()`
  returns the decision payload:
  - `{"decision": "approve"}` → `approved`.
  - anything else → `rejected`, with a `ToolMessage` carrying the user's feedback
    and "do NOT retry unchanged".

**`tools`** (async) — execution:
- For each approved tool call, looks it up in `tool_map`, runs it with a **120s
  timeout**, truncates output to **8000 chars**, wraps exceptions into `ERROR …`
  strings (never crashes the graph). Every result becomes a `ToolMessage`.
- **Repeated-call guard:** a call identical (name + canonical args) to one already
  made this turn still executes (a write may legitimately need to), but when the
  result is also identical it is replaced by an `UNCHANGED — …` stub telling the
  model to stop reading and answer; repeated identical errors keep the `ERROR`
  prefix so the reflection nudge still fires. Kills the read-ping-pong loops that
  weak models fall into. (The `agent` node also injects a "wrap up, N steps left"
  note over the last 4 loops before the circuit breaker.)
- Loops back to `agent`, which sees the results and either calls more tools or
  produces a final answer.

### Resumability

Because the checkpointer persists state per `thread_id`, an `interrupt()` pause
survives a process restart. The channel resumes by re-invoking the graph with a
`Command(resume=decision)` on the **same thread_id**.

### Background-job helper

`ainvoke_autoresolve(graph, prompt, thread_id)` runs a scheduled job and **auto-
rejects any approval pause** (nobody's watching), looping up to 5 times, returning
the final AI text. Used by `briefing` and `heartbeat`.

---

## 5. Routing & agent profiles

### Router (`core/router.py`)
A cheap structured classification on the **fast** tier into one of:
`email · calendar · code · research · fitness · memory · task · chat`, with a
`confidence` score. `INTENT_TO_PROFILE` maps intents to profiles; **`memory`/`task`/`chat` all
fold into `general`**. `resolve_profile()` then applies **sticky routing** against the
previous turn's checkpointed profile: below `CONFIDENCE_FLOOR = 0.6` (or on classifier
failure) it keeps the previous specialist rather than dropping to `general`, and a
confident general-mapped intent on a short follow-up (<40 chars — "yes", "??") also
stays in the flow. A confident specialist intent always switches. This is what keeps
multi-turn flows (the gym intake interview) inside their specialist across one-word
replies. *The router must never break a conversation.*

### Profiles (`agents/*.py`)
An `AgentProfile` is just:

```python
@dataclass(frozen=True)
class AgentProfile:
    name: str
    description: str
    tier: str = "standard"                  # which LLM tier this agent uses
    tool_keywords: tuple[str, ...] | None   # substring filter over tool names
    instructions: str = ""                  # injected into the system prompt
```

| Profile   | Tier     | Tool keywords (substring match)                      | Focus |
|-----------|----------|------------------------------------------------------|-------|
| general   | standard | (none → all tools)                                   | chat, tasks, files, memory |
| email     | standard | mail, gmail, message, draft, label, thread           | triage / summarize / draft |
| calendar  | standard | calendar, event, schedule, meeting, availability     | availability / events |
| coder     | standard | github, git, repo, pull, issue, commit, file, python | PRs / issues / code |
| research  | **deep** | search, fetch, web, browse, read, url, http          | multi-step web research |
| gym       | standard | fitness, workout, exercise, gym, cardio, health      | intake / plans / coaching |

`registry.filter_tools()` keeps a tool if its name contains any profile keyword
**or** it's in `ALWAYS_INCLUDE = {remember, recall_memories, forget, run_python}`.
If the filter would leave no domain-specific tools, it **falls back to all tools**
(so calendar still works even before a calendar MCP server is configured).

**Adding a subagent = one file** exporting `PROFILE`, plus one import line in
`registry.py`. No graph changes.

---

## 6. The LLM layer (`core/llm.py` + `config/models.json`)

Code **never names a model** — it names a **tier**. Each tier is an ordered list
of `[primary, …fallbacks]`; `acomplete()` walks the list until one succeeds,
raising `AllModelsFailed` only if all do.

```jsonc
"tiers": {
  "fast":     [llamacpp/gemma-3n-e4b, ollama/llama3.2:3b, gpt-4o-mini, claude-haiku-4-5],
  "standard": [llamacpp/gemma-3n-e4b, ollama/llama3.1:8b, gpt-4o-mini, claude-sonnet-4-6],
  "deep":     [llamacpp/gemma-3n-e4b, ollama/llama3.1:8b, gpt-4o-mini, claude-opus-4-8, claude-sonnet-4-6]
}
"embeddings": { ollama/nomic-embed-text, dim: 768 }
"params":     { temperature: 0.3, max_tokens: 2048, timeout: 90 }
```

- `acomplete()` — chat completion, returns the raw LiteLLM response.
- `astructured()` — tries native `response_format`, falls back to **prompted JSON**
  + schema-validate (`router` and memory extraction depend on this).
- `aembed()` — embeds one string and **guards the dimension** against the configured
  `dim` (mixing embedding models in one vec table is silently catastrophic).
- `litellm.drop_params = True` lets the same call work across providers that don't
  support every parameter.

**Local backends (no key, local-first).** Every tier now leads with **llama.cpp**,
then **Ollama**, then cloud — matching the `local-ollama-setup` memory's local-first
intent. Two ways to run a local model:

- **llama.cpp** (primary) — start `llama-server` with `scripts/llamacpp.sh`. It serves
  an OpenAI-compatible API on `http://localhost:8080/v1`, so the entry is plain
  `openai/<name>` + `api_base` + a dummy `api_key` (LiteLLM has no dedicated llama.cpp
  provider; the `llamacpp/` label above is just shorthand). Default model is **Gemma 3n
  E4B**; `scripts/llamacpp.sh e2b` swaps in the lighter **E2B** variant, and any GGUF
  repo works (`scripts/llamacpp.sh ggml-org/gemma-3-12b-it-GGUF`). The `model` string is
  cosmetic for llama.cpp — only the port must match the JSON.
- **Ollama** (fallback) — `ollama serve` on `:11434`, `ollama/<name>`.

The fallback chain means an *unstarted* local server just errors and the next entry is
tried, so it's safe to keep llama.cpp first even when it isn't running. Want max
reasoning quality on `deep`? Move the cloud entry to the front of that tier — pure JSON,
no code change.

---

## 7. Tools: MCP, memory, sandbox

Friday's full toolset = **MCP tools + memory tools + fitness tools + `run_python`**.

### MCP (`core/tools.py` + `config/mcp.json`)
Claude-Desktop-style server config. Conventions:
- Server names starting with `_` are **disabled templates** (skipped). Ships with
  `filesystem` enabled (scoped to `FRIDAY_WORKSPACE`); `_github_remote` and
  `_google_workspace` are ready to enable by removing the `_` and setting env vars.
- `${ENV_VAR}` is substituted at load time by `settings.load_config`.
- **Each server loads in isolation** — one broken server (e.g. bad Google OAuth)
  never blocks the others.
- Tools in `blocked_tools` are filtered out before the model ever sees them.
- Warns if > 40 tools load (big tool lists burn context and confuse routing).

**Adding a capability = adding an entry to `mcp.json`.** That's the whole
modularity story for integrations.

### Memory tools (`core/memory.py`)
`remember`, `recall_memories`, `forget` — exposed to the agent so it can manage
long-term memory on request (and always retained via `ALWAYS_INCLUDE`).

### Fitness tools (`core/fitness.py`)
The gym trainer's data layer: `get_/update_fitness_profile`,
`get_/save_workout_plan`, `get_todays_workout`, `record_workout`,
`get_workout_log`. Files under `memory/fitness/` (PROFILE.md / PLAN.md / LOG.md,
replaced versions archived to `history/`) are the source of truth and stay
human-editable. Reads and the log append run free; profile/plan saves pause for
approval — the trainer proposes, the owner approves. `PLAN.md` holds one
`## <Weekday>` section per day; `todays_session()` extracts today's section for
the daily alert without any LLM call.

### Sandbox (`core/sandbox.py`)
`run_python(code)` runs in a throwaway `python:3.12-slim` Docker container with
`--network=none`, 512 MB, 1 CPU, 30s timeout, `--rm`. Its name matches the `run_`
approval pattern, so it's **approval-gated on top of the sandbox**. Degrades to a
clear error if Docker isn't installed.

---

## 8. The approval / safety model (`core/approval.py` + `config/policies.json`)

This is the project's spine — **don't weaken it.**

```jsonc
"auto_allow_patterns":      ["get_","list_","search_","read_","fetch_","find_","query_","view_","remember","recall"],
"require_approval_patterns": ["send_","create_","delete_","update_","write_","post_","move_","edit_","exec","run_"],
"blocked_tools":            [],
"default":                  "require_approval"   // fail safe
```

`requires_approval(name)` precedence: **explicit require → allow → default**. An
unknown tool that matches nothing → **requires approval** (deny by default).

Layered defenses (from README §"Safety model"):
1. **Channel allowlists are hardcoded to the owner** (Telegram user id, WhatsApp
   JID). Non-owner messages are dropped before any processing.
2. Reads run free; writes/sends/deletes pause; unknown tools require approval.
   Every decision is written to the `tool_audit` table.
3. **Background jobs use the autonomous graph that auto-rejects all writes.**
4. Web/email/file content is treated as **data, not instructions** (SOUL.md rule 3).
5. `run_python` is **no-network Docker AND approval-gated**.
6. Secrets only in `.env`; configs reference `${VAR}` names.

`render_preview()` produces the human-readable "here's what I'm about to do" block
shown in the approval prompt (args truncated to 1200 chars).

---

## 9. Memory system (`core/memory.py`)

**Source of truth = SQLite (+ sqlite-vec); `memory/MEMORY.md` is an auto-generated
mirror.** Everything degrades gracefully offline (no embeddings → store without a
vector, recall falls back to keyword `LIKE`).

| Piece | What it does |
|-------|--------------|
| `add_memory` | Normalizes text, embeds, **dedupes** (cosine distance < 0.08 = "already known"), inserts into `memories` + `vec_memories`, rewrites `MEMORY.md`. |
| `recall` | Vector search (distance ≤ 0.55), falls back to keyword search. Returns `[kind] text` lines. Injected into the system prompt each turn. |
| `forget_matching` | Soft-deletes (`deleted=1`) the top matches by vector or keyword. |
| `after_turn` | **Post-turn fire-and-forget hook.** Logs the turn to today's note, then asks the fast tier to extract durable facts (≤2 per turn); stores any with **confidence ≥ 0.8**. Skipped entirely for gym turns — fitness data lives in `memory/fitness/`, the gym agent `remember`s its own one-liner. Never raises. |
| `consolidate_today` | Nightly: compresses today's raw log into 5-8 bullets at the top of the note. |

Memory `kind ∈ {fact, preference, project, event}` (enforced by a DB CHECK
constraint). Daily notes live in `memory/notes/YYYY-MM-DD.md`.

**Explicit control:** say "remember/forget X" → the agent calls the tools.
**Implicit:** the extraction hook quietly captures stable facts.

---

## 10. Skills (`core/skills.py` + `skills/*.md`)

A skill is a markdown file with tiny frontmatter:

```markdown
---
name: email_style
description: How Ankit writes emails
triggers: draft, reply, email, mail      # substring match on the user message
agents: email                            # or "all", or blank
---
<body injected verbatim into the system prompt when matched>
```

Matched when **any trigger substring is in the message** *or* **the active profile
is listed in `agents`** (`all` = every profile). Files are **re-read on every
match** — edit a skill and behavior changes on the very next message, no restart.
Ships with `email_style`, `daily_note_format`, `fitness_intake`, `gym_program_design`.

This is the lightweight alternative to hardcoding workflows: prompt-level behavior
lives in editable text, versioned alongside the code. The gym agent leans on this
hardest: its entire domain knowledge — the intake questionnaire + level rubric
(`fitness_intake`) and the programming rules (`gym_program_design`) — is skill
markdown, so coaching behavior is tunable without touching Python.

---

## 11. Proactivity (`core/scheduler.py`, `agents/briefing.py`, `agents/heartbeat.py`)

APScheduler cron jobs, defined in code at startup (deterministic → no persistent
jobstore needed):

| Time (IST)        | Job | What it does |
|-------------------|-----|--------------|
| 06:30 (`FRIDAY_WORKOUT_ALERT`) | `workout` | Sends today's `## <Weekday>` section of `memory/fitness/PLAN.md` — **pure file read, no LLM**. Deduped per day via `alerts_sent` (`workout:<date>`); silent if there's no plan or no section for today. Urgent (bypasses quiet hours) since the owner scheduled it deliberately. Empty env value disables. |
| 08:00             | `briefing` | One agentic pass through the **main graph** (read-only via auto-resolve): calendar + unread email triage + GitHub + yesterday's open loops → ≤15 lines to the owner. Missing integrations silently skip. |
| 07:35             | `flush` | Sends any messages queued during quiet hours. |
| */30, 08–22       | `heartbeat` | Runs `HEARTBEAT.md` checklist on the **autonomous graph** (read-only by construction). Emits a strict JSON array of `{key, message}` findings; `alerts_sent` table **dedupes by key** so the same item never pings twice. Most runs are silent. |
| 23:30             | `consolidate` | Compresses today's note. |

`core/notify.py` handles outbound owner messages: **quiet hours 23:00–07:30**
queue non-urgent messages (flushed at 07:35); urgent ones go through. Long
messages are chunked to Telegram's 4096-char limit.

---

## 12. Channels (`channels/*.py`)

All four channels are thin adapters over the **same graph**; they differ only in
how they render the approval interrupt and collect the decision.

| Channel | Transport | Approval UX | Memory hook | Persistence |
|---------|-----------|-------------|-------------|-------------|
| **telegram** | long-poll | inline ✅/❌/✏️ buttons; Edit = free-text feedback | yes | `friday.db` thread `tg:<chat>` |
| **cli** | stdin REPL | `y / n / feedback` prompt | yes | `friday.db` thread `cli:default` |
| **whatsapp** | WebSocket → Node bridge | text reply `yes / no / changes` | yes | `friday.db` thread `wa:<jid>` |
| **web** | Chainlit | Approve/Reject action buttons | yes | `friday.db` thread `web:default` |

Common pattern in every channel:

```python
while True:
    result = await graph.ainvoke(payload, config)
    if result.get("__interrupt__"):        # approval needed
        show preview; collect decision
        payload = Command(resume={"decision": ..., "reason": ...})
        continue
    show last_ai_text(result); fire memory.after_turn(...); break
```

**Telegram specifics:** owner-only guard (`_is_owner`), `/new` rotates the thread,
`/status` shows tool count, `/briefing` triggers one on demand. Voice notes are
transcribed locally with faster-whisper (`uv sync --extra voice`) then treated as
text. A typing indicator is kept alive while the graph runs.

**WhatsApp bridge** (`bridge/whatsapp/bridge.js`): a Baileys Node sidecar that
exposes a localhost-only WebSocket. Protocol:
`bridge→friday {"from": jid, "text": …}` / `friday→bridge {"to": jid, "text": …}`.
⚠ Unofficial WhatsApp automation violates ToS (ban risk) — use a secondary number,
keep `wa-auth/` (credential material) chmod 700.

---

## 13. Persistence (`core/db.py` + `friday.db`)

**One SQLite file holds everything**, in WAL mode so the checkpointer's async
connection and our sync connections coexist:

- **LangGraph checkpoint tables** (created by `AsyncSqliteSaver`) — conversations
  and pending interrupts.
- `memories` — id, created_at, kind (CHECK constraint), text, source, deleted.
- `vec_memories` — `vec0` virtual table, `float[768]`, cosine distance.
- `tool_audit` — append-only log of every gated decision
  (`auto_allowed|approved|rejected|blocked|error`).
- `alerts_sent` — heartbeat dedup keys.

`sqlite-vec` is loaded as an extension per connection; if unavailable, vector
recall is disabled and the code falls back to keyword search.

**Backup = zip the folder** (`scripts/backup.sh`). The whole brain is this
directory.

---

## 14. Cross-cutting design rules

- **Tiers, not models.** Swap providers via `config/models.json` only.
- **Capabilities are data.** New integration → `mcp.json`. New specialist →
  `agents/*.py`. New workflow → `skills/*.md`. New personality → `identity/*.md`.
- **Fail safe.** Unknown tools require approval; background jobs can't write;
  router/recall/memory-hook failures degrade to a working default, never a crash.
- **Untrusted content is data, not instructions** (prompt-injection defense lives
  in SOUL.md rule 3 and the research profile).
- **Graceful offline degradation** everywhere: no embeddings, no Docker, no
  Phoenix, one dead MCP server — each fails soft.
- **Everything observable.** Phoenix traces (LiteLLM spans) + the `tool_audit`
  table.

---

## 15. Request lifecycle — a worked example

> *User (Telegram): "Reply to Priya's email confirming the 3 PM demo."*

1. **telegram.`_on_message`** → owner check → `graph.ainvoke({messages:[Human]},
   {thread_id: "tg:<chat>"})`.
2. **route** → router classifies `email` (high confidence) → profile `email`;
   `recall("reply to Priya…")` pulls any relevant memories; `email_style` skill
   matches (trigger "reply" + agent "email"). `context_block` assembled.
3. **agent** (standard tier) → system prompt = SOUL + USER + clock + email tools
   digest + email instructions + email_style + memories. The LLM calls
   `gmail_search_emails` to find Priya's thread.
4. **tool_gate** → `search` matches auto-allow → runs immediately.
5. **agent** → reads the thread, drafts a reply, calls `gmail_send_email`.
6. **tool_gate** → `send_` requires approval → `interrupt()` with a preview of the
   draft. Graph pauses; Telegram shows the draft + ✅/❌/✏️.
7. User taps ✅ → channel resumes with `Command(resume={"decision":"approve"})` →
   gate returns `approved` → **tools** sends the email.
8. **agent** → confirms in ≤3 lines → no tool calls → `END`.
9. **after_turn** (background) → logs the turn to today's note, maybe extracts a
   durable fact (e.g. a recurring contact) at ≥0.8 confidence.

---

## 16. Where to start reading

| If you want to understand… | Read |
|----------------------------|------|
| The whole control flow | `core/graph.py` (top docstring + the four nodes) |
| How a message picks an agent | `core/router.py` + `agents/registry.py` |
| The safety guarantees | `core/approval.py` + `config/policies.json` + SOUL.md |
| Adding an integration | `core/tools.py` + `config/mcp.json` |
| Adding a specialist | any `agents/<x>.py` + `agents/registry.py` |
| Memory behavior | `core/memory.py` (docstring lists every piece) |
| Proactive features | `core/scheduler.py` → `agents/briefing.py`, `heartbeat.py` |
| A channel's quirks | the docstring at the top of each `channels/*.py` |

---

## 17. Running & testing

```bash
uv sync                              # + --extra voice / --extra web / --extra dev
uv run python -m tests.smoke         # offline: db, graph, policies, memory, skills
uv run python -m tests.eval_routing  # needs a live fast tier; <90% = fix router prompt
uv run python -m channels.cli        # talk to it in the terminal
uv run phoenix serve                 # traces at http://localhost:6006
uv run python main.py                # the real thing (Telegram)
```

`tests/smoke.py` is the fastest way to confirm the engine is intact after a
change — it exercises every core module with injected vectors and no network.
