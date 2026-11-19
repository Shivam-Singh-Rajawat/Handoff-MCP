import base64
import time
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from starlette.testclient import TestClient

from handoff.providers import ProviderError, Providers, Reply
from handoff.server import http_app
from handoff.store import Handoffs
from handoff.workspace import Workspace


class WorkspaceTests(unittest.TestCase):
    def completed(self, client, run):
        deadline = time.monotonic() + 5
        while run["status"] in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(0.02)
            run = client.get("/api/runs/" + run["id"], headers=self.headers).json()
        self.assertEqual(run["status"], "completed")
        return run

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Handoffs(Path(directory.name) / "history.sqlite", Providers(offline=True))
        self.token = "x" * 32
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_csv_preview_and_two_way_workflow(self):
        with TestClient(http_app(self.store, self.token, ["testserver"])) as client:
            self.assertIn("Open your workspace", client.get("/").text)
            self.assertEqual(client.get("/api/runs").status_code, 401)
            preview = client.post("/api/prepare", headers=self.headers, json={
                "name": "rows.csv", "content": base64.b64encode(b"name,score\nA,10\nB,\n").decode()
            })
            self.assertEqual(preview.status_code, 200, preview.text)
            context = preview.json()["context"]
            self.assertIn('"score": 1', context)
            self.assertEqual(client.post("/api/runs", headers=self.headers, json={}).status_code, 400)
            for lead, helper in (("gpt", "gemini"), ("gemini", "gpt")):
                result = client.post("/api/runs", headers=self.headers, json={
                    "lead": lead, "task": "Review the scores", "context": context, "approved": True,
                })
                self.assertEqual(result.status_code, 202, result.text)
                run = self.completed(client, result.json())
                self.assertEqual(run["status"], "completed")
                self.assertEqual([run["steps"][stage]["model"] for stage in ("draft", "review", "final")],
                                 [f"fixture-{lead}", f"fixture-{helper}", f"fixture-{lead}"])
                self.assertEqual(Workspace(self.store).get(run["id"]), run)

    def test_pdf_page_reference(self):
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=200)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })})
        })
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 20 100 Td (Source evidence) Tj ET")
        page[NameObject("/Contents")] = stream
        pdf = BytesIO()
        writer.write(pdf)
        with TestClient(http_app(self.store, self.token, ["testserver"])) as client:
            preview = client.post("/api/prepare", headers=self.headers, json={
                "name": "source.pdf", "content": base64.b64encode(pdf.getvalue()).decode(),
            })
            self.assertEqual(preview.json()["context"], "[Page 1]\nSource evidence")

    def test_retry_preserves_draft_and_uses_review(self):
        workspace = Workspace(self.store)
        draft = Reply("Original draft", "fixture-gpt", 1, None, None, None)
        review = Reply("Verified finding", "fixture-gemini", 1, None, None, None)
        with patch.object(self.store.providers, "call", side_effect=[draft, ProviderError("timeout")]):
            failed = workspace.create("gpt", "Review", "source text")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "timeout")
        with patch.object(self.store.providers, "call", side_effect=[review, draft]) as calls:
            result = workspace.retry(failed["id"])
        self.assertEqual(calls.call_count, 2)
        self.assertIn("Original draft", calls.call_args_list[0].args[1])
        self.assertIn("Verified finding", calls.call_args_list[1].args[1])
        self.assertEqual(result["status"], "completed")
        with self.assertRaises(ValueError):
            workspace.retry(failed["id"])

    def test_public_demo_is_separate_from_private_runs(self):
        with TestClient(http_app(self.store, self.token, ["testserver"])) as client:
            self.assertIn("Scripted walkthrough", client.get("/").text)
            self.assertEqual(client.get("/healthz").json(), {"status": "ok"})
            self.assertEqual(client.get("/api/runs/unknown").status_code, 401)
            self.assertEqual(client.post("/api/prepare", json={}).status_code, 401)
            self.assertEqual(client.post("/mcp", json={}).status_code, 401)
            request = {"lead": "gemini", "task": "Review the scores", "context": "Evidence", "approved": True}
            for mode, stages in (("single", ("final",)), ("same", ("draft", "review", "final"))):
                response = client.post("/api/runs", headers=self.headers, json=request | {"mode": mode})
                self.assertEqual(response.status_code, 202, response.text)
                self.assertIn("no-store", response.headers["cache-control"])
                run = self.completed(client, response.json())
                self.assertEqual(run["mode"], mode)
                self.assertEqual(tuple(run["steps"]), stages)
                self.assertTrue(all(step["model"] == "fixture-gemini" for step in run["steps"].values()))
            response = client.post("/api/runs", headers=self.headers, json=request | {"mode": ["single"]})
            self.assertEqual(response.status_code, 400)

    def test_invited_workspace_cannot_read_owner_runs(self):
        guest = {"Authorization": "Bearer " + "y" * 32}
        app = http_app(self.store, self.token, ["testserver"], {"guest": "y" * 32})
        with TestClient(app) as client:
            result = client.post("/api/runs", headers=self.headers, json={
                "lead": "gpt", "task": "Review the scores", "context": "Private",
                "approved": True,
            })
            self.assertEqual(result.status_code, 202)
            self.assertEqual(client.get("/api/runs/" + result.json()["id"], headers=guest).status_code, 404)
            self.assertEqual(client.get("/api/runs", headers=guest).json()["runs"], [])

    def test_github_issue_import_previews_source_without_contacting_models(self):
        with TestClient(http_app(self.store, self.token, ["testserver"])) as client:
            with patch("handoff.connectors.httpx.get", return_value=httpx.Response(200, json={
                "title": "Check CSV", "body": "One row is missing.",
            })) as get, patch.object(self.store.providers, "call") as call:
                data = {"url": "https://github.com/example/repo/issues/1"}
                self.assertEqual(client.post("/api/import-issue", json=data).status_code, 401)
                result = client.post("/api/import-issue", headers=self.headers, json=data)
                self.assertEqual(result.status_code, 200)
                self.assertIn(data["url"], result.json()["context"])
                self.assertIn("One row is missing.", result.json()["context"])
                self.assertEqual(client.post("/api/import-issue", headers=self.headers,
                                             json={"url": "http://localhost/issues/1"}).status_code, 400)
                get.assert_called_once()
                call.assert_not_called()
