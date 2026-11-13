"""Settings, loaded once, validated at startup.

The rule this file enforces: a production process must refuse to boot in an
unsafe configuration. Half-configured outreach software is worse than none —
it either sends nothing (so you think it works) or sends without compliance
controls (so it gets your domain blacklisted).

Anything optional degrades to a documented default; anything security-critical
defaults to CLOSED.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

TRUE = {"1", "true", "yes", "on"}


def _b(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in TRUE


def _s(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _i(name: str, default: int) -> int:
    try:
        return int(_s(name) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    env: str = "development"
    dry_run: bool = True
    # public surface
    public_base_url: str = "http://localhost:8000"
    api_token: str = ""
    # mailgun
    mailgun_domain: str = ""
    mailgun_api_key: str = ""
    mailgun_api_base: str = "https://api.mailgun.net/v3"
    mailgun_sender: str = ""
    mailgun_sender_name: str = ""
    mailgun_reply_to: str = ""
    mailgun_signing_key: str = ""
    mailgun_webhook_tolerance_s: int = 900
    reply_forward_to: str = ""
    # compliance
    unsub_secret: str = ""
    allow_role_addresses: bool = False
    send_tz: str = "America/New_York"
    # storage
    db_path: str = "localreach.db"
    outbox_dir: str = "./outbox"
    # behaviour
    ai_base_url: str = ""
    ai_model: str = ""
    ai_api_key: str = ""
    worker_limit: int = 25
    worker_interval_s: int = 60
    log_level: str = "INFO"
    generated_dev_secret: bool = field(default=False)

    @property
    def production(self) -> bool:
        return self.env.lower() in {"prod", "production"}


def load() -> Settings:
    """Read env once; invent a dev-only secret so tests never need one."""
    unsub = _s("UNSUB_SECRET")
    generated = False
    if not unsub:
        # Random per-process: makes dev safe, and makes any production
        # misconfiguration loudly wrong instead of silently weak.
        unsub = secrets.token_hex(32)
        generated = True

    return Settings(
        env=_s("APP_ENV", "development"),
        dry_run=_b("DRY_RUN", default=True),
        public_base_url=_s("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        api_token=_s("API_TOKEN"),
        mailgun_domain=_s("MAILGUN_DOMAIN"),
        mailgun_api_key=_s("MAILGUN_API_KEY"),
        mailgun_api_base=_s("MAILGUN_API_BASE", "https://api.mailgun.net/v3").rstrip("/"),
        mailgun_sender=_s("MAILGUN_SENDER"),
        mailgun_sender_name=_s("MAILGUN_SENDER_NAME", "Cyberal Solutions"),
        mailgun_reply_to=_s("MAILGUN_REPLY_TO"),
        mailgun_signing_key=_s("MAILGUN_SIGNING_KEY"),
        mailgun_webhook_tolerance_s=_i("MAILGUN_WEBHOOK_TOLERANCE_S", 900),
        reply_forward_to=_s("REPLY_FORWARD_TO"),
        unsub_secret=unsub,
        allow_role_addresses=_b("ALLOW_ROLE_ADDRESSES"),
        send_tz=_s("SEND_TZ", "America/New_York"),
        db_path=_s("LOCALREACH_DB", str(Path(__file__).resolve().parent.parent / "localreach.db")),
        outbox_dir=_s("OUTBOX_DIR", "./outbox"),
        ai_base_url=_s("AI_BASE_URL"),
        ai_model=_s("AI_MODEL"),
        ai_api_key=_s("AI_API_KEY", "ollama"),
        worker_limit=_i("WORKER_LIMIT", 25),
        worker_interval_s=_i("WORKER_INTERVAL_S", 60),
        log_level=_s("LOG_LEVEL", "INFO").upper(),
        generated_dev_secret=generated,
    )


class ConfigError(RuntimeError):
    pass


_CACHED: Settings | None = None


def get_settings() -> Settings:
    """Process-wide settings, loaded once, so every module agrees on secrets."""
    global _CACHED
    if _CACHED is None:
        _CACHED = load()
    return _CACHED


def reset_settings_cache() -> None:
    global _CACHED
    _CACHED = None


def validate(s: Settings) -> list[str]:
    """Return fatal problems. Called at startup in production only."""
    problems: list[str] = []

    if not s.api_token:
        problems.append("API_TOKEN is empty — the API would be world-writable")
    elif len(s.api_token) < 24:
        problems.append("API_TOKEN is shorter than 24 chars — use `openssl rand -hex 32`")

    if s.generated_dev_secret:
        problems.append("UNSUB_SECRET is unset — unsubscribe links would break on restart")
    if len(s.unsub_secret) < 32:
        problems.append("UNSUB_SECRET is shorter than 32 chars — use `openssl rand -hex 32`")

    if not s.public_base_url.startswith("https://"):
        problems.append(f"PUBLIC_BASE_URL must be https in production (got {s.public_base_url!r})")

    if not s.dry_run:
        if not s.mailgun_domain:
            problems.append("MAILGUN_DOMAIN is empty but DRY_RUN=0")
        if not s.mailgun_api_key:
            problems.append("MAILGUN_API_KEY is empty but DRY_RUN=0")
        if not s.mailgun_sender:
            problems.append("MAILGUN_SENDER is empty but DRY_RUN=0")
        if s.mailgun_sender and s.mailgun_domain:
            dom = s.mailgun_sender.rsplit("@", 1)[-1].lower()
            if dom != s.mailgun_domain.lower():
                problems.append(
                    f"MAILGUN_SENDER domain ({dom}) is not the authenticated Mailgun "
                    f"domain ({s.mailgun_domain}) — Mailgun will reject the send"
                )
        if not s.mailgun_signing_key:
            problems.append(
                "MAILGUN_SIGNING_KEY is empty but DRY_RUN=0 — webhooks would be "
                "unauthenticated (anyone could forge a bounce or an unsubscribe)"
            )

    return problems
