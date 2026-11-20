import asyncio
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from starlette.testclient import TestClient

from handoff.evaluation import run_task, score
from handoff.providers import Providers, Reply
from handoff.server import http_app
from handoff.store import Handoffs
from handoff.tasks import TASKS


class BridgeTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "history.sqlite"

    def test_real_stdio_protocol(self):
        async def exercise():
            parameters = StdioServerParameters(command=sys.executable, args=[
                "-m", "handoff.cli", "--offline", "--db", str(self.path), "serve",
            ])
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    tools = {tool.name for tool in (await client.list_tools()).tools}
                    self.assertTrue({"ask_model", "get_handoff", "retry_handoff", "create_project", "get_artifact"} <= tools)
                    arguments = {"target": "gemini", "task": "Review", "context": "example"}
                    denied = await client.call_tool("ask_model", arguments)
                    self.assertTrue(denied.isError)
                    arguments["confirmed"] = True
                    response = await client.call_tool("ask_model", arguments)
                    self.assertFalse(response.isError)
                    record = response.structuredContent
                    self.assertEqual(record["status"], "completed")
                    result = await client.call_tool("get_handoff", {"handoff_id": record["id"]})
                    self.assertEqual(result.structuredContent, record)

        asyncio.run(exercise())

    def test_http_auth_and_discovery(self):
        token = "x" * 32
        store = Handoffs(self.path, Providers(offline=True))
        app = http_app(store, token, ["testserver"])
        with TestClient(app) as client:
            request = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "test", "version": "1"}}}
            self.assertEqual(client.post("/mcp", json=request).status_code, 401)
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
            self.assertEqual(client.post("/mcp", json=request, headers=headers | {"Host": "invalid"}).status_code, 421)
            response = client.post("/mcp", json=request, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["result"]["serverInfo"]["name"], "Handoff-MCP")
            tools = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertEqual(len(tools.json()["result"]["tools"]), 11)
            result = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "ask_model", "arguments": {
                    "target": "gpt", "task": "Check a claim", "context": "synthetic text", "confirmed": True,
                }},
            }).json()["result"]
            self.assertEqual(result["structuredContent"]["status"], "completed")

    def test_timeout_and_retry_are_persisted(self):
        seen = []
        def respond(request):
            seen.append(request)
            if len(seen) == 1:
                raise httpx.ReadTimeout("simulated", request=request)
            return httpx.Response(200, json={
                "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "Try a unit test."}]}}],
                "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 200, "thoughtsTokenCount": 50},
            })
        env = {"GEMINI_API_KEY": "fake", "GEMINI_MODEL": "test",
               "GEMINI_INPUT_USD_PER_MILLION": "1", "GEMINI_OUTPUT_USD_PER_MILLION": "4"}
        providers = Providers(transport=httpx.MockTransport(respond))
        with patch.dict(os.environ, env):
            failed = Handoffs(self.path, providers).ask("gemini", "Review", "selected snippet")
            self.assertEqual(failed["error"], "timeout")
            store = Handoffs(self.path, providers)
            self.assertEqual(store.get(failed["id"]), failed)
            recovered = store.retry(failed["id"])
            self.assertEqual(recovered["previous_id"], failed["id"])
            self.assertEqual(recovered["response"]["text"], "Try a unit test.")
            self.assertAlmostEqual(recovered["response"]["estimated_cost_usd"], 0.002)
            self.assertEqual(seen[0].content, seen[1].content)

    def test_gpt_adapter(self):
        def respond(request):
            self.assertEqual(request.url.path, "/v1/responses")
            self.assertEqual(request.headers["Authorization"], "Bearer fake")
            return httpx.Response(200, json={
                "status": "completed", "output": [
                    {"type": "reasoning"}, {"type": "message", "content": [{"type": "output_text", "text": "result"}]},
                ],
                "usage": {"input_tokens": 20, "output_tokens": 10},
            })
        with patch.dict(os.environ, {"HANDOFF_GPT_BACKEND": "openai", "OPENAI_API_KEY": "fake", "OPENAI_MODEL": "test"}):
            reply = Providers(transport=httpx.MockTransport(respond)).call("gpt", "hello")
        self.assertEqual((reply.text, reply.input_tokens, reply.output_tokens), ("result", 20, 10))

    def test_groq_backend_uses_its_own_credentials(self):
        def respond(request):
            self.assertEqual(str(request.url), "https://api.groq.com/openai/v1/responses")
            self.assertEqual(request.headers["Authorization"], "Bearer groq-test")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "openai/gpt-oss-20b")
            self.assertFalse(body["store"])
            return httpx.Response(200, json={
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "result"}]}],
                "usage": {"input_tokens": 20, "output_tokens": 10},
            })

        env = {"HANDOFF_GPT_BACKEND": "groq", "GROQ_API_KEY": "groq-test",
               "GROQ_MODEL": "openai/gpt-oss-20b", "OPENAI_API_KEY": "separate-key"}
        with patch.dict(os.environ, env, clear=True):
            reply = Providers(transport=httpx.MockTransport(respond)).call("gpt", "hello")
        self.assertEqual((reply.text, reply.model), ("result", "openai/gpt-oss-20b"))

    def test_evaluator_catches_wrong_code_and_swaps_roles(self):
        store = Handoffs(self.path, Providers(offline=True))
        for task in TASKS:
            self.assertLess(score(task, task.buggy, True)["passed"], len(task.examples))
            self.assertEqual(score(task, task.fixed, True)["passed"], len(task.examples))
        for lead, helper in (("gpt", "gemini"), ("gemini", "gpt")):
            result = run_task(store, TASKS[0], lead, "handoff")
            self.assertEqual(result["passed"], 3)
            self.assertEqual([call["model"] for call in result["calls"]],
                             [f"fixture-{lead}", f"fixture-{helper}", f"fixture-{lead}"])

    def test_synthesis_receives_advice_and_tests_catch_regression(self):
        task = TASKS[0]
        bad_advice = "Use filter(bool, xs) to drop missing values."
        replies = [Reply(task.buggy, "test-lead", 0, None, None, None),
                   Reply(bad_advice, "test-helper", 0, None, None, None),
                   Reply(task.buggy, "test-lead", 0, None, None, None)]
        providers = Providers(offline=True)
        with patch.object(providers, "call", side_effect=replies) as call:
            result = run_task(Handoffs(self.path, providers), task, "gpt", "handoff")
        self.assertIn(bad_advice, call.call_args_list[2].args[1])
        self.assertEqual(result["status"], "scored")
        self.assertLess(result["passed"], result["total"])
