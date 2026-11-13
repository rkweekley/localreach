"""LocalReach — a local-business outreach platform.

Design constraints baked in from the Resquared teardown:
  * never send to an unverified address
  * never send to a suppressed address (checked at send time, not enqueue time)
  * every email carries a physical address and a working one-click unsubscribe
  * no open-tracking pixel (it hurts deliverability and buys nothing)
"""
import os

from fastapi import FastAPI

from . import routes_ai, routes_campaigns, routes_data, routes_webhooks
from .db import init_db

app = FastAPI(
    title="LocalReach",
    version="0.1.0",
    description="Local business outreach: discover, enrich, personalize, sequence, reply.",
)

app.include_router(routes_data.router)
app.include_router(routes_campaigns.router)
app.include_router(routes_ai.router)
app.include_router(routes_webhooks.router)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "version": app.version,
        "dry_run": os.getenv("DRY_RUN", "1") == "1",
        "sender": os.getenv("MAILGUN_SENDER", "outreach@example.com"),
    }
