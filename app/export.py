"""Paste-in export: the email the mailer would have sent, as text you can copy.

Ryan sends by hand from his own mailbox for now, so the thing that matters is
that the copied text is what the app would actually have sent -- same subject,
same body, same CAN-SPAM footer, and the same tracked unsubscribe link.

That last part is not decoration. The footer is appended at *send* time by the
mailer, so a hand-copied body silently ships without a postal address unless
this module puts it back. And the unsubscribe link is what makes a hand-sent
email still feed `suppressions`: the recipient clicks /u/<token>, the app
records it, and the next run skips them. Copy the body without the footer and
both of those quietly stop working -- which is the exact failure the compliance
layer exists to prevent.

Nothing here invents a recipient or a draft. A lead with no first-touch text
comes back as `has_draft: False` and an empty body, because "copy" must never
turn into "compose on the sender's behalf".
"""
from __future__ import annotations

import csv
import io

from . import compliance
from .config import get_settings

# Short enough to read in a spreadsheet, complete enough that a row can be sent
# without opening the app.
COLUMNS = [
    "lead_id", "business", "send_as", "to", "contact_name", "email_status",
    "city", "state", "website", "subject", "body", "blocked", "block_reason",
]

_SELECT = """
SELECT cl.id AS lead_id, cl.client_business_id,
       cb.name AS client_name, cb.domain AS client_domain,
       cb.address1 AS client_address1, cb.city AS client_city,
       cb.state AS client_state, cb.postal_code AS client_postal,
       b.name, b.city, b.state, b.domain, b.website, b.phone,
       rl.email_subject, rl.email_body, rl.contact_hint,
       ct.email, ct.email_status, ct.full_name AS contact_name
  FROM client_leads cl
  JOIN businesses cb ON cb.id = cl.client_business_id
  JOIN businesses b  ON b.id  = cl.business_id
  LEFT JOIN research_leads rl ON rl.id = cl.research_lead_id
  LEFT JOIN contacts ct ON ct.business_id = b.id AND ct.is_primary = 1
 WHERE cl.workspace_id = ?
"""


def _workspace(conn, workspace_id: str) -> dict:
    row = conn.execute(
        "SELECT id, name, physical_address FROM workspaces WHERE id = ?", (workspace_id,)
    ).fetchone()
    if not row:
        raise KeyError(workspace_id)
    return dict(row)


def _assemble(conn, ws: dict, row) -> dict:
    """Turn one lead row into the text a human would paste.

    Body construction mirrors routes_campaigns.py: the client is the sender in
    lead mode, so the client's name and address identify the message; when we
    have no researched address for them we fall back to the workspace address
    and the footer says it was sent on their behalf.
    """
    subject = (row["email_subject"] or "").strip()
    body = (row["email_body"] or "").strip()
    to = (row["email"] or "").strip()
    has_draft = bool(subject or body)

    warnings: list[str] = []
    blocked = False
    block_reason = ""

    if not has_draft:
        warnings.append("No first touch drafted for this lead yet.")
    if not to:
        warnings.append("No email address on file - this is a call, not an email.")
    elif (row["email_status"] or "") != "verified":
        warnings.append(
            "Address is %s, not verified. A hand-sent bounce still costs reputation."
            % (row["email_status"] or "unchecked")
        )

    if to:
        reason = compliance.suppressed(ws["id"], to)
        if reason:
            blocked = True
            block_reason = reason
            warnings.append("Suppressed (%s) - do not send." % reason)

    footer_included = False
    text = ""
    token = ""
    if has_draft:
        client = {
            "name": row["client_name"],
            "address1": row["client_address1"],
            "city": row["client_city"],
            "state": row["client_state"],
            "postal_code": row["client_postal"],
        }
        if to:
            # A token per recipient is what keeps a hand-sent email feeding the
            # suppression table: the /u/<token> click is the only signal we get.
            token = compliance.unsubscribe_token(ws["id"], to)
            addr = compliance.address_line(client) or ws["physical_address"]
            text = body + compliance.footer_for(
                row["client_name"], addr, token, operator=ws["name"]
            )
            footer_included = True
        else:
            # No recipient, so there is nothing to identify and no link that
            # could ever be clicked. Better a bare draft than a footer pointing
            # at an empty token.
            text = body

    head = []
    if to:
        head.append("To: " + to)
    head.append("Subject: " + (subject or ""))
    copy_text = "\n".join(head) + "\n\n" + text if text else "\n".join(head)

    return {
        "lead_id": row["lead_id"],
        "business": row["name"],
        "send_as": row["client_name"],
        "to": to,
        "contact_name": row["contact_name"] or row["contact_hint"] or "",
        "email_status": row["email_status"] or "unchecked",
        "city": row["city"] or "",
        "state": row["state"] or "",
        "website": row["domain"] or row["website"] or "",
        "subject": subject,
        "body": text,
        "footer_included": footer_included,
        "unsubscribe_url": ("%s/u/%s" % (get_settings().public_base_url, token)) if token else "",
        "has_draft": has_draft,
        "blocked": blocked,
        "block_reason": block_reason,
        "warnings": warnings,
        "copy_text": copy_text,
    }


def lead_email(conn, workspace_id: str, lead_id: str) -> dict | None:
    ws = _workspace(conn, workspace_id)
    row = conn.execute(_SELECT + " AND cl.id = ?", (workspace_id, lead_id)).fetchone()
    if not row:
        return None
    return _assemble(conn, ws, row)


def _cell(value) -> str:
    """Spreadsheet-safe cell.

    A business legitimately named `=HYPERLINK("http://evil","click")` is a live
    formula when Excel opens the file, so identity fields get an apostrophe
    guard. The body is left alone: it is prose meant to be pasted into a mail
    client, and mangling it to protect a spreadsheet nobody will read it in is
    the wrong trade.
    """
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "@", "\t", "\r"):
        return "'" + text
    return text


def leads_csv(conn, workspace_id: str) -> str:
    ws = _workspace(conn, workspace_id)
    rows = conn.execute(_SELECT + " ORDER BY cb.name, b.name", (workspace_id,)).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for row in rows:
        item = _assemble(conn, ws, row)
        writer.writerow([
            _cell(item["lead_id"]), _cell(item["business"]), _cell(item["send_as"]),
            _cell(item["to"]), _cell(item["contact_name"]), _cell(item["email_status"]),
            _cell(item["city"]), _cell(item["state"]), _cell(item["website"]),
            _cell(item["subject"]), item["body"],
            "yes" if item["blocked"] else "",
            _cell(item["block_reason"]),
        ])
    return buf.getvalue()
