"""Prove a lead's website, and strip unproven addresses out of its hint.

Research is told to *read* a source, never to infer from a name. On production
data it inferred anyway: a lead called "Legacy 740" was stored with
``legacy740.com`` attached and a ``contact_hint`` reading "General mailbox
staff@legacy740.com and phone (740) 434-7613 listed on their contact page". The
signal source - a Chamber ribbon-cutting notice - published no domain, no
address and no phone. The domain resolves to "a sophisticated digital gallery
designed for architects and creatives", which is not a gym, not a clinic, and
not this business. Nothing was read: the URL was built from the business name,
and the mailbox was built from the URL.

That failure is expensive in exactly the way this product cannot afford - a
bounce, or worse a delivered pitch, into a stranger's inbox under a client's
signature. So two invariants, enforced where the lead is written:

1. A stored domain must be legible as *this* business. Either the signal source
   printed it, or the site itself names the business in enough detail to rule
   out a namesake: two distinctive name words, or one word plus the town. A
   domain that cannot be proven is dropped, not kept "just in case" - an
   unproven URL is how a lead turns into a wrong send.
2. A stored hint carries no address. Addresses come from ``app/discover.py``,
   which only ever returns strings printed on a page, and are DNS-checked
   before they can be mailed. A hint is prose for a human; it is not evidence.

Every network read goes through ``_fetch``, so tests hand in a fake site and no
test ever touches the internet.
"""
from __future__ import annotations

import re

from . import audit

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Words that appear in half the company names in any town: they cannot prove
# identity, and requiring them would let "Main Street Services LLC" match any
# site that says "main street". Place names are excluded for the same reason -
# a town in the name is checked separately, against the town we already know.
_GENERIC = frozenset({
    "the", "and", "for", "llc", "inc", "incorporated", "co", "company", "corp",
    "corporation", "ltd", "limited", "llp", "pllc", "group", "holdings", "shop",
    "store", "stores", "services", "service", "solutions", "center", "centre",
    "company", "associates", "partners", "enterprises", "industries", "supply",
    "main", "street", "road", "avenue", "suite", "north", "south", "east", "west",
    "best", "local", "quality", "premier", "elite", "pro", "pros", "team",
    "ohio", "county", "city", "valley", "river", "ridge",
})


def bare_host(raw: str | None) -> str:
    """'https://WWW.Example.com/about' -> 'example.com'. Never raises."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"^[a-z]+://", "", s)
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    s = s.split("@")[-1].split(":", 1)[0]
    s = s.strip(".")
    if s.startswith("www."):
        s = s[4:]
    if "." not in s or " " in s:
        return ""
    return s


def visible_text(html: str | None) -> str:
    """Readable text only: scripts, styles and tags removed, whitespace folded."""
    if not html:
        return ""
    t = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&#64;", "@").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", t).strip()


def distinctive_words(name: str | None) -> list[str]:
    """Name words strong enough to prove identity, deduped, order kept."""
    out: list[str] = []
    for w in re.findall(r"[a-z]{4,}", (name or "").lower()):
        if w not in _GENERIC and w not in out:
            out.append(w)
    return out


def _site_text(domain: str, fetch) -> tuple[bool, str, str]:
    """(reachable, visible_text, url) for a domain, https first then http."""
    for url in (f"https://{domain}", f"http://{domain}"):
        try:
            page = fetch(url)
        except Exception:  # noqa: BLE001 - a dead site is a normal answer here
            continue
        if page is not None and getattr(page, "ok", False):
            text = visible_text(getattr(page, "text", "") or "")
            if text:
                return True, text, url
    return False, "", ""


def check_lead(lead: dict, locality: str | None = None, _fetch=None) -> dict:
    """Return {"domain": proven-or-empty, "note": why}.

    ``locality`` is the town the lead is meant to be in (the client's own town
    for this product). It is the tie-breaker when the name has only one strong
    word - "Legacy 740" has one, so the site has to also say "Marietta".
    """
    name = str(lead.get("name") or "").strip()
    raw = lead.get("domain") or lead.get("website") or ""
    domain = bare_host(raw)
    if not domain:
        return {"domain": "", "note": ""}

    fetch = _fetch or audit.fetch
    source = str(lead.get("signal_source") or "").strip()

    # Best case: the page the agent says it read actually printed the URL.
    if source:
        try:
            page = fetch(source)
        except Exception:  # noqa: BLE001
            page = None
        if page is not None and getattr(page, "ok", False):
            body = (getattr(page, "text", "") or "").lower()
            if domain in body or f"www.{domain}" in body:
                return {"domain": domain, "note": f"domain printed on {source}"}

    # Otherwise the site has to identify itself as this business.
    reachable, text, url = _site_text(domain, fetch)
    if not reachable:
        return {"domain": "",
                "note": f"dropped {domain}: site unreachable, so it cannot confirm identity"}

    low = text.lower()
    words = distinctive_words(name)
    hits = [w for w in words if w in low]
    place = (locality or "").strip().lower()
    place_hit = bool(place) and place in low

    if len(words) >= 2 and len(hits) >= 2:
        return {"domain": domain, "note": f"domain confirmed by {url} (names {', '.join(hits)})"}
    if len(words) == 1 and hits and place_hit:
        return {"domain": domain,
                "note": f"domain confirmed by {url} (names {hits[0]} in {locality})"}

    # Rejected: say what the site calls itself, so a human can see the mismatch
    # without re-reading the crawl. Truncated - this column is a note, not a dump.
    shape = low[:120].strip()
    why = "name too generic to confirm" if not words else "site does not name this business"
    return {"domain": "",
            "note": f"dropped {domain}: {why} (site reads: \"{shape}\")"}


def sanitize_hint(hint: str | None) -> tuple[str, list[str]]:
    """Strip any address out of a hint. Returns (text, addresses_removed).

    Kept separate from ``check_lead`` because it needs no network and must run
    even when the lead has no domain at all - the fabricated address in
    production came with a fabricated domain, but the two can appear alone.
    """
    text = (hint or "").strip()
    if not text:
        return "", []
    found = EMAIL_RE.findall(text)
    if not found:
        return text[:300], []
    text = EMAIL_RE.sub(" ", text)
    # Tidy the hole the address left: "mailbox  and phone" -> "mailbox and phone".
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"([,;:])\s*([,.;:])", r"\1", text)
    text = text.strip(" ,;:-")
    note = "[unverified addresses removed - none were printed on a source we read]"
    return (f"{text} {note}".strip())[:300], found
