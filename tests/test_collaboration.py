import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import time

import httpx
from starlette.testclient import TestClient

from handoff.jobs import Jobs
from handoff.projects import Projects
from handoff.providers import Providers
from handoff.server import http_app
from handoff.store import Handoffs


class CollaborationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "history.sqlite"
        self.store = Handoffs(self.path, Providers(offline=True))

    def test_queue_idempotency_budget_and_restart(self):
        jobs = Jobs(self.store)
        with patch.dict(os.environ, {"HANDOFF_DAILY_RUNS": "1"}):
            run = jobs.submit("claude", "Review", "Evidence", helper="gemini", request_key="stable")
            self.assertEqual(jobs.submit("claude", "Review", "Evidence", request_key="stable")["id"], run["id"])
            with self.assertRaises(ValueError):
                jobs.submit("gpt", "Another", "")
        restarted = Jobs(Handoffs(self.path, Providers(offline=True)))
        self.assertTrue(restarted.once())
        result = restarted.workspace.get(run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["steps"]["review"]["model"], "fixture-gemini")

    def test_collaborator_access_and_artifact_persistence(self):
        token = "a" * 32
        headers = {"Authorization": "Bearer " + token}
        app = http_app(self.store, token, ["testserver"], {
            "teammate": {"token": "b" * 32, "workspace": "owner"},
            "private": "c" * 32,
        })
        project = Projects(self.store).create("Team", "Evidence")
        artifact = Projects(self.store).save(project["id"], "report.md", "Findings")
        with TestClient(app) as client:
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "get_artifact", "arguments": {"artifact_id": artifact["id"]}}}
            base = {"Accept": "application/json, text/event-stream"}
            shared = client.post("/mcp", json=request, headers=base | {"Authorization": "Bearer " + "b" * 32}).json()
            self.assertEqual(shared["result"]["structuredContent"]["content"], "Findings", shared)
            denied = client.post("/mcp", json=request, headers=base | {"Authorization": "Bearer " + "c" * 32}).json()
            self.assertTrue(denied["result"]["isError"])
        self.assertEqual(Projects(Handoffs(self.path, Providers(True))).artifact(artifact["id"])["content"], "Findings")

    def test_claude_wire_format(self):
        def respond(request):
            self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
            self.assertEqual(json.loads(request.content)["messages"][0]["content"], "Review")
            return httpx.Response(200, json={"stop_reason": "end_turn", "content": [
                {"type": "text", "text": "Checked"}], "usage": {"input_tokens": 3, "output_tokens": 2}})
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fixture", "ANTHROPIC_MODEL": "test"}):
            self.assertEqual(Providers(transport=httpx.MockTransport(respond)).call("claude", "Review").text, "Checked")

    def test_backup_restore_and_provider_limit(self):
        from handoff.backup import backup_tree
        project = Projects(self.store).create("Restore me", "Context")
        saved = backup_tree(self.path, Path(self.temp.name).parent / ("backup-" + project["id"]))
        import shutil
        self.addCleanup(shutil.rmtree, str(Path(saved[0]).parent))
        restored = Handoffs(Path(saved[0]), Providers(True))
        self.assertEqual(Projects(restored).get(project["id"])["context"], "Context")
        with patch.dict(os.environ, {"HANDOFF_DAILY_CALLS": "1"}):
            self.store.providers.call("gpt", "one")
            from handoff.providers import ProviderError
            with self.assertRaises(ProviderError):
                self.store.providers.call("gemini", "two")

    def test_oauth_rejects_wrong_audience_and_unlisted_user(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from handoff.auth import OAuth
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        config = {
            "HANDOFF_OAUTH_ISSUER": "https://identity.example",
            "HANDOFF_PUBLIC_URL": "https://bridge.example",
            "HANDOFF_OAUTH_JWKS_URL": "https://identity.example/jwks",
            "HANDOFF_OAUTH_SUBJECTS": '{"user-one":"owner"}',
        }
        claims = dict(iss=config["HANDOFF_OAUTH_ISSUER"], sub="user-one",
                      aud="https://bridge.example/mcp", exp=int(time.time()) + 60, scope="handoff")
        with patch.dict(os.environ, config):
            auth = OAuth()
            from types import SimpleNamespace
            with patch.object(auth.keys, "get_signing_key_from_jwt", return_value=SimpleNamespace(key=key.public_key())):
                encode = lambda value: jwt.encode(value, key, algorithm="RS256")
                self.assertEqual(auth.workspace(encode(claims)), "owner")
                self.assertIsNone(auth.workspace(encode(claims | {"aud": "other"})))
                self.assertIsNone(auth.workspace(encode(claims | {"sub": "outsider"})))
                self.assertIsNone(auth.workspace(encode(claims | {"exp": 1})))
                self.assertIsNone(auth.workspace(encode(claims | {"scope": "other"})))

    def test_interrupted_run_preserves_completed_step(self):
        from dataclasses import asdict
        from handoff.providers import Reply
        jobs = Jobs(self.store)
        run = jobs.submit("gpt", "Review", "Evidence")
        run["status"] = "running"
        run["steps"]["draft"] = asdict(Reply("Already drafted", "fixture-gpt", 0, None, None, None))
        jobs.workspace.save(run)
        with self.store.connection() as db:
            db.execute("UPDATE job SET status='running' WHERE id=?", (run["id"],))
        jobs.start()
        jobs.close()
        failed = jobs.workspace.get(run["id"])
        self.assertEqual(failed["error"], "interrupted")
        retry = jobs.retry(run["id"])
        self.assertEqual(retry["steps"]["draft"]["text"], "Already drafted")
        jobs.once()
        self.assertEqual(jobs.workspace.get(retry["id"])["status"], "completed")

    def test_worker_lease_prevents_overlapping_deploy_workers(self):
        first = Jobs(self.store)
        second = Jobs(self.store)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        with self.store.connection() as db:
            db.execute("UPDATE worker_lease SET expires=0")
        self.assertTrue(second.acquire())
        self.assertFalse(first.acquire())
