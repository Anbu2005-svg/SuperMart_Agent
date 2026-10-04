"""
AI Sales & Demand Anomaly Detection Skill for SuperMart AI Ops Agent.

Detects unusual sales volume patterns using statistical run-rate analysis (Z-score & zero-sales dropouts):
  - Demand Spikes (Surge Anomalies): Outlier high daily volumes (panic buying, bulk hoarding, sudden trends).
  - Deadlock Drops: Popular fast-moving products that abruptly dropped to zero sales (out-of-shelf, hidden in stockroom).
  - Stock Run-Out Risks: Items with high velocity projected to stockout within 48 hours.
"""

import math
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from db.models import get_db_connection


def detect_sales_anomalies(
    days: int = 14,
    z_threshold: float = 2.0
) -> Dict[str, Any]:
    """
    Detect statistical sales anomalies across all supermarket products.
    - days: Analysis window (default 14 days)
    - z_threshold: Standard deviations threshold to trigger anomaly alert (default 2.0)
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # 1. Daily item sales over the lookback window
        cur.execute("""
            SELECT 
                DATE(b.finalized_at) AS sale_date,
                bi.sku_id,
                p.name AS product_name,
                p.category,
                p.quantity AS current_stock,
                SUM(bi.qty) AS daily_qty_sold
            FROM bill_items bi
            JOIN bills b ON bi.bill_id = b.bill_id
            JOIN products p ON bi.sku_id = p.sku_id
            WHERE b.status = 'finalized'
              AND b.finalized_at >= CURRENT_DATE - INTERVAL '%s days'
            GROUP BY DATE(b.finalized_at), bi.sku_id, p.name, p.category, p.quantity
            ORDER BY bi.sku_id, sale_date ASC
        """, (days,))
        rows = cur.fetchall()

        # 2. Today's sales
        cur.execute("""
            SELECT bi.sku_id, SUM(bi.qty) AS today_qty
            FROM bill_items bi
            JOIN bills b ON bi.bill_id = b.bill_id
            WHERE b.status = 'finalized'
              AND DATE(b.finalized_at) = CURRENT_DATE
            GROUP BY bi.sku_id
        """)
        today_sales_rows = cur.fetchall()
        today_sales_map = {r["sku_id"]: float(r["today_qty"]) for r in today_sales_rows}

        # 3. All active products to detect zero-sales anomalies
        cur.execute("SELECT sku_id, name, category, quantity, reorder_level FROM products WHERE is_active = TRUE")
        all_products = cur.fetchall()
        cur.close()

        # Group historical daily quantities per SKU
        sku_hist: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            sid = r["sku_id"]
            if sid not in sku_hist:
                sku_hist[sid] = {
                    "sku_id": sid,
                    "name": r["product_name"],
                    "category": r["category"],
                    "current_stock": float(r["current_stock"]),
                    "daily_quantities": []
                }
            sku_hist[sid]["daily_quantities"].append(float(r["daily_qty_sold"]))

        anomalies = []

        # Analyze each SKU with history
        for sid, data in sku_hist.items():
            qtys = data["daily_quantities"]
            n = len(qtys)
            if n < 2:
                continue

            mean = sum(qtys) / n
            variance = sum((x - mean) ** 2 for x in qtys) / (n - 1)
            stdev = math.sqrt(variance)

            today_val = today_sales_map.get(sid, 0.0)

            # Check 1: Demand Surge Anomaly (Today is high outlier)
            if stdev > 0 and today_val > 0:
                z = (today_val - mean) / stdev
                if z >= z_threshold:
                    anomalies.append({
                        "anomaly_type": "DEMAND_SURGE",
                        "severity": "CRITICAL" if z > 3.0 else "WARNING",
                        "sku_id": sid,
                        "product_name": data["name"],
                        "category": data["category"],
                        "today_sales": today_val,
                        "mean_daily_sales": round(mean, 2),
                        "z_score": round(z, 2),
                        "description": f"Unusual sales spike! Sold {today_val:.0f} units today vs 14-day average of {mean:.1f} (Z-score: +{z:.1f}σ).",
                        "action": "Check inventory replenishment and verify if bulk discount / event is driving demand."
                    })

            # Check 2: Imminent Stockout Risk based on velocity
            if mean > 0 and data["current_stock"] > 0:
                days_left = data["current_stock"] / mean
                if days_left <= 2.0:
                    anomalies.append({
                        "anomaly_type": "IMMINENT_STOCKOUT_RISK",
                        "severity": "HIGH",
                        "sku_id": sid,
                        "product_name": data["name"],
                        "category": data["category"],
                        "current_stock": data["current_stock"],
                        "mean_daily_sales": round(mean, 2),
                        "projected_days_remaining": round(days_left, 1),
                        "description": f"Stock depleted rapidly! At current velocity of {mean:.1f}/day, remaining {data['current_stock']:.0f} units will stock out in {days_left:.1f} days.",
                        "action": "Notify supplier for reorder immediately to avoid lost sales."
                    })

            # Check 3: Sudden Sales Dropout (Fast mover dropped to 0 sales)
            if mean >= 3.0 and today_val == 0.0:
                anomalies.append({
                    "anomaly_type": "VELOCITY_DROPOUT",
                    "severity": "MEDIUM",
                    "sku_id": sid,
                    "product_name": data["name"],
                    "category": data["category"],
                    "mean_daily_sales": round(mean, 2),
                    "today_sales": 0.0,
                    "description": f"Fast-moving product (avg {mean:.1f}/day) had 0 recorded sales today.",
                    "action": "Inspect storefront shelf to verify item is visible, priced, and not hidden in backroom."
                })

        return {
            "status": "success",
            "lookback_days": days,
            "z_threshold": z_threshold,
            "total_products_monitored": len(sku_hist),
            "anomalies_detected_count": len(anomalies),
            "anomalies": anomalies
        }
    finally:
        conn.close()
