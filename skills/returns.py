"""
Returns & Refund Management.

Handles product returns: validates against original bill, reverses stock,
records refund amount, and creates audit trail entries.
"""
import uuid
import logging
from typing import Dict, Any, List, Optional

from db.models import get_db_connection, immediate_transaction
from skills.audit import _log_event

logger = logging.getLogger(__name__)


def process_return(bill_id: str, sku_or_name: str, qty: float,
                   reason: str = "Customer return") -> Dict[str, Any]:
    """
    Process a product return against a finalized bill.
    Validates the item was part of the bill, reverses stock, and records refund.
    """
    import math
    if not bill_id or not isinstance(bill_id, str):
        return {"status": "error", "message": "bill_id is required."}
    if not sku_or_name or not isinstance(sku_or_name, str):
        return {"status": "error", "message": "Product SKU or name is required."}
    if not isinstance(qty, (int, float)) or not math.isfinite(qty) or qty <= 0:
        return {"status": "error", "message": "Return quantity must be a positive number."}

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()

            # Validate the bill exists and was finalized (lock row to serialize concurrent returns)
            cur.execute("SELECT * FROM bills WHERE bill_id = %s FOR UPDATE", (bill_id.strip(),))
            bill = cur.fetchone()
            if not bill:
                cur.close()
                return {"status": "error", "message": f"Bill '{bill_id}' not found."}
            if bill["status"] != "finalized":
                cur.close()
                return {"status": "error", "message": f"Bill '{bill_id}' is not finalized (status: {bill['status']}). Returns only apply to finalized bills."}

            # Find the matching bill item
            cur.execute("""
                SELECT bi.*, p.name, p.unit, p.sku_id AS product_sku
                FROM bill_items bi
                JOIN products p ON bi.sku_id = p.sku_id
                WHERE bi.bill_id = %s AND (p.sku_id = %s OR p.name ILIKE %s)
                LIMIT 1
            """, (bill_id, sku_or_name.strip(), f"%{sku_or_name.strip()}%"))
            bill_item = cur.fetchone()
            if not bill_item:
                cur.close()
                return {"status": "error", "message": f"Product '{sku_or_name}' was not found in bill '{bill_id}'."}

            # Check return qty doesn't exceed billed qty
            billed_qty = bill_item["qty"]

            # Check already returned qty
            cur.execute("""
                SELECT COALESCE(SUM(qty), 0) AS already_returned
                FROM returns WHERE bill_id = %s AND sku_id = %s
            """, (bill_id, bill_item["product_sku"]))
            already_returned = cur.fetchone()["already_returned"]
            available_to_return = billed_qty - already_returned

            if qty > available_to_return:
                cur.close()
                return {
                    "status": "error",
                    "message": (f"Cannot return {qty} {bill_item['unit']}. "
                                f"Billed: {billed_qty}, Already returned: {already_returned}, "
                                f"Available to return: {available_to_return}")
                }

            # Calculate refund amount (proportional)
            unit_price_with_gst = bill_item["line_total"] / bill_item["qty"]
            refund_amount = round(qty * unit_price_with_gst, 2)

            # Generate return ID
            return_id = f"RET-{uuid.uuid4().hex[:8].upper()}"

            # Record the return
            cur.execute("""
                INSERT INTO returns (return_id, bill_id, sku_id, qty, refund_amount, reason)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (return_id, bill_id, bill_item["product_sku"], qty, refund_amount, reason or "Customer return"))

            # Reverse stock — add quantity back to inventory
            cur.execute("""
                UPDATE products SET quantity = quantity + %s, updated_at = CURRENT_TIMESTAMP
                WHERE sku_id = %s
            """, (qty, bill_item["product_sku"]))

            # Log audit event
            _log_event(
                event_type="RETURN_PROCESSED",
                entity_type="return",
                entity_id=return_id,
                details={
                    "bill_id": bill_id,
                    "sku_id": bill_item["product_sku"],
                    "product_name": bill_item["name"],
                    "qty_returned": qty,
                    "refund_amount": refund_amount,
                    "reason": reason
                },
                old_value=0,
                new_value=refund_amount,
                conn=conn
            )

            cur.close()

        return {
            "status": "success",
            "return_id": return_id,
            "bill_id": bill_id,
            "product": bill_item["name"],
            "sku_id": bill_item["product_sku"],
            "qty_returned": qty,
            "unit": bill_item["unit"],
            "refund_amount": refund_amount,
            "reason": reason,
            "message": (
                f"✅ Return processed successfully!\n"
                f"🔖 Return ID: {return_id}\n"
                f"📦 {qty} {bill_item['unit']} of {bill_item['name']} returned\n"
                f"💰 Refund: ₹{refund_amount:,.2f}\n"
                f"📋 Stock restored | Reason: {reason}"
            )
        }
    except Exception as e:
        logger.error(f"Return processing error: {e}")
        return {"status": "error", "message": f"Error processing return: {str(e)}"}
    finally:
        conn.close()


def list_returns(bill_id: str = None, days: int = 7) -> Dict[str, Any]:
    """
    List recent returns, optionally filtered by bill_id.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        if bill_id:
            cur.execute("""
                SELECT r.*, p.name AS product_name, p.unit
                FROM returns r
                JOIN products p ON r.sku_id = p.sku_id
                WHERE r.bill_id = %s
                ORDER BY r.created_at DESC
            """, (bill_id.strip(),))
        else:
            cur.execute("""
                SELECT r.*, p.name AS product_name, p.unit
                FROM returns r
                JOIN products p ON r.sku_id = p.sku_id
                WHERE r.created_at >= CURRENT_DATE - INTERVAL '%s days'
                ORDER BY r.created_at DESC
                LIMIT 50
            """, (max(1, int(days)),))

        rows = cur.fetchall()
        cur.close()

        returns_list = [{
            "return_id": r["return_id"],
            "bill_id": r["bill_id"],
            "product": r["product_name"],
            "qty": r["qty"],
            "unit": r["unit"],
            "refund_amount": round(r["refund_amount"], 2),
            "reason": r["reason"],
            "date": str(r["created_at"])
        } for r in rows]

        total_refunded = sum(r["refund_amount"] for r in returns_list)

        return {
            "status": "success",
            "count": len(returns_list),
            "total_refunded": round(total_refunded, 2),
            "returns": returns_list,
            "message": f"📋 {len(returns_list)} return(s) found. Total refunded: ₹{total_refunded:,.2f}"
        }
    finally:
        conn.close()
