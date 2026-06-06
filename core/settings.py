"""Central settings: paths, env vars, and JSON config loading.

Secrets live ONLY in .env / OS environment. JSON configs may reference them
as ${VAR_NAME}; they are substituted at load time.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

log = logging.getLogger("friday.settings")

# --- paths -----------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "friday.db"
IDENTITY_DIR = ROOT / "identity"
MEMORY_DIR = ROOT / "memory"
NOTES_DIR = MEMORY_DIR / "notes"
SKILLS_DIR = ROOT / "skills"
CONFIG_DIR = ROOT / "config"

load_dotenv(ROOT / ".env")

# --- env -------------------------------------------------------------------
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_OWNER_ID: int = int(os.getenv("TELEGRAM_OWNER_ID", "0") or 0)
TRACING_ENABLED: bool = os.getenv("FRIDAY_TRACING", "1") not in ("0", "false", "")

WORKSPACE = Path(os.path.expanduser(os.getenv("FRIDAY_WORKSPACE", "~/friday-workspace")))
WORKSPACE.mkdir(parents=True, exist_ok=True)
os.environ["FRIDAY_WORKSPACE"] = str(WORKSPACE)  # so ${FRIDAY_WORKSPACE} resolves in mcp.json

TIMEZONE = os.getenv("FRIDAY_TZ", "Asia/Kolkata")

# --- config loading --------------------------------------------------------
_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _substitute_env(value: Any) -> Any:
    """Recursively replace ${VAR} with environment values in a parsed JSON tree."""
    if isinstance(value, str):

        def repl(match: re.Match[str]) -> str:
            var = match.group(1)
            resolved = os.getenv(var)
            if resolved is None:
                log.warning("config references ${%s} but it is not set in the environment", var)
                return match.group(0)
            return resolved

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


def load_config(name: str) -> dict[str, Any]:
    """Load config/<name>.json with ${ENV} substitution applied."""
    path = CONFIG_DIR / f"{name}.json"
    with open(path, encoding="utf-8") as fh:
        return _substitute_env(json.load(fh))


MODELS = load_config("models")
POLICIES = load_config("policies")
MCP = load_config("mcp")


def validate() -> list[str]:
    """Return a list of human-readable problems (empty = good to go)."""
    problems: list[str] = []
    if not TELEGRAM_BOT_TOKEN:
        problems.append("TELEGRAM_BOT_TOKEN is not set (Telegram channel will not start)")
    if not TELEGRAM_OWNER_ID:
        problems.append("TELEGRAM_OWNER_ID is not set (bot would answer NOBODY — it allowlists you)")
    if not any(os.getenv(k) for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")):
        problems.append("no cloud LLM key set — only local Ollama tiers will work")
    return problems
