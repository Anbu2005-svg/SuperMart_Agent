"""
Interactive Web Dashboard & KPI Operations Hub for SuperMart AI Ops Agent.

Compiles real-time metrics across:
  - Today's Revenue, Bills Count, Average Basket Value
  - Live Cash Drawer float and balance
  - Low stock warning alerts
  - GST Net Tax Liability (ITC)
  - Top category velocity
  - Recent transactions table
Generates a standalone, responsive, high-aesthetic HTML5/CSS interactive dashboard file.
"""

import os
from typing import Dict, Any, List, Optional
from datetime import datetime
from db.models import get_db_connection
from skills.cash_drawer import get_cash_drawer_status
from skills.gst_itc import get_gst_itc_summary
from skills.reorder_alerts import check_low_stock_reorder_alerts


def get_dashboard_summary() -> Dict[str, Any]:
    """Retrieve unified executive dashboard summary across all supermarket subsystems."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # 1. Today's sales performance
        cur.execute("""
            SELECT 
                COUNT(*) AS total_bills,
                COALESCE(SUM(total), 0) AS total_revenue,
                COALESCE(SUM(cgst + sgst), 0) AS total_tax,
                COALESCE(AVG(total), 0) AS avg_bill_size
            FROM bills
            WHERE status = 'finalized'
              AND DATE(finalized_at) = CURRENT_DATE
        """)
        today_row = cur.fetchone()

        # 2. Payment mode breakdown today
        cur.execute("""
            SELECT payment_mode, COUNT(*) AS count, COALESCE(SUM(total), 0) AS volume
            FROM bills
            WHERE status = 'finalized' AND DATE(finalized_at) = CURRENT_DATE
            GROUP BY payment_mode
        """)
        pmode_rows = cur.fetchall()

        # 3. Category distribution today
        cur.execute("""
            SELECT p.category, COALESCE(SUM(bi.line_total), 0) AS revenue, SUM(bi.qty) AS units_sold
            FROM bill_items bi
            JOIN bills b ON bi.bill_id = b.bill_id
            JOIN products p ON bi.sku_id = p.sku_id
            WHERE b.status = 'finalized' AND DATE(b.finalized_at) = CURRENT_DATE
            GROUP BY p.category
            ORDER BY revenue DESC
            LIMIT 5
        """)
        cat_rows = cur.fetchall()

        # 4. Recent 8 transactions
        cur.execute("""
            SELECT b.bill_id, b.invoice_number, b.status, b.payment_mode, b.total,
                   COALESCE(c.name, 'Walk-in') AS customer_name,
                   b.finalized_at
            FROM bills b
            LEFT JOIN customers c ON b.customer_id = c.customer_id
            ORDER BY COALESCE(b.finalized_at, b.created_at) DESC
            LIMIT 8
        """)
        recent_bills = cur.fetchall()
        cur.close()

        # 5. Connected subsystem data
        drawer = get_cash_drawer_status()
        itc = get_gst_itc_summary()
        reorder = check_low_stock_reorder_alerts()

        return {
            "status": "success",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "today_kpis": {
                "total_revenue": round(float(today_row["total_revenue"] or 0), 2),
                "total_bills": int(today_row["total_bills"] or 0),
                "avg_bill_size": round(float(today_row["avg_bill_size"] or 0), 2),
                "total_tax_collected": round(float(today_row["total_tax"] or 0), 2)
            },
            "payment_modes": [
                {
                    "mode": (r["payment_mode"] or "cash").upper(),
                    "count": r["count"],
                    "volume": round(float(r["volume"]), 2)
                } for r in pmode_rows
            ],
            "top_categories": [
                {
                    "category": r["category"],
                    "revenue": round(float(r["revenue"]), 2),
                    "units": float(r["units_sold"])
                } for r in cat_rows
            ],
            "cash_drawer": {
                "status": drawer.get("drawer_status", "closed"),
                "expected_cash": drawer.get("reconciliation", {}).get("expected_cash_in_drawer", 0.0) if drawer.get("status") == "success" else 0.0
            },
            "gst_position": itc.get("net_tax_computation", {}).get("tax_position", "Neutral"),
            "low_stock_alerts_count": reorder.get("count", 0),
            "recent_bills": [
                {
                    "bill_id": b["bill_id"],
                    "invoice_no": b["invoice_number"] or "-",
                    "status": b["status"],
                    "customer": b["customer_name"],
                    "mode": (b["payment_mode"] or "cash").upper(),
                    "total": round(float(b["total"]), 2),
                    "time": str(b["finalized_at"])[:16] if b["finalized_at"] else "-"
                } for b in recent_bills
            ]
        }
    finally:
        conn.close()


def export_dashboard_html(output_file: Optional[str] = None) -> Dict[str, Any]:
    """
    Generate an interactive, responsive HTML5 executive dashboard file with rich styling.
    Saved to generated_docs/dashboard.html by default.
    """
    data = get_dashboard_summary()
    kpis = data["today_kpis"]
    drawer = data["cash_drawer"]
    bills = data["recent_bills"]
    categories = data["top_categories"]

    os.makedirs("generated_docs", exist_ok=True)
    out_path = output_file or os.path.join("generated_docs", "dashboard.html")

    cat_bars_html = "".join([
        f"""
        <div style="margin-bottom: 12px;">
            <div style="display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 4px;">
                <span>{c['category']}</span>
                <span style="font-weight: 600;">₹{c['revenue']:.2f} ({c['units']:.0f} units)</span>
            </div>
            <div style="background: rgba(255,255,255,0.08); border-radius: 6px; height: 8px; overflow: hidden;">
                <div style="background: linear-gradient(90deg, #3b82f6, #06b6d4); height: 100%; width: {min(100, (c['revenue'] / max(kpis['total_revenue'], 1)) * 100)}%;"></div>
            </div>
        </div>
        """ for c in categories
    ]) or "<p style='color: #94a3b8; font-size: 13px;'>No sales recorded today yet.</p>"

    bills_rows_html = "".join([
        f"""
        <tr>
            <td style="padding: 10px 14px; font-family: monospace; font-size: 12px; color: #38bdf8;">{b['bill_id']}</td>
            <td style="padding: 10px 14px;">{b['customer']}</td>
            <td style="padding: 10px 14px;"><span style="background: rgba(59, 130, 246, 0.15); color: #60a5fa; padding: 2px 8px; border-radius: 4px; font-size: 11px;">{b['mode']}</span></td>
            <td style="padding: 10px 14px; font-weight: 600;">₹{b['total']:.2f}</td>
            <td style="padding: 10px 14px;"><span style="background: {'rgba(34, 197, 94, 0.15)' if b['status'] == 'finalized' else 'rgba(239, 68, 68, 0.15)'}; color: {'#4ade80' if b['status'] == 'finalized' else '#f87171'}; padding: 2px 8px; border-radius: 4px; font-size: 11px; text-transform: uppercase;">{b['status']}</span></td>
            <td style="padding: 10px 14px; color: #94a3b8; font-size: 12px;">{b['time']}</td>
        </tr>
        """ for b in bills
    ]) or "<tr><td colspan='6' style='text-align: center; padding: 20px; color: #94a3b8;'>No bills found</td></tr>"

    drawer_exp = drawer.get("expected_cash", 0.0)
    drawer_display = f"₹{drawer_exp:.2f}" if drawer.get("status") == "open" else "CLOSED"
    low_stock_color = "#f87171" if data["low_stock_alerts_count"] > 0 else "#4ade80"

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SuperMart AI — Executive Operations Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Inter', sans-serif; background: #0b0f19; color: #f1f5f9; padding: 24px; min-height: 100vh; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 28px; border-bottom: 1px solid rgba(255,255,255,0.08); padding-bottom: 20px; }}
        .header h1 {{ font-size: 24px; font-weight: 700; background: linear-gradient(135deg, #60a5fa, #a78bfa); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
        .live-tag {{ background: rgba(34, 197, 94, 0.15); color: #4ade80; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; }}
        .live-dot {{ width: 8px; height: 8px; background: #22c55e; border-radius: 50%; box-shadow: 0 0 8px #22c55e; }}
        .grid-kpi {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 24px; }}
        .card {{ background: rgba(17, 24, 39, 0.8); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 12px; padding: 20px; backdrop-filter: blur(10px); }}
        .card-label {{ font-size: 12px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; font-weight: 500; }}
        .card-val {{ font-size: 28px; font-weight: 700; color: #ffffff; }}
        .card-sub {{ font-size: 12px; color: #64748b; margin-top: 6px; }}
        .layout-2col {{ display: grid; grid-template-columns: 2fr 1fr; gap: 20px; margin-bottom: 24px; }}
        table {{ width: 100%; border-collapse: collapse; text-align: left; }}
        th {{ background: rgba(255,255,255,0.03); color: #94a3b8; font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; padding: 12px 14px; border-bottom: 1px solid rgba(255,255,255,0.08); }}
        tr:hover {{ background: rgba(255,255,255,0.02); }}
        @media (max-width: 900px) {{ .layout-2col {{ grid-template-columns: 1fr; }} }}
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>🛒 SuperMart AI Operations Command</h1>
            <p style="color: #64748b; font-size: 13px; margin-top: 4px;">Live Operations & Financial Overview • {data['timestamp']}</p>
        </div>
        <div class="live-tag">
            <span class="live-dot"></span> LIVE SYSTEM
        </div>
    </div>

    <div class="grid-kpi">
        <div class="card">
            <div class="card-label">Today's Revenue</div>
            <div class="card-val" style="color: #38bdf8;">₹{kpis['total_revenue']:.2f}</div>
            <div class="card-sub">{kpis['total_bills']} finalized bills today</div>
        </div>
        <div class="card">
            <div class="card-label">Avg Bill Value</div>
            <div class="card-val" style="color: #a78bfa;">₹{kpis['avg_bill_size']:.2f}</div>
            <div class="card-sub">Per checkout ticket</div>
        </div>
        <div class="card">
            <div class="card-label">Cash Drawer Status</div>
            <div class="card-val" style="color: #4ade80;">{drawer_display}</div>
            <div class="card-sub">Drawer State: <strong style="text-transform: uppercase;">{drawer['status']}</strong></div>
        </div>
        <div class="card">
            <div class="card-label">Low Stock Alerts</div>
            <div class="card-val" style="color: {low_stock_color};">{data['low_stock_alerts_count']}</div>
            <div class="card-sub">Items at or below reorder level</div>
        </div>
        <div class="card">
            <div class="card-label">Net GST Position</div>
            <div class="card-val" style="font-size: 16px; margin-top: 6px;">{data['gst_position']}</div>
            <div class="card-sub">Output GST vs Input ITC</div>
        </div>
    </div>

    <div class="layout-2col">
        <div class="card">
            <h3 style="font-size: 16px; margin-bottom: 16px; font-weight: 600;">🧾 Recent Checkout Transactions</h3>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr>
                            <th>Bill ID</th>
                            <th>Customer</th>
                            <th>Mode</th>
                            <th>Total</th>
                            <th>Status</th>
                            <th>Timestamp</th>
                        </tr>
                    </thead>
                    <tbody>
                        {bills_rows_html}
                    </tbody>
                </table>
            </div>
        </div>

        <div class="card">
            <h3 style="font-size: 16px; margin-bottom: 16px; font-weight: 600;">📊 Category Sales Velocity</h3>
            {cat_bars_html}
        </div>
    </div>
</body>
</html>
"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return {
        "status": "success",
        "message": f"Interactive Web Dashboard exported to {out_path}.",
        "file_path": out_path,
        "kpis": kpis
    }
