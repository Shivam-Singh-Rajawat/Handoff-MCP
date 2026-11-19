"""Workspace database connections with SQLite and PostgreSQL support."""
from contextlib import contextmanager
import atexit
import hashlib
import os
import re
import sqlite3
import threading

_pools = {}
_pool_lock = threading.Lock()


@atexit.register
def close_pools():
    for pool in list(_pools.values()):
        pool.close()



class Row:
    """Expose the index, key, and iteration behavior used by sqlite3.Row."""
    def __init__(self, names, values):
        self.names = names
        self.values = values

    def keys(self):
        return self.names

    def __getitem__(self, key):
        return self.values[key if isinstance(key, int) else self.names.index(key)]

    def __iter__(self):
        return iter(self.values)


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def fetchone(self):
        values = self.cursor.fetchone()
        return Row([column.name for column in self.cursor.description], values) if values is not None else None

    def fetchall(self):
        return list(self)

    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row


class PostgresConnection:
    def __init__(self, connection, lock_id):
        self.connection = connection
        self.lock_id = lock_id

    def execute(self, statement, params=()):
        if statement == "BEGIN IMMEDIATE":
            return Cursor(self.connection.execute("SELECT pg_advisory_xact_lock(%s)", (self.lock_id,)))
        statement = statement.replace("?", "%s").replace("rowid", "creation_order")
        if isinstance(params, dict):
            statement = re.sub(r":([a-z_]+)", r"%(\1)s", statement)
        if re.search(r"CREATE TABLE IF NOT EXISTS (project|workspace_run|job)\b", statement):
            statement = statement.replace("id TEXT PRIMARY KEY", "creation_order BIGSERIAL UNIQUE, id TEXT PRIMARY KEY")
        return Cursor(self.connection.execute(statement, params))


class Database:
    def __init__(self, path, workspace="owner"):
        self.path = path
        self.url = os.getenv("DATABASE_URL", "")
        digest = hashlib.sha256(workspace.encode()).hexdigest()
        self.schema = "mh_" + digest[:20]
        self.lock_id = int(digest[:15], 16)
        if self.url:
            from psycopg import sql
            from psycopg_pool import ConnectionPool
            with _pool_lock:
                key = (self.url, self.schema)
                if key not in _pools:
                    _pools[key] = ConnectionPool(self.url, min_size=0, max_size=4,
                                                 timeout=15, max_idle=60,
                                                 kwargs={"connect_timeout": 10})
                self.pool = _pools[key]
            with self.pool.connection() as connection:
                connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self):
        if self.url:
            from psycopg import sql
            with self.pool.connection() as connection:
                connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(self.schema)))
                yield PostgresConnection(connection, self.lock_id)
        else:
            connection = sqlite3.connect(self.path, timeout=20)
            connection.row_factory = sqlite3.Row
            try:
                with connection:
                    yield connection
            finally:
                connection.close()
