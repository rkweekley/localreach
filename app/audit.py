"""Does this prospect have a problem the researched business sells a fix for?

"Open issues" as a lead signal only works if the issue is real and verifiable.
This module looks at a prospect's public site and records concrete, checkable
defects — no HTTPS, no mobile viewport, stale copyright, no contact route, no
schema, slow first byte — and nothing that depends on an LLM's opinion. Every
finding carries the evidence that produced it, so `signal_source` can point at
the page and a human can confirm it in ten seconds.

The mapping from finding -> "which service fixes this" lives in `GAP_SERVICES`,
which is deliberately small and honest: we only claim a gap when we can name the
remedy. A finding we cannot connect to something the client sells is still
recorded, just not used as a reason to email.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import httpx

UA = "Mozilla/5.0 (compatible; LocalReachAudit/1.0; +https://reach.cyberalsolutions.com)"
TIMEOUT = 8.0
SLOW_S = 3.5
HEAVY_BYTES = 2_500_000

# finding key -> (severity, weight, service category it maps to)
GAP_SERVICES: dict[str, tuple[str, int, str]] = {
    "site_down":       ("high",   40, "hosting"),
    "no_https":        ("high",   30, "hosting"),
    "no_redirect":     ("medium", 15, "hosting"),
    "no_viewport":     ("high",   25, "web_design"),
    "no_contact_form": ("medium", 20, "web_design"),
    "stale_site":      ("medium", 20, "web_design"),
    "no_meta":         ("medium", 15, "seo"),
    "no_schema":       ("low",    10, "seo"),
    "no_sitemap":      ("low",    10, "seo"),
    "no_analytics":    ("low",    10, "marketing"),
    "slow":            ("medium", 20, "performance"),
    "heavy":           ("low",    10, "performance"),
}

SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1}


@dataclass
class Page:
    url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""
    elapsed: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 400


def fetch(url: str) -> Page:
    """One GET, no retries, no JS. If the server needs a browser to answer, the
    audit reports `site_down` rather than pretending it looked fine."""
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                          headers={"user-agent": UA}) as client:
            started = datetime.now(timezone.utc)
            r = client.get(url)
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            return Page(url=str(r.url), status=r.status_code,
                        headers={k.lower(): v for k, v in r.headers.items()},
                        text=r.text[:400_000], elapsed=elapsed)
    except Exception as exc:  # noqa: BLE001
        return Page(url=url, status=0, error=f"{type(exc).__name__}: {exc}")


def _finding(key: str, evidence: str) -> dict:
    severity, weight, service = GAP_SERVICES[key]
    return {"key": key, "severity": severity, "weight": weight,
            "service": service, "evidence": evidence}


def audit_page(home: Page, http: Page | None, extras: dict[str, Page] | None = None,
               now_year: int | None = None) -> list[dict]:
    """Pure function over already-fetched pages. Keeps the logic testable without
    a network, which is the only reason any of this is trustworthy."""
    now_year = now_year or datetime.now(timezone.utc).year
    extras = extras or {}
    out: list[dict] = []

    if home.error or home.status >= 400:
        return [_finding("site_down", home.error or f"home page returned HTTP {home.status}")]

    html = home.text or ""
    low = html.lower()
    final = (home.url or "").lower()
    headers = home.headers or {}

    if final.startswith("http://"):
        out.append(_finding("no_https", f"final URL is {home.url}"))
    if http is not None and http.ok and (http.url or "").lower().startswith("http://"):
        out.append(_finding("no_redirect", "http:// serves content instead of redirecting to https"))

    if "name=\"viewport\"" not in low and "name='viewport'" not in low:
        out.append(_finding("no_viewport", "no <meta name=\"viewport\"> — page is not mobile-ready"))

    if not re.search(r"<form\b", low) and "mailto:" not in low:
        out.append(_finding("no_contact_form", "no form and no mailto: link — no route to contact"))

    years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)[^0-9]{0,20}(20\d\d)", low)]
    if years:
        newest = max(years)
        if newest < now_year - 1:
            out.append(_finding("stale_site", f"copyright still says {newest}"))
    else:
        out.append(_finding("stale_site", "no copyright year anywhere on the page"))

    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    desc = re.search(r"<meta[^>]+name=[\"']description[\"'][^>]*>", low)
    missing = [n for n, v in (("title", title), ("meta description", desc)) if not v]
    if missing:
        out.append(_finding("no_meta", f"missing {' and '.join(missing)}"))

    if "application/ld+json" not in low:
        out.append(_finding("no_schema", "no JSON-LD structured data — bad for local search"))

    robots = extras.get("robots")
    sitemap = extras.get("sitemap")
    if sitemap is not None and not sitemap.ok:
        out.append(_finding("no_sitemap", "/sitemap.xml is missing"))
    elif robots is not None and robots.ok and "sitemap:" not in (robots.text or "").lower():
        out.append(_finding("no_sitemap", "robots.txt does not reference a sitemap"))

    if not any(m in low for m in ("googletagmanager", "gtag(", "google-analytics",
                                  "analytics.js", "plausible.io", "matomo")):
        out.append(_finding("no_analytics", "no analytics tag — they cannot see their own traffic"))

    if home.elapsed > SLOW_S:
        out.append(_finding("slow", f"home page took {home.elapsed:.1f}s"))

    length = headers.get("content-length")
    try:
        size = int(length) if length else len(html.encode("utf-8", "ignore"))
    except ValueError:
        size = len(html.encode("utf-8", "ignore"))
    if size > HEAVY_BYTES:
        out.append(_finding("heavy", f"home page is {size / 1_000_000:.1f} MB"))

    return out


def audit_site(domain: str, _fetch: Callable[[str], Page] | None = None) -> dict:
    """Audit `domain` and return findings plus a headline score.

    `score` is 0-100 where higher means "more broken, and therefore a better
    prospect" — capped so a dead site does not outrank a live one with five
    fixable problems we could actually sell.

    `_fetch` is resolved at call time on purpose: binding it as a default
    argument froze the module-level `fetch` at import, which made the engine
    untestable and silently sent test runs to the live internet.
    """
    _fetch = _fetch or fetch
    domain = (domain or "").strip().lower()
    domain = re.sub(r"^https?://", "", domain).split("/")[0]
    if not domain:
        return {"domain": "", "findings": [], "score": 0, "services": []}

    home = _fetch(f"https://{domain}")
    http = None
    if home.error:
        # Try plain http before declaring it dead: a lot of small-business sites
        # never got a certificate at all.
        http = _fetch(f"http://{domain}")
        if http.ok:
            home, http = http, None

    extras = {}
    if home.ok:
        extras["robots"] = _fetch(f"{_root(home.url)}/robots.txt")
        extras["sitemap"] = _fetch(f"{_root(home.url)}/sitemap.xml")

    findings = audit_page(home, http, extras)
    findings.sort(key=lambda f: (-f["weight"], f["key"]))
    score = min(100, sum(f["weight"] for f in findings))
    services = sorted({f["service"] for f in findings})
    return {
        "domain": domain,
        "final_url": home.url,
        "site_ok": home.ok,
        "findings": findings,
        "score": score,
        "services": services,
        "top": findings[0]["key"] if findings else "",
    }


def _root(url: str) -> str:
    m = re.match(r"^(https?://[^/]+)", url or "")
    return m.group(1) if m else (url or "")


def summarize(result: dict) -> str:
    """One sentence a human can read in the lead row, with the strongest evidence."""
    if not result.get("findings"):
        return ""
    top = result["findings"][:3]
    return "; ".join(f["evidence"] for f in top)
