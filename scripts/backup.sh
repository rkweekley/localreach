#!/usr/bin/env bash
# Nightly SQLite backup with retention + integrity verification.
#
#   ./scripts/backup.sh [KEEP_DAYS]
#
# Uses sqlite3's online backup API inside the running container, so it is safe
# while the web and worker processes are writing (a plain `cp` of a WAL
# database can capture a torn state). The copy is then integrity-checked here
# before old backups are pruned — a corrupt backup is worse than no backup,
# because it makes you stop looking.
set -euo pipefail
cd "$(dirname "$0")/.."

KEEP="${1:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="backups/localreach-${STAMP}.db"
mkdir -p backups
chmod 700 backups

docker exec localreach python -c "import sqlite3; s=sqlite3.connect('/data/localreach.db'); d=sqlite3.connect('/data/backup.tmp'); s.backup(d); d.close(); s.close()"

docker cp localreach:/data/backup.tmp "$OUT" >/dev/null
docker exec localreach rm -f /data/backup.tmp
chmod 600 "$OUT"

# Verify before trusting it, then compress.
python3 - "$OUT" <<'PY'
import sqlite3, sys
db = sys.argv[1]
conn = sqlite3.connect(db)
ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
rows = conn.execute("SELECT count(*) FROM contacts").fetchone()[0]
conn.close()
assert ok == "ok", f"integrity_check failed: {ok}"
assert tables >= 5, f"only {tables} tables — backup looks empty"
print(f"verified {db}: integrity={ok} tables={tables} contacts={rows}")
PY

gzip -f "$OUT"
find backups -name 'localreach-*.db.gz' -type f -mtime "+${KEEP}" -delete
echo "backup ok: ${OUT}.gz ($(du -h "${OUT}.gz" | cut -f1)) | kept: $(ls -1 backups/*.db.gz | wc -l)"
