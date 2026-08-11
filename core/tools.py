"""MCP tool loading.

config/mcp.json lists servers in Claude-Desktop style. Conventions:
  - server names starting with "_" are disabled templates (skipped)
  - ${ENV_VAR} references are substituted by settings.load_config
  - "oauth": true means the server authenticates with OAuth 2.1 + PKCE
    (no API key to paste) — core/mcp_auth.py supplies the provider, and an
    un-authorized server is skipped with a hint instead of erroring
  - each server is loaded in isolation: one broken server never takes
    down the rest (you still get filesystem if google OAuth is misconfigured)

Every loaded tool is tagged with its source server in `tool.metadata`
("mcp_server"), so an agent profile can claim a whole server's tools without
guessing their names — see registry.filter_tools / AgentProfile.tool_servers.

Adding a capability to Friday = adding an entry to mcp.json. That's the
whole modularity story.
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from core import approval, mcp_auth, settings

log = logging.getLogger("friday.tools")


def _enabled_servers() -> dict[str, dict[str, Any]]:
    servers = settings.MCP.get("servers", {})
    return {name: conf for name, conf in servers.items() if not name.startswith("_")}


def _tag(tool: BaseTool, server: str) -> BaseTool:
    """Record which server a tool came from (profiles filter on it)."""
    try:
        tool.metadata = {**(tool.metadata or {}), "mcp_server": server}
    except Exception:  # a tool type that won't take metadata still works fine
        log.debug("could not tag %s with its server", tool.name)
    return tool


def _prepare(name: str, conf: dict[str, Any]) -> dict[str, Any] | None:
    """Config the adapter can consume: resolves the `oauth` marker into an auth
    provider. None means 'skip this server' (not authorized yet)."""
    conf = dict(conf)  # never mutate the loaded config
    if not conf.pop("oauth", False):
        return conf
    if not mcp_auth.is_authorized(name):
        log.info(
            "MCP '%s': not authorized yet — run `uv run python -m core.mcp_auth login %s` "
            "to connect it (skipping for now)", name, name
        )
        return None
    conf["auth"] = mcp_auth.provider_for(name, conf["url"])
    return conf


async def load_mcp_tools() -> list[BaseTool]:
    """Connect to every enabled MCP server and return the merged tool list."""
    tools: list[BaseTool] = []
    for name, conf in _enabled_servers().items():
        try:
            prepared = _prepare(name, conf)
            if prepared is None:
                continue
            client = MultiServerMCPClient({name: prepared})
            server_tools = await client.get_tools()
            kept = [_tag(t, name) for t in server_tools if not approval.is_blocked(t.name)]
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
