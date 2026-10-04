"""
Customer Self-Service Digital Catalog Skill.

Empowers neighborhood customers to browse store products directly:
  - Generates categorized digital catalog with real-time stock availability and prices
  - Produces mobile-friendly, beautiful standalone HTML interactive catalog
  - Formats instant Telegram / WhatsApp shareable text catalogs
  - Embeds WhatsApp 1-click order buttons for frictionless grocery ordering
"""

import os
import html
from typing import Dict, Any, List, Optional
from db.models import get_db_connection
from skills.whatsapp import DEFAULT_SHOP_NAME

DEFAULT_PHONE_NUMBER = os.getenv("SHOP_PHONE", "919876543210").strip()


def generate_digital_catalog(
    category: Optional[str] = None,
    in_stock_only: bool = True,
    search_query: Optional[str] = None
) -> Dict[str, Any]:
    """
    Retrieve structured catalog items grouped by category with formatted text for messaging.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        query = """
            SELECT sku_id, name, barcode, category, mrp, quantity, unit
            FROM products
            WHERE 1=1
        """
        params = []

        if in_stock_only:
            query += " AND quantity > 0"

        if category and category.strip():
            query += " AND category ILIKE %s"
            params.append(f"%{category.strip()}%")

        if search_query and search_query.strip():
            query += " AND (name ILIKE %s OR category ILIKE %s)"
            q = f"%{search_query.strip()}%"
            params.extend([q, q])

        query += " ORDER BY category ASC, name ASC;"
        cur.execute(query, tuple(params))
        rows = cur.fetchall()

        # Group by category
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            cat = r["category"] or "General Groceries"
            if cat not in grouped:
                grouped[cat] = []
            grouped[cat].append({
                "sku_id": r["sku_id"],
                "name": r["name"],
                "price": float(r["mrp"] or 0.0),
                "unit": r["unit"] or "pcs",
                "in_stock": float(r["quantity"] or 0.0) > 0,
                "stock_quantity": float(r["quantity"] or 0.0)
            })

        # Generate Telegram / WhatsApp friendly markdown format
        msg_lines = [f"🏬 *{DEFAULT_SHOP_NAME} - Digital Catalog*", "━━━━━━━━━━━━━━━━━━━━"]
        if category:
            msg_lines.append(f"📂 *Category:* {category}")
        msg_lines.append(f"📦 Total Items: {len(rows)}\n")

        for cat_name, items in grouped.items():
            msg_lines.append(f"🏷️ *{cat_name.upper()}*")
            for item in items:
                stock_tag = "✅ In Stock" if item["in_stock"] else "❌ Out of Stock"
                msg_lines.append(f"• *{item['name']}* — ₹{item['price']:.2f}/{item['unit']} ({stock_tag})")
            msg_lines.append("")

        msg_lines.append("📲 *To Order:* Reply with the items you need or call us!")

        return {
            "status": "success",
            "total_items": len(rows),
            "categories_count": len(grouped),
            "categories": list(grouped.keys()),
            "catalog_by_category": grouped,
            "formatted_message": "\n".join(msg_lines)
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to generate catalog: {str(e)}"}
    finally:
        conn.close()


def export_html_catalog(
    file_path: str = "data/store_catalog.html",
    shop_name: str = DEFAULT_SHOP_NAME,
    phone_number: str = DEFAULT_PHONE_NUMBER
) -> Dict[str, Any]:
    """
    Generate an offline-capable, responsive HTML digital catalog with instant search & filter.
    """
    catalog_res = generate_digital_catalog(in_stock_only=False)
    if catalog_res["status"] != "success":
        return catalog_res

    grouped = catalog_res["catalog_by_category"]

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{html.escape(shop_name)} - Online Catalog</title>
    <style>
        :root {{
            --primary: #10b981;
            --primary-dark: #059669;
            --bg: #f8fafc;
            --card-bg: #ffffff;
            --text-main: #0f172a;
            --text-muted: #64748b;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text-main);
            margin: 0;
            padding: 16px;
        }}
        .header {{
            text-align: center;
            padding: 24px 12px;
            background: linear-gradient(135deg, #10b981, #047857);
            color: white;
            border-radius: 16px;
            margin-bottom: 20px;
            box-shadow: 0 4px 14px rgba(16, 185, 129, 0.25);
        }}
        .header h1 {{ margin: 0 0 8px 0; font-size: 24px; }}
        .header p {{ margin: 0; opacity: 0.9; font-size: 14px; }}
        .search-box {{
            width: 100%;
            max-width: 500px;
            margin: 0 auto 20px auto;
            display: block;
            padding: 12px 18px;
            font-size: 16px;
            border: 1px solid #cbd5e1;
            border-radius: 9999px;
            box-sizing: border-box;
            outline: none;
            box-shadow: 0 2px 6px rgba(0,0,0,0.05);
        }}
        .search-box:focus {{ border-color: var(--primary); }}
        .category-section {{
            margin-bottom: 24px;
        }}
        .category-title {{
            font-size: 18px;
            font-weight: 700;
            margin-bottom: 12px;
            color: #065f46;
            border-left: 4px solid var(--primary);
            padding-left: 8px;
        }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
            gap: 12px;
        }}
        .item-card {{
            background: var(--card-bg);
            padding: 14px;
            border-radius: 12px;
            border: 1px solid #e2e8f0;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            box-shadow: 0 1px 3px rgba(0,0,0,0.04);
        }}
        .item-name {{ font-weight: 600; font-size: 15px; margin-bottom: 6px; }}
        .item-price {{ font-size: 18px; font-weight: 700; color: #047857; }}
        .item-unit {{ font-size: 12px; color: var(--text-muted); font-weight: normal; }}
        .badge {{
            display: inline-block;
            font-size: 11px;
            padding: 3px 8px;
            border-radius: 6px;
            font-weight: 600;
            margin-top: 8px;
        }}
        .badge-instock {{ background: #d1fae5; color: #065f46; }}
        .badge-outstock {{ background: #fee2e2; color: #991b1b; }}
        .wa-btn {{
            display: block;
            text-align: center;
            background: #25d366;
            color: white;
            text-decoration: none;
            padding: 8px;
            border-radius: 8px;
            margin-top: 10px;
            font-weight: 600;
            font-size: 13px;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>{html.escape(shop_name)}</h1>
        <p>Live Digital Store Catalog • Order via WhatsApp or Phone</p>
    </div>

    <input type="text" id="searchInput" class="search-box" placeholder="🔍 Search product or category..." onkeyup="filterItems()">

    <div id="catalogContainer">
"""

    for cat_name, items in grouped.items():
        html_content += f"""
        <div class="category-section" data-category="{html.escape(cat_name.lower())}">
            <div class="category-title">📂 {html.escape(cat_name)}</div>
            <div class="grid">
        """
        for it in items:
            badge_cls = "badge-instock" if it["in_stock"] else "badge-outstock"
            badge_txt = "In Stock" if it["in_stock"] else "Out of Stock"
            wa_order_link = f"https://wa.me/{phone_number}?text=Hello%2C+I+want+to+order+{html.escape(it['name'])}+priced+Rs.{it['price']:.2f}"
            html_content += f"""
                <div class="item-card" data-name="{html.escape(it['name'].lower())}">
                    <div>
                        <div class="item-name">{html.escape(it['name'])}</div>
                        <div class="item-price">₹{it['price']:.2f} <span class="item-unit">/ {html.escape(it['unit'])}</span></div>
                        <span class="badge {badge_cls}">{badge_txt}</span>
                    </div>
                    <a href="{wa_order_link}" target="_blank" class="wa-btn">💬 Order on WhatsApp</a>
                </div>
            """
        html_content += """
            </div>
        </div>
        """

    html_content += """
    </div>

    <script>
        function filterItems() {
            const query = document.getElementById('searchInput').value.toLowerCase();
            const cards = document.querySelectorAll('.item-card');
            cards.forEach(card => {
                const name = card.getAttribute('data-name');
                if (name.includes(query)) {
                    card.style.display = 'flex';
                } else {
                    card.style.display = 'none';
                }
            });
            const sections = document.querySelectorAll('.category-section');
            sections.forEach(sec => {
                const visibleCards = sec.querySelectorAll('.item-card[style*="display: flex"]');
                sec.style.display = (query === "" || visibleCards.length > 0) ? "block" : "none";
            });
        }
    </script>
</body>
</html>
"""

    from skills.security import validate_safe_workspace_path
    ok, target_path, err_msg = validate_safe_workspace_path(
        file_path, default_dir="data", allowed_dirs=["data", "generated_docs"]
    )
    if not ok:
        return {"status": "error", "message": f"Unauthorized HTML catalog export path: {err_msg}"}

    with open(target_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return {
        "status": "success",
        "file_path": target_path,
        "total_items": catalog_res["total_items"],
        "message": f"Digital HTML catalog created at '{target_path}'."
    }
