"""Domain research -> leads -> emails, driven from the UI.

The domain the operator types in is the CLIENT: the business whose outreach we
are running. The leads are that client's prospects - companies that would buy
what the client sells. Three capabilities:

  1. POST /research/domain   read the client's site and say what they sell
  2. POST /research/leads    find the buyer trades in the DB and list them
  3. POST /research/email    draft the client's first touch, one per lead

Direct prospecting (no client domain) still works: then the domain is the
prospect and the mail speaks for this workspace.

Everything here is additive to the existing /api surface and sits behind the
same bearer token as the rest of the app. No new credential, no new trust
boundary, no build step.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import ai, audit, discover, leadguard, research, verify, voice
from .db import connect, new_id, norm_company, rows, utcnow
from .security import RateLimiter, client_ip

log = logging.getLogger("localreach.research")

router = APIRouter(prefix="/api", tags=["research"])

# Research hits third-party sites, so it gets its own budget: an operator
# clicking fast must not turn this box into a scraper, and each request can
# legitimately take several seconds.
_research_limiter = RateLimiter()
RESEARCH_LIMIT = 30
RESEARCH_WINDOW_S = 60


def _throttle(request: Request) -> None:
    _research_limiter.check(f"research:{client_ip(request)}", RESEARCH_LIMIT, RESEARCH_WINDOW_S)


def _load_profile(row: Any) -> dict | None:
    raw = row["site_research"] if row is not None and "site_research" in row.keys() else None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _persist(conn, business_id: str, profile: dict) -> dict:
    """Store research on the row. Website/domain are filled only if blank."""
    now = utcnow()
    conn.execute(
        """UPDATE businesses
              SET site_research = ?,
                  researched_at = ?,
                  website = COALESCE(NULLIF(website, ''), ?),
                  domain  = COALESCE(NULLIF(domain, ''), ?),
                  updated_at = ?
            WHERE id = ?""",
        (json.dumps(profile), now, profile.get("url"), profile.get("domain"), now, business_id),
    )
    conn.commit()
    return profile


# ------------------------------------------------------------------ endpoints

class DomainIn(BaseModel):
    url: str
    business_id: str | None = None
    deep: bool = True
    persist: bool = True


@router.post("/research/domain")
def research_domain(body: DomainIn, request: Request) -> dict:
    """Read one website and return what the business actually sells."""
    _throttle(request)
    try:
        profile = research.research(body.url, deep=body.deep)
    except research.ResearchError as exc:
        raise HTTPException(422, str(exc)) from exc

    business = None
    if body.business_id:
        conn = connect()
        try:
            row = conn.execute("SELECT * FROM businesses WHERE id = ?", (body.business_id,)).fetchone()
            if not row:
                raise HTTPException(404, "business not found")
            if body.persist:
                profile = _persist(conn, body.business_id, profile)
            business = dict(row)
            business.update({"site_research": profile})
        finally:
            conn.close()

    return {"profile": profile, "business": business, "brief": research.brief(profile)}


class BatchIn(BaseModel):
    workspace_id: str
    limit: int = 5
    refresh: bool = False


@router.post("/research/batch")
def research_batch(body: BatchIn, request: Request) -> dict:
    """Research every company in the workspace that has a site but no profile."""
    _throttle(request)
    workspace_id = body.workspace_id.strip()
    if not workspace_id:
        raise HTTPException(422, "workspace_id is required")
    limit = max(1, min(int(body.limit or 5), 10))
    refresh = body.refresh

    conn = connect()
    try:
        sql = """
          SELECT b.* FROM businesses b
          JOIN list_members m ON m.business_id = b.id
          JOIN lists l ON l.id = m.list_id
          WHERE l.workspace_id = ?
            AND COALESCE(b.website, '') NOT IN ('', '-')
        """
        args: list[Any] = [workspace_id]
        if not refresh:
            sql += " AND COALESCE(b.site_research, '') = ''"
        sql += " GROUP BY b.id ORDER BY b.created_at DESC LIMIT ?"
        args.append(limit)
        targets = rows(conn.execute(sql, args))
    finally:
        conn.close()

    results = []
    for biz in targets:
        try:
            profile = research.research(biz.get("website") or "", deep=True)
        except research.ResearchError as exc:
            results.append({"business_id": biz["id"], "name": biz.get("name"),
                            "ok": False, "error": str(exc)})
            continue
        conn = connect()
        try:
            _persist(conn, biz["id"], profile)
        finally:
            conn.close()
        results.append({
            "business_id": biz["id"], "name": biz.get("name"), "ok": True,
            "what_they_do": profile.get("what_they_do"),
            "services": profile.get("services"),
            "used_llm": profile.get("used_llm"),
        })
    return {"requested": len(targets), "ok": sum(1 for r in results if r["ok"]), "results": results}


@router.get("/research/business/{business_id}")
def research_for_business(business_id: str) -> dict:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM businesses WHERE id = ?", (business_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "business not found")
    profile = _load_profile(row)
    return {
        "business": dict(row) | {"site_research": profile},
        "profile": profile,
        "brief": research.brief(profile) if profile else "",
        "needs_research": profile is None and bool(row["website"]),
    }


# ------------------------------------------------------- lookalike lead pull

STOP = {"the", "and", "for", "with", "your", "our", "you", "all", "inc", "llc", "co",
        "services", "service", "company", "solutions", "group"}


def _tokens(text: str) -> set[str]:
    out = set()
    for w in (text or "").lower().replace("_", " ").replace("-", " ").split():
        w = "".join(c for c in w if c.isalnum())
        if len(w) > 2 and w not in STOP:
            out.add(w)
    return out


def _singular(tok: str) -> str:
    """Crude plural folding so "contractor" matches "contractors"."""
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def _norm_tokens(text: str) -> set[str]:
    return {_singular(t) for t in _tokens(text)}


def _buyer_categories(conn, profile: dict) -> tuple[list[str], float, list[dict], str]:
    """Find the categories in this database that *buy* what the client sells.

    The researched business is the seller, so matching its own category returns
    its competitors, which is precisely backwards for a lead list. Buyers are
    described by the client's ICP, not by the client: we match the local taxonomy
    against `icp` / `customers` / `buying_signals` and drop any category that
    overlaps the seller's own trade.

    Returns (categories, confidence, options, note). An empty list means we found
    no buyer category; the caller then searches the area unfiltered by trade and
    says so, rather than pretending a trade match exists.
    """
    cats = rows(conn.execute(
        """SELECT category, COUNT(*) AS n FROM businesses
            WHERE category IS NOT NULL AND category != ''
         GROUP BY category ORDER BY n DESC LIMIT 400"""
    ))
    if not cats:
        return [], 0.0, [], "No categories in this database yet. Import a lead list first."

    seller = _norm_tokens(" ".join([
        str(profile.get("industry") or ""),
        str(profile.get("what_they_do") or ""),
        " ".join(str(s) for s in profile.get("services") or []),
    ]))
    want = _norm_tokens(" ".join([
        str(profile.get("icp") or ""),
        str(profile.get("customers") or ""),
        str(profile.get("who_pays") or ""),
        " ".join(str(s) for s in profile.get("buying_signals") or []),
    ]))
    if not want:
        return [], 0.0, cats[:6], (
            "This profile does not say who buys from them, so no trade filter "
            "was applied. Add a customer description to sharpen it."
        )

    scored = []
    for c in cats:
        have = _norm_tokens(str(c["category"]))
        if not have or have <= seller:
            continue  # the client's own trade, or a strict slice of it: a rival
        union = have | seller
        if union and len(have & seller) / len(union) >= 0.6:
            continue  # near-synonym of the client's trade, still a rival
        overlap = len(want & have)
        if not overlap:
            continue
        scored.append({"category": c["category"], "n": c["n"],
                       "score": round(overlap / max(1, len(have)), 3)})
    scored.sort(key=lambda r: (-r["score"], -r["n"]))
    if not scored:
        return [], 0.0, cats[:6], (
            "No buyer category in this database matches their customers, so the "
            "search is unfiltered by trade. The agent-researched leads are the "
            "sharper list."
        )
    top = scored[:3]
    return [r["category"] for r in top], top[0]["score"], scored[:6], (
        f"Treated {profile.get('name') or 'the researched business'} as the seller; "
        f"these are the trades that buy from them."
    )


class LeadPullIn(BaseModel):
    workspace_id: str
    url: str | None = None
    category: str | None = None
    city: str | None = None
    state: str | None = None
    limit: int = 25
    verified_email_only: bool = False


@router.post("/research/leads")
def pull_leads(body: LeadPullIn, request: Request) -> dict:
    """Find the local businesses that match what the researched company does.

    The domain is the *seed*: its industry, services and service area become the
    search. If the operator overrides a filter, the override wins.
    """
    _throttle(request)
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM workspaces WHERE id = ?", (body.workspace_id,)).fetchone():
            raise HTTPException(404, "workspace not found")

        profile: dict = {}
        seed_business_id = None
        if body.url:
            try:
                profile = research.research(body.url, deep=True)
            except research.ResearchError as exc:
                raise HTTPException(422, str(exc)) from exc
            seed = conn.execute(
                "SELECT id FROM businesses WHERE domain = ? OR website LIKE ?",
                (profile.get("domain"), f"%{profile.get('domain')}%"),
            ).fetchone()
            seed_business_id = seed["id"] if seed else None

        category = body.category
        targets: list[str] = [category] if category else []
        note = ""
        cat_score = 0.0
        alts: list[dict] = []
        if not targets and profile:
            targets, cat_score, alts, note = _buyer_categories(conn, profile)
        city = body.city
        state = body.state
        if not city and not state and profile.get("service_area"):
            parts = [p.strip() for p in str(profile["service_area"]).split(",")]
            if len(parts) >= 2 and len(parts[-1]) == 2:
                state = parts[-1].upper()
                city = parts[0]

        where = ["1=1"]
        args: list[Any] = []
        if targets:
            where.append("b.category IN (" + ",".join("?" * len(targets)) + ")")
            args.extend(targets)
        if city:
            where.append("b.city = ?"); args.append(city)
        if state:
            where.append("b.state = ?"); args.append(state)
        if seed_business_id:
            where.append("b.id != ?"); args.append(seed_business_id)
        if body.verified_email_only:
            where.append("c.email_status = 'verified'")

        limit = max(1, min(int(body.limit or 25), 100))
        sql = f"""
          SELECT b.id, b.name, b.category, b.city, b.state, b.website, b.phone,
                 b.rating, b.review_count,
                 c.id AS contact_id, c.full_name AS contact_name, c.title,
                 c.email, c.email_status,
                 CASE WHEN COALESCE(b.site_research, '') != '' THEN 1 ELSE 0 END AS researched,
                 s.fit_score
            FROM businesses b
            LEFT JOIN contacts c ON c.business_id = b.id AND c.is_primary = 1
            LEFT JOIN scores s ON s.business_id = b.id
           WHERE {' AND '.join(where)}
           ORDER BY COALESCE(s.fit_score, 0) DESC, b.review_count DESC
           LIMIT ?"""
        args.append(limit)
        results = rows(conn.execute(sql, args))

        # a lead already enrolled shouldn't be offered twice
        enrolled = {
            r["business_id"] for r in conn.execute(
                """SELECT e.business_id FROM enrollments e
                     JOIN sequences s ON s.id = e.sequence_id
                    WHERE s.workspace_id = ? AND e.status IN ('active', 'paused')""",
                (body.workspace_id,),
            )
        }
        for r in results:
            r["already_enrolled"] = r["id"] in enrolled
            r["researched"] = bool(r["researched"])
    finally:
        conn.close()

    filters = {"category": category, "categories": targets, "city": city, "state": state}
    return {
        "filters": filters,
        "matched_category": targets[0] if targets else None,
        "target_categories": targets,
        "category_confidence": cat_score,
        "category_options": alts,
        "direction_note": note,
        "profile": profile or None,
        "brief": research.brief(profile) if profile else "",
        "count": len(results),
        "results": results,
        "hint": (
            None if results else
            (note or "") + (
                f" No prospects in {city or 'this area'} match yet. Import a lead "
                "list, or relax the filters."
            )
        ),
    }


# ------------------------------------------------------------ email drafting

class EmailIn(BaseModel):
    workspace_id: str
    business_id: str
    profile_id: str | None = None
    # Who the email is written *as*. Normally left blank: the lead's research
    # row already records which client it was found for.
    client_business_id: str | None = None
    length: int = 100
    signature: str = ""
    refresh_research: bool = False


@router.post("/research/email")
def draft_for_business(body: EmailIn) -> dict:
    """One grounded email for one lead.

    Direction matters more than grammar here. When the lead was researched for
    a client, the email is written *as that client*, addressed to the lead —
    it is the client's outreach, run under the client's name. When there is no
    client, the workspace is selling its own services, which is the original
    behaviour and still the right one for direct prospecting.
    """
    conn = connect()
    try:
        biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (body.business_id,)).fetchone()
        if not biz:
            raise HTTPException(404, "business not found")
        business = dict(biz)
        contact = None
        c = conn.execute(
            "SELECT * FROM contacts WHERE business_id = ? AND is_primary = 1", (body.business_id,)
        ).fetchone()
        if c:
            contact = dict(c)

        # The client is the researched business this lead was found for.
        client = None
        if body.client_business_id:
            crow = conn.execute(
                "SELECT * FROM businesses WHERE id = ?", (body.client_business_id,)
            ).fetchone()
            client = dict(crow) if crow else None
        if client is None:
            crow = conn.execute(
                "SELECT b.* FROM research_leads rl JOIN businesses b ON b.id = rl.client_business_id"
                " WHERE rl.business_id = ? AND rl.client_business_id IS NOT NULL"
                " ORDER BY rl.created_at DESC LIMIT 1",
                (body.business_id,),
            ).fetchone()
            client = dict(crow) if crow else None

        seller = None
        if client is None:
            if body.profile_id:
                p = conn.execute("SELECT * FROM ai_profiles WHERE id = ?", (body.profile_id,)).fetchone()
                seller = dict(p) if p else None
            if not seller:
                p = conn.execute(
                    "SELECT * FROM ai_profiles WHERE workspace_id = ? ORDER BY readiness DESC LIMIT 1",
                    (body.workspace_id,),
                ).fetchone()
                seller = dict(p) if p else None
            if not seller:
                raise HTTPException(
                    422,
                    "No AI profile for this workspace. Create one under Voice first: "
                    "the email needs an offer and tone to be worth sending.",
                )

        profile = _load_profile(biz)
    finally:
        conn.close()

    fetch_error = None
    if body.refresh_research or (not profile and business.get("website")):
        try:
            fetched = research.research(business.get("website") or "", deep=True)
        except research.ResearchError as exc:
            fetched = None
            fetch_error = str(exc)
        else:
            conn = connect()
            try:
                profile = _persist(conn, body.business_id, fetched)
            finally:
                conn.close()

    brief = research.brief(profile) if profile else ""
    signature = body.signature
    if client is not None:
        # Speak as the client. Their own site research is the only description
        # of them we hold, and it is enough for the model to write in their
        # voice instead of pitching our services to their prospects.
        seller = ai.voice_from_research(_load_profile(client), seller or {})
        seller["name"] = client.get("name") or ""
        signature = signature or seller["name"]
    draft = ai.draft_email(seller, business, contact, brief, body.length, signature)
    draft.update({
        "business_id": body.business_id,
        "business_name": business.get("name"),
        "contact_email": (contact or {}).get("email"),
        "contact_name": (contact or {}).get("full_name"),
        # Who this email is from and who it is to. The UI needs both to label
        # the draft honestly; without them a client's outreach looks like ours.
        "direction": "client_to_lead" if client else "us_to_prospect",
        "as_client": (client or {}).get("name") or "",
        "as_client_id": (client or {}).get("id") or "",
        "signature": signature,
        "evidence": (profile or {}).get("talking_point") or "",
        "pages": (profile or {}).get("pages") or [],
        "research_error": fetch_error,
        "email_ready": bool(
            (contact or {}).get("email") and (contact or {}).get("email_status") == "verified"
        ),
    })
    return draft


class ContactIn(BaseModel):
    business_id: str
    email: str
    full_name: str | None = None
    title: str | None = None
    make_primary: bool = False


@router.post("/research/contact")
def add_site_contact(body: ContactIn) -> dict:
    """Save an address found on the prospect's site.

    Deliberately stored as `unverified`: it came off a public page, which is a
    fine lead but not proof the mailbox exists. The send gate keeps it out of
    any sequence until someone verifies it, so this can't quietly burn a domain.
    """
    email = (body.email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(422, "That does not look like an email address")
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM businesses WHERE id = ?", (body.business_id,)).fetchone():
            raise HTTPException(404, "business not found")
        existing = conn.execute("SELECT id FROM contacts WHERE email = ?", (email,)).fetchone()
        if existing:
            return {"created": False, "contact_id": existing["id"], "email": email,
                    "email_status": "unverified"}
        has_primary = conn.execute(
            "SELECT 1 FROM contacts WHERE business_id = ? AND is_primary = 1", (body.business_id,)
        ).fetchone()
        cid = new_id()
        conn.execute(
            """INSERT INTO contacts (id, business_id, full_name, title, email, email_status,
                                     email_source, verified_at, is_primary, created_at)
               VALUES (?, ?, ?, ?, ?, 'unverified', 'website', NULL, ?, ?)""",
            (cid, body.business_id, body.full_name, body.title, email,
             1 if (body.make_primary or not has_primary) else 0, utcnow()),
        )
        conn.commit()
        return {"created": True, "contact_id": cid, "email": email, "email_status": "unverified"}
    finally:
        conn.close()


@router.get("/research/status")
def research_status() -> dict:
    """What the UI shows in the corner: is research/LLM live right now?

    `prospects`/`researched` are counted over real lead relationships. Counting
    `businesses` made the pill read "0 of 2 researched" on a database whose only two
    rows were the client companies themselves — research targets are prospects, not
    registry entries.
    """
    conn = connect()
    try:
        prospects = conn.execute(
            "SELECT COUNT(DISTINCT business_id) AS n FROM client_leads"
        ).fetchone()["n"]
        done = conn.execute(
            """SELECT COUNT(DISTINCT b.id) AS n FROM businesses b
               JOIN client_leads cl ON cl.business_id = b.id
               WHERE COALESCE(b.site_research, '') != ''"""
        ).fetchone()["n"]
        clients = conn.execute(
            "SELECT COUNT(DISTINCT business_id) AS n FROM workspace_clients WHERE status = 'active'"
        ).fetchone()["n"]
        total = conn.execute("SELECT COUNT(*) AS n FROM businesses").fetchone()["n"]
    finally:
        conn.close()
    return {
        "llm": ai.llm_health(),
        "researched": done,
        "prospects": prospects,
        "clients": clients,
        "businesses": total,  # registry size; not a research target count
    }


SIGNAL_KINDS = {
    "hiring", "permit", "expansion", "review", "rfp", "license", "news", "social", "other",
}


def _lead_domain(raw: str) -> str:
    """Accept a bare domain or a full URL; store the bare host."""
    d = (raw or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0].split("?")[0].strip()
    return d[4:] if d.startswith("www.") else d


def _business_by_name(conn, name: str) -> str | None:
    """Find the business row for a company name, ignoring legal suffixes.

    Only used when the domain did not match: `Acme Roofing LLC` and
    `Acme Roofing, Inc.` are one company, and treating them as two is how the
    same prospect ends up in the list twice. Compares in Python so the match is
    exact on the normalised name rather than a fuzzy LIKE.
    """
    norm = norm_company(name)
    if not norm:
        return None
    first = norm.split()[0]
    for r in conn.execute("SELECT id, name FROM businesses WHERE name LIKE ?", (first + "%",)):
        if norm_company(r["name"]) == norm:
            return r["id"]
    return None


def _store_research_leads(
    conn,
    workspace_id: str,
    queue_id: str | None,
    client_bid: str | None,
    profile: dict,
    template: dict | None = None,
    guard=None,
) -> dict:
    """Save the companies the agent found *for* the researched business.

    The researched site is the client here, not the target: every row is somebody
    who buys what the client sells. Leads become real businesses so they can be
    sequenced like any other lead, but no contact is created - the agent is told
    never to invent an address, so a hint stays a hint until a human checks it.

    ``guard`` is ``app.leadguard.check_lead`` in production: it drops a domain
    that its own site does not confirm as this business, and records why. It is
    injected rather than imported so the storage tests stay offline; the agent
    once attached a plausible-looking domain to a lead whose source published
    none, and that domain belonged to an unrelated site.
    """
    found = [x for x in (profile.get("leads") or []) if isinstance(x, dict)]
    client = None
    if client_bid:
        client = conn.execute("SELECT name, domain, city FROM businesses WHERE id = ?", (client_bid,)).fetchone()
    client_name = (client["name"] if client else "") or "researched business"
    client_city = (client["city"] if client else "") or ""

    lid = None
    if found:
        list_name = f"Leads for {client_name}"[:120]
        row = conn.execute(
            "SELECT id FROM lists WHERE workspace_id = ? AND name = ?", (workspace_id, list_name)
        ).fetchone()
        if row:
            lid = row["id"]
        else:
            lid = new_id()
            conn.execute(
                "INSERT INTO lists (id, workspace_id, name, created_at) VALUES (?,?,?,?)",
                (lid, workspace_id, list_name, utcnow()),
            )

    created = updated = skipped = 0
    for lead in found:
        name = str(lead.get("name") or "").strip()
        domain = _lead_domain(lead.get("domain") or lead.get("website") or "")
        if not name:
            skipped += 1
            continue
        if domain and domain == _lead_domain((client["domain"] if client else "") or ""):
            skipped += 1  # the client is not its own lead
            continue
        if not str(lead.get("signal_source") or "").strip():
            skipped += 1  # unsourced signals are guesses, and guesses waste sends
            continue

        # A domain the agent inferred from the business name is worse than no
        # domain: it gets crawled, ranked, and eventually mailed. Prove it or drop
        # it, and keep the reason on the row so the decision is reviewable.
        had_domain = bool(domain)
        evidence_note = ""
        if domain and guard:
            verdict = guard({"name": name, "domain": domain,
                             "signal_source": lead.get("signal_source") or ""},
                            locality=client_city)
            domain = verdict.get("domain") or ""
            evidence_note = str(verdict.get("note") or "")[:400]

        # Independent of the domain: a hint is prose, and an address inside prose
        # has never been checked against a page. Strip it; the crawler is the only
        # thing allowed to hand us an address.
        hint_text, dropped = leadguard.sanitize_hint(str(lead.get("contact_hint") or ""))
        if dropped:
            extra = f"{len(dropped)} address(es) removed from hint"
            evidence_note = f"{evidence_note}; {extra}".strip("; ")

        kind = str(lead.get("signal_kind") or "other").strip().lower()
        if kind not in SIGNAL_KINDS:
            kind = "other"
        try:
            fit = max(0, min(100, int(lead.get("fit") or 0)))
        except (TypeError, ValueError):
            fit = 0

        bid = None
        if domain:
            biz = conn.execute("SELECT id FROM businesses WHERE domain = ?", (domain,)).fetchone()
            if biz:
                bid = biz["id"]
        if not bid:
            # Domain is the strongest identity, but not the only one: the agent
            # re-proposes known companies without their URL, and a lead with no
            # site of its own still needs a business row or it can never be
            # attributed, shown, or worked (9 such rows on 2026-11-15).
            bid = _business_by_name(conn, name)
        if not bid:
            bid = new_id()
            conn.execute(
                """INSERT INTO businesses (id, name, website, domain, source, source_ref,
                                           fetched_at, created_at, updated_at)
                   VALUES (?,?,?,?, 'agent_lead', ?, ?, ?, ?)""",
                (bid, name, f"https://{domain}" if domain else None, domain or None,
                 client_name[:120], utcnow(), utcnow(), utcnow()),
            )

        # Research found this prospect for *this* workspace, so it belongs to this
        # workspace's pool. Without the row it appears in every workspace's
        # Unassigned bucket instead of only the one that paid for the research.
        conn.execute(
            """INSERT OR IGNORE INTO workspace_businesses
                   (workspace_id, business_id, origin, created_at)
               VALUES (?,?,'research',?)""",
            (workspace_id, bid, utcnow()),
        )

        existing = None
        if domain:
            existing = conn.execute(
                "SELECT id FROM research_leads WHERE client_business_id = ? AND domain = ?",
                (client_bid, domain),
            ).fetchone()
        if existing is None:
            # A lead with no site of its own still has to dedupe, or every run
            # re-adds it and the list fills with the same company. Match on name
            # regardless of the stored domain: the agent sometimes re-proposes a
            # known company without its URL, and a domain-only check let a second
            # copy through (dup "Discovery World on Market", 2026-11-15).
            existing = conn.execute(
                "SELECT id FROM research_leads WHERE client_business_id = ? "
                "AND lower(name) = lower(?)",
                (client_bid, name),
            ).fetchone()
        # The agent is asked to write the first touch for this specific company.
        # Store it even if it is thin: a human reading a bad draft before sending
        # costs nothing, and sending a generic one costs the domain's reputation.
        email_obj = lead.get("email")
        if not isinstance(email_obj, dict):
            email_obj = {}
        subject = str(email_obj.get("subject") or "").strip()[:180]
        body = str(email_obj.get("body") or "").strip()[:4000]
        # No per-lead note? Use the client's own first-touch template rather than
        # letting the sequence's workspace-voice copy go out under the client's
        # signature. {{business_name}}/{{city}} survive into the override and are
        # filled from *this* lead at send time.
        if not subject and not body and isinstance(template, dict):
            subject = str(template.get("subject") or "").strip()[:180]
            body = str(template.get("body") or "").strip()[:4000]
        # Voice pass on the stored copy, whichever path wrote it (agent draft or
        # template). Templates and models both drift into the same shapes, so the
        # cleanup belongs at the write point, not in the prompt alone.
        if subject or body:
            subject = voice.fix_subject(subject)[:180]
            body = voice.fix_body(body)[:4000]
            voice_note = voice.summarise(voice.lint(subject, body))
        else:
            voice_note = ""

        payload = (
            name, str(lead.get("why_now") or "")[:600],
            kind, str(lead.get("signal_detail") or "")[:1200],
            str(lead.get("signal_source") or "")[:500],
            hint_text[:300], evidence_note, fit, bid,
            subject or None, body or None,
        )
        cleared = 1 if (had_domain and not domain) else 0
        if existing:
            lead_id = existing["id"]
            conn.execute(
                """UPDATE research_leads
                      SET name = ?,
                          domain = CASE WHEN ? = 1 THEN NULL
                                        ELSE COALESCE(NULLIF(?, ''), domain) END,
                          why_now = ?, signal_kind = ?, signal_detail = ?,
                          signal_source = ?, contact_hint = ?,
                          evidence_note = COALESCE(NULLIF(?, ''), evidence_note),
                          fit = ?, business_id = COALESCE(?, business_id),
                          email_subject = COALESCE(?, email_subject),
                          email_body = COALESCE(?, email_body)
                    WHERE id = ?""",
                (payload[0], cleared, domain) + payload[1:] + (lead_id,),
            )
            updated += 1
        else:
            lead_id = new_id()
            conn.execute(
                """INSERT INTO research_leads (name, domain, why_now, signal_kind, signal_detail,
                                               signal_source, contact_hint, evidence_note,
                                               fit, business_id,
                                               email_subject, email_body,
                                               id, queue_id, workspace_id, client_business_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (name, domain or None, str(lead.get("why_now") or "")[:600],
                 kind, str(lead.get("signal_detail") or "")[:1200],
                 str(lead.get("signal_source") or "")[:500],
                 hint_text[:300], evidence_note or None, fit, bid,
                 subject or None, body or None,
                 lead_id, queue_id, workspace_id, client_bid, utcnow()),
            )
            created += 1

        # Persist the cleaned copy and its voice report in one place, so both the
        # insert and update paths above stay readable and the note can never drift
        # from the body it describes.
        if voice_note:
            conn.execute(
                "UPDATE research_leads SET email_subject = ?, email_body = ?, "
                "voice_note = ? WHERE id = ?",
                (subject or None, body or None, voice_note[:600], lead_id),
            )

        # Ownership is a row, not a list name: "which client is this lead for"
        # has to survive renames, exports and a second client researching the
        # same company. Insert-or-ignore keeps fit/status/notes an operator
        # already set - research refreshes evidence, it does not reset the desk.
        if bid:
            conn.execute(
                """INSERT OR IGNORE INTO client_leads
                       (id, workspace_id, client_business_id, business_id, fit, status,
                        origin, research_lead_id, created_at, updated_at)
                   VALUES (?,?,?,?,?,'new','research',?,?,?)""",
                (new_id(), workspace_id, client_bid, bid, fit, lead_id, utcnow(), utcnow()),
            )
            conn.execute(
                "UPDATE client_leads SET fit = COALESCE(?, fit), research_lead_id = ?,"
                " updated_at = ? WHERE client_business_id = ? AND business_id = ?"
                "   AND status = 'new'",
                (fit, lead_id, utcnow(), client_bid, bid),
            )

        if lid and bid:
            conn.execute(
                "INSERT OR IGNORE INTO list_members (list_id, business_id, added_at) VALUES (?,?,?)",
                (lid, bid, utcnow()),
            )

    return {"found": len(found), "created": created, "updated": updated,
            "skipped": skipped, "list_id": lid}


class ImportIn(BaseModel):
    workspace_id: str
    profile: dict[str, Any]
    category: str | None = None
    city: str | None = None
    state: str | None = None


def _upsert_lead(
    conn,
    workspace_id: str,
    profile: dict,
    category: str | None = None,
    city: str | None = None,
    state: str | None = None,
) -> dict:
    """Create or refresh the lead row for a researched site.

    Shared by the synchronous import endpoint and the Hermes queue worker so both
    paths dedupe on domain and both land the contact unverified. Nothing here
    decides whether an email may be sent - that stays with the send gate.
    """
    p = profile or {}
    domain = (p.get("domain") or "").strip().lower()
    name = (p.get("business_name") or p.get("name") or domain or "").strip()
    if not name:
        raise HTTPException(422, "Nothing to import - the profile has no company name")

    area = [x.strip() for x in str(p.get("service_area") or "").split(",") if x.strip()]
    city = city or (area[0] if len(area) > 1 else None)
    state = state or (area[-1].upper() if len(area) > 1 and len(area[-1]) == 2 else None)
    emails = [e for e in (p.get("emails") or []) if isinstance(e, str) and "@" in e]
    phones = [x for x in (p.get("phones") or []) if isinstance(x, str)]

    if not conn.execute("SELECT 1 FROM workspaces WHERE id = ?", (workspace_id,)).fetchone():
        raise HTTPException(404, "workspace not found")

    existing = None
    if domain:
        existing = conn.execute("SELECT id FROM businesses WHERE domain = ?", (domain,)).fetchone()

    if existing:
        bid = existing["id"]
        conn.execute(
            """UPDATE businesses
                  SET name = CASE WHEN COALESCE(name, '') = '' THEN ? ELSE name END,
                      category = COALESCE(NULLIF(category, ''), ?),
                      city = COALESCE(NULLIF(city, ''), ?),
                      state = COALESCE(NULLIF(state, ''), ?),
                      website = COALESCE(NULLIF(website, ''), ?),
                      phone = COALESCE(NULLIF(phone, ''), ?),
                      site_research = ?, researched_at = ?, updated_at = ?
                WHERE id = ?""",
            (name, category or p.get("industry"), city, state, p.get("url"),
             (phones or [None])[0], json.dumps(p), utcnow(), utcnow(), bid),
        )
        created = False
    else:
        bid = new_id()
        conn.execute(
            """INSERT INTO businesses (id, name, category, city, state, phone, website, domain,
                                       source, source_ref, fetched_at, site_research, researched_at,
                                       created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (bid, name, category or p.get("industry"), city, state, (phones or [None])[0],
             p.get("url"), domain, "research", domain or name, utcnow(),
             json.dumps(p), utcnow(), utcnow(), utcnow()),
        )
        created = True

    lst = conn.execute(
        "SELECT id FROM lists WHERE workspace_id = ? AND name = 'Researched'",
        (workspace_id,),
    ).fetchone()
    if lst:
        lid = lst["id"]
    else:
        lid = new_id()
        conn.execute(
            "INSERT INTO lists (id, workspace_id, name, created_at) VALUES (?,?,?,?)",
            (lid, workspace_id, "Researched", utcnow()),
        )
    conn.execute(
        "INSERT OR IGNORE INTO list_members (list_id, business_id, added_at) VALUES (?,?,?)",
        (lid, bid, utcnow()),
    )

    # An address printed on their own site is a lead, not proof the mailbox
    # exists - it lands unverified and the send gate keeps it out of a
    # sequence until somebody checks it.
    saved_email = None
    for candidate in emails[:1]:
        email = candidate.strip().lower()
        if conn.execute("SELECT 1 FROM contacts WHERE email = ?", (email,)).fetchone():
            saved_email = email
            continue
        has_primary = conn.execute(
            "SELECT 1 FROM contacts WHERE business_id = ? AND is_primary = 1", (bid,)
        ).fetchone()
        conn.execute(
            """INSERT INTO contacts (id, business_id, full_name, title, email, email_status,
                                      email_source, verified_at, is_primary, created_at)
               VALUES (?,?,?,?,?, 'unverified', 'website', NULL, ?, ?)""",
            (new_id(), bid, None, None, email, 0 if has_primary else 1, utcnow()),
        )
        saved_email = email

    return {"created": created, "business_id": bid, "list_id": lid, "contact_email": saved_email}


@router.post("/research/import")
def import_researched(body: ImportIn) -> dict:
    """Turn a researched site into a real lead in this workspace.

    Deduplicates on domain, so researching the same company twice updates the
    row instead of creating a twin. The seed joins the workspace "Researched"
    list so it appears under Leads and can be sequenced like any other lead.
    """
    conn = connect()
    try:
        result = _upsert_lead(conn, body.workspace_id, body.profile, body.category, body.city, body.state)
        conn.commit()
        row = dict(conn.execute("SELECT * FROM businesses WHERE id = ?", (result["business_id"],)).fetchone())
    finally:
        conn.close()

    return {
        "created": result["created"],
        "business": row,
        "list_id": result["list_id"],
        "contact_email": result["contact_email"],
        "note": "Saved unverified - verify the address before it can send.",
    }


# ------------------------------------------------------- Hermes research queue
#
# The dashboard never calls an agent directly. It enqueues a domain; the Hermes
# worker (scripts/hermes_worker.py) pulls the row, researches the site with its
# own tools, and posts the lead plus a grounded draft back here. Pull, not push,
# so no agent endpoint has to be exposed to the internet and a box that reboots
# simply resumes on the next poll.

_QUEUE_OPEN = ("pending", "claimed")

# A run is a separate process on another machine, so "claimed" is not proof of
# life. Past this many seconds a claim is treated as abandoned and given back,
# once: a worker that dies mid-run must not block the domain forever, and a
# second death means the agent is the problem, not the claim.
STALE_CLAIM_S = int(os.getenv("RESEARCH_STALE_S", "900"))
MAX_ATTEMPTS = int(os.getenv("RESEARCH_MAX_ATTEMPTS", "2"))
# The cron runs every minute. Three missed beats is a dead worker, not jitter.
WORKER_NAME = "hermes"
WORKER_ALIVE_S = int(os.getenv("WORKER_ALIVE_S", "180"))


class QueueIn(BaseModel):
    workspace_id: str
    url: str
    requested_by: str | None = None


class ClaimIn(BaseModel):
    workspace_id: str | None = None
    limit: int = 1


class CompleteIn(BaseModel):
    status: str = "done"  # done | error
    profile: dict[str, Any] | None = None
    draft: dict[str, Any] | None = None
    error: str | None = None


class ProgressIn(BaseModel):
    stage: str
    note: str | None = None


class HeartbeatIn(BaseModel):
    name: str = WORKER_NAME
    detail: str | None = None


def _seconds_since(ts: str | None) -> int | None:
    """Age of a stored ISO timestamp. Unparseable or missing means unknown."""
    if not ts:
        return None
    try:
        then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - then).total_seconds()))


def _queue_item(row: Any) -> dict:
    data = dict(row)
    for key in ("profile", "draft"):
        raw = data.get(key)
        if raw:
            try:
                data[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                data[key] = None
    # The UI needs ages, not timestamps: it cannot parse "2026-11-17T19:38:02+00:00"
    # reliably in every browser, and a clock skew would read as a stall.
    data["age_seconds"] = _seconds_since(data.get("requested_at"))
    data["elapsed_seconds"] = _seconds_since(data.get("claimed_at"))
    data["progress_age_seconds"] = _seconds_since(data.get("progress_at"))
    data["finished_age_seconds"] = _seconds_since(data.get("finished_at"))
    # 'queued N ago' must measure the time it has been sitting unclaimed. After a
    # requeue the row keeps its original requested_at, so counting from there
    # told the operator it had been waiting for an hour when it was retried a
    # second ago.
    data["queued_age_seconds"] = (
        _seconds_since(data.get("progress_at"))
        if data.get("status") == "pending" and data.get("progress_at")
        else data["age_seconds"]
    )
    # A claim is not proof of life: the worker refreshes progress_at while it
    # works, so the last sign of life - not the claim time - decides a stall.
    last_seen = data.get("progress_at") or data.get("claimed_at")
    silent_for = _seconds_since(last_seen)
    data["silent_seconds"] = silent_for
    data["stalled"] = bool(
        data.get("status") == "claimed"
        and (silent_for is None or silent_for > STALE_CLAIM_S)
    )
    data["attempts"] = int(data.get("attempts") or 0)
    data["can_retry"] = data["stalled"] or data.get("status") == "error"
    return data


def _reap_stale(conn: Any, workspace_id: str | None = None) -> int:
    """Return abandoned claims to the queue. Refuses to retry forever."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=STALE_CLAIM_S)).replace(
        microsecond=0
    ).isoformat()
    where = (
        "status = 'claimed' AND claimed_at IS NOT NULL"
        " AND COALESCE(progress_at, claimed_at) < ?"
    )
    args: list[Any] = [cutoff]
    if workspace_id:
        where += " AND workspace_id = ?"
        args.append(workspace_id)
    stale = conn.execute(f"SELECT * FROM research_queue WHERE {where}", args).fetchall()
    for row in stale:
        if (row["attempts"] or 0) >= MAX_ATTEMPTS:
            conn.execute(
                "UPDATE research_queue SET status = 'error', finished_at = ?,"
                " error = ?, progress_stage = 'error' WHERE id = ?",
                (
                    utcnow(),
                    f"the worker claimed this {STALE_CLAIM_S // 60} minutes ago and never"
                    " reported back, twice - the agent run is failing, not the queue",
                    row["id"],
                ),
            )
        else:
            conn.execute(
                "UPDATE research_queue SET status = 'pending', claimed_at = NULL,"
                " progress_stage = 'requeued',"
                " progress_note = 'previous worker stopped reporting; retrying',"
                " progress_at = ? WHERE id = ?",
                (utcnow(), row["id"]),
            )
    return len(stale)


@router.post("/research/heartbeat")
def worker_heartbeat(body: HeartbeatIn) -> dict:
    """Worker side: 'I am alive and this is what I am doing'."""
    now = utcnow()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO worker_heartbeat (name, seen_at, detail) VALUES (?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET seen_at = excluded.seen_at,"
            " detail = excluded.detail",
            (body.name or WORKER_NAME, now, body.detail),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "seen_at": now}


@router.post("/research/queue/{qid}/progress")
def queue_progress(qid: str, body: ProgressIn) -> dict:
    """Worker side: a stage a human can watch, so silence stops meaning 'dead'."""
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM research_queue WHERE id = ?", (qid,)).fetchone()
        if not row:
            raise HTTPException(404, "queue item not found")
        if row["status"] != "claimed":
            # Only a claiming worker owns a row. Progress aimed at a pending row
            # is out of order (a late ticker, a retry that reset the claim) and
            # would paint an unclaimed domain as 'the agent is reading it'.
            # Progress after a finish is likewise not an error worth failing a run.
            return {"ok": True, "ignored": row["status"]}
        conn.execute(
            "UPDATE research_queue SET progress_stage = ?, progress_note = ?,"
            " progress_at = ? WHERE id = ?",
            (body.stage[:40], (body.note or "")[:300], utcnow(), qid),
        )
        conn.commit()
        fresh = conn.execute("SELECT * FROM research_queue WHERE id = ?", (qid,)).fetchone()
    finally:
        conn.close()
    return {"ok": True, "item": _queue_item(fresh)}


@router.post("/research/queue/{qid}/requeue")
def requeue_item(qid: str, force: bool = False) -> dict:
    """Give a dead or failed run back to the queue.

    A healthy claim is refused: restarting a run the worker is in the middle of
    would race two agents over one row and duplicate the writes.
    """
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM research_queue WHERE id = ?", (qid,)).fetchone()
        if not row:
            raise HTTPException(404, "queue item not found")
        item = _queue_item(row)
        if row["status"] in _QUEUE_OPEN and not item["stalled"] and not force:
            raise HTTPException(409, "that run is still in progress")
        conn.execute(
            "UPDATE research_queue SET status = 'pending', claimed_at = NULL,"
            " finished_at = NULL, error = NULL, attempts = 0,"
            " progress_stage = 'requeued',"
            " progress_note = 'retry requested', progress_at = ? WHERE id = ?",
            (utcnow(), qid),
        )
        conn.commit()
        fresh = conn.execute("SELECT * FROM research_queue WHERE id = ?", (qid,)).fetchone()
    finally:
        conn.close()
    return {"ok": True, "item": _queue_item(fresh)}


@router.post("/research/queue")
def enqueue_research(body: QueueIn, request: Request) -> dict:
    """Hand a domain to the Hermes agent. Idempotent while an item is open."""
    _throttle(request)
    try:
        _url, domain = research.normalize_url(body.url)
    except research.ResearchError as exc:
        raise HTTPException(422, str(exc)) from exc

    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM workspaces WHERE id = ?", (body.workspace_id,)).fetchone():
            raise HTTPException(404, "workspace not found")
        open_row = conn.execute(
            "SELECT * FROM research_queue WHERE workspace_id = ? AND domain = ?"
            " AND status IN ('pending','claimed') ORDER BY requested_at DESC LIMIT 1",
            (body.workspace_id, domain),
        ).fetchone()
        if open_row:
            item = _queue_item(open_row)
            if item["stalled"]:
                # A dead worker's row must not answer "already queued" forever:
                # the operator asked again, so take the row back and clear the
                # attempt count - a human retry earns a full try.
                conn.execute(
                    "UPDATE research_queue SET status = 'pending', claimed_at = NULL,"
                    " attempts = 0, progress_stage = 'queued',"
                    " progress_note = 'previous worker stopped reporting; re-queued by hand',"
                    " progress_at = ? WHERE id = ?",
                    (utcnow(), open_row["id"]),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM research_queue WHERE id = ?", (open_row["id"],)
                ).fetchone()
                return {
                    "duplicate": False,
                    "retried": True,
                    "item": _queue_item(row),
                    "note": "The previous attempt died - re-queued for the agent.",
                }
            return {
                "duplicate": True,
                "item": item,
                "note": "Already queued - the agent will pick it up.",
            }
        qid = new_id()
        conn.execute(
            "INSERT INTO research_queue (id, workspace_id, domain, status, requested_by, requested_at)"
            " VALUES (?,?,?, 'pending', ?, ?)",
            (qid, body.workspace_id, domain, body.requested_by, utcnow()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM research_queue WHERE id = ?", (qid,)).fetchone()
    finally:
        conn.close()
    return {"duplicate": False, "item": _queue_item(row), "note": "Queued for the Hermes agent."}


@router.get("/research/queue")
def list_queue(workspace_id: str, limit: int = 25) -> dict:
    conn = connect()
    try:
        fetched = conn.execute(
            "SELECT * FROM research_queue WHERE workspace_id = ? ORDER BY requested_at DESC LIMIT ?",
            (workspace_id, max(1, min(limit, 100))),
        ).fetchall()
        beat = conn.execute(
            "SELECT * FROM worker_heartbeat WHERE name = ?", (WORKER_NAME,)
        ).fetchone()
    finally:
        conn.close()
    items = [_queue_item(r) for r in fetched]
    beat_age = _seconds_since(beat["seen_at"]) if beat else None
    return {
        "items": items,
        "pending": sum(1 for i in items if i["status"] in _QUEUE_OPEN),
        "done": sum(1 for i in items if i["status"] == "done"),
        "stalled": sum(1 for i in items if i["stalled"]),
        # A queue with no worker is not a queue. Report it so the page can say
        # "the cron is down" instead of leaving the operator waiting on nothing.
        "worker": {
            "name": WORKER_NAME,
            "seen_at": beat["seen_at"] if beat else None,
            "age_seconds": beat_age,
            "detail": beat["detail"] if beat else None,
            "alive": beat_age is not None and beat_age <= WORKER_ALIVE_S,
        },
        "server_time": utcnow(),
    }


@router.post("/research/queue/claim")
def claim_queue(body: ClaimIn) -> dict:
    """Worker side: take the oldest pending rows atomically (BEGIN IMMEDIATE)."""
    limit = max(1, min(body.limit, 5))
    conn = connect()
    claimed: list[dict] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        # A worker that died mid-run must not keep a domain locked. Reap first,
        # in the same transaction as the claim, so two workers cannot both grab
        # the reaped row.
        _reap_stale(conn, body.workspace_id)
        sql = (
            "SELECT * FROM research_queue WHERE status = 'pending'"
            + (" AND workspace_id = ?" if body.workspace_id else "")
            + " ORDER BY requested_at LIMIT ?"
        )
        args: tuple = ((body.workspace_id, limit) if body.workspace_id else (limit,))
        picks = conn.execute(sql, args).fetchall()
        now = utcnow()
        for row in picks:
            conn.execute(
                "UPDATE research_queue SET status = 'claimed', claimed_at = ?,"
                " attempts = attempts + 1, progress_stage = 'claimed',"
                " progress_note = 'worker picked it up', progress_at = ? WHERE id = ?",
                (now, now, row["id"]),
            )
            item = _queue_item(row)
            item.update(
                {
                    "status": "claimed",
                    "claimed_at": now,
                    "attempts": (row["attempts"] or 0) + 1,
                }
            )
            claimed.append(item)
        conn.commit()
    finally:
        conn.close()
    return {"claimed": claimed, "count": len(claimed)}


@router.post("/research/queue/{qid}/complete")
def complete_queue(qid: str, body: CompleteIn) -> dict:
    """Worker side: store the lead, the draft, or the failure reason."""
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM research_queue WHERE id = ?", (qid,)).fetchone()
        if not row:
            raise HTTPException(404, "queue item not found")
        if row["status"] == "done":
            return {"ok": True, "already": True, "item": _queue_item(row)}

        now = utcnow()
        profile = body.profile or {}
        if body.status == "done" and not (profile.get("business_name") or profile.get("name")):
            conn.execute(
                "UPDATE research_queue SET status = 'error',"
                " error = 'the worker returned no company name', finished_at = ?,"
                " progress_stage = 'error' WHERE id = ?",
                (now, qid),
            )
        elif body.status == "done":
            lead = _upsert_lead(conn, row["workspace_id"], profile)
            # Researching a site is how a client gets on file: the researched
            # company IS the client, and the leads are businesses that could buy
            # from it. Registering it here means a wiped database can be rebuilt
            # by researching the client's site once.
            conn.execute(
                """INSERT OR IGNORE INTO workspace_clients
                       (id, workspace_id, business_id, status, source, created_at)
                   VALUES (?,?,?,'active','research',?)""",
                (new_id(), row["workspace_id"], lead["business_id"], now),
            )
            summary = _store_research_leads(
                conn, row["workspace_id"], qid, lead["business_id"], profile,
                template=body.draft if isinstance(body.draft, dict) else None,
                guard=leadguard.check_lead,
            )
            profile["leads_found"] = summary["created"] + summary["updated"]
            profile["leads_list_id"] = summary["list_id"]
            conn.execute(
                "UPDATE research_queue SET status = 'done', business_id = ?, profile = ?,"
                " draft = ?, error = NULL, finished_at = ?, progress_stage = 'done',"
                " progress_note = ?, progress_at = ? WHERE id = ?",
                (
                    lead["business_id"],
                    json.dumps(profile),
                    json.dumps(body.draft) if body.draft else None,
                    now,
                    f"found {profile['leads_found']} leads",
                    now,
                    qid,
                ),
            )
        else:
            conn.execute(
                "UPDATE research_queue SET status = 'error', error = ?, finished_at = ?,"
                " progress_stage = 'error', progress_note = ?, progress_at = ? WHERE id = ?",
                (
                    (body.error or "worker reported an error")[:500],
                    now,
                    (body.error or "worker reported an error")[:300],
                    now,
                    qid,
                ),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM research_queue WHERE id = ?", (qid,)).fetchone()
    finally:
        conn.close()
    return {"ok": True, "item": _queue_item(row)}


@router.get("/research/agent-leads")
def list_research_leads(
    workspace_id: str, client_business_id: str | None = None, limit: int = 100
) -> dict:
    """Every business the agent has found *for* a client, newest signal first.

    Sorted by fit so the operator works the strongest match first; the signal
    source travels with each row so a claim can always be checked.
    """
    conn = connect()
    try:
        sql = "SELECT * FROM research_leads WHERE workspace_id = ?"
        args: list[Any] = [workspace_id]
        if client_business_id:
            sql += " AND client_business_id = ?"
            args.append(client_business_id)
        sql += " ORDER BY fit DESC, created_at DESC LIMIT ?"
        args.append(max(1, min(500, limit)))
        items = [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
    return {"items": items, "count": len(items)}


# ---------------------------------------------------------------- verification

class VerifyBatchIn(BaseModel):
    """Verify the contacts attached to one research run.

    `contact_ids` wins when given; otherwise every contact belonging to a lead
    from that client (or that queue row) is checked. Nothing here sends mail —
    it only decides whether the send gate will let an address through later.
    """

    contact_ids: list[str] | None = None
    client_business_id: str | None = None
    queue_id: str | None = None
    limit: int = 50


@router.post("/contacts/{contact_id}/verify")
def verify_one_contact(contact_id: str) -> dict:
    conn = connect()
    try:
        res = verify.verify_contact(conn, contact_id)
        conn.commit()
        if res.get("reason") == "not_found":
            raise HTTPException(404, "contact not found")
        return res
    finally:
        conn.close()


@router.post("/research/leads/verify")
def verify_lead_contacts(body: VerifyBatchIn) -> dict:
    conn = connect()
    try:
        if body.contact_ids:
            ids = [c for c in body.contact_ids if c][: max(1, min(200, body.limit * 4))]
        else:
            sql = (
                "SELECT DISTINCT c.id AS id FROM contacts c "
                "JOIN research_leads rl ON rl.business_id = c.business_id "
                "WHERE COALESCE(c.email, '') != ''"
            )
            args: list = []
            if body.client_business_id:
                sql += " AND rl.client_business_id = ?"
                args.append(body.client_business_id)
            if body.queue_id:
                sql += " AND rl.queue_id = ?"
                args.append(body.queue_id)
            # Only re-check what is not already proven, so a second click is cheap.
            sql += " AND COALESCE(c.email_status, 'unverified') != 'verified'"
            sql += " LIMIT ?"
            args.append(max(1, min(200, body.limit)))
            ids = [r["id"] for r in conn.execute(sql, args).fetchall()]
        out = verify.verify_batch(conn, ids)
        conn.commit()
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------- gap audit
#
# "Open issues" is the other half of the user's ask: a prospect with a broken or
# stale site is a prospect with a reason to reply. The audit is deterministic —
# HTTP, headers and HTML only — so every reason to email is backed by evidence a
# human can confirm. Nothing here sends mail or edits a lead's signal.

class AuditIn(BaseModel):
    domain: str


class AuditBatchIn(BaseModel):
    client_business_id: str | None = None
    queue_id: str | None = None
    limit: int = 8
    rescore: bool = False


@router.post("/audit/site")
def audit_site_route(body: AuditIn) -> dict:
    """Audit one domain on demand. Read-only; safe to click repeatedly."""
    return audit.audit_site(body.domain)


def _audit_rows(conn, sql: str, args: list, limit: int) -> list[dict]:
    out: list[dict] = []
    for row in conn.execute(sql, args).fetchall():
        if len(out) >= limit:
            break
        domain = (row["domain"] or "").strip()
        if not domain:
            continue
        try:
            res = audit.audit_site(domain)
        except Exception as exc:  # noqa: BLE001 - a bad site must not stop the batch
            log.warning("audit failed", extra={"extra_fields": {
                "lead_id": row["id"], "domain": domain, "error": str(exc)}})
            continue
        detail = audit.summarize(res)
        conn.execute(
            "UPDATE research_leads SET audit_score = ?, audit_detail = ? WHERE id = ?",
            (res["score"], detail or None, row["id"]),
        )
        out.append({"id": row["id"], "name": row["name"], "domain": domain,
                    "score": res["score"], "services": res["services"], "detail": detail})
    conn.commit()
    return out


@router.post("/research/leads/audit")
def audit_lead_batch(body: AuditBatchIn) -> dict:
    """Audit the sites of leads from one research run.

    Leads with a domain and no audit yet are done first; `rescore` re-checks
    everything (sites get fixed, and a fixed site is no longer a reason to email).
    """
    limit = max(1, min(40, body.limit))
    sql = ("SELECT id, name, domain, audit_score FROM research_leads "
           "WHERE COALESCE(domain, '') != ''")
    args: list = []
    if body.client_business_id:
        sql += " AND client_business_id = ?"
        args.append(body.client_business_id)
    if body.queue_id:
        sql += " AND queue_id = ?"
        args.append(body.queue_id)
    if not body.rescore:
        sql += " AND audit_score IS NULL"
    sql += " ORDER BY fit DESC NULLS LAST, created_at DESC"
    conn = connect()
    try:
        out = _audit_rows(conn, sql, args, limit)
    finally:
        conn.close()
    scored = sorted(out, key=lambda x: -x["score"])
    return {"checked": len(out), "results": out,
            "best": scored[0] if scored else None}


def audit_pending(limit: int = 2) -> int:
    """Worker hook: audit a couple of un-audited leads per tick.

    Deliberately tiny and best-effort. Auditing inside the research-complete
    request would let one slow prospect site stall a whole research run, so it
    happens on the send loop's idle time instead, a few at a time.
    """
    conn = connect()
    try:
        sql = ("SELECT id, name, domain, audit_score FROM research_leads "
               "WHERE COALESCE(domain, '') != '' AND audit_score IS NULL "
               "ORDER BY created_at DESC LIMIT ?")
        done = _audit_rows(conn, sql, [max(1, limit)], max(1, limit))
    except Exception:  # noqa: BLE001 - never break sending for an audit
        log.exception("audit pass failed")
        return 0
    finally:
        conn.close()
    if done:
        log.info("audited leads", extra={"extra_fields": {"count": len(done)}})
    return len(done)


# ------------------------------------------------------------ contact finder
#
# A lead with no address is unsendable: `POST /enroll` skips it with `no_contact`,
# which is exactly why the pipeline used to dead-end after research. This closes
# that gap without inventing anything — addresses come off the prospect's own
# site, then get DNS-verified before they are usable.

class DiscoverIn(BaseModel):
    client_business_id: str | None = None
    queue_id: str | None = None
    limit: int = 8
    rediscovar: bool = False


def _insert_contact(conn, business_id: str, cand: dict, verify_detail: str | None,
                    email_status: str) -> str | None:
    cid = new_id()
    cur = conn.execute(
        "INSERT OR IGNORE INTO contacts (id, business_id, email, email_status, email_source,"
        " verified_at, is_primary, created_at, verify_detail) VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, business_id, cand["email"], email_status, "website",
         utcnow() if email_status == "verified" else None,
         1 if cand.get("_primary") else 0, utcnow(), verify_detail),
    )
    return cid if cur.rowcount else None


def _record_attempt(conn, lead_id: str, note: str) -> None:
    """Stamp a crawl attempt so the worker can leave this lead alone for a while."""
    try:
        conn.execute("UPDATE research_leads SET discover_attempted_at = ?, discover_note = ?"
                     " WHERE id = ?", (utcnow(), str(note)[:200], lead_id))
        conn.commit()
    except Exception:  # noqa: BLE001 - bookkeeping must never lose the crawl result
        log.exception("could not record discover attempt",
                      extra={"extra_fields": {"lead": lead_id}})


def discover_for_lead(conn, lead: dict, fetch=None) -> dict:
    """Find + verify contacts for one lead. Returns a counted outcome."""
    domain = (lead["domain"] or "").strip()
    if not domain:
        return {"lead_id": lead["id"], "found": 0, "reason": "no_domain"}
    if not lead["business_id"]:
        # contacts.business_id is NOT NULL; without the business row there is
        # nothing to attach an address to, and retrying will not change that.
        _record_attempt(conn, lead["id"], "skipped: lead has no business row")
        return {"lead_id": lead["id"], "found": 0, "reason": "no_business"}

    found = discover.discover(domain, _fetch=fetch)
    cands = found["candidates"]
    pages = found["pages_checked"]
    if not cands:
        # Say so on the row. A lead whose site prints no address is a normal,
        # permanent answer - not work to repeat on the next tick.
        _record_attempt(conn, lead["id"],
                        f"no address printed on {pages} page(s)")
        return {"lead_id": lead["id"], "found": 0, "reason": "no_address_published",
                "pages_checked": pages}

    created, verified = 0, 0
    has_existing = conn.execute("SELECT 1 FROM contacts WHERE business_id = ? LIMIT 1",
                                (lead["business_id"],)).fetchone() is not None
    for i, cand in enumerate(cands):
        cand = {**cand, "_primary": i == 0 and not has_existing}
        try:
            verdict = verify.verify_address(cand["email"])
        except Exception:  # noqa: BLE001 - a DNS hiccup must not lose the address
            verdict = {"status": "unverified", "reason": "dns_unavailable", "detail": {}}
        status = verdict.get("status") or "unverified"
        raw_detail = verdict.get("detail")
        detail = raw_detail if isinstance(raw_detail, str) else json.dumps(
            {"reason": verdict.get("reason"), **(raw_detail or {})}, default=str)
        if _insert_contact(conn, lead["business_id"], cand, detail, status):
            created += 1
            if status == "verified":
                verified += 1
        else:
            # Address already known (unique index on email) — refresh its verdict.
            conn.execute("UPDATE contacts SET email_status = ?, verify_detail = ?,"
                         " verified_at = COALESCE(verified_at, ?) WHERE email = ?",
                         (status, detail, utcnow() if status == "verified" else None,
                          cand["email"]))
    _record_attempt(conn, lead["id"], f"{created} contact(s) from {pages} page(s)")
    conn.commit()
    return {"lead_id": lead["id"], "name": lead["name"], "domain": domain,
            "found": created, "verified": verified, "reason": "ok"}


@router.post("/research/leads/discover")
def discover_contacts_route(body: DiscoverIn) -> dict:
    """Find published email addresses for the leads of one research run."""
    limit = max(1, min(25, body.limit))
    sql = ("SELECT rl.id, rl.name, rl.domain, rl.business_id FROM research_leads rl "
           "WHERE COALESCE(rl.domain, '') != ''")
    args: list = []
    if body.client_business_id:
        sql += " AND rl.client_business_id = ?"
        args.append(body.client_business_id)
    if body.queue_id:
        sql += " AND rl.queue_id = ?"
        args.append(body.queue_id)
    if not body.rediscovar:
        sql += (" AND NOT EXISTS (SELECT 1 FROM contacts c WHERE c.business_id = rl.business_id)")
    sql += " ORDER BY rl.fit DESC NULLS LAST, rl.created_at DESC LIMIT ?"
    args.append(limit)

    conn = connect()
    results = []
    try:
        for lead in conn.execute(sql, args).fetchall():
            try:
                results.append(discover_for_lead(conn, lead))
            except Exception:  # noqa: BLE001 - one odd site must not stop the batch
                log.exception("discover failed", extra={"extra_fields": {"lead": lead["id"]}})
    finally:
        conn.close()
    total = sum(r.get("found", 0) for r in results)
    return {"leads": len(results), "contacts_found": total, "results": results}


def discover_pending(limit: int = 1, retry_after_days: int = 14) -> int:
    """Worker hook: one lead's contacts discovered per tick. Slow on purpose —
    this makes outbound HTTP to a stranger's website, so it runs on idle time.

    Ordering is "never tried first, then least recently tried", and a lead that
    was tried recently is skipped. Without that, "newest first" plus "no contact
    yet" pinned the queue to whatever lead happened to publish no address: it was
    crawled every tick and no other lead ever got a turn. A site that prints no
    address is a settled answer, not pending work; re-asking every minute is also
    indistinguishable from scraping to the person paying for the bandwidth.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retry_after_days))
    cutoff = cutoff.replace(microsecond=0).isoformat()
    conn = connect()
    try:
        sql = ("SELECT rl.id, rl.name, rl.domain, rl.business_id FROM research_leads rl "
               "WHERE COALESCE(rl.domain, '') != '' "
               "  AND (rl.discover_attempted_at IS NULL OR rl.discover_attempted_at < ?) "
               "  AND NOT EXISTS (SELECT 1 FROM contacts c WHERE c.business_id = rl.business_id) "
               "ORDER BY (rl.discover_attempted_at IS NOT NULL), "
               "         rl.discover_attempted_at ASC, rl.created_at DESC LIMIT ?")
        todo = conn.execute(sql, (cutoff, max(1, limit))).fetchall()
        n = 0
        for lead in todo:
            try:
                discover_for_lead(conn, lead)
                n += 1
            except Exception:  # noqa: BLE001
                log.exception("discover failed", extra={"extra_fields": {"lead": lead["id"]}})
    finally:
        conn.close()
    return n
