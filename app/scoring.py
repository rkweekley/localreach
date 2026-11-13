"""Fit + demand scoring — the "RE2 Score" analogue.

Two separate ideas, deliberately not blended into one meaningless number:
  fit_score     how well this business matches the offer/ICP        (0-100)
  demand_score  how much local search demand its category has      (0-100)
  reach_score   how likely we are to actually land in an inbox     (0-100)

The UI shows all three. A business with high demand and no reachable
email is a phone-call task, not an email task, and the app says so.
"""
from __future__ import annotations

import json

from .db import connect, utcnow


def demand_for(category: str, state: str, city: str) -> int:
    """Monthly local search volume -> 0-100, from the category_demand table.

    Until that table is populated (Keyword Planner import), fall back to review
    density, which is a decent free proxy for how much commerce a category does
    locally. The fallback is labelled in the API response so nobody mistakes a
    proxy for real demand data.
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT monthly_volume FROM category_demand WHERE category = ? AND state = ? AND (city = ? OR city IS NULL) ORDER BY city IS NULL LIMIT 1",
            (category, state, city),
        ).fetchone()
        if row and row["monthly_volume"]:
            # 1,000 monthly searches in a metro is a strong category; 5,000+ is saturated
            return max(0, min(100, int(row["monthly_volume"] / 50)))
        row = conn.execute(
            "SELECT AVG(review_count) AS avg_reviews FROM businesses WHERE category = ? AND state = ?",
            (category, state),
        ).fetchone()
        avg = (row["avg_reviews"] or 0) if row else 0
    finally:
        conn.close()
    return max(0, min(100, int(avg / 2)))


def compute(business: dict, contact: dict | None, icp_categories: set[str] | None = None) -> dict:
    factors: dict[str, int] = {}

    # ---- fit ----
    fit = 40
    if icp_categories and business.get("category") in icp_categories:
        fit += 25
        factors["category_match"] = 25
    elif icp_categories:
        factors["category_match"] = 0

    reviews = business.get("review_count") or 0
    if reviews >= 100:
        fit += 15
        factors["established"] = 15
    elif reviews >= 25:
        fit += 10
        factors["established"] = 10
    elif reviews >= 5:
        fit += 5
        factors["established"] = 5

    rating = business.get("rating") or 0
    if rating >= 4.5:
        fit += 8
        factors["reputation"] = 8

    if business.get("years_in_business") and business["years_in_business"] >= 5:
        fit += 7
        factors["tenure"] = 7
    if business.get("is_franchise"):
        fit -= 10
        factors["franchise_penalty"] = -10

    fit = max(0, min(100, fit))

    # ---- reach ----
    reach = 0
    if contact:
        status = contact.get("email_status")
        reach = {"verified": 90, "risky": 45, "guessed": 25, "unverified": 20, "invalid": 0}.get(status, 20)
        if contact.get("title") and any(
            k in (contact["title"] or "").lower() for k in ("owner", "president", "founder", "principal", "ceo")
        ):
            reach += 10
    reach = max(0, min(100, reach))

    demand = demand_for(business.get("category") or "", business.get("state") or "", business.get("city") or "")

    result = {"fit_score": fit, "reach_score": reach, "demand_score": demand, "factors": factors}

    conn = connect()
    try:
        conn.execute(
            """INSERT INTO scores (business_id, fit_score, demand_score, reach_score, factors, computed_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT (business_id) DO UPDATE SET
                 fit_score = excluded.fit_score,
                 demand_score = excluded.demand_score,
                 reach_score = excluded.reach_score,
                 factors = excluded.factors,
                 computed_at = excluded.computed_at""",
            (business["id"], fit, demand, reach, json.dumps(factors), utcnow()),
        )
        conn.commit()
    finally:
        conn.close()
    return result
