"""
Smart Cross-Sell & Market Basket Recommendation Skill.

Uses Market Basket Analysis (Association Mining) on past store bills:
  - Discovers items frequently bought together (e.g. Milk & Bread, Tea & Sugar, Maggie & Cheese)
  - Provides real-time cross-sell suggestions during checkout to increase average basket value
  - Identifies top co-purchased product pairs across the supermarket
"""

from collections import Counter
from typing import Dict, Any, List, Optional
from db.models import get_db_connection


def get_cross_sell_suggestions(
    items: Any,
    top_n: int = 5
) -> Dict[str, Any]:
    """
    Given items currently being billed or viewed, recommend top complementary items.
    """
    if not items:
        return {"status": "error", "message": "At least one product item or name is required."}

    if isinstance(items, str):
        query_items = [i.strip() for i in items.split(",") if i.strip()]
    elif isinstance(items, list):
        query_items = [str(i).strip() for i in items if str(i).strip()]
    else:
        query_items = [str(items).strip()]

    if not query_items:
        return {"status": "error", "message": "Invalid item list."}

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        matched_skus = set()
        matched_names = []
        for q in query_items:
            cur.execute("""
                SELECT sku_id, name FROM products 
                WHERE sku_id = %s OR name ILIKE %s LIMIT 1;
            """, (q, f"%{q}%"))
            r = cur.fetchone()
            if r:
                matched_skus.add(r["sku_id"])
                matched_names.append(r["name"])

        if not matched_skus:
            # Fallback to general best sellers
            cur.execute("""
                SELECT p.sku_id, p.name, p.category, p.mrp, SUM(bi.qty) as sold
                FROM bill_items bi
                JOIN products p ON bi.sku_id = p.sku_id
                GROUP BY p.sku_id, p.name, p.category, p.mrp
                ORDER BY sold DESC LIMIT %s;
            """, (top_n,))
            bestsellers = cur.fetchall()
            return {
                "status": "success",
                "source": "store_bestsellers_fallback",
                "target_items": query_items,
                "suggestions": [
                    {
                        "sku_id": b["sku_id"],
                        "name": b["name"],
                        "category": b["category"],
                        "selling_price": float(b["mrp"] or 0.0),
                        "reason": "Top-selling store favorite"
                    }
                    for b in bestsellers
                ]
            }

        sku_list = list(matched_skus)
        cur.execute("""
            SELECT DISTINCT bill_id 
            FROM bill_items 
            WHERE sku_id = ANY(%s);
        """, (sku_list,))
        target_bills = [r["bill_id"] for r in cur.fetchall()]

        if not target_bills:
            # Fallback to catalog items
            cur.execute("""
                SELECT DISTINCT p.sku_id, p.name, p.category, p.mrp 
                FROM products p
                WHERE p.sku_id != ALL(%s) AND p.quantity > 0
                ORDER BY p.mrp DESC LIMIT %s;
            """, (sku_list, top_n))
            fallback = cur.fetchall()
            return {
                "status": "success",
                "source": "category_catalog_fallback",
                "target_items": matched_names,
                "suggestions": [
                    {
                        "sku_id": f["sku_id"],
                        "name": f["name"],
                        "category": f["category"],
                        "selling_price": float(f["mrp"] or 0.0),
                        "reason": "Popular in store inventory"
                    }
                    for f in fallback
                ]
            }

        total_target_bills = len(target_bills)

        cur.execute("""
            SELECT bi.sku_id, p.name, p.category, p.mrp, COUNT(DISTINCT bi.bill_id) as co_occurrences
            FROM bill_items bi
            JOIN products p ON bi.sku_id = p.sku_id
            WHERE bi.bill_id = ANY(%s) AND bi.sku_id != ALL(%s)
            GROUP BY bi.sku_id, p.name, p.category, p.mrp
            ORDER BY co_occurrences DESC
            LIMIT %s;
        """, (target_bills, sku_list, top_n))
        co_rows = cur.fetchall()

        suggestions = []
        for r in co_rows:
            co_cnt = int(r["co_occurrences"])
            confidence_pct = round((co_cnt / total_target_bills) * 100, 1)
            suggestions.append({
                "sku_id": r["sku_id"],
                "name": r["name"],
                "category": r["category"],
                "selling_price": float(r["mrp"] or 0.0),
                "co_occurrence_count": co_cnt,
                "confidence_pct": confidence_pct,
                "reason": f"Bought together in {confidence_pct}% of past orders"
            })

        return {
            "status": "success",
            "source": "market_basket_analysis",
            "target_items": matched_names,
            "orders_analyzed": total_target_bills,
            "suggestions_count": len(suggestions),
            "suggestions": suggestions
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to get cross-sell suggestions: {str(e)}"}
    finally:
        conn.close()


def get_top_market_baskets(top_n: int = 10) -> Dict[str, Any]:
    """
    Identify the most frequent paired products bought together across all bills.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                p1.name AS item_a,
                p2.name AS item_b,
                COUNT(*) AS pair_frequency
            FROM bill_items b1
            JOIN bill_items b2 ON b1.bill_id = b2.bill_id AND b1.sku_id < b2.sku_id
            JOIN products p1 ON b1.sku_id = p1.sku_id
            JOIN products p2 ON b2.sku_id = p2.sku_id
            GROUP BY p1.name, p2.name
            ORDER BY pair_frequency DESC
            LIMIT %s;
        """, (top_n,))
        rows = cur.fetchall()

        baskets = [
            {
                "item_a": r["item_a"],
                "item_b": r["item_b"],
                "pair_frequency": int(r["pair_frequency"]),
                "description": f"🛒 Frequently Paired: {r['item_a']} + {r['item_b']} ({r['pair_frequency']} times)"
            }
            for r in rows
        ]

        return {
            "status": "success",
            "total_baskets_found": len(baskets),
            "top_pairs": baskets
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to retrieve market baskets: {str(e)}"}
    finally:
        conn.close()
