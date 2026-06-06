# Friday — personal AI assistant

One Python process. SQLite + markdown. No Docker, no Postgres.
Telegram in, MCP tools out, every risky action pauses for your approval,
everything traced in Phoenix.

## Quickstart

```bash
# 1. deps (Python 3.11+, Node for MCP servers, Ollama optional-but-recommended)
uv sync
ollama pull llama3.1:8b && ollama pull nomic-embed-text   # local "fast" tier

# 2. secrets
cp .env.example .env        # fill in: bot token, owner id, one LLM key

# 3. sanity check (offline, no keys needed)
uv run python -m tests.smoke

# 4. talk to it in the terminal first
uv run python -m channels.cli

# 5. observability (separate terminal, optional but recommended)
uv run phoenix serve        # UI: http://localhost:6006

# 6. the real thing
uv run python main.py       # message your bot on Telegram
```

## Telegram setup
1. @BotFather → /newbot → copy token into `.env` (`TELEGRAM_BOT_TOKEN`)
2. Message @userinfobot → copy your numeric id into `.env` (`TELEGRAM_OWNER_ID`)
3. `uv run python main.py`, then message your bot. Anyone who isn't you is ignored.

## Adding capabilities (MCP)
Everything Friday can *do* comes from MCP servers in `config/mcp.json`.
Ships with `filesystem` enabled (scoped to `FRIDAY_WORKSPACE`). Two disabled
templates are included — remove the leading `_` to enable after configuring:

- `_github_remote` — GitHub's hosted MCP; needs `GITHUB_TOKEN` (PAT) in `.env`.
- `_google_workspace` — Gmail + Calendar via the `workspace-mcp` package
  (`uvx workspace-mcp`). Follow that project's README for Google OAuth setup
  (client id/secret from Google Cloud Console), then set the env vars it needs.
  Check its docs for current flags — pin the version once it works.

Servers starting with `_` are skipped. Secrets never go in this file — use
`${ENV_VAR}` references.

## How approval works
Reads (`get_/list_/search_/…`) run freely. Writes (`send_/create_/delete_/…`)
pause the graph and ping you with Approve / Reject / Edit buttons. *Edit* =
send free-text feedback; the agent revises and asks again. Unknown tools
require approval by default. Rules: `config/policies.json`. Audit trail:
`tool_audit` table in `friday.db`.

## Identity
- `identity/SOUL.md` — personality + hard rules (edit freely)
- `identity/USER.md` — facts about you, injected every turn
- `memory/MEMORY.md` — long-term notes (auto-curated from Phase 2)

## Model tiers
`config/models.json` defines `fast` / `standard` / `deep`, each an ordered
[primary, …fallbacks] list in LiteLLM naming (`ollama/…`, `anthropic/…`,
`openai/…`, `openrouter/…`). Code only ever references tiers — switching
providers is a JSON edit. Pin models you actually have access to.

## Project layout
```
core/      settings · db · llm (tiers) · prompts · approval · tools (MCP) · graph
channels/  telegram (buttons, allowlist) · cli (dev REPL)
agents/    (Phase 2+: briefing, email, research, coder, calendar)
identity/  SOUL.md · USER.md · HEARTBEAT.md
memory/    MEMORY.md · notes/
config/    models.json · mcp.json · policies.json
tests/     smoke.py
friday.db  checkpoints + memories + audit (auto-created)
```

## Troubleshooting
- **First local-model call times out** → Ollama cold start; it warms up after one call.
- **Google OAuth dies weekly** → consent screen is in Testing mode; publish it or re-auth.
- **MCP server fails to load** → others still load; check the log line, fix that server's config.
- **No traces** → is `phoenix serve` running? `FRIDAY_TRACING=0` disables cleanly.
