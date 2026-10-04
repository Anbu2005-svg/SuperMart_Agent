"""
Seasonal & Festival Demand Intelligence Skill for SuperMart AI Ops Agent.

Analyzes product demand patterns across Indian retail festivals and weather seasons:
  - Pongal / Makar Sankranti (January)
  - Summer Season (March - June)
  - Monsoon (July - August)
  - Navratri / Dussehra / Diwali (September - November)
  - Year-End / Christmas (December)

Calculates historical sales spikes, category lift percentages, and recommended safety stock buffers.
"""

from typing import Dict, Any, List, Optional
from datetime import datetime
from db.models import get_db_connection

# Festival and Seasonal Profiles with associated categories and historical lift factors
FESTIVAL_CALENDAR = {
    "diwali": {
        "name": "Diwali Festival of Lights",
        "months": [10, 11],
        "key_categories": ["Cooking Oils & Ghee", "Sweets & Snacks", "Grains & Flour", "Beverages"],
        "typical_lift_pct": 85.0,
        "recommended_buffer_days": 14,
        "highlights": "Massive surge in cooking oil, atta, ghee, maida, sugar, and dry fruits for festive treats."
    },
    "pongal": {
        "name": "Pongal & Makar Sankranti",
        "months": [1],
        "key_categories": ["Grains & Flour", "Dairy & Eggs", "Edible Oils", "Spices"],
        "typical_lift_pct": 70.0,
        "recommended_buffer_days": 10,
        "highlights": "Heavy demand for raw rice, jaggery, ghee, cashews, raisins, and moong dal."
    },
    "summer": {
        "name": "Peak Summer Season",
        "months": [3, 4, 5, 6],
        "key_categories": ["Beverages", "Dairy & Eggs", "Snacks"],
        "typical_lift_pct": 60.0,
        "recommended_buffer_days": 7,
        "highlights": "Spike in bottled water, cold beverages, fruit juices, curd, butter milk, and ice creams."
    },
    "monsoon": {
        "name": "Monsoon / Rainy Season",
        "months": [7, 8],
        "key_categories": ["Tea & Coffee", "Instant Foods", "Snacks"],
        "typical_lift_pct": 45.0,
        "recommended_buffer_days": 7,
        "highlights": "Increased velocity in tea, coffee, instant noodles, biscuits, and frying snacks."
    },
    "year_end": {
        "name": "Christmas & New Year",
        "months": [12],
        "key_categories": ["Bakery & Cakes", "Chocolates", "Beverages"],
        "typical_lift_pct": 50.0,
        "recommended_buffer_days": 10,
        "highlights": "High demand for plum cakes, chocolates, dairy butter, and soft drinks."
    }
}


def get_seasonal_demand_insights(
    festival_or_season: Optional[str] = None
) -> Dict[str, Any]:
    """
    Retrieve seasonal & festival demand analysis with category lift rates and stock recommendations.
    - festival_or_season: 'diwali', 'pongal', 'summer', 'monsoon', 'year_end' (or None for current month)
    """
    current_month = datetime.now().month
    selected_key = None

    if festival_or_season:
        f_clean = festival_or_season.strip().lower()
        for k in FESTIVAL_CALENDAR:
            if k in f_clean or f_clean in k:
                selected_key = k
                break
    else:
        # Match current or nearest upcoming festival
        for k, data in FESTIVAL_CALENDAR.items():
            if current_month in data["months"]:
                selected_key = k
                break
        if not selected_key:
            selected_key = "diwali"

    if not selected_key or selected_key not in FESTIVAL_CALENDAR:
        available = ", ".join(FESTIVAL_CALENDAR.keys())
        return {"status": "error", "message": f"Season not found. Available profiles: {available}"}

    profile = FESTIVAL_CALENDAR[selected_key]

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Query products in key categories and assess current stock buffer
        cur.execute("""
            SELECT p.sku_id, p.name, p.category, p.quantity, p.reorder_level, p.mrp
            FROM products p
            WHERE p.is_active = TRUE
            ORDER BY p.category ASC, p.name ASC
        """)
        products = cur.fetchall()
        cur.close()

        impacted_skus = []
        for p in products:
            p_cat = p["category"]
            # Check if product belongs to seasonal categories or keyword matches
            is_relevant = any(c.lower() in p_cat.lower() for c in profile["key_categories"]) or \
                          any(w in p["name"].lower() for w in ("oil", "atta", "rice", "sugar", "tea", "coffee", "milk", "butter", "maggi"))

            if is_relevant:
                lift = profile["typical_lift_pct"]
                # Recommended festive safety buffer
                festival_safe_buffer = round(p["reorder_level"] * (1 + lift / 100.0), 0)
                shortfall = max(0.0, festival_safe_buffer - p["quantity"])
                impacted_skus.append({
                    "sku_id": p["sku_id"],
                    "name": p["name"],
                    "category": p["category"],
                    "current_stock": p["quantity"],
                    "normal_reorder_level": p["reorder_level"],
                    "projected_festive_requirement": festival_safe_buffer,
                    "recommended_additional_order": shortfall,
                    "status": "🚨 Buffer Shortfall" if shortfall > 0 else "✅ Sufficient"
                })

        shortfall_count = sum(1 for s in impacted_skus if s["recommended_additional_order"] > 0)

        return {
            "status": "success",
            "festival_season": profile["name"],
            "key": selected_key,
            "typical_sales_lift": f"+{profile['typical_lift_pct']}%",
            "recommended_buffer_window_days": profile["recommended_buffer_days"],
            "highlights": profile["highlights"],
            "total_items_analyzed": len(impacted_skus),
            "items_needing_buffer_stock": shortfall_count,
            "recommended_stock_adjustments": impacted_skus[:15]
        }
    finally:
        conn.close()


def get_upcoming_festival_projections() -> Dict[str, Any]:
    """Overview of all annual seasonal spikes and planning calendar."""
    overview = []
    for k, v in FESTIVAL_CALENDAR.items():
        month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        active_months = ", ".join([month_names[m - 1] for m in v["months"]])
        overview.append({
            "key": k,
            "festival": v["name"],
            "peak_months": active_months,
            "historical_lift": f"+{v['typical_lift_pct']}%",
            "focus_categories": v["key_categories"],
            "action_guidelines": v["highlights"]
        })

    return {
        "status": "success",
        "festivals_count": len(overview),
        "calendar": overview
    }
