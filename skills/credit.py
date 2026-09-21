import math
from typing import Dict, Any, List, Optional
from db.models import get_db_connection, immediate_transaction
from skills.audit import _log_event


def _get_customer_by_name(conn, name: str) -> Optional[Any]:
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers WHERE name ILIKE %s", (f"%{name.strip()}%",))
    row = cur.fetchone()
    cur.close()
    return row


def charge_khata(customer_name: str, amount: float, bill_id: Optional[str] = None) -> Dict[str, Any]:
    """Add a credit charge to a customer's khata ledger. Enforces credit limit if set (>0)."""
    if not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
        return {"status": "error", "message": "Charge amount must be a positive finite number."}

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            # Lock the customer row so balance + limit checks are race-free
            cur.execute("""
                SELECT * FROM customers WHERE name ILIKE %s FOR UPDATE
            """, (f"%{customer_name.strip()}%",))
            customer = cur.fetchone()
            if not customer:
                return {
                    "status": "error",
                    "error_type": "CustomerNotFound",
                    "message": f"Customer '{customer_name}' not found in credit ledger. Create the customer record first or check spelling."
                }

            # Credit limit check (0 = unlimited)
            credit_limit = customer.get("credit_limit") or 0
            current_balance = customer["khata_balance"]
            projected_balance = current_balance + amount

            if credit_limit > 0 and projected_balance > credit_limit:
                return {
                    "status": "error",
                    "error_type": "CreditLimitExceeded",
                    "message": (f"Credit limit exceeded for {customer['name']}. "
                                f"Limit: ₹{credit_limit:.2f}, Current: ₹{current_balance:.2f}, "
                                f"Requested: ₹{amount:.2f}, Would become: ₹{projected_balance:.2f}"),
                    "credit_limit": credit_limit,
                    "current_balance": current_balance,
                    "requested_amount": amount,
                    "projected_balance": projected_balance
                }

            cid = customer["customer_id"]
            cur.execute("""
                INSERT INTO khata_transactions (customer_id, type, amount, bill_id)
                VALUES (%s, 'charge', %s, %s)
            """, (cid, amount, bill_id))

            cur.execute("""
                UPDATE customers
                SET khata_balance = khata_balance + %s, updated_at = CURRENT_TIMESTAMP
                WHERE customer_id = %s
            """, (amount, cid))

            cur.execute("SELECT khata_balance FROM customers WHERE customer_id = %s", (cid,))
            new_balance = cur.fetchone()["khata_balance"]

            _log_event(conn, "KHATA_CHARGED", "customer", customer["name"],
                       details={"amount": amount, "bill_id": bill_id},
                       old_value=customer["khata_balance"], new_value=new_balance)
            cur.close()

        return {
            "status": "success",
            "message": f"Charged ₹{amount:.2f} to {customer['name']}'s khata. New balance: ₹{new_balance:.2f}",
            "customer_name": customer["name"],
            "charged_amount": amount,
            "new_balance": new_balance,
            "credit_limit": credit_limit
        }
    finally:
        conn.close()


def record_payment(customer_name: str, amount: float) -> Dict[str, Any]:
    """Record a credit repayment from a customer to reduce their khata balance."""
    if not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
        return {"status": "error", "message": "Payment amount must be a positive finite number."}

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("SELECT * FROM customers WHERE name ILIKE %s FOR UPDATE", (f"%{customer_name.strip()}%",))
            customer = cur.fetchone()
            if not customer:
                return {
                    "status": "error",
                    "error_type": "CustomerNotFound",
                    "message": f"Customer '{customer_name}' not found in credit ledger. Cannot record payment for a non-existent customer."
                }

            # Guard: cannot overpay beyond outstanding balance
            if amount > customer["khata_balance"] + 0.009:
                return {
                    "status": "error",
                    "error_type": "OverpaymentBlocked",
                    "message": (f"Payment of ₹{amount:.2f} exceeds {customer['name']}'s outstanding balance of "
                                f"₹{customer['khata_balance']:.2f}. Please record the exact or a lower amount.")
                }

            cid = customer["customer_id"]
            cur.execute("""
                INSERT INTO khata_transactions (customer_id, type, amount)
                VALUES (%s, 'payment', %s)
            """, (cid, amount))

            cur.execute("""
                UPDATE customers
                SET khata_balance = khata_balance - %s, updated_at = CURRENT_TIMESTAMP
                WHERE customer_id = %s
            """, (amount, cid))

            cur.execute("SELECT khata_balance FROM customers WHERE customer_id = %s", (cid,))
            new_balance = cur.fetchone()["khata_balance"]

            _log_event(conn, "KHATA_PAYMENT_RECORDED", "customer", customer["name"],
                       details={"amount": amount},
                       old_value=customer["khata_balance"], new_value=new_balance)
            cur.close()

        return {
            "status": "success",
            "message": f"Recorded payment of ₹{amount:.2f} from {customer['name']}. Remaining khata balance: ₹{new_balance:.2f}",
            "customer_name": customer["name"],
            "payment_amount": amount,
            "new_balance": new_balance
        }
    finally:
        conn.close()


def get_khata_balance(customer_name: str) -> Dict[str, Any]:
    """Get current credit (khata) balance and transaction history for a customer."""
    conn = get_db_connection()
    try:
        customer = _get_customer_by_name(conn, customer_name)
        if not customer:
            return {
                "status": "error",
                "error_type": "CustomerNotFound",
                "message": f"Customer '{customer_name}' not found in credit ledger."
            }

        cid = customer["customer_id"]
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM khata_transactions
            WHERE customer_id = %s
            ORDER BY created_at DESC
            LIMIT 10
        """, (cid,))
        txs = cur.fetchall()
        cur.close()

        history = [{
            "id": tx["id"],
            "type": tx["type"],
            "amount": tx["amount"],
            "bill_id": tx["bill_id"],
            "created_at": str(tx["created_at"])
        } for tx in txs]

        return {
            "status": "success",
            "customer_name": customer["name"],
            "khata_balance": customer["khata_balance"],
            "credit_limit": customer.get("credit_limit") or 0,
            "recent_transactions": history
        }
    finally:
        conn.close()


def list_all_khata() -> Dict[str, Any]:
    """List all customers with non-zero khata balance."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM customers WHERE khata_balance != 0 ORDER BY khata_balance DESC")
        customers = cur.fetchall()
        cur.close()

        items = [{
            "customer_id": c["customer_id"],
            "name": c["name"],
            "khata_balance": c["khata_balance"],
            "credit_limit": c.get("credit_limit") or 0
        } for c in customers]

        return {"status": "success", "count": len(items), "khata_ledger": items}
    finally:
        conn.close()


def set_credit_limit(customer_name: str, credit_limit: float) -> Dict[str, Any]:
    """Set or update a customer's credit limit. Use 0 for unlimited."""
    if not isinstance(credit_limit, (int, float)) or not math.isfinite(credit_limit) or credit_limit < 0:
        return {"status": "error", "message": "Credit limit must be a non-negative finite number."}

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("SELECT * FROM customers WHERE name ILIKE %s FOR UPDATE", (f"%{customer_name.strip()}%",))
            customer = cur.fetchone()
            if not customer:
                return {
                    "status": "error",
                    "error_type": "CustomerNotFound",
                    "message": f"Customer '{customer_name}' not found in credit ledger."
                }

            cid = customer["customer_id"]
            cur.execute("""
                UPDATE customers
                SET credit_limit = %s, updated_at = CURRENT_TIMESTAMP
                WHERE customer_id = %s
            """, (credit_limit, cid))

            _log_event(conn, "CREDIT_LIMIT_UPDATED", "customer", customer["name"],
                       details={"credit_limit": credit_limit},
                       old_value=customer.get("credit_limit") or 0, new_value=credit_limit)
            cur.close()

        limit_str = "unlimited" if credit_limit == 0 else f"₹{credit_limit:.2f}"
        return {
            "status": "success",
            "message": f"Credit limit for {customer['name']} set to {limit_str}.",
            "customer_name": customer["name"],
            "credit_limit": credit_limit
        }
    finally:
        conn.close()


def get_customer_details(customer_name: str) -> Dict[str, Any]:
    """Get full customer details including credit limit, balance, and contact info."""
    conn = get_db_connection()
    try:
        customer = _get_customer_by_name(conn, customer_name)
        if not customer:
            return {
                "status": "error",
                "error_type": "CustomerNotFound",
                "message": f"Customer '{customer_name}' not found."
            }

        return {
            "status": "success",
            "customer": {
                "customer_id": customer["customer_id"],
                "name": customer["name"],
                "khata_balance": customer["khata_balance"],
                "credit_limit": customer.get("credit_limit") or 0,
                "phone": customer.get("phone"),
                "address": customer.get("address"),
                "created_at": str(customer.get("created_at")) if customer.get("created_at") else None
            }
        }
    finally:
        conn.close()


def khata_reminders(min_balance: float = 100.0) -> Dict[str, Any]:
    """
    Generate khata payment reminder messages for customers with outstanding
    balances above `min_balance`. The agent can present these so the owner can
    forward them to customers (auto-send is a stretch goal).
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT name, khata_balance, credit_limit, COALESCE(phone, '') AS phone
            FROM customers
            WHERE khata_balance >= %s
            ORDER BY khata_balance DESC
        """, (min_balance,))
        rows = cur.fetchall()
        cur.close()

        reminders = []
        for r in rows:
            msg = (f"Namaste {r['name']}! 🙏 This is a friendly reminder from your kirana store. "
                   f"Your pending khata balance is ₹{r['khata_balance']:.2f}. "
                   f"Kindly clear it at your convenience. Thank you! 🛒")
            reminders.append({
                "customer_name": r["name"],
                "khata_balance": r["khata_balance"],
                "phone": r["phone"] or None,
                "reminder_message": msg
            })

        return {
            "status": "success",
            "count": len(reminders),
            "message": f"Generated {len(reminders)} khata payment reminder(s) for balances above ₹{min_balance:.0f}.",
            "reminders": reminders
        }
    finally:
        conn.close()
