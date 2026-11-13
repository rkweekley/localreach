"""Website research: turn a prospect's domain into facts an email can cite.

Why this exists: the AI layer can only write something specific if it knows
something specific. `website` used to be passed through as a bare string, so
every generated email was generic ("you're a roofer in Columbus"). This module
fetches the prospect's own site, extracts what they actually sell, and caches
the result on the business row with provenance.

Design rules:
  * No LLM required. Extraction is stdlib-only; the model only *structures*
    what the HTML already said. A dead model degrades quality, never uptime.
  * No network required. A site that 404s, times out, or blocks us returns a
    partial profile with an explicit `error`, never an exception to the caller.
  * SSRF-guarded. The server fetches a URL a human typed, so private, loopback
    and link-local addresses are refused before a socket is opened. Without
    this, one paste of http://192.168.0.1 would let the API read the LAN.
  * Polite. One request per page, 10s timeout, 512 KB cap, honest User-Agent,
    no crawling beyond the homepage plus at most two same-site pages.
"""
from __future__ import annotations

import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

UA = "LocalReachBot/1.0 (+https://reach.cyberalsolutions.com; prospect research)"
MAX_BYTES = 512 * 1024
TIMEOUT = 10
MAX_EXTRA_PAGES = 2

# Pages that usually describe what a business does, in the order worth trying.
CANDIDATE_PATHS = ("/about", "/services", "/what-we-do", "/about-us")

_NAV_WORDS = {
    "home", "about", "about us", "contact", "contact us", "services", "service",
    "blog", "news", "careers", "jobs", "reviews", "gallery", "portfolio", "faq",
    "faqs", "privacy", "privacy policy", "terms", "terms of service", "login",
    "sign in", "menu", "search", "cart", "shop", "store", "locations", "team",
    "our team", "quote", "get a quote", "free estimate", "estimate", "call now",
    "learn more", "read more", "see more", "click here", "back to top",
}

_FREE_MAIL = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "mail.com", "protonmail.com", "msn.com", "live.com",
}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PLACEHOLDER_EMAILS = {
    "you@email.com", "your@email.com", "name@email.com", "email@email.com",
    "you@example.com", "name@example.com", "email@example.com", "test@test.com",
    "user@example.com", "yourname@email.com", "info@yourdomain.com",
}


def _is_placeholder_email(email: str) -> bool:
    """Form mock-ups ("you@email.com") must never become a lead's contact address."""
    if email in _PLACEHOLDER_EMAILS:
        return True
    local, _, domain = email.partition("@")
    return domain in ("example.com", "example.org", "domain.com", "email.com", "test.com") or local in ("you", "your", "yourname", "name", "email", "test")


_PHONE_RE = re.compile(r"\(?\b\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
_CITY_STATE_RE = re.compile(r"\b([A-Z][a-zA-Z.\-]+(?: [A-Z][a-zA-Z.\-]+){0,2}),\s*([A-Z]{2})\b")


class ResearchError(ValueError):
    """A bad input we can explain to the operator."""


# ---------------------------------------------------------------- url handling

def normalize_url(raw: str) -> tuple[str, str]:
    """Accept 'acme.com', 'www.acme.com/about', 'https://acme.com' alike.

    Returns (canonical_url, domain). Raises ResearchError with a message worth
    showing a human, because this is the one thing they can fix themselves.
    """
    s = (raw or "").strip()
    if not s:
        raise ResearchError("Enter a domain, e.g. acmesupply.com")
    if any(c.isspace() for c in s):
        raise ResearchError("A domain cannot contain spaces")
    if "://" not in s:
        s = "https://" + s
    parts = urllib.parse.urlsplit(s)
    if parts.scheme not in ("http", "https"):
        raise ResearchError("Only http and https URLs can be researched")
    host = (parts.hostname or "").strip().rstrip(".")
    if not host:
        raise ResearchError("That URL has no hostname")
    if "." not in host:
        raise ResearchError(f"'{host}' is not a domain — try something like acme.com")
    netloc = host if not parts.port else f"{host}:{parts.port}"
    path = parts.path.rstrip("/") or ""
    return f"{parts.scheme}://{netloc}{path}", host


def _assert_public(host: str) -> None:
    """Refuse anything that could reach the host network or cloud metadata."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ResearchError(f"Could not resolve {host} — check the spelling") from exc
    for info in infos:
        addr = info[4][0]
        ip = socket.inet_pton(socket.AF_INET6, addr) if ":" in addr else socket.inet_aton(addr)
        if _is_private(addr, ip):
            raise ResearchError(
                f"{host} resolves to a private address ({addr}); refusing to fetch it"
            )


def _is_private(addr: str, packed: bytes) -> bool:
    import ipaddress

    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return True
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


# --------------------------------------------------------------------- fetching

def fetch(url: str) -> tuple[str, str]:
    """GET one page. Returns (html, final_url). Raises ResearchError."""
    parts = urllib.parse.urlsplit(url)
    _assert_public(parts.hostname or "")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",  # no gzip: keeps stdlib-only handling honest
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "html" not in ctype and "xml" not in ctype and ctype:
                raise ResearchError(f"That URL served {ctype.split(';')[0]}, not a web page")
            raw = resp.read(MAX_BYTES)
            charset = resp.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace"), resp.geturl()
    except ResearchError:
        raise
    except urllib.error.HTTPError as exc:
        raise ResearchError(f"The site answered HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ResearchError(f"Could not load {url}: {reason}") from exc


# --------------------------------------------------------------------- parsing

class _Extractor(HTMLParser):
    """Collects only what a sales email could honestly cite."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.headings: list[tuple[str, str]] = []
        self.list_items: list[str] = []
        self.mailtos: list[str] = []
        self.tels: list[str] = []
        self.links: list[str] = []
        self.text_chunks: list[str] = []
        self._stack: list[str] = []
        self._buf: list[str] = []
        self._capture: str | None = None
        self._skip = 0
        self._li_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "svg", "iframe"):
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "title":
            self._capture = "title"
        elif tag in ("h1", "h2", "h3"):
            self._capture = tag
            self._buf = []
        elif tag == "li":
            self._li_depth += 1
            self._buf = []
            self._stack.append("li")
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            val = (a.get("content") or "").strip()
            if key and val and key not in self.meta:
                self.meta[key] = val
        elif tag == "a":
            href = (a.get("href") or "").strip()
            if href.lower().startswith("mailto:"):
                self.mailtos.append(href[7:].split("?")[0])
            elif href.lower().startswith("tel:"):
                self.tels.append(href[4:])
            elif href.startswith("http"):
                self.links.append(href)
        if tag in ("p", "div", "br", "section", "article", "tr", "h4", "h5", "footer"):
            self.text_chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "iframe"):
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "title":
            self._capture = None
        elif tag in ("h1", "h2", "h3") and self._capture == tag:
            text = _clean_text("".join(self._buf))
            if text:
                self.headings.append((tag, text))
            self._capture = None
            self._buf = []
        elif tag == "li":
            self._li_depth = max(0, self._li_depth - 1)
            if "li" in self._stack:
                self._stack.remove("li")
            text = _clean_text("".join(self._buf))
            if text and len(text) <= 160:
                self.list_items.append(text)
            self._buf = []

    def handle_data(self, data):
        if self._skip:
            return
        if self._capture == "title":
            self.title += data
        elif self._capture in ("h1", "h2", "h3"):
            self._buf.append(data)
        elif self._li_depth:
            self._buf.append(data)
        self.text_chunks.append(data)


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip(" \t\n\r·•|-–—>»")


def _pretty_domain(domain: str) -> str:
    """'www.mrhandyman.com' -> 'Mrhandyman'. Last resort, when the page names nothing."""
    d = re.sub(r"^(https?://)?(www\d*\.)?", "", (domain or "").lower()).split("/")[0]
    label = re.sub(r"[-_]+", " ", d.split(".")[0]).strip()
    if not label:
        return ""
    return label.title() if label.islower() else label


def _sentence(text: str, limit: int) -> str:
    """Trim to a whole sentence: a hook that stops mid-clause reads like a broken mail merge."""
    t = _clean_text(text)
    if len(t) <= limit:
        return t
    cut = t[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= 40:
        return cut[: end + 1]
    return cut[: cut.rfind(" ")].rstrip(",;:") + "..."


def _split_title(title: str) -> tuple[str, str]:
    """'Acme Roofing | Columbus OH Roof Repair' -> ('Acme Roofing', '...')."""
    t = _clean_text(title)
    if not t:
        return "", ""
    parts = [p.strip() for p in re.split(r"\s*[|•·—–:–]\s*|\s+-\s+", t) if p.strip()]
    if not parts:
        return t, ""
    # the business name is the shortest segment that is not a slogan sentence
    name = min(parts, key=len) if len(parts) > 1 else parts[0]
    rest = " ".join(p for p in parts if p is not name)
    return name, rest


def _useful(items: list[str], limit: int = 12) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        t = _clean_text(raw)
        if not (3 <= len(t) <= 70):
            continue
        low = t.lower().strip(".")
        if low in _NAV_WORDS or low in seen:
            continue
        if t.endswith(("?", "!")) and len(t) > 40:
            continue
        if sum(c.isdigit() for c in t) > 4:
            continue
        seen.add(low)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def extract(html: str, url: str) -> dict:
    """HTML -> raw facts. Pure function, no network, no model: unit-testable."""
    p = _Extractor()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML is not a failure mode worth raising
        pass

    domain = urllib.parse.urlsplit(url).hostname or ""
    name, tagline = _split_title(p.title)
    name = _clean_text(p.meta.get("og:site_name") or name)
    if not name:
        name, _t = _split_title(p.meta.get("og:title") or p.meta.get("twitter:title") or "")

    h1 = [t for tag, t in p.headings if tag == "h1"]
    h2 = [t for tag, t in p.headings if tag == "h2"]
    h3 = [t for tag, t in p.headings if tag == "h3"]

    body = _clean_text(" ".join(p.text_chunks))
    body = re.sub(r"\s{2,}", " ", body)[:4000]

    emails = []
    for e in p.mailtos + _EMAIL_RE.findall(html[:200000]):
        e = e.strip().strip(".").lower()
        if "@" not in e or e in emails:
            continue
        if e.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            continue
        if _is_placeholder_email(e):
            continue
        emails.append(e)
    emails = emails[:6]

    phones = []
    for raw in list(p.tels) + [m.group(0) for m in _PHONE_RE.finditer(body)]:
        t = re.sub(r"[^\d+]", "", raw)
        if len(t) >= 10 and t not in phones:
            phones.append(t)
        if len(phones) >= 3:
            break

    places = []
    for m in _CITY_STATE_RE.finditer(body):
        cand = f"{m.group(1).strip()}, {m.group(2)}"
        if cand.lower() not in {c.lower() for c in places}:
            places.append(cand)
        if len(places) >= 4:
            break

    socials = sorted({
        re.sub(r"^https?://(www\.)?", "", l).split("/")[0]
        for l in p.links
        if re.search(r"(facebook|instagram|linkedin|youtube|twitter|x)\.com", l)
    })

    return {
        "url": url,
        "domain": domain,
        "name": name,
        "title": _clean_text(p.title),
        "meta_description": _clean_text(p.meta.get("description") or p.meta.get("og:description") or ""),
        "tagline": tagline,
        "h1": _useful(h1, 3),
        "headings": _useful(h2 + h3, 14),
        "services": _useful(h2 + h3 + p.list_items, 12),
        "list_items": _useful(p.list_items, 14),
        "emails": emails,
        "free_mail": [e for e in emails if e.split("@")[-1] in _FREE_MAIL],
        "phones": phones,
        "places": places,
        "socials": socials,
        "text": body,
    }


# ------------------------------------------------------------------- structuring

SUMMARIZE_PROMPT = """You are reading a small business's website to brief a salesperson.

SITE DATA
domain: {domain}
title: {title}
meta description: {meta}
headlines: {headings}
page items: {items}
page text (truncated): {text}

Return ONE json object, no prose, no markdown fence, with exactly these keys:
{{
  "business_name": "as the site presents itself",
  "what_they_do": "one plain sentence, max 25 words",
  "services": ["3-6 concrete services or products they sell"],
  "customers": "who they serve, max 12 words",
  "service_area": "city/region they name, or empty string",
  "industry": "short category, e.g. roofing, dental, hvac, marketing agency",
  "differentiators": ["up to 3 specific claims the site makes"],
  "talking_point": "one specific, factual detail a cold email could reference, max 20 words"
}}
Only use facts visible in the site data. If something is unknown, use ""."""


def _parse_json_object(text: str) -> dict | None:
    """Model output -> dict, tolerating fences and leading chatter."""
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def _heuristic_profile(facts: dict) -> dict:
    """What we can say without a model. Never empty-handed."""
    name = facts.get("name") or ""
    if not name:
        h1s = facts.get("h1") or []
        name = h1s[0] if h1s and len(h1s[0]) <= 60 else ""
    if not name:
        name = _pretty_domain(facts.get("domain", ""))
    what = facts.get("meta_description") or facts.get("tagline") or ""
    if not what:
        h = facts.get("h1") or facts.get("headings") or []
        what = h[0] if h else ""
    services = facts.get("services") or facts.get("headings") or []
    return {
        "business_name": name,
        "what_they_do": _sentence(what, 220),
        "services": services[:6],
        "customers": "",
        "service_area": (facts.get("places") or [""])[0],
        "industry": "",
        "differentiators": [],
        "talking_point": facts.get("tagline") or _sentence(what, 140),
    }


def summarize(facts: dict) -> dict:
    """Structure the facts with the LLM when available; else heuristics."""
    from . import ai

    heuristic = _heuristic_profile(facts)
    prompt = SUMMARIZE_PROMPT.format(
        domain=facts.get("domain", ""),
        title=facts.get("title", "")[:200],
        meta=(facts.get("meta_description") or "")[:300],
        headings="; ".join(facts.get("headings") or [])[:700],
        items="; ".join(facts.get("services") or [])[:700],
        text=(facts.get("text") or "")[:1800],
    )
    raw = ai._llm(prompt, max_tokens=500, temperature=0.1)
    parsed = _parse_json_object(raw) if raw else None
    if not parsed:
        heuristic["used_llm"] = False
        return heuristic

    out = dict(heuristic)
    for key in ("business_name", "what_they_do", "customers", "service_area", "industry", "talking_point"):
        val = parsed.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()[:300]
    for key in ("services", "differentiators"):
        val = parsed.get(key)
        if isinstance(val, list):
            clean = [str(v).strip()[:120] for v in val if str(v).strip()][:6]
            if clean:
                out[key] = clean
    out["used_llm"] = True
    return out


# ----------------------------------------------------------------- public entry

def research(raw_url: str, deep: bool = True) -> dict:
    """Domain -> profile. Never raises for a remote failure; inspect `error`."""
    url, _domain = normalize_url(raw_url)
    profile: dict = {
        "input": raw_url,
        "url": url,
        "pages": [],
        "fetched_at": None,
        "error": None,
        "used_llm": False,
    }
    html, final_url = fetch(url)
    base = extract(html, final_url)
    profile["pages"].append(final_url)
    profile["url"] = final_url

    facts = dict(base)
    if deep:
        merged_head: list[str] = list(base.get("headings") or [])
        merged_items: list[str] = list(base.get("services") or [])
        merged_text: list[str] = [base.get("text") or ""]
        merged_emails: list[str] = list(base.get("emails") or [])
        origin = urllib.parse.urlsplit(final_url)
        for path in CANDIDATE_PATHS[:MAX_EXTRA_PAGES]:
            try:
                extra_html, extra_url = fetch(f"{origin.scheme}://{origin.netloc}{path}")
            except ResearchError:
                continue
            extra = extract(extra_html, extra_url)
            if not extra.get("title") and not extra.get("text"):
                continue
            profile["pages"].append(extra_url)
            merged_head += extra.get("headings") or []
            merged_items += extra.get("services") or []
            merged_text.append(extra.get("text") or "")
            for e in extra.get("emails") or []:
                if e not in merged_emails:
                    merged_emails.append(e)
        facts["headings"] = _useful(merged_head, 18)
        facts["services"] = _useful(merged_items, 14)
        facts["text"] = re.sub(r"\s{2,}", " ", " ".join(merged_text))[:6000]
        facts["emails"] = merged_emails[:6]
        facts["free_mail"] = [e for e in facts["emails"] if e.split("@")[-1] in _FREE_MAIL]
        if not facts.get("places"):
            facts["places"] = base.get("places") or []

    structured = summarize(facts)
    structured.update({
        "input": raw_url,
        "url": profile["url"],
        "domain": facts.get("domain", ""),
        "pages": profile["pages"],
        "emails": facts.get("emails") or [],
        "free_mail": facts.get("free_mail") or [],
        "phones": facts.get("phones") or [],
        "socials": facts.get("socials") or [],
        "title": facts.get("title", ""),
        "meta_description": facts.get("meta_description", ""),
        "places": facts.get("places") or [],
        "used_llm": structured.get("used_llm", False),
        "error": None,
    })
    from .db import utcnow

    structured["fetched_at"] = utcnow()
    return structured


def brief(profile: dict | str | None) -> str:
    """Compact one-liner for prompts and {{ai_snippet}} observed detail."""
    if not profile:
        return ""
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except json.JSONDecodeError:
            return profile[:400]
    if not isinstance(profile, dict):
        return ""
    bits: list[str] = []
    if profile.get("what_they_do"):
        bits.append(str(profile["what_they_do"]))
    services = profile.get("services") or []
    if services:
        bits.append("services: " + ", ".join(str(s) for s in services[:4]))
    if profile.get("customers"):
        bits.append("serves " + str(profile["customers"]))
    if profile.get("service_area"):
        bits.append("in " + str(profile["service_area"]))
    if profile.get("talking_point") and str(profile["talking_point"]) not in " ".join(bits):
        bits.append("site says: " + str(profile["talking_point"]))
    return "; ".join(b for b in bits if b)[:600]
