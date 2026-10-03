"""
End-of-Day (EOD) Executive Report Skill for SuperMart AI Ops Agent.

Compiles an end-of-day closing summary for the supermarket owner:
  - Total Gross Turnover & Finalized Bill Count
  - Cash-in-Drawer & Digital (UPI/Card) reconciliation
  - Day's Net Estimated Gross Profit & Margin %
  - Khata Credit Flow (Credit Given vs Credit Collected)
  - Fast-moving items sold today
  - High-priority stockout and expiry warnings for tomorrow morning
"""

from datetime import date, datetime, timedelta
from typing import Dict, Any, Optional
from db.models import get_db_connection
from skills.whatsapp import DEFAULT_SHOP_NAME


def generate_end_of_day_report(date_str: Optional[str] = None) -> Dict[str, Any]:
    """
    Generate complete End-of-Day (EOD) financial and operational briefing for the owner.
    """
    if date_str:
        try:
            target_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
        except ValueError:
            return {"status": "error", "message": f"Invalid date format '{date_str}'. Expected YYYY-MM-DD."}
    else:
        target_date = date.today()

    target_str = target_date.isoformat()

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # 1. Bills & Financials
        cur.execute("""
            SELECT COUNT(*) AS bill_count,
                   COALESCE(SUM(subtotal), 0) AS subtotal,
                   COALESCE(SUM(cgst + sgst), 0) AS total_tax,
                   COALESCE(SUM(total), 0) AS gross_total
            FROM bills
            WHERE status = 'finalized' AND finalized_at::date = %s::date
        """, (target_str,))
        bill_stats = cur.fetchone()

        # 2. Payment Modes Breakdown (Drawer Cash vs UPI vs Card vs Khata)
        cur.execute("""
            SELECT COALESCE(payment_mode, 'cash') AS mode,
                   COUNT(*) AS count,
                   COALESCE(SUM(total), 0) AS total_amt
            FROM bills
            WHERE status = 'finalized' AND finalized_at::date = %s::date
            GROUP BY payment_mode
        """, (target_str,))
        pay_rows = cur.fetchall()
        pay_map = {r["mode"].lower(): float(r["total_amt"]) for r in pay_rows}

        cash_in_drawer = pay_map.get("cash", 0.0)
        upi_total = pay_map.get("upi", 0.0)
        card_total = pay_map.get("card", 0.0)
        khata_billed = pay_map.get("khata", 0.0)

        # 3. Cost of Goods Sold & Profit
        cur.execute("""
            SELECT COALESCE(SUM(bi.qty * p.cost_price), 0) AS cogs,
                   COALESCE(SUM(bi.qty * (bi.unit_price - p.cost_price)), 0) AS profit
            FROM bill_items bi
            JOIN bills b ON bi.bill_id = b.bill_id
            JOIN products p ON bi.sku_id = p.sku_id
            WHERE b.status = 'finalized' AND b.finalized_at::date = %s::date
        """, (target_str,))
        profit_row = cur.fetchone()
        cogs = float(profit_row["cogs"])
        gross_profit = float(profit_row["profit"])
        revenue = float(bill_stats["subtotal"])
        gross_margin = round((gross_profit / revenue * 100), 1) if revenue > 0 else 0.0

        # 4. Khata Collections today
        cur.execute("""
            SELECT COALESCE(SUM(amount), 0) AS recovered
            FROM khata_transactions
            WHERE type = 'payment' AND created_at::date = %s::date
        """, (target_str,))
        khata_recovered = float(cur.fetchone()["recovered"])

        # 5. Top 5 items sold today
        cur.execute("""
            SELECT p.name, SUM(bi.qty) AS total_qty, p.unit, SUM(bi.line_total) AS revenue
            FROM bill_items bi
            JOIN bills b ON bi.bill_id = b.bill_id
            JOIN products p ON bi.sku_id = p.sku_id
            WHERE b.status = 'finalized' AND b.finalized_at::date = %s::date
            GROUP BY p.name, p.unit
            ORDER BY total_qty DESC
            LIMIT 5
        """, (target_str,))
        top_items = cur.fetchall()

        # 6. Critical Low Stock Alerts
        cur.execute("""
            SELECT name, quantity, unit, reorder_level
            FROM products
            WHERE is_active = TRUE AND quantity <= reorder_level
            ORDER BY (quantity / NULLIF(reorder_level, 0)) ASC
            LIMIT 4
        """)
        low_items = cur.fetchall()
        cur.close()

        bills_count = bill_stats["bill_count"]
        gross_turnover = float(bill_stats["gross_total"])

        lines = [
            f"🌙 **End-of-Day Store Report — {target_str}**",
            f"🏬 **{DEFAULT_SHOP_NAME}**\n",
            f"📊 **Financial Summary:**",
            f"• 🧾 Total Invoices: **{bills_count}**",
            f"• 💰 Gross Turnover (with GST): **₹{gross_turnover:,.2f}**",
            f"• 📈 Est. Gross Profit: **₹{gross_profit:,.2f}** ({gross_margin}% Margin)\n",
            f"💵 **Cash Drawer & Digital Collections:**",
            f"• 💵 Cash in Hand: **₹{cash_in_drawer:,.2f}**",
            f"• 📲 UPI Received: **₹{upi_total:,.2f}**",
            f"• 💳 Card Swipes: **₹{card_total:,.2f}**",
            f"• 📝 Khata Credit Given: **₹{khata_billed:,.2f}**",
            f"• 🤝 Khata Debt Recovered Today: **₹{khata_recovered:,.2f}** (Net delta: {'+' if (khata_recovered - khata_billed) >= 0 else ''}₹{(khata_recovered - khata_billed):,.2f})\n"
        ]

        if top_items:
            lines.append("🏆 **Top Selling Products Today:**")
            for i, it in enumerate(top_items, 1):
                lines.append(f"  {i}. {it['name']} — {it['total_qty']} {it['unit']} (₹{float(it['revenue']):,.2f})")
            lines.append("")

        if low_items:
            lines.append("🚨 **Low Stock Reminders for Tomorrow:**")
            for it in low_items:
                lines.append(f"  • {it['name']}: {it['quantity']} {it['unit']} remaining (Reorder at {it['reorder_level']})")
        else:
            lines.append("✅ All product stock levels are healthy for tomorrow morning.")

        return {
            "status": "success",
            "date": target_str,
            "bills_count": bills_count,
            "gross_turnover": round(gross_turnover, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_margin_pct": gross_margin,
            "collections": {
                "cash": round(cash_in_drawer, 2),
                "upi": round(upi_total, 2),
                "card": round(card_total, 2),
                "khata_billed": round(khata_billed, 2),
                "khata_recovered": round(khata_recovered, 2)
            },
            "top_products": [{
                "name": t["name"],
                "qty": t["total_qty"],
                "revenue": round(float(t["revenue"]), 2)
            } for t in top_items],
            "low_stock_alerts": [{
                "name": l["name"],
                "quantity": l["quantity"],
                "reorder_level": l["reorder_level"]
            } for l in low_items],
            "message": "\n".join(lines)
        }
    finally:
        conn.close()
