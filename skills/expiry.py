"""
Smart Expiry & Dynamic Markdown Engine Skill for SuperMart AI Ops Agent.

Proactively monitors batch shelf-life and recommends automated tiered markdowns:
- Expired (<= 0 days): Hazardous stock write-off alert (FSSAI compliance)
- Critical Clearance (1-3 days): 50% Flash Clearance / BOGO promotion
- Fast Clearance (4-7 days): 25% Clearance bundle
- Early Clearance (8-15 days): 10% Early Bird promotion

Calculates stock at risk (₹) vs recoverable revenue to minimize supermarket inventory shrinkage.
"""

import logging
from datetime import date, timedelta
from typing import Dict, Any, List, Optional
from db.models import get_db_connection, immediate_transaction
from skills.audit import _log_event

logger = logging.getLogger(__name__)


def get_expiring_batches(days_ahead: int = 15) -> Dict[str, Any]:
    """
    Fetch all active inventory batches expiring within `days_ahead` days,
    as well as any batches that have already passed their expiry date.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        today = date.today()
        cutoff = today + timedelta(days=days_ahead)

        cur.execute("""
            SELECT sb.batch_id, sb.batch_code, sb.sku_id, sb.qty_remaining, sb.cost_price, sb.expiry_date,
                   p.name AS product_name, p.mrp, p.unit, p.category, p.gst_slab
            FROM stock_batches sb
            JOIN products p ON sb.sku_id = p.sku_id
            WHERE sb.qty_remaining > 0 AND sb.expiry_date IS NOT NULL AND sb.expiry_date <= %s
            ORDER BY sb.expiry_date ASC, sb.qty_remaining DESC
        """, (cutoff,))

        rows = cur.fetchall()
        cur.close()

        batches = []
        for r in rows:
            exp_date = r["expiry_date"]
            days_left = (exp_date - today).days
            batches.append({
                "batch_id": r["batch_id"],
                "batch_code": r["batch_code"],
                "sku_id": r["sku_id"],
                "product_name": r["product_name"],
                "category": r["category"],
                "unit": r["unit"],
                "qty_remaining": float(r["qty_remaining"]),
                "cost_price": float(r["cost_price"] or 0.0),
                "mrp": float(r["mrp"] or 0.0),
                "expiry_date": str(exp_date),
                "days_left": days_left
            })

        return {
            "status": "success",
            "count": len(batches),
            "days_ahead": days_ahead,
            "batches": batches
        }
    finally:
        conn.close()


def recommend_markdown_discounts(days_ahead: int = 15) -> Dict[str, Any]:
    """
    Analyze near-expiry stock and calculate recommended clearance markdown discounts.
    Provides estimated revenue recovery and action plan for store operations.
    """
    res = get_expiring_batches(days_ahead=days_ahead)
    if res.get("status") != "success":
        return res

    batches = res.get("batches", [])
    if not batches:
        return {
            "status": "success",
            "count": 0,
            "batches": [],
            "total_at_risk_value": 0.0,
            "total_recoverable_revenue": 0.0,
            "message": f"🎉 Excellent! No stock batches expiring within the next {days_ahead} days."
        }

    recommendations = []
    total_at_risk = 0.0
    total_recoverable = 0.0

    for b in batches:
        days = b["days_left"]
        mrp = b["mrp"]
        qty = b["qty_remaining"]
        stock_value = round(mrp * qty, 2)
        total_at_risk += stock_value

        if days <= 0:
            tier = "EXPIRED"
            discount_pct = 100.0
            markdown_price = 0.0
            rec_action = "🚨 HAZARDOUS: Remove immediately from shelves! Write-off as damage."
            recoverable = 0.0
        elif days <= 3:
            tier = "CRITICAL_CLEARANCE"
            discount_pct = 50.0
            markdown_price = round(mrp * 0.50, 2)
            rec_action = f"🔥 50% Flash Sale: Mark down to ₹{markdown_price:.2f} or offer 'Buy 1 Get 1 Free' at checkout counter."
            recoverable = round(markdown_price * qty, 2)
        elif days <= 7:
            tier = "FAST_CLEARANCE"
            discount_pct = 25.0
            markdown_price = round(mrp * 0.75, 2)
            rec_action = f"⚡ 25% Clearance: Mark down to ₹{markdown_price:.2f}. Bundle with fast-moving staples."
            recoverable = round(markdown_price * qty, 2)
        else:
            tier = "EARLY_PROMOTION"
            discount_pct = 10.0
            markdown_price = round(mrp * 0.90, 2)
            rec_action = f"🏷️ 10% Early Promo: Mark down to ₹{markdown_price:.2f}. Place on promotional aisle."
            recoverable = round(markdown_price * qty, 2)

        total_recoverable += recoverable

        recommendations.append({
            "sku_id": b["sku_id"],
            "batch_code": b["batch_code"],
            "product_name": b["product_name"],
            "qty_remaining": qty,
            "unit": b["unit"],
            "expiry_date": b["expiry_date"],
            "days_left": days,
            "tier": tier,
            "current_mrp": mrp,
            "discount_pct": discount_pct,
            "recommended_price": markdown_price,
            "stock_value_at_risk": stock_value,
            "estimated_recovery": recoverable,
            "action": rec_action
        })

    # Build human-readable formatted message for Telegram / Cashier
    lines = [
        f"🏷️ **Smart Expiry & Clearance Markdown Report** (Next {days_ahead} Days)",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"• **Batches at Risk:** {len(recommendations)}",
        f"• **Total Value at Risk:** ₹{total_at_risk:.2f}",
        f"• **Potential Recoverable Revenue:** ₹{total_recoverable:.2f}",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    ]

    for item in recommendations:
        urgency = "🚨" if item["days_left"] <= 0 else ("🔥" if item["days_left"] <= 3 else ("⚡" if item["days_left"] <= 7 else "🏷️"))
        days_str = "EXPIRED TODAY/PAST" if item["days_left"] <= 0 else f"{item['days_left']} day(s) left"
        lines.append(
            f"{urgency} **{item['product_name']}** [{item['sku_id']}]\n"
            f"   • Batch: `{item['batch_code']}` | Stock: {item['qty_remaining']:.0f} {item['unit']}\n"
            f"   • Expiry: {item['expiry_date']} ({days_str})\n"
            f"   • Current MRP: ₹{item['current_mrp']:.2f} ➔ **Clearance: ₹{item['recommended_price']:.2f} ({item['discount_pct']:.0f}% OFF)**\n"
            f"   • Strategy: _{item['action']}_\n"
        )

    lines.append("💡 *Tip: Apply markdowns before expiry to avoid dead stock loss and maintain fresh catalog turnover.*")

    return {
        "status": "success",
        "count": len(recommendations),
        "total_at_risk_value": round(total_at_risk, 2),
        "total_recoverable_revenue": round(total_recoverable, 2),
        "recommendations": recommendations,
        "message": "\n".join(lines)
    }


def apply_clearance_discount(sku_id: str, discount_pct: float) -> Dict[str, Any]:
    """
    Apply a promotional clearance discount percentage to a product's selling MRP.
    Updates the product record and logs an audit trail event.
    """
    if not sku_id or not isinstance(sku_id, str):
        return {"status": "error", "message": "sku_id is required."}
    if discount_pct < 0 or discount_pct > 90:
        return {"status": "error", "message": "discount_pct must be between 0% and 90%."}

    clean_sku = sku_id.strip()
    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("SELECT sku_id, name, mrp, cost_price FROM products WHERE sku_id = %s FOR UPDATE", (clean_sku,))
            prod = cur.fetchone()
            if not prod:
                cur.close()
                return {"status": "error", "message": f"Product '{clean_sku}' not found."}

            old_mrp = float(prod["mrp"])
            new_mrp = round(old_mrp * (1.0 - (discount_pct / 100.0)), 2)

            cur.execute("UPDATE products SET mrp = %s WHERE sku_id = %s", (new_mrp, clean_sku))
            _log_event(
                conn, "CLEARANCE_MARKDOWN_APPLIED", "product", clean_sku,
                details={"discount_pct": discount_pct, "old_mrp": old_mrp, "new_mrp": new_mrp},
                old_value=str(old_mrp), new_value=str(new_mrp)
            )
            cur.close()

        return {
            "status": "success",
            "sku_id": clean_sku,
            "product_name": prod["name"],
            "old_mrp": old_mrp,
            "new_mrp": new_mrp,
            "discount_pct": discount_pct,
            "message": f"✅ Applied {discount_pct:.0f}% clearance discount to **{prod['name']}** (`{clean_sku}`). MRP updated: ₹{old_mrp:.2f} ➔ **₹{new_mrp:.2f}**."
        }
    finally:
        conn.close()
