import base64
import hashlib
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time
from urllib.parse import parse_qs, urlparse
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from handoff.providers import Providers
from handoff.server import http_app
from handoff.store import Handoffs


class NativeAuthTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = patch.dict(os.environ, {"HANDOFF_NATIVE_OAUTH": "true", "HANDOFF_PUBLIC_URL": "https://bridge.example"})
        config.start()
        self.addCleanup(config.stop)
        self.store = Handoffs(Path(directory.name) / "history.sqlite", Providers(True))
        self.token = "a" * 48
        self.app = http_app(self.store, self.token, ["bridge.example"])
        self.client = TestClient(self.app, base_url="https://bridge.example", follow_redirects=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def authorize(self, wrong_verifier=False):
        c = self.client
        client = c.post("/register", json={
            "redirect_uris": ["https://client.example/callback"], "client_name": "Verification Client",
            "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "scope": "handoff offline_access",
        })
        self.assertEqual(client.status_code, 201, client.text)
        client_id = client.json()["client_id"]
        verifier = "v" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        authorization = c.get("/authorize", params={
            "client_id": client_id, "response_type": "code", "redirect_uri": "https://client.example/callback",
            "scope": "handoff offline_access", "state": "client-state",
            "code_challenge": challenge, "code_challenge_method": "S256", "resource": "https://bridge.example/mcp",
        })
        self.assertIn(authorization.status_code, (302, 303), authorization.text)
        consent_url = authorization.headers["location"]
        consent = c.get(consent_url)
        csrf = re.search(r'name="csrf" value="([^"]+)"', consent.text).group(1)
        granted = c.post(consent_url, data={"csrf": csrf, "workspace_token": self.token},
                         headers={"Origin": "https://bridge.example"})
        self.assertEqual(granted.status_code, 303, granted.text)
        query = parse_qs(urlparse(granted.headers["location"]).query)
        self.assertEqual(query["state"], ["client-state"])
        payload = {"client_id": client_id, "grant_type": "authorization_code", "code": query["code"][0],
                   "redirect_uri": "https://client.example/callback", "code_verifier": "x" * 64 if wrong_verifier else verifier,
                   "resource": "https://bridge.example/mcp"}
        return client_id, payload, c.post("/token", data=payload)

    def test_pkce_exchange_refresh_replay_and_revocation(self):
        c = self.client
        metadata = c.get("/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(metadata.json()["authorization_servers"], ["https://bridge.example"])
        discovery = c.get("/.well-known/oauth-authorization-server")
        self.assertEqual(discovery.status_code, 200)
        self.assertEqual(discovery.json()["issuer"], "https://bridge.example")
        self.assertIn("none", discovery.json()["token_endpoint_auth_methods_supported"])
        self.assertIn("resource_metadata", c.get("/api/runs").headers["www-authenticate"])
        client_id, payload, response = self.authorize()
        self.assertEqual(response.status_code, 200, response.text)
        tokens = response.json()
        headers = {"Authorization": "Bearer " + tokens["access_token"]}
        self.assertEqual(c.get("/api/runs", headers=headers).status_code, 200)
        self.assertEqual(c.post("/token", data=payload).status_code, 400)
        refresh = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": tokens["refresh_token"]}
        next_tokens = c.post("/token", data=refresh)
        self.assertEqual(next_tokens.status_code, 200, next_tokens.text)
        self.assertEqual(c.post("/token", data=refresh).status_code, 400)
        revoked = c.post("/revoke", data={"client_id": client_id, "token": next_tokens.json()["access_token"]})
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(c.get("/api/runs", headers=headers).status_code, 401)

    def test_pkce_rejects_wrong_verifier(self):
        _, _, response = self.authorize(wrong_verifier=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_grant")

    def test_redirect_and_resource_binding(self):
        response = self.client.post("/register", json={
            "redirect_uris": ["http://attacker.example/callback"], "client_name": "Bad client",
            "token_endpoint_auth_method": "none",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_redirect_uri")

    def test_google_start_and_unknown_state(self):
        self.assertEqual(self.client.get("/auth/google/start").status_code, 503)
        self.assertEqual(self.client.get("/auth/google/callback?state=unknown&code=test").status_code, 400)

    def test_consent_rejects_cross_origin_and_missing_csrf(self):
        from handoff.native_auth import NativeAuth
        auth = NativeAuth(self.store, [(self.token, "owner")], {"owner"})
        transaction = "transaction-test"
        auth.put("pending", transaction, {"client_name": "Test", "params": {
            "redirect_uri": "https://client.example/callback"
        }}, 600)
        url = "/oauth/consent?transaction=" + transaction
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url).headers["referrer-policy"], "same-origin")
        self.assertEqual(self.client.post(url, data={"workspace_token": self.token},
                                         headers={"Origin": "https://other.example"}).status_code, 403)
        self.assertEqual(self.client.post(url, data={"workspace_token": self.token},
                                         headers={"Origin": "https://bridge.example"}).status_code, 403)

    def test_google_consent_uses_dedicated_cookie_and_one_use_approval(self):
        from handoff.native_auth import NativeAuth, digest
        auth = NativeAuth(self.store, [(self.token, "owner")], {"owner"})
        auth.put("pending", "google-test", {"client_name": "ChatGPT", "web_login": True}, 600)
        auth.put("google_consent", "approval-test", {
            "transaction": "google-test", "workspace": "owner", "binder": digest("approval-cookie")
        }, 300)
        c = self.client
        url = "/oauth/google-consent?approval=approval-test"
        self.assertEqual(c.get(url).status_code, 403)
        self.assertEqual(c.post("/oauth/google-consent", data={"approval": "approval-test"},
                               headers={"Origin": "https://bridge.example"}).status_code, 403)
        # A bad cookie must not burn a still-valid approval.
        c.cookies.set("mh_approval", "approval-cookie")
        c.cookies.set("mh_google", "another-login-tab")
        page = c.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Connect ChatGPT?", page.text)
        self.assertEqual(c.post("/oauth/google-consent", data={"approval": "approval-test"},
                               headers={"Origin": "https://attacker.example"}).status_code, 403)
        granted = c.post("/oauth/google-consent", data={"approval": "approval-test"},
                         headers={"Origin": "https://bridge.example"})
        self.assertEqual(granted.status_code, 303)
        self.assertEqual(c.post("/oauth/google-consent", data={"approval": "approval-test"},
                               headers={"Origin": "https://bridge.example"}).status_code, 403)

    def test_google_identity_verification_requires_nonce_audience_and_invitation(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from types import SimpleNamespace
        from handoff.native_auth import NativeAuth
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "google-client",
                                    "HANDOFF_GOOGLE_WORKSPACES": '{"owner@example.com":"owner"}'}):
            auth = NativeAuth(self.store, [(self.token, "owner")], {"owner"})
        claims = dict(iss="https://accounts.google.com", aud="google-client", sub="google-user",
                      iat=int(time.time()), exp=int(time.time()) + 120, nonce="expected",
                      email="owner@example.com", email_verified=True)
        with patch.object(auth.google_keys, "get_signing_key_from_jwt", return_value=SimpleNamespace(key=key.public_key())):
            encode = lambda payload: jwt.encode(payload, key, algorithm="RS256")
            self.assertEqual(auth.verify_google(encode(claims), "expected"), "owner")
            for changed in ({"aud": "wrong"}, {"nonce": "wrong"}, {"email": "outsider@example.com"},
                            {"email_verified": False}, {"exp": 1}):
                with self.assertRaises((ValueError, jwt.PyJWTError)):
                    auth.verify_google(encode(claims | changed), "expected")
