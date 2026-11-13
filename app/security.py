"""Authentication, webhook signature verification, and abuse limits.

Two independent trust boundaries:

  1. `/api/*`        — operator surface. Bearer token, constant-time compare.
  2. `/webhooks/*`   — Mailgun surface. HMAC over (timestamp, token), plus a
                       replay window. Without this, anyone who learns the URL
                       can forge a bounce or an unsubscribe and silently
                       destroy the list.
  3. `/u/*`          — public by necessity (one-click opt-out). Protected by a
                       signed token that embeds the address, so the endpoint
                       cannot be used to unsubscribe a third party.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import HTTPException, Request, status

from .config import Settings

# ---------------------------------------------------------------- bearer auth


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def require_token(request: Request) -> None:
    """FastAPI dependency for every /api route."""
    settings: Settings = request.app.state.settings
    if not settings.api_token:
        # Fail closed. An unauthenticated deploy is a public write API.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API token not configured")
    supplied = _bearer(request)
    if not hmac.compare_digest(supplied, settings.api_token):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ------------------------------------------------------- mailgun signature


def verify_mailgun(request: Request, timestamp: str, token: str, signature: str) -> None:
    """HMAC-SHA256(timestamp + token, signing_key), per Mailgun's spec.

    Also enforces a freshness window so a captured request cannot be replayed
    indefinitely. Raises 401 on any mismatch.
    """
    settings: Settings = request.app.state.settings
    if not settings.mailgun_signing_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "webhook signing key not configured")
    if not timestamp or not token or not signature:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing signature fields")

    try:
        ts_age = abs(time.time() - int(timestamp))
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed timestamp")
    if ts_age > settings.mailgun_webhook_tolerance_s:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "stale webhook timestamp")

    expected = hmac.new(
        settings.mailgun_signing_key.encode(),
        f"{timestamp}{token}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad webhook signature")


# ------------------------------------------------------------- rate limiting


class RateLimiter:
    """Fixed-window counter, in-process.

    Deliberately not Redis: this deployment is one container, and the endpoints
    that need protecting (webhooks, opt-out) are cheap. If we ever run multiple
    replicas, this becomes per-replica — still enough to stop a single abusive
    client, which is the threat model.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str, limit: int, window_s: int) -> None:
        now = time.time()
        cutoff = now - window_s
        with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
            bucket.append(now)


def client_ip(request: Request) -> str:
    """Trust the proxy header only because NPM sits in front and overwrites it."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
