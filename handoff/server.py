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
from starlette.responses import PlainTextResponse, JSONResponse, RedirectResponse

from .auth import OAuth
from .native_auth import NativeAuth
from .jobs import Jobs
from .projects import Projects
from .providers import Model
from .store import Handoffs
from .web import workspace_routes


def create_server(handoffs: Handoffs, hosts: list[str] | None = None, jobs=None) -> FastMCP:
    managed_jobs = jobs is None
    jobs = jobs or Jobs(handoffs)

    @asynccontextmanager
    async def server_lifespan(_):
        if managed_jobs:
            jobs.start()
        try:
            yield {}
        finally:
            if managed_jobs:
                jobs.close()

    security = None
    if hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=[f"https://{host}" for host in hosts],
        )
    mcp = FastMCP("Handoff-MCP", json_response=True, stateless_http=True,
                  transport_security=security, lifespan=server_lifespan)

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

    projects = Projects(handoffs)

    @mcp.tool()
    def create_project(title: str, context: str = "") -> dict[str, Any]:
        """Save reusable project context in the authenticated workspace."""
        return projects.create(title, context)

    @mcp.tool()
    def list_projects() -> list[dict[str, Any]]:
        """List projects available to this workspace and its collaborators."""
        return projects.list()

    @mcp.tool()
    def get_project(project_id: str) -> dict[str, Any]:
        """Load project context and artifact IDs."""
        return projects.get(project_id)

    @mcp.tool()
    def save_artifact(project_id: str, name: str, content: str) -> dict[str, Any]:
        """Save a text report, code, or extracted document for collaborators to reuse."""
        return projects.save(project_id, name, content)

    @mcp.tool()
    def get_artifact(artifact_id: str) -> dict[str, Any]:
        """Read a saved text artifact by ID."""
        return projects.artifact(artifact_id)

    if jobs is not None:
        @mcp.tool()
        def create_run(lead: Model, task: str, context: str, helper: Model | None = None,
                       mode: str = "cross", confirmed: bool = False,
                       request_key: str | None = None) -> dict[str, Any]:
            """Queue a durable collaboration run. Poll get_run for its result.
            Set confirmed when the user has authorized sharing the supplied context."""
            if not confirmed:
                raise ValueError("Authorize sharing the selected context.")
            return jobs.submit(lead, task, context, mode, helper, request_key)

        @mcp.tool()
        def retry_run(run_id: str, confirmed: bool = False) -> dict[str, Any]:
            """Queue remaining steps of a failed run under a new budgeted attempt."""
            if not confirmed:
                raise ValueError("Authorize resending the saved context.")
            return jobs.retry(run_id)

        @mcp.tool()
        def get_run(run_id: str) -> dict[str, Any]:
            """Read a queued run, its provider replies, and final answer."""
            return jobs.workspace.get(run_id) or {"error": "Run not found"}

    return mcp


def http_app(handoffs: Handoffs, token: str, hosts: list[str],
             accounts: dict[str, str] | None = None):
    if not hosts or "*" in "".join(hosts):
        raise ValueError("Set HANDOFF_HTTP_HOSTS to exact allowed Host header values.")
    if accounts is not None and not isinstance(accounts, dict):
        raise ValueError("HANDOFF_ACCOUNTS must be a JSON object.")
    tokens = {"owner": token}
    aliases = {}
    for name, secret in (accounts or {}).items():
        if isinstance(secret, dict):
            if set(secret) != {"token", "workspace"}:
                raise ValueError("Account needs token and workspace fields.")
            aliases[name] = secret["workspace"]
            secret = secret["token"]
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) or name == "owner":
            raise ValueError("Invalid workspace account name.")
        tokens[name] = secret
    if any(not isinstance(secret, str) or not re.fullmatch(r"[!-~]{32,}", secret)
           for secret in tokens.values()):
        raise ValueError("Workspace tokens must contain at least 32 printable ASCII characters.")
    if len(set(tokens.values())) != len(tokens):
        raise ValueError("Workspace tokens must be distinct.")

    oauth = OAuth()
    queues = []

    def workspace_app(store):
        jobs = Jobs(store)
        queues.append(jobs)
        app = create_server(store, hosts, jobs).streamable_http_app()
        app.router.routes.extend(workspace_routes(store, jobs))
        return app

    owner = workspace_app(handoffs)
    workspaces = [(f"Bearer {token}".encode(), owner)]
    apps = {"owner": owner}
    for name, secret in tokens.items():
        if name != "owner" and name not in aliases:
            store = Handoffs(handoffs.path.parent / "accounts" / name / "history.sqlite",
                             handoffs.providers.providers, workspace=name)
            apps[name] = workspace_app(store)
            workspaces.append((f"Bearer {secret}".encode(), apps[name]))
    for name, workspace in aliases.items():
        if workspace not in apps:
            raise ValueError("Shared workspace must reference owner or an independent account.")
        workspaces.append((f"Bearer {tokens[name]}".encode(), apps[workspace]))
    native = None
    native_app = None
    if os.getenv("HANDOFF_NATIVE_OAUTH", "false").lower() == "true":
        grants = [(secret, aliases.get(name, name)) for name, secret in tokens.items()]
        native = NativeAuth(handoffs, grants, set(apps))
        native_app = Starlette(routes=native.routes())
    public_assets = {"/", "/workspace.css", "/workspace.js", "/healthz", "/readyz"}

    @asynccontextmanager
    async def lifespan(_):
        async with AsyncExitStack() as stack:
            for app in apps.values():
                await stack.enter_async_context(app.router.lifespan_context(app))
            for queue in queues:
                queue.start()
            try:
                yield
            finally:
                for queue in queues:
                    queue.close()

    lifecycle = Starlette(lifespan=lifespan)

    async def authenticate(scope, receive, send):
        if scope["type"] == "lifespan":
            return await lifecycle(scope, receive, send)
        path = scope.get("path", "")
        if scope["type"] == "http" and path == "/api/auth-config":
            return await JSONResponse({"google": bool(native and native.google_client and native.google_secret),
                                       "native_oauth": bool(native)})(scope, receive, send)
        if scope["type"] == "http" and native and path in {
            "/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"
        }:
            return await native.metadata()(scope, receive, send)
        if scope["type"] == "http" and native and path == "/auth/logout":
            headers = dict(scope["headers"])
            if scope["method"] != "POST" or headers.get(b"origin", b"").decode() != native.origin:
                return await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)
            from starlette.requests import Request
            request = Request(scope)
            session = request.cookies.get("mh_session", "")
            native.get("session", session, consume=True)
            response = JSONResponse({"status": "logged_out"})
            response.delete_cookie("mh_session", secure=True, httponly=True, samesite="lax")
            return await response(scope, receive, send)
        if scope["type"] == "http" and native and path in {
            route.path for route in native_app.routes
        }:
            return await native_app(scope, receive, send)
        if scope["type"] == "http" and oauth.keys and scope["path"] in {
            "/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"
        }:
            return await oauth.metadata()(scope, receive, send)
        app = owner
        if scope["type"] == "http" and scope["path"] not in public_assets:
            supplied = dict(scope["headers"]).get(b"authorization", b"")
            app = None
            for authorization, workspace in workspaces:
                if hmac.compare_digest(supplied, authorization):
                    app = workspace
            if app is None and native:
                workspace_name = await native.authenticate(supplied)
                app = apps.get(workspace_name)
                if app is None and path.startswith("/api/"):
                    from starlette.requests import Request
                    request = Request(scope)
                    session = native.get("session", request.cookies.get("mh_session", ""))
                    if session:
                        if scope["method"] not in {"GET", "HEAD"} and dict(scope["headers"]).get(b"origin", b"").decode() != native.origin:
                            return await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)
                        app = apps.get(session["workspace"])
            if app is None:
                workspace_name = await oauth.authenticate(supplied)
                app = apps.get(workspace_name)
            if app is None:
                challenge = "Bearer"
                if native:
                    challenge += f' resource_metadata="{native.origin}/.well-known/oauth-protected-resource/mcp"'
                elif oauth.keys:
                    challenge += f' resource_metadata="{oauth.resource.removesuffix("/mcp")}/.well-known/oauth-protected-resource/mcp"'
                await PlainTextResponse("Unauthorized", status_code=401,
                                        headers={"WWW-Authenticate": challenge})(scope, receive, send)
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
    uvicorn.run(app, host=host, port=port, access_log=False)
