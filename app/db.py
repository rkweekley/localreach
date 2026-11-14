"""Thin sqlite3 layer. Postgres in prod, same DDL (see db/schema.sql).

Concurrency notes that matter in production:
  * WAL lets a webhook write while the send worker holds a transaction.
  * busy_timeout means a lock is waited out instead of raising.
  * `init_db()` is idempotent AND applies additive migrations, so an existing
    volume picks up new columns on deploy without a manual step.
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_DB = str(Path(__file__).resolve().parent.parent / "localreach.db")
SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def db_path() -> str:
    """Resolved per call so tests and the container can set it at any time."""
    return os.getenv("LOCALREACH_DB", _DEFAULT_DB)


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


def connect() -> sqlite3.Connection:
    # timeout=15 + WAL: without these, a webhook writing while the send worker
    # holds a transaction fails with "database is locked" under any concurrency.
    path = db_path()
    parent = os.path.dirname(path)
    if parent:  # a misconfigured LOCALREACH_DB must not fail as "unable to open database file"
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


# Columns added after the first release. Additive and nullable, so applying
# them to a live database is safe and needs no downtime.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("enrollments", "claimed_at", "TEXT"),
    ("enrollments", "claimed_by", "TEXT"),
    ("workspaces", "timezone", "TEXT"),
    # site research is cached on the business row: one fetch serves every email,
    # and a site that later goes offline cannot break an in-flight sequence.
    ("businesses", "site_research", "TEXT"),
    ("businesses", "researched_at", "TEXT"),
    # What the DNS check actually saw (mx hosts, spf/dmarc). Kept so a human can
    # audit any "verified" claim after the fact instead of trusting a badge.
    ("contacts", "verify_detail", "TEXT"),
    # Deterministic site-gap audit of a prospect (see app/audit.py): score plus the
    # exact findings, so "we found a problem with their site" is checkable, not vibes.
    ("research_leads", "audit_score", "INTEGER"),
    ("research_leads", "audit_detail", "TEXT"),
]


def _ensure_columns(conn: sqlite3.Connection) -> list[str]:
    applied: list[str] = []
    for table, column, decl in _MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # table not created yet; schema.sql handles it
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            applied.append(f"{table}.{column}")
    return applied


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA.read_text())
        applied = _ensure_columns(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_enroll_due ON enrollments(status, next_send_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_provider ON messages(provider_id)")
        conn.commit()
    finally:
        conn.close()


def healthcheck() -> tuple[bool, str]:
    """Readable and writable. A read-only volume is the classic silent failure."""
    try:
        conn = connect()
        try:
            conn.execute("SELECT 1").fetchone()
            conn.execute("CREATE TABLE IF NOT EXISTS _hc (x INTEGER)")
            conn.execute("INSERT INTO _hc (x) VALUES (1)")
            conn.execute("DROP TABLE IF EXISTS _hc")
            conn.commit()
            return True, "ok"
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return False, f"{type(exc).__name__}: {exc}"


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]
