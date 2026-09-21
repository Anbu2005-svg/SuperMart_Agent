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
