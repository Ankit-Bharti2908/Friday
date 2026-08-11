"""OAuth 2.1 for remote MCP servers that don't take a static API key.

Some MCP servers (Swiggy's, for one) authenticate with OAuth 2.1 + PKCE and
RFC 7591 Dynamic Client Registration: there is no key to paste into .env, so
the owner authorizes ONCE in a browser and Friday keeps the refresh token.

    uv run python -m core.mcp_auth login swiggy_food     # one-time, opens a browser
    uv run python -m core.mcp_auth status                # what's authorized
    uv run python -m core.mcp_auth logout swiggy_food    # forget the tokens

Mark a server `"oauth": true` in config/mcp.json and core/tools.py wires the
provider in automatically; until the login has happened that server is skipped
with a one-line hint, so Friday still boots with every other tool.

Tokens live in memory/oauth/<server>.json (chmod 600, gitignored). They are
account credentials — a Swiggy token can place real orders — so treat that
directory like .env.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from core import settings

log = logging.getLogger("friday.mcp_auth")

OAUTH_DIR = settings.MEMORY_DIR / "oauth"
# RFC 8252: loopback redirects are the right shape for a local app. Swiggy
# whitelists http://127.0.0.1/callback; the port is owner-configurable in case
# something else already holds this one.
CALLBACK_PORT = int(os.getenv("FRIDAY_OAUTH_PORT", "8976"))
REDIRECT_URI = f"http://127.0.0.1:{CALLBACK_PORT}/callback"


def _token_path(server: str):
    return OAUTH_DIR / f"{server}.json"


class FileTokenStorage(TokenStorage):
    """Token + registered-client store, one JSON file per MCP server."""

    def __init__(self, server: str) -> None:
        self.server = server
        self.path = _token_path(server)

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("oauth store for %s unreadable (%s) — re-login needed", self.server, exc)
            return {}

    def _write(self, data: dict) -> None:
        OAUTH_DIR.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(self.path, 0o600)  # credential material

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._read().get("client")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)


def is_authorized(server: str) -> bool:
    """Cheap sync check used at boot — has this server ever been logged in?"""
    path = _token_path(server)
    if not path.exists():
        return False
    try:
        tokens = json.loads(path.read_text(encoding="utf-8")).get("tokens") or {}
    except (OSError, json.JSONDecodeError):
        return False
    # A refresh token alone is enough: the provider will mint a fresh access token.
    return bool(tokens.get("access_token") or tokens.get("refresh_token"))


class _CallbackHandler(BaseHTTPRequestHandler):
    """Catches the one redirect the authorization server sends back."""

    captured: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        params = parse_qs(urlparse(self.path).query)
        _CallbackHandler.captured = {k: v[0] for k, v in params.items()}
        ok = "code" in _CallbackHandler.captured
        body = (
            "<h2>Friday is connected.</h2><p>You can close this tab.</p>"
            if ok
            else f"<h2>Authorization failed</h2><pre>{_CallbackHandler.captured}</pre>"
        )
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *_args) -> None:  # silence the default stderr spam
        return


def provider_for(server: str, url: str) -> OAuthClientProvider:
    """Build the httpx.Auth provider for a server. During normal runs no browser
    is available, so the handlers only fire if the stored grant is gone — they
    tell the owner to re-run the login instead of hanging on a dead prompt."""

    async def _no_browser(authorization_url: str) -> None:
        raise RuntimeError(
            f"{server} needs authorization (the stored grant is missing or expired). "
            f"Run: uv run python -m core.mcp_auth login {server}"
        )

    async def _no_callback() -> tuple[str, str | None]:
        raise RuntimeError(f"{server}: no interactive callback available outside login")

    return OAuthClientProvider(
        server_url=url,
        client_metadata=_client_metadata(),
        storage=FileTokenStorage(server),
        redirect_handler=_no_browser,
        callback_handler=_no_callback,
    )


def _client_metadata():
    from mcp.shared.auth import OAuthClientMetadata

    return OAuthClientMetadata(
        client_name="Friday personal assistant",
        redirect_uris=[REDIRECT_URI],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",  # public client — PKCE is the protection
    )


async def login(server: str) -> None:
    """One-time interactive authorization: registers the client (DCR), opens the
    consent page, catches the loopback redirect, and stores the tokens."""
    conf = settings.MCP.get("servers", {}).get(server) or settings.MCP.get("servers", {}).get(f"_{server}")
    if not conf:
        raise SystemExit(f"no server '{server}' in config/mcp.json")
    url = conf.get("url")
    if not url:
        raise SystemExit(f"server '{server}' has no url — OAuth login only applies to remote servers")

    httpd = HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
    _CallbackHandler.captured = {}
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    async def _open(authorization_url: str) -> None:
        print(f"\nAuthorize Friday here:\n\n  {authorization_url}\n")
        try:
            webbrowser.open(authorization_url)
        except Exception:
            pass
        print(f"(waiting for the redirect to {REDIRECT_URI} …)")

    async def _wait() -> tuple[str, str | None]:
        for _ in range(600):  # ~5 min at 0.5 s
            if _CallbackHandler.captured:
                got = _CallbackHandler.captured
                if "code" not in got:
                    raise SystemExit(f"authorization failed: {got}")
                return got["code"], got.get("state")
            await asyncio.sleep(0.5)
        raise SystemExit("timed out waiting for the browser redirect")

    provider = OAuthClientProvider(
        server_url=url,
        client_metadata=_client_metadata(),
        storage=FileTokenStorage(server),
        redirect_handler=_open,
        callback_handler=_wait,
    )

    # Touching the server with the provider attached drives the whole dance:
    # 401 -> discovery -> dynamic registration -> PKCE authorize -> token.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    try:
        async with streamablehttp_client(url, auth=provider) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                print(f"\n✅ {server} authorized — {len(tools.tools)} tools available:")
                for t in tools.tools:
                    print(f"   · {t.name}")
        print(f"\nTokens stored in {_token_path(server)} (chmod 600). "
              f"Enable the server in config/mcp.json if it still has a '_' prefix.")
    finally:
        httpd.shutdown()


def _status() -> None:
    servers = settings.MCP.get("servers", {})
    oauth_servers = [n for n, c in servers.items() if c.get("oauth")]
    if not oauth_servers:
        print("no OAuth MCP servers configured")
        return
    for name in oauth_servers:
        state = "authorized" if is_authorized(name.lstrip("_")) else "NOT authorized"
        enabled = "disabled (leading _)" if name.startswith("_") else "enabled"
        print(f"{name:24} {state:16} {enabled}")


def _logout(server: str) -> None:
    path = _token_path(server)
    if path.exists():
        path.unlink()
        print(f"forgot {server} tokens ({path})")
    else:
        print(f"{server} was not authorized")


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    match sys.argv[1:]:
        case ["login", name]:
            asyncio.run(login(name))
        case ["logout", name]:
            _logout(name)
        case ["status"]:
            _status()
        case _:
            raise SystemExit(
                "usage: python -m core.mcp_auth login <server> | logout <server> | status"
            )
