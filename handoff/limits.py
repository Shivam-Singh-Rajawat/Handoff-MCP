"""Provider-call limits shared by queued and direct MCP workflows."""
from datetime import datetime, timezone
import os
import threading

from .providers import ProviderError


class LimitedProviders:
    def __init__(self, database, providers):
        self.database = database
        self.providers = providers
        self.lock = threading.BoundedSemaphore(1)
        with database.connection() as db:
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
            with self.database.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT calls,reserved FROM provider_budget WHERE day=?", (day,)).fetchone()
                calls, reserved = tuple(row) if row else (0, 0)
                if calls >= int(os.getenv("HANDOFF_DAILY_CALLS", "300")) or (
                    reserved + reserve > int(os.getenv("HANDOFF_DAILY_OUTPUT_TOKENS", "1228800"))
                ):
                    raise ProviderError("daily_provider_budget_reached")
                db.execute("INSERT INTO provider_budget (day,calls,reserved) VALUES (?,?,?) ON CONFLICT(day) DO UPDATE SET calls=excluded.calls,reserved=excluded.reserved",
                           (day, calls + 1, reserved + reserve))
            return self.providers.call(provider, prompt, fixture)
        finally:
            self.lock.release()
