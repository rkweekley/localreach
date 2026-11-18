"""Re-check every stored lead against the provenance guard.

Run after deploying the guard so rows written before it existed do not keep
their invented domains and mailboxes. Dry-run by default: it prints what it
would change and touches nothing.

    python -m tools.backfill_provenance                 # report
    python -m tools.backfill_provenance --apply         # write
    python -m tools.backfill_provenance --apply --limit 50

Only two things are ever changed per lead: `domain` (cleared when the guard
cannot prove it belongs to this business) and `contact_hint` (addresses
stripped). `evidence_note` records why, so the change is auditable later.
"""
from __future__ import annotations

import argparse
import sys

from app import leadguard
from app.db import connect, init_db


def _rows(conn, limit: int):
    sql = """
        SELECT rl.id, rl.name, rl.domain, rl.contact_hint, rl.signal_source,
               b.city AS client_city
          FROM research_leads rl
          LEFT JOIN businesses b ON b.id = rl.client_business_id
         ORDER BY rl.fit DESC, rl.created_at DESC
         LIMIT ?
    """
    return conn.execute(sql, (limit,)).fetchall()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the changes")
    ap.add_argument("--limit", type=int, default=1000)
    args = ap.parse_args(argv)

    init_db()
    conn = connect()
    changed = 0
    try:
        rows = _rows(conn, args.limit)
        print(f"{len(rows)} lead(s) to check")
        for row in rows:
            verdict = leadguard.check_lead(
                {"name": row["name"], "domain": row["domain"],
                 "signal_source": row["signal_source"]},
                locality=row["client_city"] or "")
            new_domain = verdict["domain"]
            hint, dropped = leadguard.sanitize_hint(row["contact_hint"] or "")
            notes = [n for n in (verdict["note"],
                                 (f"{len(dropped)} address(es) removed from hint" if dropped else ""))
                     if n]

            domain_changed = leadguard.bare_host(row["domain"]) != leadguard.bare_host(new_domain)
            if not (domain_changed or dropped):
                continue
            changed += 1
            print(f"\n{row['name']}")
            if domain_changed:
                print(f"  domain: {row['domain']!r} -> {new_domain or 'NULL'}")
            if dropped:
                print(f"  hint:   dropped {', '.join(dropped)}")
            for n in notes:
                print(f"  note:   {n[:160]}")

            if args.apply:
                conn.execute(
                    """UPDATE research_leads
                          SET domain = ?, contact_hint = ?, evidence_note = ?
                        WHERE id = ?""",
                    (new_domain or None, hint,
                     " | ".join(notes)[:400] or None, row["id"]))
        if args.apply:
            conn.commit()
            print(f"\napplied to {changed} lead(s)")
        else:
            print(f"\n{changed} lead(s) would change (dry run, nothing written)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
