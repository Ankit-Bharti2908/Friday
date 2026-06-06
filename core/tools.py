"""MCP tool loading.

config/mcp.json lists servers in Claude-Desktop style. Conventions:
  - server names starting with "_" are disabled templates (skipped)
  - ${ENV_VAR} references are substituted by settings.load_config
  - each server is loaded in isolation: one broken server never takes
    down the rest (you still get filesystem if google OAuth is misconfigured)

Adding a capability to Friday = adding an entry to mcp.json. That's the
whole modularity story.
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from core import approval, settings

log = logging.getLogger("friday.tools")


def _enabled_servers() -> dict[str, dict[str, Any]]:
    servers = settings.MCP.get("servers", {})
    return {name: conf for name, conf in servers.items() if not name.startswith("_")}


async def load_mcp_tools() -> list[BaseTool]:
    """Connect to every enabled MCP server and return the merged tool list."""
    tools: list[BaseTool] = []
    for name, conf in _enabled_servers().items():
        try:
            client = MultiServerMCPClient({name: conf})
            server_tools = await client.get_tools()
            kept = [t for t in server_tools if not approval.is_blocked(t.name)]
            tools.extend(kept)
            log.info("MCP '%s': %d tools loaded", name, len(kept))
        except Exception as exc:
            log.error("MCP '%s' failed to load (%s) — continuing without it", name, exc)

    if len(tools) > 40:
        log.warning(
            "%d tools exposed — consider trimming servers/toolsets; "
            "large tool lists burn context and confuse routing",
            len(tools),
        )
    return tools
