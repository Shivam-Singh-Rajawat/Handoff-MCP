"""Persistent single-worker queue with atomic claims and daily admission limits."""
from datetime import datetime, timezone
import json
import logging
import os
import threading

from .workspace import Workspace

log = logging.getLogger(__name__)


class Jobs:
    def __init__(self, handoffs):
        self.handoffs = handoffs
        self.workspace = Workspace(handoffs)
        self.stop_event = threading.Event()
        self.thread = None
        with handoffs.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS job (
                id TEXT PRIMARY KEY, request_key TEXT UNIQUE, day TEXT NOT NULL,
                status TEXT NOT NULL, reserved_tokens INTEGER NOT NULL)""")

    def submit(self, lead, task, context, mode="cross", helper=None, request_key=None, previous=None):
        if request_key is not None and (not isinstance(request_key, str) or not 1 <= len(request_key) <= 128):
            raise ValueError("Request key must contain 1 to 128 characters.")
        daily = int(os.getenv("HANDOFF_DAILY_RUNS", "50"))
        pending = int(os.getenv("HANDOFF_PENDING_RUNS", "10"))
        budget = int(os.getenv("HANDOFF_DAILY_OUTPUT_TOKENS", "1228800"))
        reserved = int(os.getenv("HANDOFF_MAX_OUTPUT_TOKENS", "4096")) * (1 if mode == "single" else 3)
        day = datetime.now(timezone.utc).date().isoformat()
        # Serialize admission across web and MCP calls using the same database.
        with self.handoffs.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if request_key:
                previous = db.execute("SELECT id FROM job WHERE request_key=?", (request_key,)).fetchone()
                if previous:
                    return self.workspace.get(previous["id"])
            count, tokens = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(reserved_tokens),0) FROM job WHERE day=?", (day,)
            ).fetchone()
            waiting = db.execute("SELECT COUNT(*) FROM job WHERE status IN ('queued','running')").fetchone()[0]
            if count >= daily or tokens + reserved > budget or waiting >= pending:
                raise ValueError("Workspace queue or daily usage limit reached.")
            # Create the run in this transaction to prevent orphan admissions.
            from uuid import uuid4
            if not isinstance(lead, str) or not isinstance(mode, str) or lead not in ("gpt", "gemini", "claude") or mode not in ("cross", "same", "single"):
                raise ValueError("Choose a supported provider and mode.")
            if helper is not None and helper not in ("gpt", "gemini", "claude"):
                raise ValueError("Choose a supported helper.")
            if not isinstance(task, str) or not 0 < len(task.strip()) <= 4000:
                raise ValueError("Task must contain 1 to 4000 characters.")
            if not isinstance(context, str) or len(context) > 24000:
                raise ValueError("Context limit is 24000 characters.")
            other = "gemini" if lead == "gpt" else "gpt"
            run = dict(id=uuid4().hex, lead=lead, helper={"single": None, "same": lead, "cross": helper or other}[mode],
                       mode=mode, task=task, context=context, status="queued", steps={}, error=None,
                       offline=self.handoffs.providers.offline)
            if previous:
                run.update(steps=previous["steps"], previous_id=previous["id"])
            db.execute("INSERT INTO workspace_run VALUES (?, ?, ?)", (run["id"], "queued", json.dumps(run)))
            db.execute("INSERT INTO job VALUES (?, ?, ?, 'queued', ?)", (run["id"], request_key, day, reserved))
        return run

    def retry(self, run_id):
        run = self.workspace.get(run_id)
        if not run or run["status"] != "failed":
            raise ValueError("Only failed runs can be retried.")
        # A retry is a new budgeted job. Keep completed steps to avoid repeating them.
        return self.submit(run["lead"], run["task"], run["context"],
                           run.get("mode", "cross"), run["helper"], previous=run)

    def start(self):
        # One application process per database. Interrupted calls require an explicit retry
        # because an upstream call may have completed and been charged.
        with self.handoffs.connection() as db:
            rows = db.execute("SELECT data FROM workspace_run WHERE status='running'").fetchall()
            for row in rows:
                run = json.loads(row["data"])
                run.update(status="failed", error="interrupted")
                db.execute("UPDATE workspace_run SET status='failed',data=? WHERE id=?", (json.dumps(run), run["id"]))
            db.execute("UPDATE job SET status='failed' WHERE status='running'")
        self.thread = threading.Thread(target=self.work, daemon=True, name="handoff-worker")
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)

    def once(self):
        with self.handoffs.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM job WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return False
            db.execute("UPDATE job SET status='running' WHERE id=?", (row["id"],))
        run = self.workspace.get(row["id"])
        run["status"] = "running"
        self.workspace.save(run)
        try:
            run = self.workspace.execute(run)
        except Exception:
            log.exception("Job failed id=%s", run["id"])
            run.update(status="failed", error="internal_error")
            self.workspace.save(run)
        with self.handoffs.connection() as db:
            db.execute("UPDATE job SET status=? WHERE id=?", (run["status"], run["id"]))
        return True

    def work(self):
        while not self.stop_event.is_set():
            try:
                if self.once():
                    continue
            except Exception:
                log.exception("Queue worker failure")
            self.stop_event.wait(0.5)
