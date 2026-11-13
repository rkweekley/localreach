"""Structured JSON logging + request instrumentation.

Container logs are the only evidence you get after an incident, and
`docker logs` with default formatting loses the fields you need. One JSON
object per line means `jq` works and nothing is lost to line wrapping.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

REDACT = {"api_token", "authorization", "mailgun_api_key", "unsub_secret", "mailgun_signing_key"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"))


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # uvicorn keeps its own handlers; route them through ours too.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "gunicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


class RequestContext(BaseHTTPMiddleware):
    """Assigns a request id, logs one line per request, sets security headers."""

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logging.getLogger("localreach.access").exception(
                "unhandled error", extra={"extra_fields": {"rid": rid, "path": request.url.path}}
            )
            raise
        dur_ms = round((time.perf_counter() - started) * 1000, 1)
        logging.getLogger("localreach.access").info(
            "request",
            extra={"extra_fields": {
                "rid": rid,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "dur_ms": dur_ms,
            }},
        )
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response


class BodyLimit(BaseHTTPMiddleware):
    """Refuse oversized bodies before they are buffered.

    Mailgun inbound posts carry attachments, so the ceiling is generous — but
    finite. Without this, an anonymous POST can exhaust container memory.
    """

    def __init__(self, app, max_bytes: int = 8 * 1024 * 1024):
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > self.max_bytes:
            from starlette.responses import JSONResponse
            return JSONResponse({"detail": "payload too large"}, status_code=413)
        return await call_next(request)
