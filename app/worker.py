"""Background send loop.

Replica-safe by construction: the loop only calls `tick`, and `tick` claims each
enrollment before sending, so running two workers cannot double-send. That is
why there is no scheduler thread inside the web process — restarts and replica
counts stop being a correctness problem.

Run as its own container, or `python -m app.worker` for a quick manual pass with
`--once`.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

from . import config, logging_setup
from .db import connect, init_db, rows
from .routes_campaigns import tick

log = logging.getLogger("localreach.worker")
_running = True


def _stop(signum, frame):  # noqa: ARG001
    global _running
    _running = False
    log.info("shutdown signal received", extra={"extra_fields": {"signal": signum}})


def workspaces() -> list[str]:
    conn = connect()
    try:
        return [r["id"] for r in rows(conn.execute("SELECT id FROM workspaces"))]
    finally:
        conn.close()


def heartbeat(path: str) -> None:
    """Touch a file each pass so the container's healthcheck can tell a live
    worker from a wedged one. Best-effort: never let it break sending."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(int(time.time())))
    except OSError:
        log.warning("heartbeat write failed", extra={"extra_fields": {"path": path}})


def pass_once(limit: int) -> dict:
    summary: dict[str, int] = {}
    for wid in workspaces():
        try:
            result = tick(wid, limit)
            for item in result.get("results", []):
                summary[item["status"]] = summary.get(item["status"], 0) + 1
        except Exception:  # noqa: BLE001 - one bad workspace must not stop the rest
            log.exception("tick failed", extra={"extra_fields": {"workspace": wid}})
    if summary:
        log.info("worker pass", extra={"extra_fields": summary})
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    args = ap.parse_args()

    settings = config.get_settings()
    logging_setup.configure(settings.log_level)
    init_db()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log.info("worker starting", extra={"extra_fields": {
        "dry_run": settings.dry_run, "interval": settings.worker_interval_s,
        "tz": settings.send_tz}})

    beat = os.path.join(os.path.dirname(settings.db_path) or ".", "worker.heartbeat")

    if args.once:
        heartbeat(beat)
        pass_once(settings.worker_limit)
        return 0

    while _running:
        heartbeat(beat)
        pass_once(settings.worker_limit)
        # Sleep in short slices so SIGTERM lands promptly instead of after a
        # full interval (Docker sends SIGKILL 10s after SIGTERM).
        deadline = time.monotonic() + settings.worker_interval_s
        while _running and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
    log.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
