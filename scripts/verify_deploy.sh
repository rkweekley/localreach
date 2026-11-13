#!/usr/bin/env bash
# Post-deploy verification for a live LocalReach instance.
#
#   ./scripts/verify_deploy.sh [BASE_URL]
#
# Defaults to the public HTTPS endpoint. Reads API_TOKEN from ./.env by awk
# (never `source .env` — MAILGUN_SENDER_NAME contains a space).
# Exits non-zero on the first failed assertion so it is usable in CI.
set -uo pipefail
cd "$(dirname "$0")/.."

BASE="${1:-https://reach.cyberalsolutions.com}"
TOKEN="$(awk -F= '/^API_TOKEN=/{print $2}' .env | tr -d '\r')"
[ -n "$TOKEN" ] || { echo "FAIL  API_TOKEN missing from .env"; exit 1; }

fail=0
say() { printf '%-58s %s\n' "$1" "$2"; }
check() { # check <label> <expected> <actual>
  if [ "$2" = "$3" ]; then say "$1" "PASS  ($3)"; else say "$1" "FAIL  expected $2, got $3"; fail=1; fi
}
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$@"; }

check "GET / is a service identity"          200 "$(code "$BASE/")"
check "GET /health"                          200 "$(code "$BASE/health")"
check "GET /ready"                           200 "$(code "$BASE/ready")"
check "GET /api/workspaces without a token"  401 "$(code "$BASE/api/workspaces")"
check "GET /api/workspaces with the token"   200 "$(code -H "Authorization: Bearer $TOKEN" "$BASE/api/workspaces")"
check "GET /docs is not exposed in prod"     404 "$(code "$BASE/docs")"

# TLS: cert must be valid and not near expiry.
days="$(echo | openssl s_client -servername "${BASE#https://}" -connect "${BASE#https://}:443" 2>/dev/null \
  | openssl x509 -noout -checkend 604800 >/dev/null 2>&1 && echo ok || echo bad)"
check "TLS cert valid for >7 days"           ok  "$days"

# Full pipeline through the public API, then assert the rendered message.
if LOCALREACH_URL="$BASE" API_TOKEN="$TOKEN" OUTBOX_DIR=./outbox \
     python3 scripts/e2e_check.py >/tmp/lr-e2e.log 2>&1; then
  say "e2e: workspace -> import -> enroll -> tick -> outbox" "PASS"
else
  say "e2e: workspace -> import -> enroll -> tick -> outbox" "FAIL (see /tmp/lr-e2e.log)"
  tail -20 /tmp/lr-e2e.log
  fail=1
fi

echo "---- containers ----"
docker compose ps --format '{{.Service}}  {{.State}}  {{.Status}}' 2>/dev/null

exit "$fail"
