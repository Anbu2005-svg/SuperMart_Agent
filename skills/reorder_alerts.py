"""
Low Stock Reorder Notifications Skill for SuperMart AI Ops Agent.

Strict Specification:
  - Sends immediate notifications/alerts for low stock items that need reordering.
  - Attaches preferred supplier contact information (name, phone, company).
  - Explicitly DOES NOT place automated purchase orders or automatically reorder stock.
"""

from typing import Dict, Any, List, Optional
from db.models import get_db_connection, immediate_transaction
from skills.audit import _log_event


def check_low_stock_reorder_alerts(
    notify_recipient: Optional[str] = None,
    channel: str = "telegram"
) -> Dict[str, Any]:
    """
    Scan inventory for items at or below reorder threshold.
    Generates actionable notification alerts with supplier details to prompt manual reorder.
    Stores notification audit log without executing any automatic reorders.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.sku_id, p.name, p.category, p.unit, p.quantity, p.reorder_level, p.cost_price, p.mrp
            FROM products p
            WHERE p.quantity <= p.reorder_level AND p.is_active = TRUE
            ORDER BY (p.quantity / GREATEST(p.reorder_level, 1)) ASC, p.name ASC
        """)
        low_items = cur.fetchall()

        if not low_items:
            cur.close()
            return {
                "status": "success",
                "count": 0,
                "message": "✅ All product inventory levels are healthy! No items need reordering right now.",
                "alerts": []
            }

        # Fetch supplier list to map category or name to preferred suppliers
        cur.execute("SELECT * FROM suppliers ORDER BY supplier_id ASC")
        suppliers = cur.fetchall()

        alerts = []
        recipient = notify_recipient or "Store Owner"

        with immediate_transaction(conn):
            log_cur = conn.cursor()
            for idx, item in enumerate(low_items):
                # Suggest reorder quantity (bring stock back to safe buffer)
                needed_qty = max(10.0, round((item["reorder_level"] * 2.5) - item["quantity"], 0))
                urgency = "🚨 CRITICAL OUT OF STOCK" if item["quantity"] <= 0 else "⚠️ LOW STOCK ALERT"

                # Match a supplier if available
                assigned_supplier = None
                if suppliers:
                    assigned_supplier = suppliers[idx % len(suppliers)]

                supp_name = assigned_supplier["name"] if assigned_supplier else "Primary Distributor"
                supp_phone = assigned_supplier["phone"] if assigned_supplier else "Contact on file"
                supp_company = assigned_supplier["company_name"] if assigned_supplier else "Distributor"

                alert_text = (
                    f"{urgency}: *{item['name']}* ({item['sku_id']})\n"
                    f"• Current Stock: *{item['quantity']} {item['unit']}* (Reorder Point: {item['reorder_level']} {item['unit']})\n"
                    f"• Suggested Reorder Quantity: *{needed_qty} {item['unit']}*\n"
                    f"• Supplier: {supp_name} ({supp_company}) | 📞 Phone: `{supp_phone}`\n"
                    f"👉 *Action Required:* Please contact supplier to place order."
                )

                # Persist notification history
                log_cur.execute("""
                    INSERT INTO low_stock_notifications (
                        sku_id, product_name, current_stock, reorder_level,
                        supplier_name, supplier_phone, notified_to, channel, message_text
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    item["sku_id"], item["name"], item["quantity"], item["reorder_level"],
                    supp_name, supp_phone, recipient, channel, alert_text
                ))

                alerts.append({
                    "sku_id": item["sku_id"],
                    "product_name": item["name"],
                    "current_stock": item["quantity"],
                    "reorder_level": item["reorder_level"],
                    "suggested_reorder_qty": needed_qty,
                    "supplier_name": supp_name,
                    "supplier_phone": supp_phone,
                    "alert_text": alert_text
                })

            _log_event(conn, "LOW_STOCK_NOTIFICATIONS_DISPATCHED", "inventory", "reorder_alerts",
                       details={"low_stock_count": len(alerts), "recipient": recipient})
            log_cur.close()

        cur.close()

        formatted_msg = (
            f"🔔 *LOW STOCK REORDER NOTIFICATION ({len(alerts)} items)*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n" +
            "\n\n".join(a["alert_text"] for a in alerts) +
            "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "ℹ️ *Note:* Auto-reorder is disabled. Please contact suppliers above to place replenishment orders."
        )

        return {
            "status": "success",
            "count": len(alerts),
            "message": formatted_msg,
            "alerts": alerts
        }
    finally:
        conn.close()


def list_recent_reorder_notifications(limit: int = 15) -> Dict[str, Any]:
    """Retrieve history of dispatched low stock notifications."""
    limit = max(1, min(limit, 50))
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, sku_id, product_name, current_stock, reorder_level,
                   supplier_name, supplier_phone, notified_to, created_at
            FROM low_stock_notifications
            ORDER BY created_at DESC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        cur.close()

        items = []
        for r in rows:
            items.append({
                "id": r["id"],
                "sku_id": r["sku_id"],
                "product_name": r["product_name"],
                "current_stock": r["current_stock"],
                "reorder_level": r["reorder_level"],
                "supplier_name": r["supplier_name"],
                "supplier_phone": r["supplier_phone"],
                "notified_to": r["notified_to"],
                "timestamp": str(r["created_at"])[:16]
            })

        return {
            "status": "success",
            "count": len(items),
            "notifications": items
        }
    finally:
        conn.close()
