"""Pytest bootstrap: point the app at a throwaway database before it is imported."""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["LOCALREACH_DB"] = str(ROOT / "test.db")
os.environ["DRY_RUN"] = "1"
os.environ["UNSUB_SECRET"] = "test-secret"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
# WAL leaves -wal/-shm siblings behind; unlinking only the .db lets a stale
# journal shadow the fresh database between runs.
for suffix in ("", "-wal", "-shm"):
    pathlib.Path(os.environ["LOCALREACH_DB"] + suffix).unlink(missing_ok=True)
