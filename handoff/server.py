"""MCP tools for local stdio and authenticated single-tenant HTTP."""
import hmac
import os
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
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


def http_app(handoffs: Handoffs, token: str, hosts: list[str]):
    if len(token) < 32:
        raise ValueError("HANDOFF_HTTP_TOKEN must contain at least 32 characters.")
    if not hosts or "*" in "".join(hosts):
        raise ValueError("Set HANDOFF_HTTP_HOSTS to exact allowed Host header values.")
    app = create_server(handoffs, hosts).streamable_http_app()
    app.router.routes.extend(workspace_routes(handoffs))
    public_assets = {"/", "/workspace.css", "/workspace.js", "/healthz"}
    authorization = f"Bearer {token}".encode()

    async def authenticate(scope, receive, send):
        if scope["type"] == "http" and scope["path"] not in public_assets:
            supplied = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, authorization):
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
    app = http_app(handoffs, token, [entry.strip() for entry in hosts if entry.strip()])
    uvicorn.run(app, host=host, port=port)
