"""
Proactive Smart Notifications — Expiry Alerts, Low-Stock Warnings & Daily Summaries.

Background scheduler checks run automatically via APScheduler:
  - 9:00 AM IST  → expiry alerts (items expiring within 7 days)
  - 9:00 PM IST  → daily auto-summary (revenue, bills, top items)
  - Every 4 hours → critically low stock items
"""
import logging
from datetime import date, timedelta
from typing import Dict, Any, List

from db.models import get_db_connection

logger = logging.getLogger(__name__)


def check_expiring_stock(days_ahead: int = 7) -> Dict[str, Any]:
    """
    Check for stock batches expiring within the next `days_ahead` days.
    Returns structured data for proactive Telegram alerts.
    """
    try:
        days_ahead_int = int(days_ahead)
    except (ValueError, TypeError):
        return {"status": "error", "message": "days_ahead must be a valid integer between 1 and 90."}
    days_ahead = max(1, min(90, days_ahead_int))
    cutoff = (date.today() + timedelta(days=days_ahead)).isoformat()
    today = date.today().isoformat()

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT sb.batch_code, sb.sku_id, sb.qty_remaining, sb.expiry_date,
                   p.name, p.unit
            FROM stock_batches sb
            JOIN products p ON sb.sku_id = p.sku_id
            WHERE sb.expiry_date IS NOT NULL
              AND sb.expiry_date <= %s::date
              AND sb.expiry_date >= %s::date
              AND sb.qty_remaining > 0
            ORDER BY sb.expiry_date ASC
        """, (cutoff, today))
        rows = cur.fetchall()
        cur.close()

        alerts: List[Dict[str, Any]] = []
        for r in rows:
            days_left = (r["expiry_date"] - date.today()).days
            urgency = "🔴 CRITICAL" if days_left <= 2 else ("🟡 WARNING" if days_left <= 5 else "🟢 NOTICE")
            alerts.append({
                "sku_id": r["sku_id"],
                "name": r["name"],
                "batch_code": r["batch_code"],
                "qty_remaining": r["qty_remaining"],
                "unit": r["unit"],
                "expiry_date": str(r["expiry_date"]),
                "days_left": days_left,
                "urgency": urgency
            })

        if not alerts:
            return {
                "status": "success",
                "count": 0,
                "message": f"✅ No stock batches expiring within the next {days_ahead} days.",
                "alerts": []
            }

        # Build formatted alert message
        lines = [f"⚠️ **Expiry Alert** — {len(alerts)} item(s) expiring within {days_ahead} days:\n"]
        for a in alerts:
            lines.append(
                f"{a['urgency']} **{a['name']}** (Batch: {a['batch_code']})\n"
                f"   📦 {a['qty_remaining']} {a['unit']} remaining | "
                f"Expires: {a['expiry_date']} ({a['days_left']} day(s) left)"
            )

        return {
            "status": "success",
            "count": len(alerts),
            "message": "\n".join(lines),
            "alerts": alerts
        }
    finally:
        conn.close()


def check_critical_low_stock() -> Dict[str, Any]:
    """
    Check for items where stock is at or below 50% of reorder level (critically low).
    Returns structured data for proactive low-stock Telegram alerts.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT sku_id, name, quantity, reorder_level, unit
            FROM products
            WHERE is_active = TRUE AND quantity <= reorder_level
            ORDER BY (quantity / GREATEST(reorder_level, 1)) ASC
        """)
        rows = cur.fetchall()
        cur.close()

        alerts: List[Dict[str, Any]] = []
        for r in rows:
            pct = round((r["quantity"] / max(r["reorder_level"], 1)) * 100, 1)
            urgency = "🔴 OUT OF STOCK" if r["quantity"] == 0 else (
                "🟡 CRITICAL" if pct <= 50 else "🟠 LOW"
            )
            alerts.append({
                "sku_id": r["sku_id"],
                "name": r["name"],
                "quantity": r["quantity"],
                "reorder_level": r["reorder_level"],
                "unit": r["unit"],
                "stock_pct": pct,
                "urgency": urgency
            })

        if not alerts:
            return {
                "status": "success",
                "count": 0,
                "message": "✅ All stock levels are healthy — no items below reorder level.",
                "alerts": []
            }

        lines = [f"📉 **Low Stock Alert** — {len(alerts)} item(s) need restocking:\n"]
        for a in alerts:
            lines.append(
                f"{a['urgency']} **{a['name']}**\n"
                f"   📦 {a['quantity']} {a['unit']} remaining "
                f"(Reorder level: {a['reorder_level']})"
            )

        return {
            "status": "success",
            "count": len(alerts),
            "message": "\n".join(lines),
            "alerts": alerts
        }
    finally:
        conn.close()


def generate_daily_closeout_message() -> Dict[str, Any]:
    """
    Generate a comprehensive daily closeout summary message for 9 PM auto-report.
    Covers: revenue, bills count, top items, payment breakdown, pending khata.
    """
    today = date.today().isoformat()
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # Bills finalized today
        cur.execute("""
            SELECT COUNT(*) AS cnt, COALESCE(SUM(total), 0) AS revenue,
                   COALESCE(SUM(subtotal), 0) AS subtotal,
                   COALESCE(SUM(cgst + sgst), 0) AS gst_collected
            FROM bills
            WHERE status = 'finalized' AND finalized_at::date = %s::date
        """, (today,))
        summary = cur.fetchone()

        # Payment breakdown
        cur.execute("""
            SELECT COALESCE(payment_mode, 'other') AS mode, COUNT(*) AS cnt,
                   COALESCE(SUM(total), 0) AS mode_total
            FROM bills
            WHERE status = 'finalized' AND finalized_at::date = %s::date
            GROUP BY payment_mode ORDER BY mode_total DESC
        """, (today,))
        payments = cur.fetchall()

        # Top 5 items sold today
        cur.execute("""
            SELECT p.name, SUM(bi.qty) AS total_qty, p.unit,
                   SUM(bi.line_total) AS item_revenue
            FROM bill_items bi
            JOIN bills b ON bi.bill_id = b.bill_id
            JOIN products p ON bi.sku_id = p.sku_id
            WHERE b.status = 'finalized' AND b.finalized_at::date = %s::date
            GROUP BY p.name, p.unit
            ORDER BY item_revenue DESC LIMIT 5
        """, (today,))
        top_items = cur.fetchall()

        # Total pending khata balance
        cur.execute("SELECT COALESCE(SUM(khata_balance), 0) AS total_pending FROM customers WHERE khata_balance > 0")
        khata = cur.fetchone()

        # Low stock count
        cur.execute("SELECT COUNT(*) AS cnt FROM products WHERE is_active = TRUE AND quantity <= reorder_level")
        low_stock = cur.fetchone()

        cur.close()

        total_bills = summary["cnt"]
        revenue = round(summary["revenue"], 2)
        gst = round(summary["gst_collected"], 2)
        khata_pending = round(khata["total_pending"], 2)
        low_count = low_stock["cnt"]

        # Build formatted message
        lines = [
            f"📊 **Daily Closeout Report — {today}**\n",
            f"💰 **Revenue:** ₹{revenue:,.2f} ({total_bills} bill{'s' if total_bills != 1 else ''})",
            f"🧾 **GST Collected:** ₹{gst:,.2f}",
        ]

        if payments:
            pay_parts = [f"{p['mode'].upper()}: ₹{p['mode_total']:,.2f} ({p['cnt']})" for p in payments]
            lines.append(f"💳 **Payments:** {' | '.join(pay_parts)}")

        if top_items:
            lines.append("\n🏆 **Top Items Sold:**")
            for i, item in enumerate(top_items, 1):
                lines.append(f"   {i}. {item['name']} — {item['total_qty']} {item['unit']} (₹{item['item_revenue']:,.2f})")

        lines.append(f"\n📋 **Pending Khata:** ₹{khata_pending:,.2f}")
        if low_count > 0:
            lines.append(f"⚠️ **Low Stock Items:** {low_count} item(s) need restocking")
        else:
            lines.append("✅ **Stock Health:** All items above reorder level")

        lines.append("\n🌙 Good night! See you tomorrow. 🛒")

        return {
            "status": "success",
            "date": today,
            "total_bills": total_bills,
            "revenue": revenue,
            "gst_collected": gst,
            "khata_pending": khata_pending,
            "low_stock_count": low_count,
            "message": "\n".join(lines)
        }
    finally:
        conn.close()
