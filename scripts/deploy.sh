#!/usr/bin/env bash
# Deploy LocalReach on the Mac mini. Idempotent: safe to re-run for an upgrade.
#
#   ./scripts/deploy.sh            # build + start
#   ./scripts/deploy.sh --logs     # tail both services
#
# Secrets are generated once and never rotated by this script — rotating
# UNSUB_SECRET silently invalidates unsubscribe links already in inboxes.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_DIR="$(pwd)"
COMPOSE="docker compose"

if [[ "${1:-}" == "--logs" ]]; then
  exec $COMPOSE logs -f --tail=100 localreach worker
fi

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "docker not found"
docker info >/dev/null 2>&1 || die "Docker daemon is not running — open -a Docker and wait for it"

say "checking shared proxy network"
docker network inspect mac >/dev/null 2>&1 \
  || die "network 'mac' missing — it is the shared network nginx-proxy-manager is attached to"

if [[ ! -f .env ]]; then
  say "creating .env with fresh secrets"
  cp .env.example .env
  chmod 600 .env
  # `|` as delimiter: hex secrets can contain `/` and `+` never, but base64 can.
  sed -i '' "s|^API_TOKEN=.*|API_TOKEN=$(openssl rand -hex 32)|" .env
  sed -i '' "s|^UNSUB_SECRET=.*|UNSUB_SECRET=$(openssl rand -hex 32)|" .env
  echo "   generated API_TOKEN and UNSUB_SECRET"
  echo "   still to fill in: MAILGUN_API_KEY, MAILGUN_SIGNING_KEY"
else
  say ".env already present — leaving secrets untouched"
fi

# Refuse to build a deployment that would boot-loop on config validation, so
# the failure is legible here instead of in `docker logs`.
missing=()
grep -q '^MAILGUN_API_KEY=.\+' .env || missing+=("MAILGUN_API_KEY")
if grep -q '^DRY_RUN=0' .env; then
  grep -q '^MAILGUN_SIGNING_KEY=.\+' .env || missing+=("MAILGUN_SIGNING_KEY")
fi
if (( ${#missing[@]} )); then
  say "note: not yet set — ${missing[*]}"
  echo "   DRY_RUN stays 1 until the Mailgun domain is verified, so the stack still starts."
fi

say "building image"
$COMPOSE build

say "starting stack"
$COMPOSE up -d

say "waiting for health"
for i in $(seq 1 30); do
  status=$(docker inspect -f '{{.State.Health.Status}}' localreach 2>/dev/null || echo starting)
  [[ "$status" == "healthy" ]] && break
  sleep 2
done
[[ "$status" == "healthy" ]] || { $COMPOSE logs --tail=40 localreach; die "container did not become healthy"; }

say "smoke test from inside the container"
docker exec localreach python - <<'PY'
import json, os, urllib.request
base = "http://127.0.0.1:8000"
h = urllib.request.urlopen(base + "/health", timeout=5)
print("health:", h.status, h.read().decode())
tok = os.environ["API_TOKEN"]
req = urllib.request.Request(base + "/api/workspaces", headers={"Authorization": "Bearer " + tok})
ws = json.load(urllib.request.urlopen(req, timeout=10))
print("workspaces visible to the API token:", len(ws) if isinstance(ws, list) else ws)
req = urllib.request.Request(base + "/api/workspaces")
try:
    urllib.request.urlopen(req, timeout=5)
    print("AUTH FAILURE: unauthenticated request was allowed")
except urllib.error.HTTPError as e:
    print("unauthenticated request correctly rejected:", e.code)
PY

cat <<EOF

Done. Stack state:
  $(docker ps --format '{{.Names}}  {{.Status}}' | grep localreach | tr '\n' ' ')

Next:
  1. Nginx Proxy Manager -> reach.cyberalsolutions.com -> http://localreach:8000, request a Let's Encrypt cert, enable Websockets off / Block common exploits on.
  2. curl -s https://reach.cyberalsolutions.com/health
  3. Only after Mailgun shows cyberalsolutions.com as verified: set DRY_RUN=0 and MAILGUN_SIGNING_KEY in .env, then re-run this script.
EOF
