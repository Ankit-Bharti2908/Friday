# Friday — personal AI assistant (all phases)

One Python process. SQLite + markdown. No Docker required (except the optional
code sandbox), no Postgres, no proxy servers.

**What it does:** Telegram (text + voice) and optional WhatsApp/web gateways →
intent router picks a specialist agent (email / calendar / coder / research /
gym trainer / general) → MCP tools do the work → every risky action pauses for
your Approve / Reject / Edit → it remembers you across sessions → 8 AM briefing,
30-min heartbeat alerts, daily workout alert, nightly note consolidation →
everything traced in Phoenix.

## Quickstart

```bash
# 1. deps (Python 3.11+, Node for MCP servers, a local LLM backend recommended)
uv sync                          # + --extra voice / --extra web if wanted

# local-first model (pick one; both are keyless):
scripts/llamacpp.sh              # llama.cpp → Gemma 3n E4B on :8080  (primary)
ollama pull llama3.1:8b && ollama pull nomic-embed-text   # Ollama fallback + embeddings

# 2. secrets
cp .env.example .env             # bot token, owner id; cloud LLM key OPTIONAL

# 3. offline sanity check
uv run python -m tests.smoke

# 4. talk to it in the terminal
uv run python -m channels.cli

# 5. observability (separate terminal)
uv run phoenix serve             # http://localhost:6006

# 6. the real thing
uv run python main.py            # message your bot on Telegram
```

## Channels
- **Telegram (primary):** @BotFather token + your id from @userinfobot → `.env`.
  Text and voice notes (voice needs `uv sync --extra voice`). /new /status /briefing.
- **WhatsApp (optional, ToS/ban risk — secondary number recommended):**
  `cd bridge/whatsapp && npm install && npm start`, scan the QR, set
  `WHATSAPP_ENABLED=1` + `WHATSAPP_OWNER_JID` in `.env`. Approvals are text
  replies (yes / no / your changes). `wa-auth/` is credential material: chmod 700.
- **Web (optional):** `uv sync --extra web && uv run chainlit run channels/web.py`. A
  full connector like the others — persists to `friday.db` (thread `web:default`) and
  feeds the same shared long-term memory. Run it alongside `main.py`; both share the db.

## The agentic core
- **Router** (`core/router.py`): fast-tier classification → profile. Sticky: low confidence
  or a short follow-up ("yes", "??") continues with the previous specialist, else general.
- **Profiles** (`agents/*.py`): tier + tool subset + instructions. New subagent = one small file.
- **Skills** (`skills/*.md`): drop a markdown file with triggers; it's injected when matched.
  Re-read every message — edit a skill, behavior changes immediately. Ships with
  `email_style`, `daily_note_format`, `fitness_intake`, `gym_program_design`.
- **Memory** (`core/memory.py`): SQLite + sqlite-vec is the truth, `memory/MEMORY.md` is the
  mirror. Recall is injected each turn; a post-turn hook extracts durable facts (≥0.8
  confidence); say "remember/forget X" for explicit control. Daily notes in `memory/notes/`,
  consolidated nightly at 23:30.
- **Gym trainer** (`agents/gym.py` + `core/fitness.py`): say "make me a workout plan" — it
  runs a coach-style intake interview (goal, schedule, equipment, health screen), assesses
  your level, and proposes a 7-day plan you approve. Profile/plan/log are editable markdown
  in `memory/fitness/`; every morning (`FRIDAY_WORKOUT_ALERT`, default 06:30) it pings you
  with that day's session. Knowledge lives in `skills/fitness_intake.md` + `gym_program_design.md`.
- **Reflection:** two consecutive tool failures trigger a forced critique-and-change-approach.
- **Proactivity** (`core/scheduler.py`): briefing 08:00, heartbeat every 30 min 08–22 (runs
  `identity/HEARTBEAT.md` on the read-only autonomous graph; findings deduped via `alerts_sent`
  so nothing pings twice), workout alert at `FRIDAY_WORKOUT_ALERT` (no LLM — sends today's
  section of `memory/fitness/PLAN.md`, deduped per day), quiet-hours queue flushed 07:35.

## Safety model (don't weaken these)
1. Channel allowlists are hardcoded to you (Telegram user id, WhatsApp JID).
2. Reads run free; `send_/create_/delete_/run_…` pause for approval; **unknown tools
   require approval by default** (`config/policies.json`). Audit: `tool_audit` table.
3. Background jobs use an autonomous graph that **auto-rejects** all writes.
4. Web/email content is treated as data, not instructions (see SOUL.md rule 3).
5. `run_python` executes in a no-network Docker container AND is approval-gated.
6. Secrets only in `.env`; configs reference `${VAR}` names.

## MCP tools
`config/mcp.json`, Claude-Desktop style. `filesystem` ships enabled (scoped to
`FRIDAY_WORKSPACE`). Remove the `_` prefix to enable after configuring:
`_github_remote` (hosted GitHub MCP, needs `GITHUB_TOKEN`) and `_google_workspace`
(Gmail+Calendar via `workspace-mcp` — follow that project's README for OAuth, then
pin its version). One broken server never blocks the others.

## Model tiers
`config/models.json`: `fast` / `standard` / `deep`, each `[primary, …fallbacks]` in
LiteLLM naming. Router/heartbeat/extraction run on `fast`, research synthesis on `deep`.
Switching providers = JSON edit.

**Local-first.** Every tier leads with **llama.cpp → Gemma 3n E4B** (`scripts/llamacpp.sh`,
OpenAI-compatible on `:8080`), then **Ollama**, then any cloud keys you set. Swap the
local variant with `scripts/llamacpp.sh e2b` (lighter) or pass any GGUF repo —
`scripts/llamacpp.sh ggml-org/gemma-3-12b-it-GGUF`. An unstarted local server just falls
through to the next entry, so cloud keys remain a safe optional backstop.

## Evals & ops
- Routing regression: `uv run python -m tests.eval_routing` (needs a live fast tier; <90% = fix prompt).
- Offline tests: `uv run python -m tests.smoke`.
- Backups: `scripts/backup.sh` (cron it nightly; restores = unzip).

## Layout
```
core/      settings · db · llm · prompts · approval · tools · graph · router · skills · memory · fitness · notify · scheduler · sandbox
agents/    registry + profiles (email, research, coder, calendar, gym) · briefing · heartbeat
channels/  telegram · cli · whatsapp · web
bridge/    whatsapp/ (Baileys Node sidecar)
identity/  SOUL.md · USER.md · HEARTBEAT.md      skills/  *.md
memory/    MEMORY.md (mirror) · notes/ · fitness/ config/  models · mcp · policies
tests/     smoke · routing_cases · eval_routing   scripts/ backup.sh
```

## Troubleshooting
- First local-model call slow → llama.cpp/Ollama cold start (weights loading into RAM/VRAM).
- Local tier always falling back to cloud → is `scripts/llamacpp.sh` running and on `:8080`? `curl localhost:8080/v1/models`.
- Google OAuth dies weekly → consent screen in Testing mode; publish or re-auth.
- Heartbeat too chatty → cut `HEARTBEAT.md` lines; it should usually find nothing.
- No traces → is `phoenix serve` running? `FRIDAY_TRACING=0` disables cleanly.
- WhatsApp silent → is the bridge running and paired? Check `wa-auth/` exists.
