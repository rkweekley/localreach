"""LocalReach — a local-business outreach platform.

Design constraints baked in from the Resquared teardown:
  * never send to an unverified address
  * never send to a suppressed address (checked at send time, not enqueue time)
  * every email carries a physical address and a working one-click unsubscribe
  * no open-tracking pixel (it hurts deliverability and buys nothing)

Production posture:
  * `/api/*` requires a bearer token
  * `/webhooks/*` requires a valid Mailgun HMAC signature
  * the process refuses to start in production with an unsafe configuration
"""
from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from . import config, logging_setup, routes_ai, routes_campaigns, routes_data, routes_webhooks
from .db import healthcheck, init_db
from .security import RateLimiter, require_token

log = logging.getLogger("localreach")

settings = config.get_settings()
logging_setup.configure(settings.log_level)

app = FastAPI(
    title="LocalReach",
    version="1.0.0",
    description="Local business outreach: discover, enrich, personalize, sequence, reply.",
    docs_url=None if settings.production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.production else "/openapi.json",
)
app.state.settings = settings
app.state.limiter = RateLimiter()

app.add_middleware(logging_setup.BodyLimit)
app.add_middleware(logging_setup.RequestContext)

# Operator surface — every one of these routes is authenticated.
_auth = [Depends(require_token)]
app.include_router(routes_data.router, dependencies=_auth)
app.include_router(routes_campaigns.router, dependencies=_auth)
app.include_router(routes_ai.router, dependencies=_auth)
# Public surface: signed-token opt-out, then HMAC-verified webhooks.
app.include_router(routes_webhooks.router)


@app.on_event("startup")
def _startup() -> None:
    problems = config.validate(app.state.settings)
    if problems:
        for p in problems:
            log.error("config problem: %s", p)
        if app.state.settings.production:
            raise RuntimeError("refusing to start with unsafe production config: " + "; ".join(problems))
        log.warning("running in DEVELOPMENT with %d config problem(s) — do not expose this", len(problems))
    init_db()
    log.info("startup complete", extra={"extra_fields": {
        "env": settings.env,
        "dry_run": settings.dry_run,
        "db": settings.db_path,
        "auth": bool(settings.api_token),
    }})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    rid = getattr(request.state, "request_id", "-")
    # Never leak a stack trace to the network; the trace is in the logs.
    log.exception("unhandled", extra={"extra_fields": {"rid": rid, "path": request.url.path}})
    return JSONResponse({"detail": "internal error", "request_id": rid}, status_code=500)


@app.get("/")
def index() -> dict:
    """Service identity for humans and for uptime monitors. No secrets."""
    return {
        "service": "LocalReach",
        "version": app.version,
        "status": "ok",
        "health": "/health",
        "readiness": "/ready",
        "docs": None if settings.production else "/docs",
    }


@app.get("/health")
def health() -> dict:
    """Liveness. Deliberately reveals nothing about configuration."""
    return {"ok": True, "version": app.version, "dry_run": settings.dry_run}


@app.get("/ready")
def ready() -> JSONResponse:
    """Readiness: the DB must be readable AND writable (WAL file present)."""
    ok, detail = healthcheck()
    return JSONResponse(
        {"ok": ok, "db": detail, "env": settings.env, "dry_run": settings.dry_run},
        status_code=200 if ok else 503,
    )
