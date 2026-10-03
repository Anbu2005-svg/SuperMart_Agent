"""
Supplier Ledger & Accounts Payable (Vendor Khata) Skill.

Handles end-to-end supplier/distributor credit management:
  - Register suppliers/distributors with GSTIN and contact details
  - Record vendor purchase bills & invoices with due dates
  - Record payments made to vendors (Cash, UPI, NEFT, Cheque)
  - Generate full supplier ledgers with running balance
  - List upcoming and overdue accounts payable to avoid vendor stock disruptions
"""

import math
from datetime import date, datetime, timedelta
from typing import Dict, Any, List, Optional
from db.models import get_db_connection, immediate_transaction


def add_supplier(
    name: str,
    phone: str = "",
    gstin: str = "",
    company_name: str = "",
    address: str = ""
) -> Dict[str, Any]:
    """Register a new supplier or distributor."""
    if not name or not name.strip():
        return {"status": "error", "message": "Supplier name is required."}

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        with immediate_transaction(conn):
            cur.execute("""
                INSERT INTO suppliers (name, phone, gstin, company_name, address)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING supplier_id, name, phone, gstin, company_name;
            """, (name.strip(), phone.strip(), gstin.strip().upper(), company_name.strip(), address.strip()))
            supp = cur.fetchone()

        return {
            "status": "success",
            "message": f"Supplier '{supp['name']}' registered successfully (ID: {supp['supplier_id']}).",
            "supplier": dict(supp)
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to add supplier: {str(e)}"}
    finally:
        conn.close()


def list_suppliers() -> Dict[str, Any]:
    """Retrieve all suppliers and their current outstanding payable balance."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                s.supplier_id, s.name, s.phone, s.company_name, s.gstin,
                COALESCE(SUM(b.total_amount), 0) AS total_billed,
                COALESCE(SUM(b.paid_amount), 0) AS total_paid_bills,
                (
                    SELECT COALESCE(SUM(p.amount), 0)
                    FROM supplier_payments p
                    WHERE p.supplier_id = s.supplier_id
                ) AS total_paid,
                (
                    COALESCE(SUM(b.total_amount), 0) - (
                        SELECT COALESCE(SUM(p.amount), 0)
                        FROM supplier_payments p
                        WHERE p.supplier_id = s.supplier_id
                    )
                ) AS balance_payable
            FROM suppliers s
            LEFT JOIN supplier_bills b ON s.supplier_id = b.supplier_id
            GROUP BY s.supplier_id, s.name, s.phone, s.company_name, s.gstin
            ORDER BY balance_payable DESC, s.name ASC;
        """)
        rows = cur.fetchall()
        suppliers = []
        for r in rows:
            bal = float(r["balance_payable"])
            suppliers.append({
                "supplier_id": r["supplier_id"],
                "name": r["name"],
                "phone": r["phone"] or "N/A",
                "company_name": r["company_name"] or r["name"],
                "gstin": r["gstin"] or "N/A",
                "total_billed": round(float(r["total_billed"]), 2),
                "total_paid": round(float(r["total_paid"]), 2),
                "balance_payable": round(max(0.0, bal), 2)
            })

        total_due_all = sum(s["balance_payable"] for s in suppliers)
        return {
            "status": "success",
            "count": len(suppliers),
            "total_outstanding_payable": round(total_due_all, 2),
            "suppliers": suppliers
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to list suppliers: {str(e)}"}
    finally:
        conn.close()


def _resolve_supplier_id(cur, supplier_identifier: Any) -> Optional[int]:
    """Helper to resolve supplier by numeric ID or name search."""
    if isinstance(supplier_identifier, int) or (isinstance(supplier_identifier, str) and supplier_identifier.isdigit()):
        cur.execute("SELECT supplier_id FROM suppliers WHERE supplier_id = %s", (int(supplier_identifier),))
        row = cur.fetchone()
        if row:
            return row["supplier_id"]

    name_query = str(supplier_identifier).strip()
    cur.execute("SELECT supplier_id FROM suppliers WHERE name ILIKE %s OR company_name ILIKE %s LIMIT 1",
                (f"%{name_query}%", f"%{name_query}%"))
    row = cur.fetchone()
    return row["supplier_id"] if row else None


def record_supplier_bill(
    supplier: Any,
    total_amount: float,
    vendor_bill_no: str = "",
    due_date: Optional[str] = None,
    notes: str = ""
) -> Dict[str, Any]:
    """Record an incoming goods purchase bill/invoice from a vendor."""
    try:
        amt = float(total_amount)
        if not math.isfinite(amt) or amt <= 0:
            return {"status": "error", "message": "total_amount must be a positive number."}
    except (ValueError, TypeError):
        return {"status": "error", "message": "Invalid numeric total_amount."}

    parsed_due = None
    if due_date:
        try:
            parsed_due = datetime.strptime(str(due_date).strip(), "%Y-%m-%d").date()
        except ValueError:
            parsed_due = date.today() + timedelta(days=15)
    else:
        parsed_due = date.today() + timedelta(days=15)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        supp_id = _resolve_supplier_id(cur, supplier)
        if not supp_id:
            return {"status": "error", "message": f"Supplier '{supplier}' not found. Please register the supplier first."}

        with immediate_transaction(conn):
            cur.execute("""
                INSERT INTO supplier_bills (
                    supplier_id, vendor_bill_no, total_amount, paid_amount, due_date, status, notes
                ) VALUES (%s, %s, %s, 0.0, %s, 'unpaid', %s)
                RETURNING bill_id, supplier_id, vendor_bill_no, total_amount, due_date, status;
            """, (supp_id, vendor_bill_no.strip(), amt, parsed_due, notes.strip()))
            bill = cur.fetchone()

        return {
            "status": "success",
            "message": f"Vendor bill recorded successfully (Bill ID: {bill['bill_id']}, Amount: ₹{amt:.2f}, Due: {parsed_due}).",
            "bill": dict(bill)
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to record vendor bill: {str(e)}"}
    finally:
        conn.close()


def record_supplier_payment(
    supplier: Any,
    amount: float,
    payment_mode: str = "cash",
    reference_no: str = "",
    bill_id: Optional[int] = None,
    notes: str = ""
) -> Dict[str, Any]:
    """Record a payment disbursed to a supplier."""
    try:
        amt = float(amount)
        if not math.isfinite(amt) or amt <= 0:
            return {"status": "error", "message": "amount must be a positive number."}
    except (ValueError, TypeError):
        return {"status": "error", "message": "Invalid numeric amount."}

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        supp_id = _resolve_supplier_id(cur, supplier)
        if not supp_id:
            return {"status": "error", "message": f"Supplier '{supplier}' not found."}

        with immediate_transaction(conn):
            cur.execute("""
                INSERT INTO supplier_payments (
                    supplier_id, bill_id, amount, payment_mode, reference_no, notes
                ) VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING payment_id, supplier_id, amount, payment_mode, created_at;
            """, (supp_id, bill_id, amt, payment_mode.lower(), reference_no.strip(), notes.strip()))
            pay = cur.fetchone()

            # If specific bill_id provided, update bill paid status
            if bill_id:
                cur.execute("SELECT total_amount, paid_amount FROM supplier_bills WHERE bill_id = %s", (bill_id,))
                b = cur.fetchone()
                if b:
                    new_paid = float(b["paid_amount"]) + amt
                    new_status = "paid" if new_paid >= float(b["total_amount"]) else "partially_paid"
                    cur.execute("""
                        UPDATE supplier_bills SET paid_amount = %s, status = %s WHERE bill_id = %s
                    """, (new_paid, new_status, bill_id))

        return {
            "status": "success",
            "message": f"Payment of ₹{amt:.2f} to supplier recorded successfully (Payment ID: {pay['payment_id']}).",
            "payment": dict(pay)
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to record supplier payment: {str(e)}"}
    finally:
        conn.close()


def get_supplier_ledger(supplier: Any) -> Dict[str, Any]:
    """Fetch complete vendor ledger with chronological bills, payments, and net balance."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        supp_id = _resolve_supplier_id(cur, supplier)
        if not supp_id:
            return {"status": "error", "message": f"Supplier '{supplier}' not found."}

        cur.execute("SELECT * FROM suppliers WHERE supplier_id = %s", (supp_id,))
        supplier_info = cur.fetchone()

        cur.execute("""
            SELECT bill_id, vendor_bill_no, total_amount, paid_amount, due_date, status, notes, created_at
            FROM supplier_bills WHERE supplier_id = %s
            ORDER BY created_at ASC;
        """, (supp_id,))
        bills = cur.fetchall()

        cur.execute("""
            SELECT payment_id, bill_id, amount, payment_mode, reference_no, notes, created_at
            FROM supplier_payments WHERE supplier_id = %s
            ORDER BY created_at ASC;
        """, (supp_id,))
        payments = cur.fetchall()

        total_billed = sum(float(b["total_amount"]) for b in bills)
        total_paid = sum(float(p["amount"]) for p in payments)
        balance_due = round(total_billed - total_paid, 2)

        return {
            "status": "success",
            "supplier": dict(supplier_info),
            "summary": {
                "total_billed": round(total_billed, 2),
                "total_paid": round(total_paid, 2),
                "balance_payable": round(max(0.0, balance_due), 2),
                "unpaid_bills_count": len([b for b in bills if b["status"] != "paid"])
            },
            "bills": [dict(b) for b in bills],
            "payments": [dict(p) for p in payments]
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to fetch supplier ledger: {str(e)}"}
    finally:
        conn.close()


def get_pending_payables() -> Dict[str, Any]:
    """Retrieve all pending vendor bills categorized into overdue and upcoming."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        today = date.today()

        cur.execute("""
            SELECT 
                b.bill_id, b.vendor_bill_no, b.total_amount, b.paid_amount,
                (b.total_amount - b.paid_amount) AS pending_amount,
                b.due_date, b.status, s.supplier_id, s.name AS supplier_name, s.phone
            FROM supplier_bills b
            JOIN suppliers s ON b.supplier_id = s.supplier_id
            WHERE b.status != 'paid' AND (b.total_amount - b.paid_amount) > 0
            ORDER BY b.due_date ASC;
        """)
        rows = cur.fetchall()

        overdue = []
        upcoming = []
        total_pending = 0.0

        for r in rows:
            amt = float(r["pending_amount"])
            total_pending += amt
            item = {
                "bill_id": r["bill_id"],
                "supplier_name": r["supplier_name"],
                "phone": r["phone"] or "N/A",
                "vendor_bill_no": r["vendor_bill_no"] or "N/A",
                "total_amount": round(float(r["total_amount"]), 2),
                "pending_amount": round(amt, 2),
                "due_date": str(r["due_date"]) if r["due_date"] else "N/A"
            }
            if r["due_date"] and r["due_date"] < today:
                item["days_overdue"] = (today - r["due_date"]).days
                overdue.append(item)
            else:
                upcoming.append(item)

        return {
            "status": "success",
            "total_pending_payables": round(total_pending, 2),
            "overdue_count": len(overdue),
            "upcoming_count": len(upcoming),
            "overdue_bills": overdue,
            "upcoming_bills": upcoming
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to fetch pending payables: {str(e)}"}
    finally:
        conn.close()
