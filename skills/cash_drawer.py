"""
Cash Drawer & Petty Cash Management Skill for SuperMart AI Ops Agent.

Provides shift-based cash drawer management:
  - Opening float balance tracking
  - Real-time petty cash tracking (tea/snacks, cleaning, stationery, courier, misc, owner cash drops)
  - Live cash reconciliation (opening float + cash sales - cash expenses)
  - End-of-shift closing count, variance detection, and audit trail
"""

import math
from typing import Dict, Any, List, Optional
from datetime import datetime
from db.models import get_db_connection, immediate_transaction
from skills.audit import _log_event


def open_cash_drawer(
    opening_cash: float,
    opened_by: str = "Cashier",
    notes: Optional[str] = None
) -> Dict[str, Any]:
    """
    Open a new cash drawer shift session with an initial cash float balance.
    Blocks opening if an active open drawer session already exists.
    """
    if not isinstance(opening_cash, (int, float)) or not math.isfinite(opening_cash) or opening_cash < 0:
        return {"status": "error", "message": "Opening cash float must be a non-negative number."}

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            # Check for existing open session
            cur.execute("SELECT * FROM cash_drawer_sessions WHERE status = 'open' LIMIT 1 FOR UPDATE")
            existing = cur.fetchone()
            if existing:
                cur.close()
                return {
                    "status": "error",
                    "message": f"Cash drawer is already open (Session #{existing['session_id']}, opened at {existing['opened_at']} by {existing['opened_by']}). Please close the current session first."
                }

            cur.execute("""
                INSERT INTO cash_drawer_sessions (opened_by, opening_cash, status, notes)
                VALUES (%s, %s, 'open', %s)
                RETURNING session_id, opened_at
            """, (opened_by.strip(), opening_cash, notes))
            new_sess = cur.fetchone()
            session_id = new_sess["session_id"]
            opened_at = new_sess["opened_at"]

            _log_event(conn, "CASH_DRAWER_OPENED", "cash_drawer", str(session_id),
                       details={"opened_by": opened_by, "opening_cash": opening_cash, "notes": notes},
                       new_value=opening_cash)
            cur.close()

        return {
            "status": "success",
            "message": f"✅ Cash drawer opened successfully! Session #{session_id} initialized with ₹{opening_cash:.2f} float.",
            "session_id": session_id,
            "opening_cash": opening_cash,
            "opened_by": opened_by,
            "opened_at": str(opened_at)
        }
    finally:
        conn.close()


def record_petty_cash(
    amount: float,
    category: str = "misc",
    expense_type: str = "expense",
    paid_to: Optional[str] = None,
    recorded_by: str = "Cashier",
    notes: Optional[str] = None
) -> Dict[str, Any]:
    """
    Record petty cash in or out of the active cash drawer.
    - expense_type: 'expense' (cash out), 'cash_in' (cash added to drawer), 'cash_drop' (bank transfer / safe deposit)
    - category: 'tea_snacks', 'cleaning', 'stationery', 'courier', 'misc', 'owner_withdrawal'
    """
    if not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
        return {"status": "error", "message": "Petty cash amount must be a positive number."}

    valid_types = {"expense", "cash_in", "cash_drop"}
    exp_type = expense_type.strip().lower()
    if exp_type not in valid_types:
        return {"status": "error", "message": f"Invalid expense_type. Must be one of: {', '.join(valid_types)}"}

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("SELECT session_id, status FROM cash_drawer_sessions WHERE status = 'open' ORDER BY session_id DESC LIMIT 1")
            sess = cur.fetchone()
            session_id = sess["session_id"] if sess else None

            cur.execute("""
                INSERT INTO petty_cash_expenses (session_id, expense_type, category, amount, paid_to, recorded_by, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, created_at
            """, (session_id, exp_type, category.strip().lower(), amount, paid_to, recorded_by.strip(), notes))
            rec = cur.fetchone()
            petty_id = rec["id"]

            _log_event(conn, "PETTY_CASH_RECORDED", "petty_cash", str(petty_id),
                       details={"type": exp_type, "category": category, "amount": amount, "paid_to": paid_to, "session_id": session_id})
            cur.close()

        direction = "added to" if exp_type == "cash_in" else "paid out from"
        return {
            "status": "success",
            "message": f"Recorded petty cash of ₹{amount:.2f} ({exp_type}) {direction} cash drawer. Category: {category}.",
            "petty_cash_id": petty_id,
            "session_id": session_id,
            "expense_type": exp_type,
            "category": category,
            "amount": amount,
            "paid_to": paid_to
        }
    finally:
        conn.close()


def get_cash_drawer_status() -> Dict[str, Any]:
    """
    Get live balance and reconciliation status for the current active cash drawer shift.
    Calculates:
      - Opening float
      - Total cash sales collected from finalized bills during this session
      - Total cash added (cash_in)
      - Total petty expenses (expense)
      - Total cash drops / bank deposits
      = Expected physical cash in drawer right now
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM cash_drawer_sessions WHERE status = 'open' ORDER BY session_id DESC LIMIT 1")
        session = cur.fetchone()

        if not session:
            cur.close()
            return {
                "status": "no_open_session",
                "message": "There is no active open cash drawer session right now. Open one with open_cash_drawer()."
            }

        sess_id = session["session_id"]
        opened_at = session["opened_at"]
        opening_cash = float(session["opening_cash"] or 0.0)

        # 1. Total cash sales from finalized bills since session opened
        cur.execute("""
            SELECT COALESCE(SUM(total), 0) AS total_cash_sales, COUNT(*) AS bill_count
            FROM bills
            WHERE status = 'finalized'
              AND payment_mode = 'cash'
              AND finalized_at >= %s
        """, (opened_at,))
        sales_row = cur.fetchone()
        cash_sales = float(sales_row["total_cash_sales"] or 0.0)
        cash_bill_count = int(sales_row["bill_count"] or 0)

        # 2. Petty cash transactions in this session
        cur.execute("""
            SELECT 
                COALESCE(SUM(CASE WHEN expense_type = 'expense' THEN amount ELSE 0 END), 0) AS total_expenses,
                COALESCE(SUM(CASE WHEN expense_type = 'cash_in' THEN amount ELSE 0 END), 0) AS total_cash_in,
                COALESCE(SUM(CASE WHEN expense_type = 'cash_drop' THEN amount ELSE 0 END), 0) AS total_cash_drop
            FROM petty_cash_expenses
            WHERE session_id = %s
        """, (sess_id,))
        petty_row = cur.fetchone()
        petty_expenses = float(petty_row["total_expenses"] or 0.0)
        cash_in = float(petty_row["total_cash_in"] or 0.0)
        cash_drop = float(petty_row["total_cash_drop"] or 0.0)

        # Expected Cash Formula:
        # Float + Cash Sales + Cash In - Petty Expenses - Cash Drops
        expected_cash = round(opening_cash + cash_sales + cash_in - petty_expenses - cash_drop, 2)

        # Fetch recent 5 petty expenses
        cur.execute("""
            SELECT id, expense_type, category, amount, paid_to, recorded_by, created_at
            FROM petty_cash_expenses
            WHERE session_id = %s
            ORDER BY created_at DESC
            LIMIT 5
        """, (sess_id,))
        recent_expenses = cur.fetchall()
        cur.close()

        return {
            "status": "success",
            "session_id": sess_id,
            "drawer_status": "open",
            "opened_by": session["opened_by"],
            "opened_at": str(opened_at),
            "reconciliation": {
                "opening_float": opening_cash,
                "cash_sales_revenue": cash_sales,
                "cash_sales_bills_count": cash_bill_count,
                "cash_in": cash_in,
                "petty_cash_expenses": petty_expenses,
                "cash_drops": cash_drop,
                "expected_cash_in_drawer": expected_cash
            },
            "recent_petty_expenses": [
                {
                    "id": e["id"],
                    "type": e["expense_type"],
                    "category": e["category"],
                    "amount": e["amount"],
                    "paid_to": e["paid_to"],
                    "time": str(e["created_at"])[:16]
                } for e in recent_expenses
            ]
        }
    finally:
        conn.close()


def close_cash_drawer(
    closing_cash_counted: float,
    closed_by: str = "Cashier",
    notes: Optional[str] = None
) -> Dict[str, Any]:
    """
    Close the active cash drawer shift session.
    Reconciles counted cash against expected cash and records variance:
      - variance = closing_cash_counted - expected_cash
      - variance = 0: perfectly balanced
      - variance > 0: cash surplus
      - variance < 0: cash shortage
    """
    if not isinstance(closing_cash_counted, (int, float)) or not math.isfinite(closing_cash_counted) or closing_cash_counted < 0:
        return {"status": "error", "message": "Closing cash counted must be a non-negative number."}

    status_info = get_cash_drawer_status()
    if status_info.get("status") != "success":
        return {"status": "error", "message": "No active open cash drawer session to close."}

    session_id = status_info["session_id"]
    expected = status_info["reconciliation"]["expected_cash_in_drawer"]
    variance = round(closing_cash_counted - expected, 2)

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("""
                UPDATE cash_drawer_sessions
                SET closed_by = %s,
                    closed_at = CURRENT_TIMESTAMP,
                    closing_cash_counted = %s,
                    expected_cash = %s,
                    variance = %s,
                    status = 'closed',
                    notes = COALESCE(notes, '') || %s
                WHERE session_id = %s
            """, (closed_by.strip(), closing_cash_counted, expected, variance, f" | Close notes: {notes}" if notes else "", session_id))

            _log_event(conn, "CASH_DRAWER_CLOSED", "cash_drawer", str(session_id),
                       details={"closed_by": closed_by, "counted": closing_cash_counted, "expected": expected, "variance": variance},
                       old_value=expected, new_value=closing_cash_counted)
            cur.close()

        variance_desc = "Perfectly Balanced (₹0.00)"
        if variance > 0:
            variance_desc = f"⚠️ Cash Surplus (+₹{variance:.2f})"
        elif variance < 0:
            variance_desc = f"🚨 Cash Shortage (-₹{abs(variance):.2f})"

        return {
            "status": "success",
            "message": f"Cash drawer Session #{session_id} closed successfully! Expected: ₹{expected:.2f} | Counted: ₹{closing_cash_counted:.2f} | Result: {variance_desc}",
            "session_id": session_id,
            "closing_cash_counted": closing_cash_counted,
            "expected_cash": expected,
            "variance": variance,
            "variance_status": "balanced" if variance == 0 else ("surplus" if variance > 0 else "shortage")
        }
    finally:
        conn.close()


def list_drawer_sessions(limit: int = 10) -> Dict[str, Any]:
    """Retrieve history of recent cash drawer shifts and variance reports."""
    limit = max(1, min(limit, 50))
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT session_id, opened_by, opened_at, opening_cash,
                   closed_by, closed_at, closing_cash_counted, expected_cash,
                   variance, status, notes
            FROM cash_drawer_sessions
            ORDER BY session_id DESC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        cur.close()

        sessions = []
        for r in rows:
            sessions.append({
                "session_id": r["session_id"],
                "status": r["status"],
                "opened_by": r["opened_by"],
                "opened_at": str(r["opened_at"]) if r["opened_at"] else None,
                "opening_cash": float(r["opening_cash"] or 0),
                "closed_by": r["closed_by"],
                "closed_at": str(r["closed_at"]) if r["closed_at"] else None,
                "closing_cash_counted": float(r["closing_cash_counted"]) if r["closing_cash_counted"] is not None else None,
                "expected_cash": float(r["expected_cash"]) if r["expected_cash"] is not None else None,
                "variance": float(r["variance"]) if r["variance"] is not None else None,
                "notes": r["notes"]
            })

        return {
            "status": "success",
            "count": len(sessions),
            "sessions": sessions
        }
    finally:
        conn.close()
