"""Find real, evidence-backed email addresses for a prospect.

Hard rule: this module never invents an address. No `first.last@domain` guessing,
no pattern permutation, no "info@ is probably right". Every address it returns was
literally printed on a page the prospect published, and carries the URL it came
from so a human can check it. Guessing addresses is how an outreach tool earns a
9% bounce rate and a suspended sending domain.

What it does: fetch the prospect's home page plus the usual contact pages, pull
`mailto:` links and plain-text addresses out of the HTML, drop obvious noise
(image filenames, analytics IDs, example.com placeholders), classify role vs
personal, and hand back candidates in priority order. Verification is somebody
else's job (`app/verify.py`), and permission to mail is a third thing entirely
(`app/compliance.py`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import audit
from .compliance import is_role_address

# Module-level alias so tests and callers can substitute the fetcher without
# monkeypatching the audit engine (and so the audit engine keeps its own one).
fetch = audit.fetch

# Paths worth trying, in the order a human would: the contact page first because
# that is where a business actually puts the address it wants used.
CANDIDATE_PATHS = ("/contact", "/contact-us", "/about", "/about-us", "/team")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MAILTO_RE = re.compile(r"mailto:([^\"'?>\s]+)", re.I)

# Domains that show up in HTML for reasons that have nothing to do with contact.
NOISE_DOMAINS = {
    "example.com", "example.org", "sentry.io", "wixpress.com", "sentry-next.wixpress.com",
    "godaddy.com", "squarespace.com", "wordpress.org", "w3.org", "schema.org",
    "googlemail.com", "facebook.com", "instagram.com", "cloudflare.com", "gstatic.com",
}
NOISE_LOCAL = {
    "no-reply", "noreply", "donotreply", "postmaster", "abuse", "webmaster", "privacy",
    "dmca", "unsubscribe", "bounce", "bounces", "mailer-daemon", "sentry",
}
NOISE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js", ".ico")


@dataclass
class Candidate:
    email: str
    source: str          # the URL it was found on
    is_role: bool
    page: str            # home | contact | about | ...


def extract_emails(html: str, page_url: str) -> list[tuple[str, str]]:
    """Pull (email, url) pairs out of one page. Order preserved so the first one
    found on the contact page wins, which is almost always the right inbox."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    for raw in MAILTO_RE.findall(html or ""):
        addr = _clean(raw)
        if addr and addr not in seen:
            seen.add(addr)
            found.append((addr, page_url))

    for raw in EMAIL_RE.findall(html or ""):
        addr = _clean(raw)
        if addr and addr not in seen:
            seen.add(addr)
            found.append((addr, page_url))

    return found


def _clean(raw: str) -> str | None:
    addr = (raw or "").strip().strip(".,;:<>()[]\"'").lower()
    addr = addr.replace("mailto:", "")
    # CSS/JS sprite junk and encoded entities show up constantly in scraped HTML.
    if any(addr.endswith(ext) for ext in NOISE_EXT):
        return None
    if "@" not in addr or addr.count("@") != 1:
        return None
    local, _, domain = addr.partition("@")
    if not local or not domain or "." not in domain:
        return None
    if any(domain.endswith(d) for d in NOISE_DOMAINS):
        return None
    if local in NOISE_LOCAL:
        return None
    if len(addr) > 254 or len(local) > 64:
        return None
    # Our own tooling, if it ever shows up.
    if domain.endswith("reach.cyberalsolutions.com"):
        return None
    return addr


def _same_site(candidate_domain: str, site_domain: str) -> bool:
    a = candidate_domain.removeprefix("www.")
    b = site_domain.removeprefix("www.")
    return a == b or a.endswith("." + b)


def discover(domain: str, _fetch=None) -> dict:
    """Return verified-by-evidence candidates for `domain`, best first.

    Preference order: an address on the prospect's own domain beats a gmail
    address; a personal-looking mailbox beats a role mailbox (role mailboxes are
    blocked by the send gate by default); anything on the contact page beats
    anywhere else.
    """
    _fetch = _fetch or fetch
    domain = (domain or "").strip().lower()
    domain = re.sub(r"^https?://", "", domain).split("/")[0]
    if not domain:
        return {"domain": "", "candidates": [], "pages_checked": []}

    pages: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    candidates: list[Candidate] = []

    for path, label in (("", "home"),) + tuple((p, p.strip("/").replace("-", "_"))
                                               for p in CANDIDATE_PATHS):
        url = f"https://{domain}{path}"
        if url in seen_urls:
            continue
        seen_urls.add(url)
        page = _fetch(url)
        if not page.ok or not page.text:
            if path == "" and not pages:
                # Try the http fallback once, mirroring the audit engine: a site
                # without a certificate still publishes its address.
                page = _fetch(f"http://{domain}")
            if not page.ok or not page.text:
                continue
        pages.append((page.url or url, label))
        for addr, src in extract_emails(page.text, page.url or url):
            candidates.append(Candidate(email=addr, source=src,
                                        is_role=is_role_address(addr), page=label))

    # Dedupe by address, keeping the highest-quality hit (contact page, same site).
    by_email: dict[str, Candidate] = {}
    for cand in candidates:
        prev = by_email.get(cand.email)
        if prev is None or _rank(cand, domain) < _rank(prev, domain):
            by_email[cand.email] = cand

    ordered = sorted(by_email.values(), key=lambda c: (_rank(c, domain), c.email))
    return {
        "domain": domain,
        "pages_checked": [p[0] for p in pages],
        "candidates": [
            {"email": c.email, "source": c.source, "page": c.page, "is_role": c.is_role}
            for c in ordered[:5]
        ],
    }


def _rank(c: Candidate, site_domain: str) -> tuple:
    """Lower is better. Same-site and personal win; role mailboxes last because
    the default send gate will refuse them anyway."""
    local, _, cand_domain = c.email.partition("@")
    return (
        1 if c.is_role else 0,
        0 if _same_site(cand_domain, site_domain) else 1,
        0 if c.page == "contact" else 1,
        0 if "+" not in local else 1,
        len(c.email),
    )


def best(candidates: list[dict]) -> dict | None:
    return candidates[0] if candidates else None
