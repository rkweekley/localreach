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
    conn = connect()
    try:
        wid = new_id()
        conn.execute(
            "INSERT INTO workspaces (id,name,slug,physical_address,daily_send_cap,created_at) VALUES (?,?,?,?,?,?)",
            (wid, w.name, w.slug, w.physical_address, w.daily_send_cap, utcnow()),
        )
        conn.commit()
        return {"id": wid, **w.model_dump()}
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
def import_businesses(items: list[BusinessIn]) -> dict:
    """The ingestion seam. Whatever the data source (OSM, Places, CSV, licensed
    provider), it lands here with provenance intact."""
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
        return scoring.compute(dict(b), dict(c) if c else None, set(body.icp_categories))
    finally:
        conn.close()


class ListIn(BaseModel):
    workspace_id: str
    name: str
    business_ids: list[str] = []


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
