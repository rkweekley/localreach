"""The compliance gate. Every outbound message passes through here.

Rules (non-negotiable, they are the difference between an asset and a lawsuit):
  1. Never email an address that is not `verified`.
  2. Never email a suppressed address — checked at send time, not enqueue time,
     because a suppression can land between enqueue and send.
  3. Never email across a suppressed domain (covers role-address harvesters).
  4. Every message carries a physical postal address and a one-click unsubscribe.
  5. Enforce a per-mailbox daily cap so we ramp reputation instead of spiking it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass

from .config import get_settings
from .db import connect, rows, utcnow

ROLE_PREFIXES = (
    "info", "sales", "contact", "support", "hello", "admin", "office",
    "billing", "accounts", "team", "help", "service", "customerservice",
)


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""
    detail: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def _secret() -> str:
    return os.getenv("UNSUB_SECRET") or get_settings().unsub_secret


def unsubscribe_token(workspace_id: str, email: str) -> str:
    """Self-contained token: `u2.<b64url(email)>.<hmac>`.

    The address is embedded so a clicked link can unsubscribe without the
    recipient typing anything — the old 32-hex token had no address in it, so
    the landing-page form submitted an empty field and silently unsubscribed
    nobody. The HMAC still binds the address to the workspace, so a token for
    one address cannot be replayed against another.
    """
    email_n = email.lower().strip()
    body = base64.urlsafe_b64encode(email_n.encode()).decode().rstrip("=")
    msg = f"{workspace_id}:{email_n}".encode()
    sig = hmac.new(_secret().encode(), msg, hashlib.sha256).hexdigest()[:32]
    return f"u2.{body}.{sig}"


def token_email(token: str) -> str | None:
    """Extract the address from a v2 token. Returns None if malformed."""
    parts = (token or "").split(".")
    if len(parts) != 3 or parts[0] != "u2":
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        email = base64.urlsafe_b64decode(padded).decode().strip().lower()
    except Exception:  # noqa: BLE001 - any decode failure is just an invalid token
        return None
    return email if "@" in email else None


def verify_token(workspace_id: str, email: str, token: str) -> bool:
    return hmac.compare_digest(unsubscribe_token(workspace_id, email), token or "")


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def is_role_address(email: str) -> bool:
    local = email.split("@")[0].lower()
    return any(local == p or local.startswith(p + "-") or local.startswith(p + ".") for p in ROLE_PREFIXES)


def suppressed(workspace_id: str, email: str) -> str | None:
    """Return the suppression reason, or None."""
    email = email.lower()
    dom = domain_of(email)
    conn = connect()
    try:
        hit = conn.execute(
            "SELECT reason FROM suppressions WHERE email = ? AND (workspace_id = ? OR scope = 'global')",
            (email, workspace_id),
        ).fetchone()
        if hit:
            return hit["reason"]
        hit = conn.execute(
            "SELECT reason FROM suppressions WHERE scope = 'domain' AND domain = ?", (dom,)
        ).fetchone()
        return hit["reason"] if hit else None
    finally:
        conn.close()


def sent_today(workspace_id: str, from_email: str) -> int:
    conn = connect()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM messages
               WHERE workspace_id = ? AND direction = 'outbound' AND from_email = ?
                 AND substr(sent_at, 1, 10) = substr(?, 1, 10)""",
            (workspace_id, from_email, utcnow()),
        ).fetchone()
        return int(row["n"])
    finally:
        conn.close()


def check_send(workspace_id: str, to_email: str, email_status: str, from_email: str, cap: int) -> Verdict:
    if not to_email or "@" not in to_email:
        return Verdict(False, "no_email", "contact has no email address")
    if email_status != "verified":
        return Verdict(False, "unverified",
                       f"email_status={email_status}; only verified addresses are mailable")
    if is_role_address(to_email) and not get_settings().allow_role_addresses:
        return Verdict(False, "role_address",
                       f"{to_email} is a role mailbox; set ALLOW_ROLE_ADDRESSES=1 to override")
    reason = suppressed(workspace_id, to_email)
    if reason:
        return Verdict(False, "suppressed", f"suppression: {reason}")
    if sent_today(workspace_id, from_email) >= cap:
        return Verdict(False, "daily_cap", f"{cap}/day cap reached for {from_email}")
    return Verdict(True)


def add_suppression(workspace_id: str, email: str, reason: str, scope: str = "workspace",
                    note: str | None = None, conn=None) -> None:
    """Record a suppression: address, domain, or global.

    Takes an optional existing connection: calling this with a second connection
    while the caller already holds a write transaction deadlocks SQLite
    ("database is locked") — the webhook handlers pass their own conn.
    """
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            """INSERT INTO suppressions (id, workspace_id, email, domain, reason, scope, note, created_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT (workspace_id, email) DO UPDATE SET reason = excluded.reason""",
            (os.urandom(8).hex(), workspace_id, email.lower(), domain_of(email), reason, scope, note, utcnow()),
        )
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def footer(workspace: dict, token: str) -> str:
    base = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    return (
        "\n\n--\n"
        f"{workspace['name']} · {workspace['physical_address']}\n"
        f"Don't want to hear from me? One click and you're out: {base}/u/{token}\n"
    )
