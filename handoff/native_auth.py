"""OAuth authorization provider for native MCP clients and Google workspace login.

The MCP SDK owns registration, client authentication, PKCE verification, token
request validation, and standard metadata. This module persists grants and handles
the user's explicit workspace consent.
"""
import base64
import hashlib
import hmac
from html import escape
import json
import os
import secrets
import time
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    RefreshToken, RegistrationError, TokenError,
)
from mcp.server.auth.routes import create_auth_routes, build_metadata, cors_middleware
from mcp.server.auth.middleware.client_auth import ClientAuthenticator, AuthenticationError
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, TypeAdapter
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class NativeAuth:
    def __init__(self, store, token_workspaces, workspace_names):
        self.store = store
        self.tokens = token_workspaces
        self.workspace_names = workspace_names
        self.origin = os.environ["HANDOFF_PUBLIC_URL"].rstrip("/")
        parsed = urlparse(self.origin)
        if parsed.scheme != "https" or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("HANDOFF_PUBLIC_URL must be an HTTPS origin.")
        self.resource = self.origin + "/mcp"
        self.google_client = os.getenv("GOOGLE_CLIENT_ID", "")
        self.google_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
        self.google_workspaces = json.loads(os.getenv("HANDOFF_GOOGLE_WORKSPACES", "{}"))
        if not isinstance(self.google_workspaces, dict) or any(
            not isinstance(email, str) or workspace not in workspace_names
            for email, workspace in self.google_workspaces.items()
        ):
            raise ValueError("Map allowed Google emails to configured workspace names.")
        self.google_keys = PyJWKClient("https://www.googleapis.com/oauth2/v3/certs")
        with store.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS auth_record (
                kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                expires DOUBLE PRECISION NOT NULL, PRIMARY KEY (kind,id))""")

    def put(self, kind, key, data, ttl):
        with self.store.connection() as db:
            db.execute("DELETE FROM auth_record WHERE expires < ?", (time.time(),))
            db.execute("INSERT INTO auth_record (kind,id,data,expires) VALUES (?,?,?,?)",
                       (kind, digest(key), json.dumps(data), time.time() + ttl))

    def get(self, kind, key, consume=False):
        with self.store.connection() as db:
            if consume:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM auth_record WHERE kind=? AND id=? AND expires>?",
                             (kind, digest(key), time.time())).fetchone()
            if row and consume:
                db.execute("DELETE FROM auth_record WHERE kind=? AND id=?", (kind, digest(key)))
        return json.loads(row["data"]) if row else None

    async def get_client(self, client_id):
        record = self.get("client", client_id)
        return OAuthClientInformationFull.model_validate(record) if record else None

    async def register_client(self, client_info):
        # Avoid wildcard/custom-scheme redirects. Loopback is available to CLI clients.
        for redirect in client_info.redirect_uris or []:
            url = urlparse(str(redirect))
            if (url.scheme != "https" and not (
                url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
            )) or url.fragment or url.username or url.password:
                raise RegistrationError("invalid_redirect_uri", "Use HTTPS or a loopback callback.")
        with self.store.connection() as db:
            count = db.execute("SELECT COUNT(*) FROM auth_record WHERE kind='client' AND expires>?",
                               (time.time(),)).fetchone()[0]
        if count >= 500:
            raise RegistrationError("invalid_client_metadata", "Client registration limit reached.")
        self.put("client", client_info.client_id, client_info.model_dump(mode="json"), 90 * 86400)

    async def authorize(self, client, params):
        if params.resource != self.resource:
            raise AuthorizeError("invalid_request", "Request the exact MCP resource URL.")
        if set(params.scopes or []) - {"handoff", "offline_access"}:
            raise AuthorizeError("invalid_scope", "Unsupported scope.")
        state = secrets.token_urlsafe(32)
        self.put("pending", state, {
            "client_id": client.client_id, "client_name": client.client_name or "MCP client",
            "params": params.model_dump(mode="json"),
        }, 600)
        return self.origin + "/oauth/consent?" + urlencode({"transaction": state})

    async def load_authorization_code(self, client, authorization_code):
        record = self.get("code", authorization_code)
        if not record or record["client_id"] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, **record)

    def issue_tokens(self, client_id, workspace, scopes, family=None):
        family = family or secrets.token_urlsafe(32)
        access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        now = int(time.time())
        self.put("access", access, dict(client_id=client_id, subject=workspace, scopes=scopes,
                                       resource=self.resource, expires_at=now + 3600, family=family), 3600)
        if "offline_access" in scopes:
            self.put("refresh", refresh, dict(client_id=client_id, subject=workspace, scopes=scopes,
                                             expires_at=now + 30 * 86400, family=family), 30 * 86400)
        else:
            refresh = None
        return OAuthToken(access_token=access, refresh_token=refresh, expires_in=3600, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        record = self.get("code", authorization_code.code, consume=True)
        if not record or record["client_id"] != client.client_id:
            raise TokenError("invalid_grant", "Authorization code already used or expired.")
        return self.issue_tokens(client.client_id, record["subject"], record["scopes"])

    async def load_access_token(self, token):
        record = self.get("access", token)
        if not record or self.get("revoked", record["family"]) or record["subject"] not in self.workspace_names:
            return None
        return AccessToken(token=token, **{k: v for k, v in record.items() if k != "family"})

    async def load_refresh_token(self, client, refresh_token):
        record = self.get("refresh", refresh_token)
        if not record or record["client_id"] != client.client_id or self.get("revoked", record["family"]):
            return None
        return RefreshToken(token=refresh_token, **{k: v for k, v in record.items() if k != "family"})

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        record = self.get("refresh", refresh_token.token, consume=True)
        if not record or record["client_id"] != client.client_id or self.get("revoked", record["family"]):
            raise TokenError("invalid_grant", "Refresh token already used or expired.")
        if record["subject"] not in self.workspace_names or set(scopes) - set(record["scopes"]):
            raise TokenError("invalid_scope", "Workspace or scope is unavailable.")
        return self.issue_tokens(client.client_id, record["subject"], scopes, record["family"])

    async def revoke_token(self, token):
        record = self.get("access", token.token) or self.get("refresh", token.token)
        if record:
            if not self.get("revoked", record["family"]):
                self.put("revoked", record["family"], {"revoked": True}, 31 * 86400)

    async def authenticate(self, supplied):
        try:
            value = supplied.decode()
        except UnicodeError:
            return None
        if not value.startswith("Bearer "):
            return None
        token = await self.load_access_token(value[7:])
        return token.subject if token and "handoff" in token.scopes else None

    def metadata(self):
        return JSONResponse({"resource": self.resource, "authorization_servers": [self.origin],
                             "scopes_supported": ["handoff", "offline_access"],
                             "bearer_methods_supported": ["header"]}, headers={"Access-Control-Allow-Origin": "*"})

    def page(self, body, status=200):
        return HTMLResponse(
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Model Handoff access</title><main><h1>Model Handoff</h1>' + body + '</main></html>',
            status_code=status,
            # Preserve Origin on same-origin form POSTs. no-referrer can cause
            # browsers to serialize it as null, failing our strict CSRF check.
            # Cross-origin navigations still receive no callback URL or code.
            headers={"Cache-Control": "no-store", "Referrer-Policy": "same-origin",
                     "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"},
        )

    def finish(self, transaction, workspace):
        pending = self.get("pending", transaction, consume=True)
        if not pending:
            return self.page("<p>Authorization request expired. Start again from your client.</p>", 400)
        if pending.get("web_login"):
            session = secrets.token_urlsafe(48)
            self.put("session", session, {"workspace": workspace}, 8 * 3600)
            response = RedirectResponse(self.origin, status_code=303)
            response.set_cookie("mh_session", session, secure=True, httponly=True, samesite="lax", max_age=8 * 3600)
            return response
        params = AuthorizationParams.model_validate(pending["params"])
        code = secrets.token_urlsafe(32)
        record = dict(client_id=pending["client_id"], subject=workspace, scopes=params.scopes or ["handoff"],
                      expires_at=time.time() + 120, code_challenge=params.code_challenge,
                      redirect_uri=str(params.redirect_uri),
                      redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                      resource=params.resource)
        self.put("code", code, record, 120)
        url = str(params.redirect_uri)
        query = {"code": code}
        if params.state is not None:
            query["state"] = params.state
        return RedirectResponse(url + ("&" if "?" in url else "?") + urlencode(query), status_code=303)

    async def consent(self, request):
        transaction = request.query_params.get("transaction", "")
        pending = self.get("pending", transaction)
        if not pending:
            return self.page("<p>Authorization request expired. Restart from your client.</p>", 400)
        if request.method == "GET":
            binder = secrets.token_urlsafe(32)
            self.put("consent", binder, {"transaction": transaction, "csrf": secrets.token_urlsafe(32)}, 600)
            entry = self.get("consent", binder)
            google = ('<p><a href="/auth/google/start?' + urlencode({"transaction": transaction}) +
                      '">Continue with Google</a></p>') if self.google_client and self.google_secret else ""
            client = escape(pending["client_name"])
            callback = escape(str(pending["params"]["redirect_uri"]))
            response = self.page(
                f"<h2>Connect {client}</h2><p>This client will be able to read shared context, "
                f"save projects and artifacts, and run billable model calls in your workspace.</p>"
                f"<p>Callback destination: {callback}</p>{google}"
                f'<form method="post"><input type="hidden" name="csrf" value="{entry["csrf"]}">'
                '<label>Workspace access token <input name="workspace_token" type="password" '
                'autocomplete="current-password" required></label>'
                '<button name="decision" value="allow">Allow access</button></form>'
            )
            response.set_cookie("mh_consent", binder, secure=True, httponly=True, samesite="lax", max_age=600)
            return response
        if request.headers.get("origin") != self.origin:
            return self.page("<p>Request origin was rejected.</p>", 403)
        form = await request.form()
        binder = request.cookies.get("mh_consent", "")
        entry = self.get("consent", binder, consume=True)
        if not entry or entry["transaction"] != transaction or not hmac.compare_digest(
            str(form.get("csrf", "")), entry["csrf"]
        ):
            return self.page("<p>Consent expired. Restart from your client.</p>", 403)
        supplied = str(form.get("workspace_token", ""))
        workspace = None
        for token, name in self.tokens:
            if hmac.compare_digest(supplied.encode(), token.encode()):
                workspace = name
        if not workspace:
            return self.page("<p>Workspace access token was rejected. Restart from your client.</p>", 401)
        return self.finish(transaction, workspace)

    async def google_start(self, request):
        if not self.google_client or not self.google_secret:
            return self.page("<p>Google sign-in requires administrator configuration.</p>", 503)
        transaction = request.query_params.get("transaction")
        if transaction:
            pending = self.get("pending", transaction)
            binder = self.get("consent", request.cookies.get("mh_consent", ""))
            if not pending or not binder or binder["transaction"] != transaction:
                return self.page("<p>Restart authorization from your client.</p>", 400)
        else:
            transaction = secrets.token_urlsafe(32)
            self.put("pending", transaction, {"web_login": True}, 600)
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        binder = secrets.token_urlsafe(32)
        self.put("google", state, dict(transaction=transaction, nonce=nonce, verifier=verifier,
                                       binder=digest(binder)), 600)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        query = dict(client_id=self.google_client, redirect_uri=self.origin + "/auth/google/callback",
                     response_type="code", scope="openid email", state=state, nonce=nonce,
                     code_challenge=challenge, code_challenge_method="S256", prompt="select_account")
        response = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(query))
        response.set_cookie("mh_google", binder, secure=True, httponly=True, samesite="lax", max_age=600)
        return response

    def verify_google(self, encoded, nonce):
        key = self.google_keys.get_signing_key_from_jwt(encoded)
        claims = jwt.decode(encoded, key.key, algorithms=["RS256"], audience=self.google_client,
                            issuer=["https://accounts.google.com", "accounts.google.com"],
                            options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]})
        if not hmac.compare_digest(claims["nonce"], nonce) or claims.get("email_verified") is not True:
            raise ValueError("Google identity could not be verified.")
        workspace = self.google_workspaces.get(claims.get("email", "").lower())
        if not workspace or workspace not in self.workspace_names:
            raise ValueError("This Google account has not been invited.")
        return workspace

    async def google_callback(self, request):
        entry = self.get("google", request.query_params.get("state", ""), consume=True)
        if not entry or not hmac.compare_digest(digest(request.cookies.get("mh_google", "")), entry["binder"]):
            return self.page("<p>Google login expired. Start again.</p>", 400)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post("https://oauth2.googleapis.com/token", data={
                    "client_id": self.google_client, "client_secret": self.google_secret,
                    "grant_type": "authorization_code", "code": request.query_params.get("code", ""),
                    "redirect_uri": self.origin + "/auth/google/callback", "code_verifier": entry["verifier"],
                })
                response.raise_for_status()
                encoded = response.json()["id_token"]
            workspace = await run_in_threadpool(self.verify_google, encoded, entry["nonce"])
        except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError, TypeError):
            return self.page("<p>Google login failed or the account has not been invited. Start again.</p>", 401)
        pending = self.get("pending", entry["transaction"])
        if not pending:
            return self.page("<p>Authorization request expired.</p>", 400)
        if pending.get("web_login"):
            return self.finish(entry["transaction"], workspace)
        # Google proves identity. Explicit consent is still required before issuing an MCP grant.
        approval = secrets.token_urlsafe(32)
        self.put("google_consent", approval, dict(transaction=entry["transaction"], workspace=workspace,
                                                  binder=entry["binder"]), 300)
        client = escape(pending["client_name"])
        return self.page(
            f"<h2>Connect {client}?</h2><p>Allow this client to read and write workspace context "
            "and make billable model calls?</p>"
            f'<form method="post" action="/oauth/google-consent"><input type="hidden" name="approval" '
            f'value="{approval}"><button>Allow access</button></form>'
        )

    async def google_consent(self, request):
        if request.headers.get("origin") != self.origin:
            return self.page("<p>Request origin was rejected.</p>", 403)
        form = await request.form()
        entry = self.get("google_consent", str(form.get("approval", "")), consume=True)
        if not entry or not hmac.compare_digest(digest(request.cookies.get("mh_google", "")), entry["binder"]):
            return self.page("<p>Consent expired. Start again.</p>", 403)
        return self.finish(entry["transaction"], entry["workspace"])

    async def revoke(self, request):
        try:
            client = await ClientAuthenticator(self).authenticate_request(request)
        except AuthenticationError:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        form = await request.form()
        value = form.get("token")
        if not isinstance(value, str) or not value:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        token = await self.load_access_token(value) or await self.load_refresh_token(client, value)
        if token and token.client_id == client.client_id:
            await self.revoke_token(token)
        return JSONResponse({}, headers={"Cache-Control": "no-store"})

    def routes(self):
        url = TypeAdapter(AnyHttpUrl).validate_python(self.origin)
        registration = ClientRegistrationOptions(
            enabled=True, valid_scopes=["handoff", "offline_access"], default_scopes=["handoff"])
        revocation = RevocationOptions(enabled=True)
        metadata = build_metadata(url, None, registration, revocation).model_dump(mode="json", exclude_none=True)
        metadata["issuer"] = self.origin
        metadata["token_endpoint_auth_methods_supported"] = ["none", "client_secret_basic", "client_secret_post"]
        metadata["revocation_endpoint_auth_methods_supported"] = metadata["token_endpoint_auth_methods_supported"]

        async def discovery(request):
            return JSONResponse(metadata, headers={"Access-Control-Allow-Origin": "*"})

        sdk_routes = create_auth_routes(self, url, client_registration_options=registration,
                                        revocation_options=revocation)
        return [
            Route("/.well-known/oauth-authorization-server", discovery),
            Route("/revoke", cors_middleware(self.revoke, ["POST", "OPTIONS"]), methods=["POST", "OPTIONS"]),
            *[route for route in sdk_routes if route.path not in {
                "/.well-known/oauth-authorization-server", "/revoke"}],
            Route("/oauth/consent", self.consent, methods=["GET", "POST"]),
            Route("/oauth/google-consent", self.google_consent, methods=["POST"]),
            Route("/auth/google/start", self.google_start),
            Route("/auth/google/callback", self.google_callback),
        ]
