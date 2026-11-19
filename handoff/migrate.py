"""Explicit, idempotent import of an existing SQLite workspace into PostgreSQL."""
import os
import sqlite3

from .jobs import Jobs
from .projects import Projects
from .providers import Providers
from .store import Handoffs


def migrate_sqlite(source, workspace="owner"):
    if not os.getenv("DATABASE_URL"):
        raise ValueError("Set DATABASE_URL to the destination PostgreSQL database.")
    if not source.is_file():
        raise ValueError("The source SQLite database does not exist.")
    destination = Handoffs(source, Providers(offline=True), workspace=workspace)
    Jobs(destination)
    Projects(destination)
    counts = {}
    with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as original:
        original.row_factory = sqlite3.Row
        existing = {row[0] for row in original.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with destination.connection() as target:
            for table in ("handoff", "workspace_run", "job", "project", "artifact", "provider_budget"):
                if table not in existing:
                    continue
                counts[table] = 0
                for row in original.execute(f"SELECT * FROM {table}"):
                    names = row.keys()
                    placeholders = ",".join("?" for _ in names)
                    changed = target.execute(
                        f"INSERT INTO {table} ({','.join(names)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                        tuple(row),
                    ).rowcount
                    counts[table] += changed
    return counts
