"""Persistent single-tenant handoff history."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from .database import Database
from .limits import LimitedProviders
from .providers import Model, ProviderError, Providers, helper_prompt


class Handoffs:
    def __init__(self, path: Path, providers: Providers, workspace="owner"):
        self.database = Database(path, workspace)
        self.path = path
        self.providers = providers if isinstance(providers, LimitedProviders) else LimitedProviders(self.database, providers)
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS handoff (
                id TEXT PRIMARY KEY, previous_id TEXT, target TEXT NOT NULL,
                task TEXT NOT NULL, context TEXT NOT NULL, created_at TEXT NOT NULL,
                status TEXT NOT NULL, response TEXT, error TEXT
            )""")

    def connection(self):
        return self.database.connection()

    def get(self, handoff_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM handoff WHERE id = ?", (handoff_id,)).fetchone()
        if row is None:
            raise ValueError("Handoff ID was not found.")
        result = dict(row)
        result["response"] = json.loads(result["response"]) if result["response"] else None
        return result

    def ask(self, target: Model, task: str, context: str, *, previous_id: str | None = None,
            fixture: str | None = None) -> dict:
        if not task.strip() or len(task) > 4000 or len(context) > 48000:
            raise ValueError("Provide a task up to 4,000 characters and context up to 48,000.")
        handoff_id = uuid4().hex
        with self.connection() as db:
            db.execute("""INSERT INTO handoff (id,previous_id,target,task,context,created_at,status,response,error) VALUES (?, ?, ?, ?, ?, ?, 'running', NULL, NULL)""",
                       (handoff_id, previous_id, target, task, context, datetime.now(timezone.utc).isoformat()))
        try:
            reply = self.providers.call(target, helper_prompt(task, context), fixture)
            status, payload, error = "completed", json.dumps(asdict(reply)), None
        except ProviderError as exc:
            status, payload, error = "failed", None, exc.code
        except ValueError:
            status, payload, error = "failed", None, "invalid_provider_configuration"
        with self.connection() as db:
            db.execute("UPDATE handoff SET status = ?, response = ?, error = ? WHERE id = ?",
                       (status, payload, error, handoff_id))
        return self.get(handoff_id)

    def retry(self, handoff_id: str) -> dict:
        record = self.get(handoff_id)
        if record["status"] != "failed":
            raise ValueError("Only failed handoffs can be retried. Submit a new request if a run was interrupted.")
        return self.ask(record["target"], record["task"], record["context"], previous_id=handoff_id)
