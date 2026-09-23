from datetime import date, datetime, timedelta
from typing import Dict, Any, Optional, List
from db.models import get_db_connection


def daily_summary(date_str: Optional[str] = None) -> Dict[str, Any]:
    """
    Generate daily sales summary report for a given date (YYYY-MM-DD).
    Defaults to current date if omitted.
    """
    if date_str:
        try:
            parsed_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
            target_date = parsed_date.isoformat()
        except ValueError:
            return {"status": "error", "message": f"Invalid date format '{date_str}'. Expected format is YYYY-MM-DD (e.g. 2026-09-12)."}
    else:
        target_date = date.today().isoformat()

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Finalized bills on date
        cur.execute("""
            SELECT * FROM bills
            WHERE status = 'finalized' AND finalized_at::date = %s::date
        """, (target_date,))
        bills = cur.fetchall()

        total_bills = len(bills)
        total_sales = sum(b["total"] for b in bills)
        subtotal_sales = sum(b["subtotal"] for b in bills)
        cgst_collected = sum(b["cgst"] for b in bills)
        sgst_collected = sum(b["sgst"] for b in bills)
        total_tax_collected = cgst_collected + sgst_collected

        # Payment Mode Breakdown
        payment_breakdown = {"cash": 0.0, "upi": 0.0, "card": 0.0, "khata": 0.0}
        for b in bills:
            pm = b["payment_mode"] or "other"
            if pm in payment_breakdown:
                payment_breakdown[pm] += b["total"]

        # Top 5 items sold
        cur.execute("""
            SELECT p.name, SUM(bi.qty) as total_qty, p.unit, SUM(bi.line_total) as item_revenue
            FROM bill_items bi
            JOIN bills b ON bi.bill_id = b.bill_id
            JOIN products p ON bi.sku_id = p.sku_id
            WHERE b.status = 'finalized' AND b.finalized_at::date = %s::date
            GROUP BY bi.sku_id, p.name, p.unit
            ORDER BY total_qty DESC
            LIMIT 5
        """, (target_date,))
        top_items = cur.fetchall()
        cur.close()

        items_summary = [{
            "name": row["name"],
            "total_qty": row["total_qty"],
            "unit": row["unit"],
            "revenue": row["item_revenue"]
        } for row in top_items]

        return {
            "status": "success",
            "date": target_date,
            "total_bills": total_bills,
            "total_sales": round(total_sales, 2),
            "subtotal": round(subtotal_sales, 2),
            "total_tax_collected": round(total_tax_collected, 2),
            "cgst_collected": round(cgst_collected, 2),
            "sgst_collected": round(sgst_collected, 2),
            "payment_breakdown": {k: round(v, 2) for k, v in payment_breakdown.items()},
            "top_selling_items": items_summary
        }
    finally:
        conn.close()


def close_day(date_str: Optional[str] = None) -> Dict[str, Any]:
    """Close out supermarket operations for the day and return closed daily report."""
    summary = daily_summary(date_str)
    if summary.get("status") != "success":
        return summary
    summary["message"] = f"Day {summary['date']} operations closed successfully. Total Revenue: ₹{summary['total_sales']:.2f}"
    summary["is_closed"] = True
    return summary


def period_summary(days: int = 7) -> Dict[str, Any]:
    """
    Aggregate sales summary over the last N days (default 7 = weekly).
    Returns revenue, tax, payment mix, top items and per-day trend.
    """
    days = max(1, min(90, int(days)))
    start = (date.today() - timedelta(days=days - 1)).isoformat()

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) AS bills,
                   COALESCE(SUM(total), 0) AS revenue,
                   COALESCE(SUM(subtotal), 0) AS subtotal,
                   COALESCE(SUM(cgst), 0) AS cgst,
                   COALESCE(SUM(sgst), 0) AS sgst
            FROM bills
            WHERE status = 'finalized' AND finalized_at::date >= %s::date
        """, (start,))
        totals = cur.fetchone()

        # Payment mix over the period
        cur.execute("""
            SELECT COALESCE(payment_mode, 'other') AS mode, SUM(total) AS mode_total
            FROM bills
            WHERE status = 'finalized' AND finalized_at::date >= %s::date
            GROUP BY payment_mode
        """, (start,))
        pm_rows = cur.fetchall()
        payment_breakdown = {"cash": 0.0, "upi": 0.0, "card": 0.0, "khata": 0.0}
        for r in pm_rows:
            if r["mode"] in payment_breakdown:
                payment_breakdown[r["mode"]] = float(r["mode_total"])

        # Top items over the period
        cur.execute("""
            SELECT p.name, p.sku_id, SUM(bi.qty) AS total_qty, p.unit, SUM(bi.line_total) AS item_revenue
            FROM bill_items bi
            JOIN bills b ON bi.bill_id = b.bill_id
            JOIN products p ON bi.sku_id = p.sku_id
            WHERE b.status = 'finalized' AND b.finalized_at::date >= %s::date
            GROUP BY p.sku_id, p.name, p.unit
            ORDER BY item_revenue DESC
            LIMIT 10
        """, (start,))
        top_rows = cur.fetchall()

        # Per-day trend
        cur.execute("""
            SELECT finalized_at::date AS day, SUM(total) AS day_revenue, COUNT(*) AS day_bills
            FROM bills
            WHERE status = 'finalized' AND finalized_at::date >= %s::date
            GROUP BY day ORDER BY day ASC
        """, (start,))
        trend_rows = cur.fetchall()
        cur.close()

        return {
            "status": "success",
            "period_days": days,
            "start_date": start,
            "total_bills": totals["bills"],
            "total_revenue": round(float(totals["revenue"]), 2),
            "subtotal": round(float(totals["subtotal"]), 2),
            "total_tax": round(float(totals["cgst"]) + float(totals["sgst"]), 2),
            "payment_breakdown": {k: round(v, 2) for k, v in payment_breakdown.items()},
            "top_items": [{
                "sku_id": r["sku_id"],
                "name": r["name"],
                "total_qty": r["total_qty"],
                "unit": r["unit"],
                "revenue": round(float(r["item_revenue"]), 2)
            } for r in top_rows],
            "daily_trend": [{
                "date": str(r["day"]),
                "revenue": round(float(r["day_revenue"]), 2),
                "bills": r["day_bills"]
            } for r in trend_rows]
        }
    finally:
        conn.close()


def reorder_suggestions(velocity_days: int = 7, cover_days: int = 7) -> Dict[str, Any]:
    """
    Data-driven reorder suggestions from sales velocity:
      velocity = qty sold per day over the last `velocity_days` days
      days_of_cover = current stock / velocity
      suggested_qty = velocity * cover_days - current stock (only when below cover)

    Suggests reordering items whose projected cover is below `cover_days`
    or that are at/below their reorder level.
    """
    velocity_days = max(1, min(90, int(velocity_days)))
    cover_days = max(1, int(cover_days))

    start = (date.today() - timedelta(days=velocity_days - 1)).isoformat()

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Units sold per SKU over the window
        cur.execute("""
            SELECT bi.sku_id, SUM(bi.qty) AS sold
            FROM bill_items bi
            JOIN bills b ON bi.bill_id = b.bill_id
            WHERE b.status = 'finalized' AND b.finalized_at::date >= %s::date
            GROUP BY bi.sku_id
        """, (start,))
        sold_map = {r["sku_id"]: float(r["sold"]) for r in cur.fetchall()}

        cur.execute("SELECT * FROM products WHERE is_active = TRUE ORDER BY name ASC")
        products = cur.fetchall()
        cur.close()

        suggestions: List[Dict[str, Any]] = []
        for p in products:
            sold = sold_map.get(p["sku_id"], 0.0)
            per_day = round(sold / velocity_days, 2)
            current_qty = p["quantity"]
            reorder_level = p["reorder_level"]

            days_of_cover = round(current_qty / per_day, 1) if per_day > 0 else None
            needs = False
            reason = None

            if per_day > 0 and (days_of_cover is None or days_of_cover < cover_days):
                needs = True
                reason = f"At current velocity ({per_day}/day), stock covers only {days_of_cover} day(s)."
            if current_qty <= reorder_level:
                needs = True
                reason = (reason or "") + f" Stock ({current_qty}) is at/below reorder level ({reorder_level})."

            if needs:
                target_qty = per_day * cover_days if per_day > 0 else reorder_level
                suggested_qty = max(0.0, round(target_qty - current_qty, 1))
                if suggested_qty <= 0 and current_qty <= reorder_level:
                    suggested_qty = max(reorder_level - current_qty, 0.0)
                suggestions.append({
                    "sku_id": p["sku_id"],
                    "name": p["name"],
                    "unit": p["unit"],
                    "current_stock": current_qty,
                    "reorder_level": reorder_level,
                    "avg_daily_velocity": per_day,
                    "days_of_cover": days_of_cover,
                    "suggested_reorder_qty": suggested_qty,
                    "reason": reason.strip()
                })

        suggestions.sort(key=lambda s: (s["days_of_cover"] if s["days_of_cover"] is not None else 9999))
        return {
            "status": "success",
            "count": len(suggestions),
            "message": f"{len(suggestions)} item(s) suggested for reorder based on {velocity_days}-day sales velocity.",
            "suggestions": suggestions
        }
    finally:
        conn.close()


def sales_forecast(days_history: int = 30, forecast_days: int = 7) -> Dict[str, Any]:
    """
    AI-powered demand prediction using moving average sales velocity.
    Predicts next `forecast_days` demand per product based on `days_history` of sales data.
    Flags items likely to stockout before the forecast horizon.
    """
    try:
        dh_int = int(days_history)
        fd_int = int(forecast_days)
    except (ValueError, TypeError):
        return {"status": "error", "message": "days_history and forecast_days must be valid integers."}
    days_history = max(7, min(90, dh_int))
    forecast_days = max(1, min(30, fd_int))
    start = (date.today() - timedelta(days=days_history - 1)).isoformat()

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # Get sales velocity per product over the history window
        cur.execute("""
            SELECT bi.sku_id, p.name, p.unit, p.quantity AS current_stock,
                   p.reorder_level, SUM(bi.qty) AS total_sold
            FROM bill_items bi
            JOIN bills b ON bi.bill_id = b.bill_id
            JOIN products p ON bi.sku_id = p.sku_id
            WHERE b.status = 'finalized' AND b.finalized_at::date >= %s::date
              AND p.is_active = TRUE
            GROUP BY bi.sku_id, p.name, p.unit, p.quantity, p.reorder_level
            ORDER BY total_sold DESC
        """, (start,))
        rows = cur.fetchall()
        cur.close()

        forecasts: List[Dict[str, Any]] = []
        stockout_alerts: List[str] = []

        for r in rows:
            total_sold = float(r["total_sold"])
            daily_velocity = round(total_sold / days_history, 2)
            predicted_demand = round(daily_velocity * forecast_days, 1)
            current_stock = r["current_stock"]
            days_until_stockout = round(current_stock / daily_velocity, 1) if daily_velocity > 0 else None

            will_stockout = days_until_stockout is not None and days_until_stockout < forecast_days
            risk = "🔴 HIGH" if will_stockout else (
                "🟡 MEDIUM" if days_until_stockout and days_until_stockout < forecast_days * 2 else "🟢 LOW"
            )

            forecast = {
                "sku_id": r["sku_id"],
                "name": r["name"],
                "unit": r["unit"],
                "current_stock": current_stock,
                "daily_velocity": daily_velocity,
                "predicted_demand": predicted_demand,
                "days_until_stockout": days_until_stockout,
                "stockout_risk": risk,
                "suggested_order": max(0, round(predicted_demand - current_stock, 1)) if will_stockout else 0
            }
            forecasts.append(forecast)

            if will_stockout:
                stockout_alerts.append(
                    f"⚠️ {r['name']}: stocks out in ~{days_until_stockout} days "
                    f"(need {predicted_demand} {r['unit']}, have {current_stock})"
                )

        lines = [f"📊 **Sales Forecast** (next {forecast_days} days based on {days_history}-day history):\n"]
        if stockout_alerts:
            lines.append(f"🚨 **{len(stockout_alerts)} Stockout Risk(s):**")
            lines.extend(stockout_alerts)
        else:
            lines.append("✅ No stockout risks detected for the forecast period.")

        lines.append(f"\n📈 Tracked {len(forecasts)} active product(s) with recent sales.")

        return {
            "status": "success",
            "forecast_days": forecast_days,
            "history_days": days_history,
            "product_count": len(forecasts),
            "stockout_risks": len(stockout_alerts),
            "forecasts": forecasts,
            "message": "\n".join(lines)
        }
    finally:
        conn.close()


def profit_loss_report(days: int = 30) -> Dict[str, Any]:
    """
    Generate Profit & Loss report:
      - Total Revenue, COGS, Gross Profit, Gross Margin %
      - GST Collected (government liability)
      - Per-product profitability (top 10 most/least profitable)
    """
    try:
        days_int = int(days)
    except (ValueError, TypeError):
        return {"status": "error", "message": "days must be a valid integer."}
    days = max(1, min(365, days_int))
    start = (date.today() - timedelta(days=days - 1)).isoformat()

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # Revenue & GST from finalized bills
        cur.execute("""
            SELECT COALESCE(SUM(subtotal), 0) AS revenue,
                   COALESCE(SUM(cgst + sgst), 0) AS gst_collected,
                   COALESCE(SUM(total), 0) AS total_with_gst,
                   COUNT(*) AS bill_count
            FROM bills
            WHERE status = 'finalized' AND finalized_at::date >= %s::date
        """, (start,))
        summary = cur.fetchone()

        # COGS: cost_price × qty for each sold item
        cur.execute("""
            SELECT bi.sku_id, p.name, p.cost_price, p.mrp, p.unit,
                   SUM(bi.qty) AS qty_sold,
                   SUM(bi.qty * p.cost_price) AS cogs,
                   SUM(bi.qty * bi.unit_price) AS revenue,
                   SUM(bi.qty * (bi.unit_price - p.cost_price)) AS gross_profit
            FROM bill_items bi
            JOIN bills b ON bi.bill_id = b.bill_id
            JOIN products p ON bi.sku_id = p.sku_id
            WHERE b.status = 'finalized' AND b.finalized_at::date >= %s::date
            GROUP BY bi.sku_id, p.name, p.cost_price, p.mrp, p.unit
            ORDER BY gross_profit DESC
        """, (start,))
        product_rows = cur.fetchall()
        cur.close()

        total_revenue = round(summary["revenue"], 2)
        gst_collected = round(summary["gst_collected"], 2)
        total_cogs = round(sum(float(r["cogs"]) for r in product_rows), 2)
        gross_profit = round(total_revenue - total_cogs, 2)
        gross_margin = round((gross_profit / total_revenue) * 100, 1) if total_revenue > 0 else 0.0

        # Per-product profitability
        product_profits = [{
            "name": r["name"],
            "qty_sold": round(float(r["qty_sold"]), 2),
            "unit": r["unit"],
            "revenue": round(float(r["revenue"]), 2),
            "cogs": round(float(r["cogs"]), 2),
            "gross_profit": round(float(r["gross_profit"]), 2),
            "margin_pct": round((float(r["gross_profit"]) / float(r["revenue"])) * 100, 1) if float(r["revenue"]) > 0 else 0.0
        } for r in product_rows]

        top_profitable = product_profits[:10]
        least_profitable = sorted(product_profits, key=lambda x: x["gross_profit"])[:5]

        lines = [
            f"💰 **Profit & Loss Report** (last {days} days)\n",
            f"📊 **Revenue:** ₹{total_revenue:,.2f} ({summary['bill_count']} bills)",
            f"📦 **Cost of Goods Sold:** ₹{total_cogs:,.2f}",
            f"📈 **Gross Profit:** ₹{gross_profit:,.2f}",
            f"📐 **Gross Margin:** {gross_margin}%",
            f"🧾 **GST Collected:** ₹{gst_collected:,.2f} (govt liability)",
        ]

        if top_profitable:
            lines.append("\n🏆 **Top 5 Most Profitable Products:**")
            for i, p in enumerate(top_profitable[:5], 1):
                lines.append(f"   {i}. {p['name']} — ₹{p['gross_profit']:,.2f} ({p['margin_pct']}% margin)")

        return {
            "status": "success",
            "period_days": days,
            "total_revenue": total_revenue,
            "total_cogs": total_cogs,
            "gross_profit": gross_profit,
            "gross_margin_pct": gross_margin,
            "gst_collected": gst_collected,
            "bill_count": summary["bill_count"],
            "top_profitable": top_profitable,
            "least_profitable": least_profitable,
            "message": "\n".join(lines)
        }
    finally:
        conn.close()


def customer_insights(days: int = 30, top_n: int = 10) -> Dict[str, Any]:
    """
    Top customers by spend, visit frequency, average bill size, and loyalty tier.
    """
    try:
        days_int = int(days)
        top_n_int = int(top_n)
    except (ValueError, TypeError):
        return {"status": "error", "message": "days and top_n must be valid integers."}
    days = max(1, min(365, days_int))
    top_n = max(1, min(50, top_n_int))
    start = (date.today() - timedelta(days=days - 1)).isoformat()

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT c.name, c.phone, c.khata_balance,
                   COUNT(b.bill_id) AS visit_count,
                   COALESCE(SUM(b.total), 0) AS total_spend,
                   COALESCE(AVG(b.total), 0) AS avg_bill_size,
                   MAX(b.finalized_at) AS last_visit
            FROM customers c
            LEFT JOIN bills b ON b.customer_id = c.customer_id
                AND b.status = 'finalized' AND b.finalized_at::date >= %s::date
            GROUP BY c.customer_id, c.name, c.phone, c.khata_balance
            HAVING COUNT(b.bill_id) > 0
            ORDER BY total_spend DESC
            LIMIT %s
        """, (start, top_n))
        rows = cur.fetchall()
        cur.close()

        customers: List[Dict[str, Any]] = []
        for r in rows:
            total_spend = round(float(r["total_spend"]), 2)
            # Loyalty tier based on spend in period
            if total_spend >= 10000:
                tier = "🥇 Gold"
            elif total_spend >= 5000:
                tier = "🥈 Silver"
            elif total_spend >= 1000:
                tier = "🥉 Bronze"
            else:
                tier = "⭐ Regular"

            customers.append({
                "name": r["name"],
                "phone": r["phone"] or "N/A",
                "visit_count": r["visit_count"],
                "total_spend": total_spend,
                "avg_bill_size": round(float(r["avg_bill_size"]), 2),
                "khata_pending": round(float(r["khata_balance"]), 2),
                "last_visit": str(r["last_visit"]) if r["last_visit"] else "N/A",
                "loyalty_tier": tier
            })

        lines = [f"👥 **Top {len(customers)} Customers** (last {days} days):\n"]
        for i, c in enumerate(customers, 1):
            lines.append(
                f"{i}. {c['loyalty_tier']} **{c['name']}** — "
                f"₹{c['total_spend']:,.2f} ({c['visit_count']} visit{'s' if c['visit_count'] != 1 else ''}, "
                f"avg ₹{c['avg_bill_size']:,.2f}/bill)"
            )

        return {
            "status": "success",
            "period_days": days,
            "count": len(customers),
            "customers": customers,
            "message": "\n".join(lines)
        }
    finally:
        conn.close()

