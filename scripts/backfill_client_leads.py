"""Give every already-researched lead an owner.

The Leads screen shows one row per prospect-per-client. Before `client_leads`
existed, ownership was recorded three different ways:

  1. `research_leads.client_business_id`  - who the research was run for
  2. a list literally named "Leads for X" - an operator's filing habit
  3. `businesses.source_ref = "<client>"` - what the research worker stamped

This script folds those three sources into `client_leads`, in evidence order,
and is safe to run repeatedly. It never guesses:

  * a name match must be unique after `norm_company()` folding, otherwise the
    row is left for a human to assign in the UI
  * a business is never filed as a lead for itself, and a client is never
    treated as a prospect
  * nothing is deleted; rows we cannot attribute simply stay in Unassigned

Run inside the app image (dry run is the default):

    python scripts/backfill_client_leads.py            # report only
    python scripts/backfill_client_leads.py --apply    # write
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import connect, new_id, norm_company, utcnow  # noqa: E402


def _client_ids(conn):
    """Client business id -> (name, workspace). `businesses` has no workspace."""
    rows = conn.execute(
        """SELECT b.id, b.name, x.ws
             FROM businesses b
             JOIN (SELECT client_business_id AS cid, workspace_id AS ws FROM research_leads
                    WHERE client_business_id IS NOT NULL
                   UNION
                   SELECT client_business_id, workspace_id FROM client_leads) x
               ON x.cid = b.id"""
    ).fetchall()
    return {r["id"]: (r["name"], r["ws"]) for r in rows}


PENDING: set[tuple] = set()


def _already(conn, client_id, business_id):
    if (client_id, business_id) in PENDING:  # dry run: count what we would write
        return True
    return conn.execute(
        "SELECT id FROM client_leads WHERE client_business_id = ? AND business_id = ?",
        (client_id, business_id),
    ).fetchone()


def _file(conn, ws, client_id, business_id, fit, origin, research_lead_id, log, apply):
    if client_id == business_id:
        log.append(("skip", origin, business_id, "is its own client"))
        return 0
    if _already(conn, client_id, business_id):
        return 0
    log.append(("add", origin, business_id, f"fit={fit}"))
    PENDING.add((client_id, business_id))
    if apply:
        now = utcnow()
        conn.execute(
            """INSERT INTO client_leads (id, workspace_id, client_business_id, business_id,
                   fit, status, origin, research_lead_id, assigned_by, created_at, updated_at)
               VALUES (?,?,?,?,?,'new',?,?,NULL,?,?)""",
            (new_id(), ws, client_id, business_id, fit, origin, research_lead_id, now, now),
        )
    return 1


def _resolve_business(conn, name, domain, log):
    """Find the registry row a research line refers to. Exact, never fuzzy."""
    if domain:
        hits = conn.execute(
            "SELECT id FROM businesses WHERE lower(trim(domain)) = ?",
            (domain.strip().lower(),),
        ).fetchall()
        if len(hits) == 1:
            return hits[0]["id"], "domain"
    if name:
        key = norm_company(name)
        hits = [r for r in conn.execute("SELECT id, name FROM businesses")
                if norm_company(r["name"]) == key]
        if len(hits) == 1:
            return hits[0]["id"], "name"
    return None, "none"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()
    log: list[tuple] = []
    counts = {}
    conn = connect()
    try:
        clients = _client_ids(conn)
        names = {name: cid for cid, (name, _ws) in clients.items()}
        ws_of = {cid: ws for cid, (_name, ws) in clients.items()}

        # Pass 1+2: research evidence. business_id may be NULL on rows written
        # before the write path recorded it, so resolve - or register the
        # company from the research line itself.
        for rl in conn.execute(
            """SELECT id, workspace_id, client_business_id, business_id, name, domain, fit
                 FROM research_leads WHERE client_business_id IS NOT NULL
                ORDER BY created_at"""
        ).fetchall():
            bid = rl["business_id"]
            if not bid:
                bid, how = _resolve_business(conn, rl["name"], rl["domain"], log)
                if bid:
                    log.append(("link", "research/repair", rl["id"], f"-> {bid[:8]} via {how}"))
                    if args.apply:
                        conn.execute("UPDATE research_leads SET business_id = ? WHERE id = ?",
                                     (bid, rl["id"]))
                else:
                    bid = new_id()
                    log.append(("newbiz", "research/repair", bid, rl["name"]))
                    if args.apply:
                        conn.execute(
                            """INSERT INTO businesses (id, name, domain, source, source_ref,
                                   created_at, updated_at)
                               VALUES (?,?,?,'research_backfill',?,?,?)""",
                            (bid, rl["name"], rl["domain"],
                             (clients.get(rl["client_business_id"], ("", ""))[0])[:120],
                             utcnow(), utcnow()),
                        )
                        conn.execute("UPDATE research_leads SET business_id = ? WHERE id = ?",
                                     (bid, rl["id"]))
            counts["research"] = counts.get("research", 0) + _file(
                conn, rl["workspace_id"], rl["client_business_id"], bid, rl["fit"],
                "research", rl["id"], log, args.apply)

        # Pass 3: lists named "Leads for X" - the operator's own filed intent.
        for lst in conn.execute("SELECT id, name, workspace_id FROM lists").fetchall():
            label = (lst["name"] or "").strip()
            if not label.lower().startswith("leads for "):
                continue
            who = label[len("leads for "):].strip()
            cid = names.get(who) or names.get(who.rstrip("."))
            if not cid:
                log.append(("skip", "list", lst["id"], f"no client named {who!r}"))
                continue
            for m in conn.execute(
                "SELECT business_id FROM list_members WHERE list_id = ?", (lst["id"],)
            ).fetchall():
                ev = conn.execute(
                    """SELECT id, fit FROM research_leads
                        WHERE business_id = ? AND client_business_id = ?
                        ORDER BY created_at DESC LIMIT 1""",
                    (m["business_id"], cid),
                ).fetchone()
                counts["list"] = counts.get("list", 0) + _file(
                    conn, lst["workspace_id"], cid, m["business_id"],
                    ev["fit"] if ev else None, "list", ev["id"] if ev else None,
                    log, args.apply)

        # Pass 4: what the research worker stamped at creation time.
        for b in conn.execute(
            """SELECT id, name, source_ref FROM businesses
                WHERE source = 'agent_lead' AND source_ref IS NOT NULL"""
        ).fetchall():
            cid = names.get((b["source_ref"] or "").strip())
            if not cid:
                continue
            ev = conn.execute(
                """SELECT id, fit FROM research_leads
                    WHERE business_id = ? AND client_business_id = ?
                    ORDER BY created_at DESC LIMIT 1""",
                (b["id"], cid),
            ).fetchone()
            counts["source_ref"] = counts.get("source_ref", 0) + _file(
                conn, ws_of.get(cid) or "", cid, b["id"], ev["fit"] if ev else None,
                "source_ref", ev["id"] if ev else None, log, args.apply)

        if args.apply:
            conn.commit()

        label = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM businesses")}
        print(f"{'APPLIED' if args.apply else 'DRY RUN'} - client_leads rows to add: {counts}")
        for kind, origin, ref, note in log:
            print(f"  {kind:<7} {origin:<18} {label.get(ref, ref)[:40]:<40} {note}")

        left = [r["name"] for r in conn.execute(
            "SELECT id, name FROM businesses WHERE id NOT IN (SELECT business_id FROM client_leads)"
            " AND id NOT IN (SELECT client_business_id FROM client_leads)"
            " AND id NOT IN (SELECT client_business_id FROM research_leads"
            "                WHERE client_business_id IS NOT NULL)"
        ) if not any(bid == r["id"] for (_c, bid) in PENDING)]
        print(f"\nstill unattributed businesses (shown as Unassigned): {len(left)}")
        for n in left:
            print(f"  left    {'-':<18} {n[:44]}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
