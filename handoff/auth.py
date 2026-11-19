"""Optional OAuth resource-server support using an external authorization service."""
import json
import os
from urllib.parse import urlparse

import jwt
from jwt import PyJWKClient
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse


class OAuth:
    def __init__(self):
        self.issuer = os.getenv("HANDOFF_OAUTH_ISSUER", "").strip()
        self.resource = os.getenv("HANDOFF_PUBLIC_URL", "").rstrip("/") + "/mcp"
        self.subjects = json.loads(os.getenv("HANDOFF_OAUTH_SUBJECTS", "{}"))
        self.scope = "handoff"
        self.keys = None
        if self.issuer:
            jwks = os.environ["HANDOFF_OAUTH_JWKS_URL"]
            if any(urlparse(url).scheme != "https" for url in (self.issuer, self.resource, jwks)):
                raise ValueError("OAuth issuer, public URL and JWKS URL require HTTPS.")
            if not isinstance(self.subjects, dict):
                raise ValueError("OAuth subjects must map identities to configured workspace names.")
            self.keys = PyJWKClient(jwks)

    def workspace(self, token):
        try:
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=self.resource,
                                issuer=self.issuer, options={"require": ["exp", "iss", "aud", "sub"]})
            if self.scope not in claims.get("scope", "").split():
                return None
            return self.subjects.get(claims["sub"])
        except (jwt.PyJWTError, ValueError, TypeError, AttributeError):
            return None

    async def authenticate(self, supplied):
        if not self.keys:
            return None
        try:
            value = supplied.decode()
        except UnicodeError:
            return None
        if not value.startswith("Bearer "):
            return None
        return await run_in_threadpool(self.workspace, value[7:])

    def metadata(self):
        return JSONResponse({"resource": self.resource, "authorization_servers": [self.issuer],
                             "scopes_supported": [self.scope], "bearer_methods_supported": ["header"]})
