"""Private workspace API and browser assets."""
import base64
import binascii
import json
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from .connectors import github_issue
from .providers import provider_prefix
from .workspace import MAX_CONTEXT, MAX_FILE, Workspace, attachment_context

STATIC = Path(__file__).with_name("static")


def asset(name, media_type):
    async def serve(request):
        return FileResponse(STATIC / name, media_type=media_type, headers={
            "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            "X-Content-Type-Options": "nosniff",
        })
    return serve


async def payload(request):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 3 * 1024 * 1024:
            raise ValueError("Upload limit is 2 MiB per file.")
        body.extend(chunk)
    try:
        value = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Send valid JSON.") from exc
    if not isinstance(value, dict):
        raise ValueError("Send a JSON object.")
    return value


def workspace_routes(handoffs, jobs=None):
    workspace = Workspace(handoffs)

    async def prepare(request):
        try:
            data = await payload(request)
            name, encoded = data.get("name"), data.get("content")
            if not isinstance(name, str) or not isinstance(encoded, str):
                raise ValueError("Supply a file name and its encoded content.")
            try:
                content = base64.b64decode(encoded, validate=True)
            except binascii.Error as exc:
                raise ValueError("Could not decode the upload.") from exc
            if len(content) > MAX_FILE:
                raise ValueError("Upload limit is 2 MiB per file.")
            excerpt = await run_in_threadpool(attachment_context, name, content)
            return JSONResponse({"context": excerpt[:MAX_CONTEXT], "truncated": len(excerpt) > MAX_CONTEXT})
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def import_issue(request):
        try:
            data = await payload(request)
            url = data.get("url")
            if not isinstance(url, str) or len(url) > 250:
                raise ValueError("Enter a public GitHub issue URL.")
            context = await run_in_threadpool(github_issue, url)
            return JSONResponse({"context": context[:MAX_CONTEXT], "truncated": len(context) > MAX_CONTEXT})
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def runs(request):
        if request.method == "GET":
            return JSONResponse({"runs": await run_in_threadpool(workspace.recent),
                                 "offline": handoffs.providers.offline,
                                 "gpt_backend": None if handoffs.providers.offline else provider_prefix("gpt").lower()})
        try:
            data = await payload(request)
            if data.get("approved") is not True:
                raise ValueError("Approve sharing the visible context with the selected providers.")
            lead, task, context = (data.get(key) for key in ("lead", "task", "context"))
            if not all(isinstance(item, str) for item in (lead, task, context)):
                raise ValueError("Provide the lead, task, and context as text.")
            mode = data.get("mode", "cross")
            if jobs:
                run = await run_in_threadpool(jobs.submit, lead, task, context, mode,
                                              data.get("helper"), data.get("request_key"))
                return JSONResponse(run, status_code=202)
            return JSONResponse(await run_in_threadpool(workspace.create, lead, task, context, mode), status_code=201)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def detail(request):
        run = await run_in_threadpool(workspace.get, request.path_params["run_id"])
        return JSONResponse(run or {"error": "Run not found."}, status_code=200 if run else 404)

    async def ready(request):
        try:
            with handoffs.connection() as db:
                db.execute("SELECT 1").fetchone()
            if jobs is not None and (not jobs.thread or not jobs.thread.is_alive()):
                return JSONResponse({"status": "worker_unavailable"}, status_code=503)
            return JSONResponse({"status": "ready", "offline": handoffs.providers.offline})
        except Exception:
            return JSONResponse({"status": "storage_unavailable"}, status_code=503)

    async def health(request):
        return JSONResponse({"status": "ok"})

    async def retry(request):
        try:
            if (await payload(request)).get("approved") is not True:
                raise ValueError("Approve resending the saved context.")
            return JSONResponse(await run_in_threadpool(jobs.retry if jobs else workspace.retry,
                                                        request.path_params["run_id"]), status_code=202 if jobs else 200)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    return [
        Route("/healthz", health),
        Route("/readyz", ready),
        Route("/", asset("index.html", "text/html")),
        Route("/workspace.css", asset("workspace.css", "text/css")),
        Route("/workspace.js", asset("workspace.js", "text/javascript")),
        Route("/api/prepare", prepare, methods=["POST"]),
        Route("/api/import-issue", import_issue, methods=["POST"]),
        Route("/api/runs", runs, methods=["GET", "POST"]),
        Route("/api/runs/{run_id}", detail),
        Route("/api/runs/{run_id}/retry", retry, methods=["POST"]),
    ]
