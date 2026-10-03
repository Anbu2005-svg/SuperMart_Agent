"""
Customer Purchase History & Smart Re-Purchase Suggestions Module.

Analyzes customer purchase frequency, habits, and repurchase cycles.
Provides:
  1. Detailed customer purchase history & lifetime metrics
  2. Smart itemized suggestions based on purchase cycle intervals (e.g., Rice every 14 days)
  3. Proactive store-wide repurchase alerts for the shop owner to follow up via WhatsApp
"""

import math
from datetime import date, datetime, timedelta
from typing import Dict, Any, List, Optional
from db.models import get_db_connection


def get_customer_purchase_history(customer_name: str, limit_bills: int = 10) -> Dict[str, Any]:
    """
    Retrieve comprehensive purchase history, metrics, and top bought products for a customer.
    """
    if not customer_name or not isinstance(customer_name, str):
        return {"status": "error", "message": "Customer name is required."}

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM customers WHERE name ILIKE %s", (f"%{customer_name.strip()}%",))
        customer = cur.fetchone()
        if not customer:
            return {"status": "error", "message": f"Customer '{customer_name}' not found in database."}

        cid = customer["customer_id"]

        # Fetch finalized bills
        cur.execute("""
            SELECT bill_id, invoice_number, subtotal, cgst, sgst, total, payment_mode, finalized_at
            FROM bills
            WHERE customer_id = %s AND status = 'finalized'
            ORDER BY finalized_at DESC
        """, (cid,))
        all_bills = cur.fetchall()

        if not all_bills:
            return {
                "status": "success",
                "customer_name": customer["name"],
                "phone": customer.get("phone") or "N/A",
                "khata_balance": float(customer.get("khata_balance") or 0.0),
                "total_bills": 0,
                "lifetime_spend": 0.0,
                "average_bill": 0.0,
                "top_products": [],
                "recent_bills": [],
                "message": f"ℹ️ Customer '{customer['name']}' has no finalized purchase history yet."
            }

        total_bills = len(all_bills)
        lifetime_spend = sum(float(b["total"]) for b in all_bills)
        avg_bill = round(lifetime_spend / total_bills, 2) if total_bills > 0 else 0.0

        # Fetch top purchased items
        cur.execute("""
            SELECT p.sku_id, p.name, p.unit,
                   COUNT(bi.id) AS order_occurrences,
                   SUM(bi.qty) AS total_qty_bought,
                   SUM(bi.line_total) AS total_spent_on_item,
                   MAX(b.finalized_at) AS last_purchased_at
            FROM bill_items bi
            JOIN bills b ON bi.bill_id = b.bill_id
            JOIN products p ON bi.sku_id = p.sku_id
            WHERE b.customer_id = %s AND b.status = 'finalized'
            GROUP BY p.sku_id, p.name, p.unit
            ORDER BY order_occurrences DESC, total_qty_bought DESC
            LIMIT 5
        """, (cid,))
        top_products = cur.fetchall()

        # Format recent bills up to limit
        recent = []
        for b in all_bills[:limit_bills]:
            cur.execute("""
                SELECT p.name, bi.qty, p.unit, bi.unit_price, bi.line_total
                FROM bill_items bi
                JOIN products p ON bi.sku_id = p.sku_id
                WHERE bi.bill_id = %s
            """, (b["bill_id"],))
            items = cur.fetchall()
            recent.append({
                "bill_id": b["bill_id"],
                "invoice_number": b["invoice_number"],
                "date": b["finalized_at"].strftime("%Y-%m-%d %H:%M") if b["finalized_at"] else "N/A",
                "total": round(float(b["total"]), 2),
                "payment_mode": (b["payment_mode"] or "CASH").upper(),
                "items_count": len(items),
                "items_preview": [f"{it['name']} ({it['qty']} {it['unit']})" for it in items[:3]]
            })

        cur.close()

        top_formatted = [{
            "sku_id": p["sku_id"],
            "name": p["name"],
            "unit": p["unit"],
            "order_occurrences": p["order_occurrences"],
            "total_qty_bought": round(float(p["total_qty_bought"]), 2),
            "total_spent": round(float(p["total_spent_on_item"]), 2),
            "last_purchased": p["last_purchased_at"].strftime("%Y-%m-%d") if p["last_purchased_at"] else "N/A"
        } for p in top_products]

        lines = [
            f"👤 **Customer Purchase Profile: {customer['name']}**",
            f"📞 Phone: {customer.get('phone') or 'Not Registered'}",
            f"💳 Khata Balance: ₹{float(customer.get('khata_balance') or 0.0):.2f}",
            f"🛍️ Total Orders: {total_bills} | Total Spent: ₹{lifetime_spend:,.2f} | Avg Bill: ₹{avg_bill:,.2f}\n",
            "⭐ **Frequently Bought Products:**"
        ]
        for p in top_formatted:
            lines.append(f"  • {p['name']} [{p['sku_id']}] — Bought {p['total_qty_bought']} {p['unit']} across {p['order_occurrences']} orders (Last: {p['last_purchased']})")

        lines.append(f"\n🧾 **Recent Invoices ({len(recent)} shown):**")
        for r in recent[:5]:
            lines.append(f"  • Bill #{r['invoice_number'] or r['bill_id']} ({r['date']}) — ₹{r['total']:,.2f} via {r['payment_mode']}")

        return {
            "status": "success",
            "customer_name": customer["name"],
            "phone": customer.get("phone") or "N/A",
            "khata_balance": float(customer.get("khata_balance") or 0.0),
            "total_bills": total_bills,
            "lifetime_spend": round(lifetime_spend, 2),
            "average_bill": avg_bill,
            "top_products": top_formatted,
            "recent_bills": recent,
            "message": "\n".join(lines)
        }
    finally:
        conn.close()


def get_customer_smart_suggestions(customer_name: str) -> Dict[str, Any]:
    """
    Calculate customer purchase cycle intervals and identify products overdue for repurchase.
    Also suggests complementary essentials they haven't tried yet.
    """
    if not customer_name or not isinstance(customer_name, str):
        return {"status": "error", "message": "Customer name is required."}

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM customers WHERE name ILIKE %s", (f"%{customer_name.strip()}%",))
        customer = cur.fetchone()
        if not customer:
            return {"status": "error", "message": f"Customer '{customer_name}' not found in database."}

        cid = customer["customer_id"]

        # Fetch purchase history timeline for each product bought by this customer
        cur.execute("""
            SELECT bi.sku_id, p.name, p.unit, p.mrp, b.finalized_at
            FROM bill_items bi
            JOIN bills b ON bi.bill_id = b.bill_id
            JOIN products p ON bi.sku_id = p.sku_id
            WHERE b.customer_id = %s AND b.status = 'finalized'
            ORDER BY bi.sku_id, b.finalized_at ASC
        """, (cid,))
        rows = cur.fetchall()

        # Group purchases by SKU
        history_map: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            sku = r["sku_id"]
            if sku not in history_map:
                history_map[sku] = {
                    "sku_id": sku,
                    "name": r["name"],
                    "unit": r["unit"],
                    "mrp": r["mrp"],
                    "dates": []
                }
            if r["finalized_at"]:
                history_map[sku]["dates"].append(r["finalized_at"].date())

        suggestions = []
        today = date.today()

        for sku, data in history_map.items():
            dates = sorted(data["dates"])
            if len(dates) >= 2:
                # Calculate average gap between purchases
                gaps = [(dates[i] - dates[i-1]).days for i in range(1, len(dates))]
                avg_cycle = max(3, round(sum(gaps) / len(gaps)))
                days_since_last = (today - dates[-1]).days
                
                # If days since last purchase exceeds or is close to avg_cycle
                if days_since_last >= avg_cycle:
                    overdue_days = days_since_last - avg_cycle
                    suggestions.append({
                        "sku_id": sku,
                        "name": data["name"],
                        "unit": data["unit"],
                        "mrp": data["mrp"],
                        "avg_cycle_days": avg_cycle,
                        "days_since_last": days_since_last,
                        "overdue_days": overdue_days,
                        "urgency": "🔴 OVERDUE" if overdue_days >= 3 else "🟡 DUE SOON",
                        "reason": f"Usually buys every ~{avg_cycle} days. Last bought {days_since_last} days ago."
                    })
            elif len(dates) == 1:
                # Bought once more than 21 days ago
                days_since = (today - dates[0]).days
                if days_since >= 21:
                    suggestions.append({
                        "sku_id": sku,
                        "name": data["name"],
                        "unit": data["unit"],
                        "mrp": data["mrp"],
                        "avg_cycle_days": 21,
                        "days_since_last": days_since,
                        "overdue_days": days_since - 21,
                        "urgency": "🟢 RE-ENGAGE",
                        "reason": f"Bought once {days_since} days ago. Good candidate for re-engagement."
                    })

        # Suggest top store essentials customer has NEVER bought
        cur.execute("""
            SELECT p.sku_id, p.name, p.unit, p.mrp
            FROM products p
            WHERE p.is_active = TRUE
              AND p.sku_id NOT IN (
                  SELECT bi.sku_id FROM bill_items bi
                  JOIN bills b ON bi.bill_id = b.bill_id
                  WHERE b.customer_id = %s AND b.status = 'finalized'
              )
            ORDER BY p.quantity DESC
            LIMIT 3
        """, (cid,))
        untried_essentials = cur.fetchall()
        cur.close()

        lines = [f"💡 **Smart Re-Purchase Suggestions for {customer['name']}:**\n"]
        if suggestions:
            lines.append("⏰ **Restock Reminders (Based on Purchase Cycles):**")
            for s in suggestions:
                lines.append(f"  • {s['urgency']} **{s['name']}** [{s['sku_id']}] — {s['reason']}")
        else:
            lines.append("✅ No regular items are currently overdue for repurchase.")

        if untried_essentials:
            lines.append("\n🌟 **Recommended Essentials (Never Purchased):**")
            for u in untried_essentials:
                lines.append(f"  • {u['name']} [{u['sku_id']}] – MRP: ₹{u['mrp']:.2f}")

        # WhatsApp draft message
        whatsapp_draft = (
            f"Namaste {customer['name']} ji! SuperMart here. We noticed you might be running low on "
            + (suggestions[0]['name'] if suggestions else "daily grocery essentials")
            + ". We have fresh stock ready for you! Visit us or reply to order for delivery."
        )

        return {
            "status": "success",
            "customer_name": customer["name"],
            "suggestions_count": len(suggestions),
            "suggestions": suggestions,
            "untried_recommendations": [{
                "sku_id": u["sku_id"],
                "name": u["name"],
                "mrp": float(u["mrp"])
            } for u in untried_essentials],
            "whatsapp_reminder_template": whatsapp_draft,
            "message": "\n".join(lines)
        }
    finally:
        conn.close()


def get_all_customer_repurchase_alerts() -> Dict[str, Any]:
    """
    Store-wide scanner: Detects all customers who are overdue to repurchase staple essentials.
    Enables shop owners to proactively reach out to regular customers.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT customer_id, name, phone FROM customers ORDER BY name ASC")
        customers = cur.fetchall()
        cur.close()

        alerts = []
        for cust in customers:
            sugg_res = get_customer_smart_suggestions(cust["name"])
            if sugg_res.get("status") == "success" and sugg_res.get("suggestions"):
                overdue_items = [s for s in sugg_res["suggestions"] if "OVERDUE" in s.get("urgency", "")]
                if overdue_items:
                    alerts.append({
                        "customer_name": cust["name"],
                        "phone": cust.get("phone") or "N/A",
                        "overdue_count": len(overdue_items),
                        "items": [it["name"] for it in overdue_items]
                    })

        lines = [f"📢 **Customer Repurchase Radar ({len(alerts)} customers due for essentials):**\n"]
        if alerts:
            for a in alerts[:8]:
                lines.append(f"• **{a['customer_name']}** (📞 {a['phone']}) — Due for: {', '.join(a['items'])}")
        else:
            lines.append("✅ All regular customer replenishment cycles are currently up-to-date!")

        return {
            "status": "success",
            "alert_count": len(alerts),
            "alerts": alerts,
            "message": "\n".join(lines)
        }
    finally:
        conn.close()
