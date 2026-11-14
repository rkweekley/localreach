"""Is this address actually deliverable?

Verification answers exactly one question: *will this domain accept mail for this
address*. It deliberately does NOT answer "are we allowed to mail it" — role
mailboxes, suppression, the daily cap and the verified-only rule all stay in
`compliance.check_send()`. Mixing the two is how a "verification" feature quietly
becomes a permission to spam.

Lookups go through DNS-over-HTTPS (Cloudflare's JSON API) so there is no new
dependency, no resolver config to get wrong, and works the same in Docker as it
does on a laptop. A DNS failure is never treated as a bad address: it returns
`unverified` with `reason=dns_unavailable`, because the one unforgivable bug in
this module would be marking a real prospect invalid during a network blip.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from .compliance import is_role_address
from .db import utcnow

DOH_URL = "https://cloudflare-dns.com/dns-query"
TIMEOUT = 6.0

# Deliberately short: these are throwaway domains seen in lead lists, not an
# exhaustive list. Anything unknown is treated as normal.
DISPOSABLE = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "trashmail.com", "yopmail.com", "sharklasers.com", "getnada.com",
    "dispostable.com", "throwawaymail.com", "maildrop.cc", "fakeinbox.com",
}

_SYNTAX = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")

# Statuses that may reach an inbox. `risky` is not one of them on purpose.
SENDABLE = {"verified"}


def _doh(name: str, rtype: str) -> tuple[int | None, list[str]]:
    """Return (dns_status, answers). status None means "could not tell".

    Cloudflare returns `{"Status": 0, "Answer": [{"data": "..."}]}`. Status 3 is
    NXDOMAIN (the domain does not exist), 0 with no Answer means the name exists
    but has no record of that type.
    """
    params: dict[str, Any] = {"name": name, "type": rtype}
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(DOH_URL, params=params, headers={"accept": "application/dns-json"})
            r.raise_for_status()
            body = r.json()
    except Exception:  # noqa: BLE001 - any transport/parse failure is "unknown"
        return None, []
    status = body.get("Status")
    if not isinstance(status, int):
        return None, []
    return status, [str(a.get("data") or "") for a in (body.get("Answer") or [])]


def mx_hosts(domain: str) -> tuple[bool, list[str]]:
    """(known, hosts). known=False means the lookup itself failed."""
    status, answers = _doh(domain, "MX")
    if status is None:
        return False, []
    if status == 3:
        return True, []
    hosts = [a.split()[-1].rstrip(".").lower() for a in answers if a.strip()]
    return True, [h for h in hosts if h]


def has_a_record(domain: str) -> bool:
    """No MX but an A record still accepts mail as an implicit MX (RFC 5321 5.1)."""
    status, answers = _doh(domain, "A")
    return bool(status == 0 and answers)


def txt(name: str) -> list[str]:
    status, answers = _doh(name, "TXT")
    if status != 0:
        return []
    return [a.strip('"') for a in answers]


def domain_auth(domain: str) -> dict[str, Any]:
    """SPF/DMARC presence. Informational: it says how the *recipient's* domain is
    set up, which matters for deliverability, never for whether we may send."""
    spf = next((t for t in txt(domain) if t.lower().startswith("v=spf1")), "")
    dmarc = next((t for t in txt(f"_dmarc.{domain}") if t.lower().startswith("v=dmarc1")), "")
    return {"spf": bool(spf), "dmarc": bool(dmarc), "spf_record": spf, "dmarc_record": dmarc}


def verify_address(email: str, source: str = "website") -> dict[str, Any]:
    """Classify one address.

    verified    domain accepts mail and the address was read off a public page
    risky       domain accepts mail but the address is a pattern guess, or there
                is only an implicit MX — real enough to look at, not to send
    invalid     malformed, disposable, dead domain, or no mail exchanger at all
    unverified  DNS could not be reached; retry later, do not judge
    """
    addr = (email or "").strip().lower()
    detail: dict[str, Any] = {"source": source}
    if not _SYNTAX.match(addr):
        return {"status": "invalid", "reason": "bad_syntax", "detail": detail}

    local, domain = addr.split("@", 1)
    detail["domain"] = domain
    detail["role"] = is_role_address(addr)

    if domain in DISPOSABLE:
        return {"status": "invalid", "reason": "disposable", "detail": detail}

    known, hosts = mx_hosts(domain)
    if not known:
        return {"status": "unverified", "reason": "dns_unavailable", "detail": detail}

    detail["mx"] = hosts[:3]
    detail.update(domain_auth(domain))

    if not hosts:
        if has_a_record(domain):
            return {"status": "risky", "reason": "implicit_mx", "detail": detail}
        return {"status": "invalid", "reason": "no_mx", "detail": detail}

    # A role mailbox at a live domain is deliverable — it is the send gate's job
    # to refuse it, not verification's job to call it fake.
    if source == "guessed" or detail.get("guess"):
        return {"status": "risky", "reason": "guessed_pattern", "detail": detail}

    return {"status": "verified", "reason": "mx_ok", "detail": detail}


def apply_verdict(conn, contact_id: str, verdict: dict[str, Any]) -> dict[str, Any]:
    """Write a verdict to a contact. Only moves away from `unverified` for a
    definite answer, so a failed lookup never overwrites an earlier good result."""
    import json

    row = conn.execute("SELECT id, email, email_status FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if not row:
        return {"updated": False, "reason": "not_found"}
    status = verdict["status"]
    if status == "unverified" and (row["email_status"] or "unverified") == "verified":
        return {"updated": False, "reason": "kept_verified", "email_status": "verified"}
    conn.execute(
        "UPDATE contacts SET email_status = ?, verified_at = ?, verify_detail = ? WHERE id = ?",
        (status, utcnow() if status != "unverified" else None,
         json.dumps(verdict.get("detail") or {}, sort_keys=True), contact_id),
    )
    return {
        "updated": True,
        "contact_id": contact_id,
        "email": row["email"],
        "email_status": status,
        "reason": verdict.get("reason"),
    }


def verify_contact(conn, contact_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT id, email, email_source FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if not row:
        return {"updated": False, "reason": "not_found"}
    if not (row["email"] or "").strip():
        return {"updated": False, "reason": "no_email"}
    verdict = verify_address(row["email"], source=(row["email_source"] or "website"))
    return apply_verdict(conn, contact_id, verdict)


def verify_batch(conn, contact_ids: list[str]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for cid in contact_ids:
        res = verify_contact(conn, cid)
        counts[res.get("email_status") or res.get("reason") or "skipped"] = (
            counts.get(res.get("email_status") or res.get("reason") or "skipped", 0) + 1
        )
        out.append(res)
    return {"checked": len(contact_ids), "counts": counts, "results": out}
