"""Provider-call limits shared by queued and direct MCP workflows."""
from datetime import datetime, timezone
import os
import sqlite3
import threading

from .providers import ProviderError


class LimitedProviders:
    def __init__(self, path, providers):
        self.path = path
        self.providers = providers
        self.lock = threading.BoundedSemaphore(1)
        with sqlite3.connect(path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS provider_budget (
                day TEXT PRIMARY KEY, calls INTEGER NOT NULL, reserved INTEGER NOT NULL)""")

    @property
    def offline(self):
        return self.providers.offline

    def call(self, provider, prompt, fixture=None):
        if not self.lock.acquire(blocking=False):
            raise ProviderError("workspace_busy")
        try:
            day = datetime.now(timezone.utc).date().isoformat()
            reserve = int(os.getenv("HANDOFF_MAX_OUTPUT_TOKENS", "4096"))
            with sqlite3.connect(self.path, timeout=20) as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT calls,reserved FROM provider_budget WHERE day=?", (day,)).fetchone()
                calls, reserved = tuple(row) if row else (0, 0)
                if calls >= int(os.getenv("HANDOFF_DAILY_CALLS", "300")) or (
                    reserved + reserve > int(os.getenv("HANDOFF_DAILY_OUTPUT_TOKENS", "1228800"))
                ):
                    raise ProviderError("daily_provider_budget_reached")
                db.execute("INSERT OR REPLACE INTO provider_budget VALUES (?,?,?)",
                           (day, calls + 1, reserved + reserve))
            return self.providers.call(provider, prompt, fixture)
        finally:
            self.lock.release()
