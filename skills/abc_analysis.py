"""
ABC Inventory Classification Skill.

Classifies supermarket inventory based on revenue impact (Pareto 80/20 Rule):
  - Class A (Top 80% Revenue): High-value, fast-moving items. Must never run out of stock.
  - Class B (Next 15% Revenue): Moderate sales, steady demand. Moderate safety stock.
  - Class C (Bottom 5% Revenue & Non-movers): Slow-moving items. Avoid tying up working capital.
"""

from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from db.models import get_db_connection


def compute_abc_classification(days: int = 60) -> Dict[str, Any]:
    """
    Perform ABC inventory categorization on store products based on sales revenue.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        since_date = datetime.now() - timedelta(days=max(1, int(days)))

        # 1. Total revenue per product from finalized bills
        cur.execute("""
            SELECT 
                p.sku_id, p.name, p.category, p.cost_price, p.mrp, p.quantity,
                COALESCE(SUM(bi.qty), 0) AS total_units_sold,
                COALESCE(SUM(bi.qty * bi.unit_price), 0) AS total_revenue
            FROM products p
            LEFT JOIN bill_items bi ON p.sku_id = bi.sku_id
            LEFT JOIN bills b ON bi.bill_id = b.bill_id AND b.created_at >= %s
            GROUP BY p.sku_id, p.name, p.category, p.cost_price, p.mrp, p.quantity
            ORDER BY total_revenue DESC;
        """, (since_date,))
        rows = cur.fetchall()

        if not rows:
            return {"status": "error", "message": "No inventory products found in database."}

        total_store_revenue = sum(float(r["total_revenue"]) for r in rows)

        class_a = []
        class_b = []
        class_c = []

        cumulative_rev = 0.0

        for r in rows:
            rev = float(r["total_revenue"])
            units = float(r["total_units_sold"])
            cumulative_rev += rev
            cum_pct = (cumulative_rev / total_store_revenue * 100) if total_store_revenue > 0 else 100.0

            item_info = {
                "sku_id": r["sku_id"],
                "name": r["name"],
                "category": r["category"] or "General",
                "units_sold": round(units, 2),
                "revenue": round(rev, 2),
                "revenue_share_pct": round((rev / total_store_revenue * 100) if total_store_revenue > 0 else 0.0, 2),
                "current_stock": float(r["quantity"] or 0.0),
                "mrp": float(r["mrp"] or 0.0)
            }

            if total_store_revenue == 0:
                class_c.append(item_info)
            elif cum_pct <= 80.0 or len(class_a) == 0:
                class_a.append(item_info)
            elif cum_pct <= 95.0 or len(class_b) == 0:
                class_b.append(item_info)
            else:
                class_c.append(item_info)

        def _summarize_class(items: List[Dict[str, Any]]) -> Dict[str, Any]:
            rev = sum(i["revenue"] for i in items)
            return {
                "count": len(items),
                "total_revenue": round(rev, 2),
                "revenue_pct": round((rev / total_store_revenue * 100) if total_store_revenue > 0 else 0.0, 2),
                "items": items[:15]
            }

        return {
            "status": "success",
            "period_days": days,
            "total_products_evaluated": len(rows),
            "total_period_revenue": round(total_store_revenue, 2),
            "class_a": _summarize_class(class_a),
            "class_b": _summarize_class(class_b),
            "class_c": _summarize_class(class_c),
            "strategic_recommendations": {
                "class_a": f"Class A ({len(class_a)} items): Drive ~80% of sales. Maintain tight re-order points and zero stockouts.",
                "class_b": f"Class B ({len(class_b)} items): Steady sellers. Order on standard bi-weekly schedule.",
                "class_c": f"Class C ({len(class_c)} items): Low revenue contribution. Avoid bulk ordering to prevent locked capital."
            }
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to compute ABC classification: {str(e)}"}
    finally:
        conn.close()
