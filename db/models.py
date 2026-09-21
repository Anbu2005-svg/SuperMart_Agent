import os
import threading
import psycopg2
import psycopg2.extras
import psycopg2.pool
from contextlib import contextmanager
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
DEFAULT_DB_PATH = os.getenv("DB_PATH", "supermarket.db")  # legacy compat attribute

_POOL: Optional["psycopg2.pool.ThreadedConnectionPool"] = None
_POOL_LOCK = threading.Lock()


class _PooledConnection:
    """Transparent wrapper around a pooled psycopg2 connection.

    Delegates cursor()/commit()/rollback() to the real connection, but `close()`
    returns the connection to the pool (rolling back any dangling transaction
    first) instead of physically closing it. This lets every skill keep its
    existing `conn = get_db_connection() ... finally: conn.close()` pattern
    while connections are actually recycled through the pool.
    """

    def __init__(self, pool, conn):
        object.__setattr__(self, "_pool", pool)
        object.__setattr__(self, "_conn", conn)

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        try:
            if not self._conn.closed:
                # Roll back any uncommitted work so the pooled connection is clean.
                self._conn.rollback()
        except Exception:
            # If rollback fails (e.g. broken TCP socket), discard rather than re-pooling
            try:
                self._pool.putconn(self._conn, close=True)
            except Exception:
                pass
            return
        try:
            self._pool.putconn(self._conn)
        except Exception:
            pass

    @property
    def autocommit(self):
        return self._conn.autocommit

    @autocommit.setter
    def autocommit(self, value):
        self._conn.autocommit = value

    @property
    def closed(self):
        return self._conn.closed

    def __getattr__(self, item):
        return getattr(self._conn, item)


def _get_pool() -> "psycopg2.pool.ThreadedConnectionPool":
    """Lazily create (once) and return the thread-safe connection pool."""
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                if not DATABASE_URL:
                    raise ValueError("DATABASE_URL is not set in environment variables!")
                _POOL = psycopg2.pool.ThreadedConnectionPool(
                    minconn=2,
                    maxconn=15,
                    dsn=DATABASE_URL,
                    cursor_factory=psycopg2.extras.RealDictCursor
                )
    return _POOL


def get_db_connection():
    """
    Returns a validated, pooled connection (RealDictCursor rows).
    If a connection was dropped by cloud PostgreSQL during idle periods,
    it is automatically pruned and a healthy connection is provided.
    `close()` recycles it to the pool.
    """
    pool = _get_pool()
    for attempt in range(4):
        conn = None
        try:
            conn = pool.getconn()
            if conn.closed:
                pool.putconn(conn, close=True)
                continue
            # Fast ping to guarantee socket is alive
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.rollback()
            return _PooledConnection(pool, conn)
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            if conn is not None:
                try:
                    pool.putconn(conn, close=True)
                except Exception:
                    pass
            if attempt == 2:
                # If multiple pooled connections died during idle sleep, re-create pool
                dispose_pool()
                pool = _get_pool()
        except Exception:
            if conn is not None:
                try:
                    pool.putconn(conn, close=True)
                except Exception:
                    pass
            raise

    # Fallback to direct connection if pool is completely exhausted
    direct_conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    direct_conn.autocommit = False
    return direct_conn


def dispose_pool():
    """Close every pooled connection (used on shutdown / between test suites)."""
    global _POOL
    if _POOL is not None:
        with _POOL_LOCK:
            if _POOL is not None:
                _POOL.closeall()
                _POOL = None


def init_db(schema_file: Optional[str] = None):
    """Initializes PostgreSQL database using postgres_schema.sql (idempotent)."""
    if schema_file is None:
        schema_file = os.path.join(os.path.dirname(__file__), "postgres_schema.sql")

    with open(schema_file, "r", encoding="utf-8") as f:
        sql_script = f.read()

    # Use a direct connection for DDL initialization (never from the pool).
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute(sql_script)
        conn.commit()
        cur.close()
    finally:
        conn.close()


@contextmanager
def immediate_transaction(conn):
    """
    Context manager for atomic PostgreSQL transactions.
    Commits on success, rolls back on exception.
    Accepts the pooled connection proxy — commit/rollback delegate to the real connection.
    """
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
