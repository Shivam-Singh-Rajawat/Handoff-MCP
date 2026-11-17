"""MCP tools for local stdio and authenticated single-tenant HTTP."""
from contextlib import AsyncExitStack, asynccontextmanager
import hmac
import json
import os
import re
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse

from .providers import Model
from .store import Handoffs
from .web import workspace_routes


def create_server(handoffs: Handoffs, hosts: list[str] | None = None) -> FastMCP:
    security = None
    if hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=[f"https://{host}" for host in hosts],
        )
    mcp = FastMCP("model-handoff", json_response=True, stateless_http=True,
                  transport_security=security)

    @mcp.tool()
    def ask_model(target: Model, task: str, context: str, confirmed: bool = False) -> dict[str, Any]:
        """Send the supplied context to a helper model. Set confirmed only after the
        user has authorized sending that context to the selected provider."""
        if not confirmed:
            raise ValueError("Confirm with the user before sending the selected context.")
        return handoffs.ask(target, task, context)

    @mcp.tool()
    def get_handoff(handoff_id: str) -> dict[str, Any]:
        """Retrieve a handoff, including the context, response, and status."""
        return handoffs.get(handoff_id)

    @mcp.tool()
    def retry_handoff(handoff_id: str, confirmed: bool = False) -> dict[str, Any]:
        """Retry a failed handoff using the original context after user authorization."""
        if not confirmed:
            raise ValueError("Confirm with the user before retrying the handoff.")
        return handoffs.retry(handoff_id)

    return mcp


def http_app(handoffs: Handoffs, token: str, hosts: list[str],
             accounts: dict[str, str] | None = None):
    if not hosts or "*" in "".join(hosts):
        raise ValueError("Set HANDOFF_HTTP_HOSTS to exact allowed Host header values.")
    if accounts is not None and not isinstance(accounts, dict):
        raise ValueError("HANDOFF_ACCOUNTS must be a JSON object.")
    tokens = {"owner": token}
    for name, secret in (accounts or {}).items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) or name == "owner":
            raise ValueError("Invalid workspace account name.")
        tokens[name] = secret
    if any(not isinstance(secret, str) or not re.fullmatch(r"[!-~]{32,}", secret)
           for secret in tokens.values()):
        raise ValueError("Workspace tokens must contain at least 32 printable ASCII characters.")
    if len(set(tokens.values())) != len(tokens):
        raise ValueError("Workspace tokens must be distinct.")

    def workspace_app(store):
        app = create_server(store, hosts).streamable_http_app()
        app.router.routes.extend(workspace_routes(store))
        return app

    owner = workspace_app(handoffs)
    workspaces = [(f"Bearer {token}".encode(), owner)]
    for name, secret in tokens.items():
        if name != "owner":
            store = Handoffs(handoffs.path.parent / "accounts" / name / "history.sqlite",
                             handoffs.providers)
            workspaces.append((f"Bearer {secret}".encode(), workspace_app(store)))
    public_assets = {"/", "/workspace.css", "/workspace.js", "/healthz"}

    @asynccontextmanager
    async def lifespan(_):
        async with AsyncExitStack() as stack:
            for _, app in workspaces:
                await stack.enter_async_context(app.router.lifespan_context(app))
            yield

    lifecycle = Starlette(lifespan=lifespan)

    async def authenticate(scope, receive, send):
        if scope["type"] == "lifespan":
            return await lifecycle(scope, receive, send)
        app = owner
        if scope["type"] == "http" and scope["path"] not in public_assets:
            supplied = dict(scope["headers"]).get(b"authorization", b"")
            app = None
            for authorization, workspace in workspaces:
                if hmac.compare_digest(supplied, authorization):
                    app = workspace
            if app is None:
                await PlainTextResponse("Unauthorized", status_code=401,
                                        headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
                return

        async def protect_response(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), (b"cache-control", b"no-store")]
            await send(message)

        await app(scope, receive, protect_response)

    return authenticate


def serve_http(handoffs: Handoffs, host: str, port: int):
    token = os.getenv("HANDOFF_HTTP_TOKEN", "")
    hosts = os.getenv("HANDOFF_HTTP_HOSTS")
    if hosts is None:
        hosts = os.getenv("RENDER_EXTERNAL_HOSTNAME") or f"localhost:{port},127.0.0.1:{port}"
    hosts = hosts.split(",")
    try:
        accounts = json.loads(os.getenv("HANDOFF_ACCOUNTS", "{}"))
    except json.JSONDecodeError:
        raise ValueError("HANDOFF_ACCOUNTS must be valid JSON.") from None
    app = http_app(handoffs, token, [entry.strip() for entry in hosts if entry.strip()], accounts)
    uvicorn.run(app, host=host, port=port)
