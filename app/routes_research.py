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
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import ai, research
from .db import connect, new_id, rows, utcnow
from .security import RateLimiter, client_ip

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


class ImportIn(BaseModel):
    workspace_id: str
    profile: dict[str, Any]
    category: str | None = None
    city: str | None = None
    state: str | None = None


@router.post("/research/import")
def import_researched(body: ImportIn) -> dict:
    """Turn a researched site into a real lead in this workspace.

    Deduplicates on domain, so researching the same company twice updates the
    row instead of creating a twin. The seed joins the workspace "Researched"
    list so it appears under Leads and can be sequenced like any other lead.
    """
    p = body.profile or {}
    domain = (p.get("domain") or "").strip().lower()
    name = (p.get("business_name") or p.get("name") or domain or "").strip()
    if not name:
        raise HTTPException(422, "Nothing to import - the profile has no company name")

    area = [x.strip() for x in str(p.get("service_area") or "").split(",") if x.strip()]
    city = body.city or (area[0] if len(area) > 1 else None)
    state = body.state or (area[-1].upper() if len(area) > 1 and len(area[-1]) == 2 else None)
    emails = [e for e in (p.get("emails") or []) if isinstance(e, str) and "@" in e]
    phones = [x for x in (p.get("phones") or []) if isinstance(x, str)]

    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM workspaces WHERE id = ?", (body.workspace_id,)).fetchone():
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
                (name, body.category or p.get("industry"), city, state, p.get("url"),
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
                (bid, name, body.category or p.get("industry"), city, state, (phones or [None])[0],
                 p.get("url"), domain, "research", domain or name, utcnow(),
                 json.dumps(p), utcnow(), utcnow(), utcnow()),
            )
            created = True

        lst = conn.execute(
            "SELECT id FROM lists WHERE workspace_id = ? AND name = 'Researched'",
            (body.workspace_id,),
        ).fetchone()
        if lst:
            lid = lst["id"]
        else:
            lid = new_id()
            conn.execute(
                "INSERT INTO lists (id, workspace_id, name, created_at) VALUES (?,?,?,?)",
                (lid, body.workspace_id, "Researched", utcnow()),
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

        conn.commit()
        row = dict(conn.execute("SELECT * FROM businesses WHERE id = ?", (bid,)).fetchone())
    finally:
        conn.close()

    return {
        "created": created,
        "business": row,
        "list_id": lid,
        "contact_email": saved_email,
        "note": "Saved unverified - verify the address before it can send.",
    }
