"""Thin sqlite3 layer. Postgres in prod, same DDL (see db/schema.sql)."""
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.getenv("LOCALREACH_DB", str(Path(__file__).resolve().parent.parent / "localreach.db"))
SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


def connect() -> sqlite3.Connection:
    # timeout=15 + WAL: without these, a webhook writing while the send worker
    # holds a transaction fails with "database is locked" under any concurrency.
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA.read_text())
        conn.commit()
    finally:
        conn.close()


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]
