"""Domain research -> leads -> emails, driven from the UI.

Three capabilities the operator gets from one domain they type in:
  1. POST /research/domain   read the prospect's site and say what they sell
  2. POST /research/leads    turn that into a mailable lead list from the DB
  3. POST /research/email    write one email per lead, citing their own site

Everything here is additive to the existing /api surface and sits behind the
same bearer token as the rest of the app. No new credential, no new trust
boundary, no build step.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import ai, audit, discover, research, verify
from .db import connect, new_id, rows, utcnow
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


def _best_category(conn, profile: dict) -> tuple[str | None, float, list[dict]]:
    """Map free-text industry onto the taxonomy already in this database."""
    cats = rows(conn.execute(
        """SELECT category, COUNT(*) AS n FROM businesses
            WHERE category IS NOT NULL AND category != ''
         GROUP BY category ORDER BY n DESC LIMIT 400"""
    ))
    if not cats:
        return None, 0.0, []
    want = _tokens(" ".join([
        str(profile.get("industry") or ""),
        str(profile.get("what_they_do") or ""),
        " ".join(str(s) for s in profile.get("services") or []),
    ]))
    if not want:
        return None, 0.0, cats[:6]
    scored = []
    for c in cats:
        have = _tokens(str(c["category"]))
        if not have:
            continue
        overlap = len(want & have)
        if not overlap:
            continue
        scored.append({"category": c["category"], "n": c["n"],
                       "score": round(overlap / max(1, len(have)), 3)})
    scored.sort(key=lambda r: (-r["score"], -r["n"]))
    if not scored:
        return None, 0.0, cats[:6]
    return scored[0]["category"], scored[0]["score"], scored[:6]


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
        if not category and profile:
            category, _score, _alts = _best_category(conn, profile)
        city = body.city
        state = body.state
        if not city and not state and profile.get("service_area"):
            parts = [p.strip() for p in str(profile["service_area"]).split(",")]
            if len(parts) >= 2 and len(parts[-1]) == 2:
                state = parts[-1].upper()
                city = parts[0]

        where = ["1=1"]
        args: list[Any] = []
        if category:
            where.append("b.category = ?"); args.append(category)
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

        _cat, cat_score, alts = _best_category(conn, profile) if profile else (None, 0, [])
    finally:
        conn.close()

    filters = {"category": category, "city": city, "state": state}
    return {
        "filters": filters,
        "matched_category": category,
        "category_confidence": cat_score,
        "category_options": alts,
        "profile": profile or None,
        "brief": research.brief(profile) if profile else "",
        "count": len(results),
        "results": results,
        "hint": (
            None if results else
            "No businesses match yet. Import a lead list for "
            f"{city or 'this area'}, or relax the filters."
        ),
    }


# ------------------------------------------------------------ email drafting

class EmailIn(BaseModel):
    workspace_id: str
    business_id: str
    profile_id: str | None = None
    length: int = 100
    signature: str = ""
    refresh_research: bool = False


@router.post("/research/email")
def draft_for_business(body: EmailIn) -> dict:
    """One grounded email for one lead. Refreshes site research on demand."""
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

        seller = None
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
    draft = ai.draft_email(seller, business, contact, brief, body.length, body.signature)
    draft.update({
        "business_id": body.business_id,
        "business_name": business.get("name"),
        "contact_email": (contact or {}).get("email"),
        "contact_name": (contact or {}).get("full_name"),
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
    """What the UI shows in the corner: is research/LLM live right now?"""
    conn = connect()
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM businesses").fetchone()["n"]
        done = conn.execute(
            "SELECT COUNT(*) AS n FROM businesses WHERE COALESCE(site_research, '') != ''"
        ).fetchone()["n"]
    finally:
        conn.close()
    return {"llm": ai.llm_health(), "researched": done, "businesses": total}


SIGNAL_KINDS = {
    "hiring", "permit", "expansion", "review", "rfp", "license", "news", "social", "other",
}


def _lead_domain(raw: str) -> str:
    """Accept a bare domain or a full URL; store the bare host."""
    d = (raw or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0].split("?")[0].strip()
    return d[4:] if d.startswith("www.") else d


def _store_research_leads(
    conn,
    workspace_id: str,
    queue_id: str | None,
    client_bid: str | None,
    profile: dict,
) -> dict:
    """Save the companies the agent found *for* the researched business.

    The researched site is the client here, not the target: every row is somebody
    who buys what the client sells. Leads become real businesses so they can be
    sequenced like any other lead, but no contact is created - the agent is told
    never to invent an address, so a hint stays a hint until a human checks it.
    """
    found = [x for x in (profile.get("leads") or []) if isinstance(x, dict)]
    client = None
    if client_bid:
        client = conn.execute("SELECT name, domain FROM businesses WHERE id = ?", (client_bid,)).fetchone()
    client_name = (client["name"] if client else "") or "researched business"

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
        if not bid and domain:
            bid = new_id()
            conn.execute(
                """INSERT INTO businesses (id, name, website, domain, source, source_ref,
                                           fetched_at, created_at, updated_at)
                   VALUES (?,?,?,?, 'agent_lead', ?, ?, ?, ?)""",
                (bid, name, f"https://{domain}", domain, client_name[:120], utcnow(),
                 utcnow(), utcnow()),
            )

        existing = None
        if domain:
            existing = conn.execute(
                "SELECT id FROM research_leads WHERE client_business_id = ? AND domain = ?",
                (client_bid, domain),
            ).fetchone()
        if existing is None:
            # A lead with no site of its own still has to dedupe, or every run
            # re-adds it and the list fills with the same company.
            existing = conn.execute(
                "SELECT id FROM research_leads WHERE client_business_id = ? "
                "AND COALESCE(domain, '') = '' AND lower(name) = lower(?)",
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

        payload = (
            name, domain, str(lead.get("why_now") or "")[:600],
            kind, str(lead.get("signal_detail") or "")[:1200],
            str(lead.get("signal_source") or "")[:500],
            str(lead.get("contact_hint") or "")[:300], fit, bid,
            subject or None, body or None,
        )
        if existing:
            conn.execute(
                """UPDATE research_leads
                      SET name = ?, domain = ?, why_now = ?, signal_kind = ?, signal_detail = ?,
                          signal_source = ?, contact_hint = ?, fit = ?, business_id = COALESCE(?, business_id),
                          email_subject = COALESCE(?, email_subject),
                          email_body = COALESCE(?, email_body)
                    WHERE id = ?""",
                payload + (existing["id"],),
            )
            updated += 1
        else:
            conn.execute(
                """INSERT INTO research_leads (name, domain, why_now, signal_kind, signal_detail,
                                               signal_source, contact_hint, fit, business_id,
                                               email_subject, email_body,
                                               id, queue_id, workspace_id, client_business_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                payload + (new_id(), queue_id, workspace_id, client_bid, utcnow()),
            )
            created += 1

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


def _queue_item(row: Any) -> dict:
    data = dict(row)
    for key in ("profile", "draft"):
        raw = data.get(key)
        if raw:
            try:
                data[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                data[key] = None
    return data


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
            return {
                "duplicate": True,
                "item": _queue_item(open_row),
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
    finally:
        conn.close()
    items = [_queue_item(r) for r in fetched]
    return {
        "items": items,
        "pending": sum(1 for i in items if i["status"] in _QUEUE_OPEN),
        "done": sum(1 for i in items if i["status"] == "done"),
    }


@router.post("/research/queue/claim")
def claim_queue(body: ClaimIn) -> dict:
    """Worker side: take the oldest pending rows atomically (BEGIN IMMEDIATE)."""
    limit = max(1, min(body.limit, 5))
    conn = connect()
    claimed: list[dict] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
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
                " attempts = attempts + 1 WHERE id = ?",
                (now, row["id"]),
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
                " error = 'the worker returned no company name', finished_at = ? WHERE id = ?",
                (now, qid),
            )
        elif body.status == "done":
            lead = _upsert_lead(conn, row["workspace_id"], profile)
            summary = _store_research_leads(
                conn, row["workspace_id"], qid, lead["business_id"], profile
            )
            profile["leads_found"] = summary["created"] + summary["updated"]
            profile["leads_list_id"] = summary["list_id"]
            conn.execute(
                "UPDATE research_queue SET status = 'done', business_id = ?, profile = ?,"
                " draft = ?, error = NULL, finished_at = ? WHERE id = ?",
                (
                    lead["business_id"],
                    json.dumps(profile),
                    json.dumps(body.draft) if body.draft else None,
                    now,
                    qid,
                ),
            )
        else:
            conn.execute(
                "UPDATE research_queue SET status = 'error', error = ?, finished_at = ? WHERE id = ?",
                ((body.error or "worker reported an error")[:500], now, qid),
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


def discover_for_lead(conn, lead: dict, fetch=None) -> dict:
    """Find + verify contacts for one lead. Returns a counted outcome."""
    domain = (lead["domain"] or "").strip()
    if not domain:
        return {"lead_id": lead["id"], "found": 0, "reason": "no_domain"}
    found = discover.discover(domain, _fetch=fetch)
    cands = found["candidates"]
    if not cands:
        return {"lead_id": lead["id"], "found": 0, "reason": "no_address_published",
                "pages_checked": found["pages_checked"]}

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


def discover_pending(limit: int = 1) -> int:
    """Worker hook: one lead's contacts discovered per tick. Slow on purpose —
    this makes outbound HTTP to a stranger's website, so it runs on idle time."""
    conn = connect()
    try:
        sql = ("SELECT rl.id, rl.name, rl.domain, rl.business_id FROM research_leads rl "
               "WHERE COALESCE(rl.domain, '') != '' AND NOT EXISTS "
               "(SELECT 1 FROM contacts c WHERE c.business_id = rl.business_id) "
               "ORDER BY rl.created_at DESC LIMIT ?")
        todo = conn.execute(sql, (max(1, limit),)).fetchall()
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
