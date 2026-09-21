"""
Shared test fixtures for the Supermarket Ops Agent test suite.

Requires a PostgreSQL test database. Configure via:
    TEST_DATABASE_URL=postgres://...   (preferred — CI / docker)
    or DATABASE_URL=postgres://...      (fallback — local .env)

If no Postgres is reachable, database-dependent tests are SKIPPED (not failed)
so pure-logic tests (GST math, harness config, unit pricing) still run anywhere.
"""
import os
import uuid
import pytest

# Load .env BEFORE resolving DATABASE_URL (db/models.py loads it too, but only at import)
from dotenv import load_dotenv
load_dotenv()
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


def _resolve_test_db_url() -> str:
    url = os.getenv("TEST_DATABASE_URL", "").strip() or os.getenv("DATABASE_URL", "").strip()
    return url


def _db_available() -> bool:
    url = _resolve_test_db_url()
    if not url:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=5)
        conn.close()
        return True
    except Exception:
        return False


DB_AVAILABLE = _db_available()

# Tables to truncate between tests (order matters for FKs; bills/bill_items via CASCADE)
_TRUNCATE_TABLES = [
    "bill_items",
    "bills",
    "khata_transactions",
    "stock_batches",
    "customers",
    "products",
    "preferences",
    "idempotency_log",
    "audit_log",
    "authenticated_users",
]


def reset_and_seed():
    """Truncate all business tables and re-seed with the standard dataset."""
    from db.models import init_db, get_db_connection
    from db.seed import seed_database

    init_db()  # idempotent — applies new column migrations too

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        for table in _TRUNCATE_TABLES:
            cur.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")
        conn.commit()
        cur.close()
    finally:
        conn.close()

    seed_database()


@pytest.fixture(autouse=True)
def _skip_when_no_db(request):
    """Session-safe guard: skip DB-dependent tests when Postgres is unreachable."""
    if not DB_AVAILABLE and not getattr(request.module, "NO_DB", False):
        pytest.skip("PostgreSQL test database not configured (set TEST_DATABASE_URL)")
    yield


@pytest.fixture(autouse=True)
def clean_seeded_db(request):
    """
    Function-scoped autouse fixture: gives every DB-dependent test a freshly
    truncated + re-seeded database. Pure-logic modules set NO_DB = True.
    """
    if not DB_AVAILABLE or getattr(request.module, "NO_DB", False):
        yield
        return

    reset_and_seed()
    yield


@pytest.fixture
def unique_suffix() -> str:
    """Random suffix helper so parallel test runs never collide on names."""
    return uuid.uuid4().hex[:8]
