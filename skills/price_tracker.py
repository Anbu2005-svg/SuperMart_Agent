"""
Purchase Cost & Price Inflation Tracker Skill.

Monitors vendor wholesale cost price fluctuations over time:
  - Automatically records historical cost and selling price adjustments
  - Detects inflation trends and cost surges across inventory items
  - Flags margin shrinkage (when supplier cost rises but retail price wasn't updated)
  - Recommends optimized retail prices to maintain healthy supermarket profit margins (e.g. 20%)
"""

import math
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from db.models import get_db_connection, immediate_transaction


def log_price_change(
    sku_id: str,
    new_cost_price: Optional[float] = None,
    new_selling_price: Optional[float] = None,
    source: str = "manual"
) -> Dict[str, Any]:
    """
    Update product prices and log the transition to price_history.
    """
    if not sku_id:
        return {"status": "error", "message": "sku_id is required."}

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT sku_id, name, cost_price, mrp FROM products WHERE sku_id = %s", (sku_id.strip(),))
        product = cur.fetchone()
        if not product:
            return {"status": "error", "message": f"Product with SKU '{sku_id}' not found."}

        old_cost = float(product["cost_price"] or 0.0)
        old_selling = float(product["mrp"] or 0.0)

        effective_cost = float(new_cost_price) if new_cost_price is not None else old_cost
        effective_selling = float(new_selling_price) if new_selling_price is not None else old_selling

        if not math.isfinite(effective_cost) or effective_cost < 0:
            return {"status": "error", "message": "Cost price must be non-negative."}
        if not math.isfinite(effective_selling) or effective_selling < 0:
            return {"status": "error", "message": "Selling price must be non-negative."}

        with immediate_transaction(conn):
            # Record audit history
            cur.execute("""
                INSERT INTO price_history (
                    sku_id, old_cost_price, new_cost_price, old_selling_price, new_selling_price, source
                ) VALUES (%s, %s, %s, %s, %s, %s);
            """, (sku_id.strip(), old_cost, effective_cost, old_selling, effective_selling, source.strip()))

            # Update product master record
            cur.execute("""
                UPDATE products 
                SET cost_price = %s, mrp = %s
                WHERE sku_id = %s;
            """, (effective_cost, effective_selling, sku_id.strip()))

        return {
            "status": "success",
            "message": f"Price updated and logged for '{product['name']}'.",
            "sku_id": sku_id,
            "product_name": product["name"],
            "old_cost_price": old_cost,
            "new_cost_price": effective_cost,
            "old_selling_price": old_selling,
            "new_selling_price": effective_selling
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to log price change: {str(e)}"}
    finally:
        conn.close()


def check_price_inflation(days: int = 90, target_margin_pct: float = 20.0) -> Dict[str, Any]:
    """
    Analyze cost increases over the past N days and identify products with margin compression.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        since_date = datetime.now() - timedelta(days=max(1, int(days)))

        cur.execute("""
            SELECT 
                p.sku_id, p.name, p.category, p.cost_price AS current_cost, p.mrp AS current_selling,
                h.old_cost_price AS initial_cost, h.recorded_at
            FROM products p
            JOIN (
                SELECT DISTINCT ON (sku_id) sku_id, old_cost_price, recorded_at
                FROM price_history
                WHERE recorded_at >= %s AND old_cost_price > 0
                ORDER BY sku_id, recorded_at ASC
            ) h ON p.sku_id = h.sku_id
            WHERE p.cost_price > h.old_cost_price;
        """, (since_date,))
        rows = cur.fetchall()

        inflation_items = []
        for r in rows:
            cur_cost = float(r["current_cost"])
            init_cost = float(r["initial_cost"])
            cur_selling = float(r["current_selling"])

            cost_inflation_pct = round(((cur_cost - init_cost) / init_cost) * 100, 2)
            cur_margin_pct = round(((cur_selling - cur_cost) / cur_selling * 100) if cur_selling > 0 else 0.0, 2)
            
            # Target selling price = cost / (1 - target_margin)
            target_margin_frac = min(0.8, max(0.05, float(target_margin_pct) / 100.0))
            recommended_selling = round(cur_cost / (1.0 - target_margin_frac), 2)

            margin_shrunk = cur_margin_pct < target_margin_pct

            inflation_items.append({
                "sku_id": r["sku_id"],
                "name": r["name"],
                "category": r["category"] or "General",
                "initial_cost_price": round(init_cost, 2),
                "current_cost_price": round(cur_cost, 2),
                "current_selling_price": round(cur_selling, 2),
                "cost_inflation_pct": cost_inflation_pct,
                "current_margin_pct": cur_margin_pct,
                "target_margin_pct": round(target_margin_pct, 1),
                "margin_compressed": margin_shrunk,
                "recommended_selling_price": recommended_selling
            })

        # Sort by highest cost inflation
        inflation_items.sort(key=lambda x: x["cost_inflation_pct"], reverse=True)

        return {
            "status": "success",
            "period_days": days,
            "inflated_products_count": len(inflation_items),
            "margin_compressed_count": len([i for i in inflation_items if i["margin_compressed"]]),
            "items": inflation_items
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to calculate price inflation: {str(e)}"}
    finally:
        conn.close()


def get_product_price_history(sku_or_name: str) -> Dict[str, Any]:
    """Retrieve chronological price history entries for a specific product."""
    if not sku_or_name:
        return {"status": "error", "message": "sku_or_name is required."}

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT sku_id, name, cost_price, mrp 
            FROM products 
            WHERE sku_id = %s OR name ILIKE %s LIMIT 1;
        """, (sku_or_name.strip(), f"%{sku_or_name.strip()}%"))
        prod = cur.fetchone()
        if not prod:
            return {"status": "error", "message": f"Product '{sku_or_name}' not found."}

        cur.execute("""
            SELECT id, old_cost_price, new_cost_price, old_selling_price, new_selling_price, source, recorded_at
            FROM price_history
            WHERE sku_id = %s
            ORDER BY recorded_at DESC;
        """, (prod["sku_id"],))
        history = cur.fetchall()

        return {
            "status": "success",
            "sku_id": prod["sku_id"],
            "name": prod["name"],
            "current_cost_price": float(prod["cost_price"] or 0.0),
            "current_selling_price": float(prod["mrp"] or 0.0),
            "total_revisions": len(history),
            "history": [
                {
                    "id": h["id"],
                    "old_cost": float(h["old_cost_price"] or 0.0),
                    "new_cost": float(h["new_cost_price"] or 0.0),
                    "old_selling": float(h["old_selling_price"] or 0.0),
                    "new_selling": float(h["new_selling_price"] or 0.0),
                    "source": h["source"],
                    "recorded_at": str(h["recorded_at"])
                }
                for h in history
            ]
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to fetch price history: {str(e)}"}
    finally:
        conn.close()
