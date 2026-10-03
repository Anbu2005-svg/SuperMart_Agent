"""Local Database Clone & Offline Adapter for SuperMart Ops Agent.

Clones the schema and data from the remote Prisma Cloud PostgreSQL database
into a local, zero-latency SQLite database file (supermarket_local.db).

Provides a drop-in `get_local_db_connection()` that matches psycopg2's RealDictCursor
interface so all skills can run 100% locally without consuming any Prisma Cloud quota.
"""

import os
import sys
import re
import json
import sqlite3
import datetime
from pathlib import Path
from contextlib import contextmanager
from typing import Dict, Any, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

LOCAL_DB_PATH = PROJECT_ROOT / "supermarket_local.db"


class LocalCursorProxy:
    """Wraps an sqlite3.Cursor to provide psycopg2-compatible parameter binding & dict access."""

    def __init__(self, cursor: sqlite3.Cursor):
        self._cur = cursor

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    def _convert_query(self, query: str) -> str:
        # Strip PostgreSQL-specific locking and casting
        q = re.sub(r"\bFOR\s+UPDATE\b", "", query, flags=re.IGNORECASE)
        q = re.sub(r"::\w+", "", q)
        # Convert %s placeholders to ? placeholders
        q = q.replace("%s", "?")
        # Convert %(name)s placeholders to :name
        q = re.sub(r"%\((\w+)\)s", r":\1", q)
        # Convert NULLS LAST/FIRST (unsupported in older sqlite)
        q = re.sub(r"\bNULLS\s+(LAST|FIRST)\b", "", q, flags=re.IGNORECASE)
        # Convert ILIKE to LIKE (SQLite LIKE is case-insensitive for ASCII)
        q = re.sub(r"\bILIKE\b", "LIKE", q, flags=re.IGNORECASE)
        # Convert ON CONFLICT (sku_id) DO UPDATE SET EXCLUDED.x
        return q

    def execute(self, query: str, params=None):
        clean_query = self._convert_query(query)
        if params is None:
            return self._cur.execute(clean_query)
        return self._cur.execute(clean_query, params)

    def executemany(self, query: str, seq_of_params):
        clean_query = self._convert_query(query)
        return self._cur.executemany(clean_query, seq_of_params)

    def fetchone(self) -> Optional[Dict[str, Any]]:
        row = self._cur.fetchone()
        if row is None:
            return None
        return dict(row)

    def fetchall(self) -> List[Dict[str, Any]]:
        rows = self._cur.fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self._cur.close()


class LocalConnectionProxy:
    """Wraps an sqlite3.Connection to provide psycopg2 Connection semantics."""

    def __init__(self, raw_conn: sqlite3.Connection):
        self._conn = raw_conn
        self._conn.row_factory = sqlite3.Row

    def cursor(self, *args, **kwargs):
        return LocalCursorProxy(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    @property
    def closed(self) -> bool:
        try:
            self._conn.cursor()
            return False
        except Exception:
            return True


@contextmanager
def local_immediate_transaction(conn):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_local_db_connection() -> LocalConnectionProxy:
    """Returns an isolated, local connection to supermarket_local.db with WAL mode."""
    conn = sqlite3.connect(str(LOCAL_DB_PATH), timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return LocalConnectionProxy(conn)


def init_local_sqlite_schema() -> None:
    """Create all supermarket tables in the local SQLite database."""
    conn = sqlite3.connect(str(LOCAL_DB_PATH))
    cur = conn.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS products (
        sku_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        unit TEXT NOT NULL,
        base_unit TEXT DEFAULT 'piece',
        conversion_factor REAL DEFAULT 1.0,
        price_per_base_unit REAL,
        is_loose INTEGER DEFAULT 0,
        cost_price REAL NOT NULL,
        mrp REAL NOT NULL,
        gst_slab REAL NOT NULL DEFAULT 0,
        hsn_code TEXT,
        quantity REAL NOT NULL DEFAULT 0,
        reorder_level REAL NOT NULL DEFAULT 10,
        barcode TEXT UNIQUE,
        is_active INTEGER DEFAULT 1,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode);

    CREATE TABLE IF NOT EXISTS customers (
        customer_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        khata_balance REAL DEFAULT 0,
        credit_limit REAL DEFAULT 0,
        phone TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS stock_batches (
        batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
        sku_id TEXT NOT NULL,
        batch_code TEXT NOT NULL,
        qty_received REAL NOT NULL,
        qty_remaining REAL NOT NULL,
        cost_price REAL NOT NULL,
        mrp REAL NOT NULL,
        expiry_date TEXT,
        received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(sku_id) REFERENCES products(sku_id)
    );
    CREATE INDEX IF NOT EXISTS idx_batches_expiry ON stock_batches(sku_id, expiry_date);

    CREATE TABLE IF NOT EXISTS bills (
        bill_id TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'draft',
        customer_id INTEGER,
        payment_mode TEXT,
        payment_ref TEXT,
        subtotal REAL DEFAULT 0,
        cgst REAL DEFAULT 0,
        sgst REAL DEFAULT 0,
        total REAL DEFAULT 0,
        invoice_number INTEGER,
        place_of_supply TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        finalized_at TIMESTAMP,
        FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
    );

    CREATE TABLE IF NOT EXISTS bill_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bill_id TEXT NOT NULL,
        sku_id TEXT NOT NULL,
        qty REAL NOT NULL,
        unit_price REAL NOT NULL,
        gst_slab REAL NOT NULL DEFAULT 0,
        line_total REAL NOT NULL,
        FOREIGN KEY(bill_id) REFERENCES bills(bill_id),
        FOREIGN KEY(sku_id) REFERENCES products(sku_id)
    );

    CREATE TABLE IF NOT EXISTS returns (
        return_id INTEGER PRIMARY KEY AUTOINCREMENT,
        bill_id TEXT NOT NULL,
        sku_id TEXT NOT NULL,
        qty REAL NOT NULL,
        refund_amount REAL NOT NULL,
        return_type TEXT NOT NULL,
        customer_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS khata_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        amount REAL NOT NULL,
        bill_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
    );

    CREATE TABLE IF NOT EXISTS preferences (
        owner_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY (owner_id, key)
    );

    CREATE TABLE IF NOT EXISTS idempotency_log (
        update_id TEXT PRIMARY KEY,
        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS shops (
        shop_id INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_name TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        shop_address TEXT,
        shop_gstin TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS user_sessions (
        telegram_id TEXT PRIMARY KEY,
        shop_id INTEGER NOT NULL,
        authenticated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS authenticated_users (
        telegram_id TEXT PRIMARY KEY,
        phone_number TEXT,
        authenticated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        entity_type TEXT,
        entity_id TEXT,
        details TEXT,
        old_value REAL,
        new_value REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_id);
    CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event_type);
    """)

    conn.commit()
    conn.close()


def clone_prisma_to_local() -> Dict[str, int]:
    """Perform a one-time read from Prisma Cloud to clone all live data into local SQLite."""
    from db.models import get_db_connection

    print("📦 Initializing local SQLite clone schema...")
    init_local_sqlite_schema()

    print("🌐 Fetching current state from Prisma Cloud PostgreSQL (single read pass)...")
    cloud_conn = get_db_connection()
    try:
        cur = cloud_conn.cursor()
        cur.execute("SELECT * FROM products")
        products = cur.fetchall()

        cur.execute("SELECT * FROM customers")
        customers = cur.fetchall()

        cur.execute("SELECT * FROM stock_batches")
        batches = cur.fetchall()

        cur.execute("SELECT * FROM preferences")
        preferences = cur.fetchall()

        cur.close()
    finally:
        cloud_conn.close()

    print(f"📥 Inserting {len(products)} products and {len(customers)} customers into local SQLite...")
    local_conn = sqlite3.connect(str(LOCAL_DB_PATH))
    lcur = local_conn.cursor()

    # Clear previous local clone rows
    lcur.execute("DELETE FROM bill_items")
    lcur.execute("DELETE FROM bills")
    lcur.execute("DELETE FROM stock_batches")
    lcur.execute("DELETE FROM products")
    lcur.execute("DELETE FROM customers")

    # Insert products
    for p in products:
        lcur.execute("""
            INSERT INTO products (sku_id, name, category, unit, base_unit, conversion_factor,
                                  price_per_base_unit, is_loose, cost_price, mrp, gst_slab,
                                  hsn_code, quantity, reorder_level, barcode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            p["sku_id"], p["name"], p["category"], p["unit"],
            p.get("base_unit") or "piece", p.get("conversion_factor") or 1.0,
            p.get("price_per_base_unit") or p["mrp"], 1 if p.get("is_loose") else 0,
            float(p["cost_price"]), float(p["mrp"]), float(p.get("gst_slab", 0.0)),
            p.get("hsn_code"), float(p["quantity"]), float(p.get("reorder_level", 10.0)),
            p.get("barcode")
        ))

    # Insert customers
    for c in customers:
        lcur.execute("""
            INSERT INTO customers (customer_id, name, khata_balance, credit_limit, phone)
            VALUES (?, ?, ?, ?, ?)
        """, (
            c.get("customer_id"), c["name"], float(c.get("khata_balance", 0.0)),
            float(c.get("credit_limit", 0.0)), c.get("phone")
        ))

    # Insert stock batches
    for b in batches:
        lcur.execute("""
            INSERT INTO stock_batches (batch_id, sku_id, batch_code, qty_received, qty_remaining,
                                       cost_price, mrp, expiry_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            b.get("batch_id"), b["sku_id"], b["batch_code"],
            float(b["qty_received"]), float(b["qty_remaining"]),
            float(b["cost_price"]), float(b["mrp"]), str(b.get("expiry_date")) if b.get("expiry_date") else None
        ))

    local_conn.commit()
    local_conn.close()

    counts = {
        "products": len(products),
        "customers": len(customers),
        "stock_batches": len(batches),
    }
    print(f"✅ Local database cloned successfully at: {LOCAL_DB_PATH}")
    print(f"   • Products: {counts['products']}")
    print(f"   • Customers: {counts['customers']}")
    print(f"   • Stock Batches: {counts['stock_batches']}")
    return counts


if __name__ == "__main__":
    clone_prisma_to_local()
