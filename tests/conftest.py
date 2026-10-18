import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


import os

import pytest

PG_URL = os.environ.get("IRIIS_TEST_PG_URL", "")


@pytest.fixture(params=["sqlite", "postgres"])
def db(request, tmp_path):
    """A clean database: SQLite always, Postgres when IRIIS_TEST_PG_URL is set."""
    from iriis.storage import DB
    if request.param == "sqlite":
        return DB("", tmp_path / "t.sqlite3")
    if not PG_URL:
        pytest.skip("set IRIIS_TEST_PG_URL to run the Postgres variant")
    d = DB(PG_URL)
    for t in ("facts", "notes", "reminders", "audit_log", "messages", "conversation_summaries"):
        d.execute(f"DELETE FROM {t}")
    return d
