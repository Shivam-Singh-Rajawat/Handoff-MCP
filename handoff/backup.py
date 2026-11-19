"""Consistent SQLite snapshots for the owner and invited workspaces."""
from pathlib import Path
import sqlite3


def backup_tree(database: Path, destination: Path):
    root = database.parent.resolve()
    destination = destination.resolve()
    if destination == root or root in destination.parents:
        raise ValueError("Choose a backup destination outside the data directory.")
    destination.mkdir(parents=True, exist_ok=False)
    paths = [database, *sorted((root / "accounts").glob("*/history.sqlite"))]
    saved = []
    for source in paths:
        target = destination / source.resolve().relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as original:
            with sqlite3.connect(target) as snapshot:
                original.backup(snapshot)
                if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed.")
        target.chmod(0o600)
        saved.append(str(target))
    return saved
