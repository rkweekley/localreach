# LocalReach Runbook

Production instance: **https://reach.cyberalsolutions.com**
Host: Mac mini `cyberal@69.133.124.51`, repo at `~/docker/localreach`, Docker behind
Nginx Proxy Manager (proxy host **23**, `mac` external network, upstream `http://localreach:8000`).

## 1. Deploy / upgrade

```bash
ssh cyberal@69.133.124.51
cd ~/docker/localreach
git fetch origin && git reset --hard origin/main   # never edit the box in place
./scripts/deploy.sh                                # build + start, idempotent
./scripts/verify_deploy.sh                         # exits non-zero if anything is wrong
```

`deploy.sh` never rotates secrets. Rotating `UNSUB_SECRET` invalidates every
unsubscribe link already sitting in someone's inbox — only rotate it deliberately,
with that tradeoff accepted.

## 2. Verify

`./scripts/verify_deploy.sh [BASE_URL]` asserts, in order: root identity card 200,
`/health` 200, `/ready` 200, `/api/workspaces` 401 without a token, 200 with one,
`/docs` **404** in production, TLS issuer/expiry, then runs the full
`scripts/e2e_check.py` pipeline (workspace → import → score → sequence → enroll → tick
→ outbox) and greps the dry-run message for `From: info@`, a `/u/u2.` one-click
unsubscribe header, and the absence of `o:tracking-opens`.

Auth boundary is a bearer token: `Authorization: Bearer $API_TOKEN`. `/api/*` without it
is 401, never 404 — a 404 would mean the route vanished.

## 3. Sending identity

| Setting | Value |
|---|---|
| `MAILGUN_DOMAIN` | `cyberalsolutions.com` |
| `MAILGUN_SENDER` | `info@cyberalsolutions.com` |
| `MAILGUN_SENDER_NAME` | `Cyberal Solutions` |
| `MAILGUN_REPLY_TO` | `info@cyberalsolutions.com` |
| `PUBLIC_BASE_URL` | `https://reach.cyberalsolutions.com` |

`info@` is an outbound identity only. The recipient-side role-address gate
(`ALLOW_ROLE_ADDRESSES`, default off) is unrelated: it refuses to *send to*
`info@`/`sales@`/`admin@` prospects, which is where cold outreach gets marked as spam.

## 4. Mailgun domain setup (one-time, blocks live sending)

The domain exists in Mailgun but is **unverified**, so `DRY_RUN=0` sends would be
rejected. DNS is hosted on **Google Cloud DNS** (`ns-cloud-c{1..4}.googledomains.com`).
Publish:

| Type | Name | Value | TTL |
|---|---|---|---|
| TXT | `cyberalsolutions.com` | `v=spf1 include:spf.runbox.com include:mailgun.org -all` | 300 |
| TXT | `k1._domainkey.cyberalsolutions.com` | `k=rsa; p=MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC1x+0DkYz4pNRpwOV5V+PJFe91mERaA9hS3sPSkS6An+8/OQe1eq//jv4MLipTQFCC/mYZmxkBgRQiZJGadHA3ULkquoFXrf+XGrN6ZZKL8gagFREFWbE719cI1deI1tidjprtPii5/GzFhltk+ChcfVXNwJEV7s4GqqkaMNdQxwIDAQAB` | 300 |
| CNAME | `email.cyberalsolutions.com` | `mailgun.org` | 300 |

Rules that matter:

- **Merge the SPF record — do not add a second one.** The current root TXT is
  `v=spf1 include:spf.runbox.com -all`. Two SPF records is a permanent `permerror`.
  Mailgun's dashboard suggests `v=spf1 include:mailgun.org ~all` for a domain that only
  sends through Mailgun; this domain also sends through Runbox, so both includes are
  required. Keeping `-all` is deliberate — it is stricter and both senders are declared.
- **Do not publish Mailgun's MX records.** Mailgun suggests `mxa/mxb.mailgun.org` for
  inbound parsing; `info@` currently delivers to Runbox and must keep doing so. Replies
  belong in the Runbox mailbox, not in a webhook.
- Then verify: `POST /v3/domains/cyberalsolutions.com/verify` (or the dashboard
  "Verify DNS records"), and copy the **webhook signing key** into `.env` as
  `MAILGUN_SIGNING_KEY`. `config.validate()` refuses to boot with `DRY_RUN=0` and no
  signing key, because unsigned webhooks mean bounces/complaints/unsubscribes are
  silently ignored — you would keep mailing people who opted out.

## 5. Going live (deliberately manual)

```bash
cd ~/docker/localreach
# 1. domain must read "active" in Mailgun, DKIM validated
# 2. MAILGUN_SIGNING_KEY present in .env
sed -i '' 's/^DRY_RUN=.*/DRY_RUN=0/' .env
docker compose up -d && ./scripts/verify_deploy.sh
```

Start at a low daily cap and raise it weekly. A brand-new sending domain that opens at
volume gets throttled or blocked; the ramp is what protects deliverability, and no
amount of code fixes a burned domain.

## 6. Backups

Nightly at 03:15 via cron on the Mac (`./scripts/backup.sh`, retention 14 days,
`~/docker/localreach/backups/`). Each run uses SQLite's online backup API (safe under
concurrent writes), then verifies `PRAGMA integrity_check` and a table count **before**
pruning old copies. Log: `/tmp/lr-backup.log`.

Restore:

```bash
cd ~/docker/localreach
docker compose stop
gunzip -c backups/localreach-<STAMP>.db.gz > /tmp/restore.db
docker run --rm -v localreach_localreach-data:/data -v /tmp:/host alpine \
  sh -c 'cp /host/restore.db /data/localreach.db && rm -f /data/localreach.db-wal /data/localreach.db-shm'
docker compose up -d && ./scripts/verify_deploy.sh
```

## 7. Health and failure modes

- `localreach` — web/API, healthcheck on `/health`.
- `localreach-worker` — pull-based send loop, healthcheck on a heartbeat file
  (`/data/worker.heartbeat`, written every pass). The web image's HTTP healthcheck
  would falsely report a healthy worker as unhealthy, so the worker overrides it.
  Stale heartbeat = wedged loop, and Docker restarts it.
- Sends are **claimed before sending** (`claimed_at`/`claimed_by`, 15-minute stale
  window), so a crash mid-send cannot double-mail anyone.
- Retries only on `408, 425, 429, 500, 502, 503, 504`. A hard bounce is a suppression,
  not a retry.
- Logs are JSON per line with a redaction set for tokens and keys:
  `docker compose logs -f localreach | jq -r '.msg'`.

## 8. Rollback

```bash
cd ~/docker/localreach && git reset --hard <previous-sha> && ./scripts/deploy.sh
```
Database migrations are additive, so rolling back the code does not require rolling
back the schema.
