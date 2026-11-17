"""Discovery + data routes: workspaces, business import, search, lists."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import scoring
from .compliance import suppressed
from .db import connect, new_id, rows, utcnow

router = APIRouter(prefix="/api", tags=["data"])


class WorkspaceIn(BaseModel):
    name: str
    slug: str
    physical_address: str
    daily_send_cap: int = 40


@router.post("/workspaces")
def create_workspace(w: WorkspaceIn) -> dict:
    """Create a workspace, or return the existing one with the same slug.

    Idempotent on purpose: re-running a deploy probe or an e2e check must not
    raise `UNIQUE constraint failed` and turn a healthy instance into a 500.
    """
    conn = connect()
    try:
        existing = rows(conn.execute("SELECT * FROM workspaces WHERE slug = ?", (w.slug,)))
        if existing:
            return {**existing[0], "existed": True}
        wid = new_id()
        conn.execute(
            "INSERT INTO workspaces (id,name,slug,physical_address,daily_send_cap,created_at) VALUES (?,?,?,?,?,?)",
            (wid, w.name, w.slug, w.physical_address, w.daily_send_cap, utcnow()),
        )
        conn.commit()
        return {"id": wid, **w.model_dump(), "existed": False}
    finally:
        conn.close()


@router.get("/workspaces")
def list_workspaces() -> list[dict]:
    """Operational visibility: what this instance actually serves.

    Also the cheapest authenticated probe for a deploy check — if this answers
    with the token and 401s without it, auth is wired correctly.
    """
    conn = connect()
    try:
        return rows(conn.execute("SELECT * FROM workspaces ORDER BY created_at"))
    finally:
        conn.close()


class BusinessIn(BaseModel):
    name: str
    category: str | None = None
    city: str | None = None
    state: str | None = None
    address1: str | None = None
    phone: str | None = None
    website: str | None = None
    rating: float | None = None
    review_count: int = 0
    years_in_business: int | None = None
    is_franchise: bool = False
    source: str = "csv"
    source_ref: str | None = None
    license_note: str | None = None
    # optional inline contact
    contact_email: str | None = None
    contact_name: str | None = None
    contact_title: str | None = None
    email_status: str = "unverified"


@router.post("/businesses/import")
def import_businesses(items: list[BusinessIn], workspace_id: str | None = None) -> dict:
    """The ingestion seam. Whatever the data source (OSM, Places, CSV, licensed
    provider), it lands here with provenance intact. When a workspace is named,
    the prospect is also recorded as belonging to it - that is what keeps one
    workspace's imports out of another workspace's Unassigned bucket."""
    conn = connect()
    created = updated = 0
    try:
        for b in items:
            domain = None
            if b.website:
                domain = b.website.replace("https://", "").replace("http://", "").split("/")[0].lower().removeprefix("www.")
            existing = None
            if b.source_ref:
                existing = conn.execute(
                    "SELECT id FROM businesses WHERE source = ? AND source_ref = ?",
                    (b.source, b.source_ref),
                ).fetchone()
            if not existing and domain:
                existing = conn.execute("SELECT id FROM businesses WHERE domain = ?", (domain,)).fetchone()

            if existing:
                bid = existing["id"]
                conn.execute(
                    """UPDATE businesses SET name=?, category=?, city=?, state=?, phone=?, website=?, domain=?,
                       rating=?, review_count=?, years_in_business=?, is_franchise=?, fetched_at=?, updated_at=?
                       WHERE id=?""",
                    (b.name, b.category, b.city, b.state, b.phone, b.website, domain, b.rating,
                     b.review_count, b.years_in_business, int(b.is_franchise), utcnow(), utcnow(), bid),
                )
                updated += 1
            else:
                bid = new_id()
                conn.execute(
                    """INSERT INTO businesses (id,name,category,city,state,address1,phone,website,domain,rating,
                       review_count,years_in_business,is_franchise,source,source_ref,fetched_at,license_note,
                       created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (bid, b.name, b.category, b.city, b.state, b.address1, b.phone, b.website, domain,
                     b.rating, b.review_count, b.years_in_business, int(b.is_franchise), b.source,
                     b.source_ref, utcnow(), b.license_note, utcnow(), utcnow()),
                )
                created += 1

            if workspace_id:
                conn.execute(
                    """INSERT OR IGNORE INTO workspace_businesses
                           (workspace_id, business_id, origin, created_at)
                       VALUES (?,?,?,?)""",
                    (workspace_id, bid, b.source or "import", utcnow()),
                )

            if b.contact_email:
                email = b.contact_email.lower()
                found = conn.execute("SELECT id FROM contacts WHERE email = ?", (email,)).fetchone()
                if found:
                    conn.execute(
                        "UPDATE contacts SET email_status = ?, full_name = COALESCE(?, full_name), title = COALESCE(?, title) WHERE id = ?",
                        (b.email_status, b.contact_name, b.contact_title, found["id"]),
                    )
                else:
                    conn.execute(
                        """INSERT INTO contacts (id,business_id,full_name,title,email,email_status,email_source,
                           is_primary,created_at) VALUES (?,?,?,?,?,?,?,1,?)""",
                        (new_id(), bid, b.contact_name, b.contact_title, email,
                         b.email_status, b.source, utcnow()),
                    )
        conn.commit()
    finally:
        conn.close()
    return {"created": created, "updated": updated}


@router.get("/search")
def search(
    workspace_id: str,
    category: str | None = None,
    city: str | None = None,
    state: str | None = None,
    min_rating: float | None = None,
    min_reviews: int | None = None,
    min_fit: int = 0,
    verified_email_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    where = ["1=1"]
    args: list[Any] = []
    if category:
        where.append("b.category = ?"); args.append(category)
    if city:
        where.append("b.city = ?"); args.append(city)
    if state:
        where.append("b.state = ?"); args.append(state)
    if min_rating:
        where.append("b.rating >= ?"); args.append(min_rating)
    if min_reviews:
        where.append("b.review_count >= ?"); args.append(min_reviews)
    if verified_email_only:
        where.append("c.email_status = 'verified'")

    sql = f"""
      SELECT b.*, s.fit_score, s.demand_score, s.reach_score, s.factors,
             c.id AS contact_id, c.email, c.email_status, c.full_name AS contact_name, c.title
      FROM businesses b
      LEFT JOIN scores s ON s.business_id = b.id
      LEFT JOIN contacts c ON c.business_id = b.id AND c.is_primary = 1
      WHERE {' AND '.join(where)}
      ORDER BY COALESCE(s.fit_score, 0) DESC, b.review_count DESC
      LIMIT ? OFFSET ?
    """
    conn = connect()
    try:
        data = rows(conn.execute(sql, (*args, limit, offset)))
    finally:
        conn.close()

    # suppress anything this workspace has opted out
    out = []
    for r in data:
        if r.get("email") and suppressed(workspace_id, r["email"]):
            r["email"] = None
            r["suppressed"] = True
        if (r.get("fit_score") or 0) >= min_fit:
            out.append(r)
    return {"count": len(out), "results": out}


class ScoreIn(BaseModel):
    icp_categories: list[str] = []
    # Scoring is a per-workspace act on a per-client relationship: the FIT column
    # the operator is looking at is client_leads.fit. Passing the workspace (and
    # optionally the client) lets the score land where the screen reads it.
    workspace_id: str | None = None
    client_business_id: str | None = None


@router.post("/businesses/{business_id}/score")
def score_business(business_id: str, body: ScoreIn) -> dict:
    conn = connect()
    try:
        b = conn.execute("SELECT * FROM businesses WHERE id = ?", (business_id,)).fetchone()
        if not b:
            raise HTTPException(404, "business not found")
        c = conn.execute(
            "SELECT * FROM contacts WHERE business_id = ? AND is_primary = 1", (business_id,)
        ).fetchone()

        icp = set(body.icp_categories)
        if not icp and body.client_business_id:
            # No ICP was supplied, so derive one from the categories this client
            # already has leads in. An empty ICP scores every prospect the same,
            # which is what made "Score selected" look broken.
            icp = {r["category"] for r in conn.execute(
                """SELECT DISTINCT b2.category FROM client_leads cl
                     JOIN businesses b2 ON b2.id = cl.business_id
                    WHERE cl.client_business_id = ? AND b2.category IS NOT NULL""",
                (body.client_business_id,),
            )}
            if not icp:
                # Scoring against nothing is not a score, it is a shuffle: every
                # prospect lands on the same base number and the operator loses the
                # ranking they had. Refuse and say what is missing instead.
                raise HTTPException(
                    422,
                    "No ICP for this client yet: it has no categorised leads. "
                    "Set a category on at least one lead, or pass icp_categories.",
                )
        elif not icp and body.workspace_id:
            # Unassigned prospects are scored on their own merits, but the bar they
            # are measured against should still be the workspace's own targeting.
            icp = {r["category"] for r in conn.execute(
                """SELECT DISTINCT b2.category FROM client_leads cl
                     JOIN businesses b2 ON b2.id = cl.business_id
                    WHERE cl.workspace_id = ? AND b2.category IS NOT NULL""",
                (body.workspace_id,),
            )}

        result = scoring.compute(dict(b), dict(c) if c else None, icp)

        updated = 0
        if body.workspace_id:
            sql = "UPDATE client_leads SET fit = ? WHERE business_id = ? AND workspace_id = ?"
            args: list[Any] = [result.get("fit_score") or result.get("fit"), business_id,
                               body.workspace_id]
            if body.client_business_id:
                sql += " AND client_business_id = ?"
                args.append(body.client_business_id)
            updated = conn.execute(sql, args).rowcount or 0
            conn.commit()
        result["assigned_rows_updated"] = updated
        return result
    finally:
        conn.close()


class ListIn(BaseModel):
    workspace_id: str
    name: str
    business_ids: list[str] = []


@router.get("/lists")
def list_lists(workspace_id: str) -> list[dict]:
    """Saved static lists, newest first, with their size.

    `Save as list` used to write `lists`/`list_members` and nothing ever read
    them back, so a saved list disappeared once the toast faded. This is the
    read half, and `/api/leads?list_id=` is the other half.
    """
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT l.id, l.name, l.created_at, COUNT(m.business_id) AS members
                 FROM lists l
                 LEFT JOIN list_members m ON m.list_id = l.id
                WHERE l.workspace_id = ?
                GROUP BY l.id, l.name, l.created_at
                ORDER BY l.created_at DESC""",
            (workspace_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/lists")
def create_list(body: ListIn) -> dict:
    conn = connect()
    try:
        lid = new_id()
        conn.execute(
            "INSERT INTO lists (id,workspace_id,name,created_at) VALUES (?,?,?,?)",
            (lid, body.workspace_id, body.name, utcnow()),
        )
        for bid in body.business_ids:
            conn.execute(
                "INSERT OR IGNORE INTO list_members (list_id,business_id,added_at) VALUES (?,?,?)",
                (lid, bid, utcnow()),
            )
        conn.commit()
        return {"id": lid, "name": body.name, "members": len(body.business_ids)}
    finally:
        conn.close()


@router.post("/lists/{list_id}/members")
def add_members(list_id: str, business_ids: list[str]) -> dict:
    conn = connect()
    try:
        added = 0
        for bid in business_ids:
            cur = conn.execute(
                "INSERT OR IGNORE INTO list_members (list_id,business_id,added_at) VALUES (?,?,?)",
                (list_id, bid, utcnow()),
            )
            added += cur.rowcount
        conn.commit()
        return {"added": added}
    finally:
        conn.close()


@router.get("/lists/{list_id}")
def get_list(list_id: str) -> dict:
    conn = connect()
    try:
        lst = conn.execute("SELECT * FROM lists WHERE id = ?", (list_id,)).fetchone()
        if not lst:
            raise HTTPException(404, "list not found")
        members = rows(conn.execute(
            """SELECT b.*, c.email, c.email_status FROM list_members m
               JOIN businesses b ON b.id = m.business_id
               LEFT JOIN contacts c ON c.business_id = b.id AND c.is_primary = 1
               WHERE m.list_id = ?""", (list_id,)))
        return {**dict(lst), "members": members}
    finally:
        conn.close()
