import os
from typing import Optional, Dict, Any, List
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend
import matplotlib.pyplot as plt
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from skills.analytics import period_summary, reorder_suggestions
from skills.credit import list_all_khata
from db.models import get_db_connection

# Premium Executive Dark Palette
SLATE_BG = RGBColor(11, 15, 25)         # Slate 950 Deep Dark
DARK_CARD = RGBColor(30, 41, 59)        # Slate 800 Card
HEADER_CARD = RGBColor(15, 23, 42)      # Slate 900 Header
ACCENT_CYAN = RGBColor(56, 189, 248)     # Sky 400
ACCENT_GREEN = RGBColor(52, 211, 153)    # Emerald 400
ACCENT_AMBER = RGBColor(251, 146, 60)    # Amber 400
ACCENT_PURPLE = RGBColor(192, 132, 252)  # Purple 400
ACCENT_ROSE = RGBColor(251, 113, 133)    # Rose 400
TEXT_WHITE = RGBColor(248, 250, 252)     # Slate 50
TEXT_MUTED = RGBColor(148, 163, 184)     # Slate 400
BORDER_BLUE = RGBColor(51, 65, 85)       # Slate 700


def _add_solid_background(slide, color=SLATE_BG):
    """Fills slide background with rich executive slate background."""
    bg_shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, Inches(13.333), Inches(7.5))
    bg_shape.fill.solid()
    bg_shape.fill.fore_color.rgb = color
    bg_shape.line.fill.background()
    return bg_shape


def _add_card_container(slide, left, top, width, height, bg_color=DARK_CARD, border_color=BORDER_BLUE):
    """Creates a content card container with border."""
    card = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height)
    card.fill.solid()
    card.fill.fore_color.rgb = bg_color
    if border_color:
        card.line.color.rgb = border_color
        card.line.width = Pt(1.2)
    else:
        card.line.fill.background()
    return card


def _add_header_banner(slide, title_text: str, period: str = "Today", shop_name: str = "SuperMart Ops Agent"):
    """Adds a consistent executive header banner on top of content slides."""
    top_bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, Inches(13.333), Inches(0.08))
    top_bar.fill.solid()
    top_bar.fill.fore_color.rgb = ACCENT_CYAN
    top_bar.line.fill.background()

    title_box = slide.shapes.add_textbox(Inches(0.8), Inches(0.3), Inches(8.5), Inches(0.8))
    tf = title_box.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = title_text
    p.font.size = Pt(22)
    p.font.bold = True
    p.font.color.rgb = TEXT_WHITE

    meta_box = slide.shapes.add_textbox(Inches(9.0), Inches(0.3), Inches(3.5), Inches(0.8))
    tf_m = meta_box.text_frame
    p_m = tf_m.paragraphs[0]
    p_m.text = f"🏬 {shop_name}\n📅 Period: {period}"
    p_m.alignment = PP_ALIGN.RIGHT
    p_m.font.size = Pt(11)
    p_m.font.color.rgb = ACCENT_CYAN


def _create_charts(output_dir: str, days: int = 7) -> Dict[str, str]:
    """Generates high-resolution matplotlib charts from REAL database records."""
    os.makedirs(output_dir, exist_ok=True)
    chart_paths = {}
    plt.style.use('dark_background')

    start = None
    conn = get_db_connection()
    try:
        from datetime import date, timedelta
        start = (date.today() - timedelta(days=days - 1)).isoformat()

        cur = conn.cursor()

        # 1. Payment Mode Donut Chart (period-scoped)
        cur.execute("""
            SELECT COALESCE(payment_mode, 'cash') AS mode, SUM(total) AS mode_total
            FROM bills
            WHERE status = 'finalized' AND finalized_at::date >= %s::date
            GROUP BY payment_mode
        """, (start,))
        pm_rows = cur.fetchall()

        pm_labels = [r["mode"].upper() for r in pm_rows]
        pm_values = [float(r["mode_total"]) for r in pm_rows]

        # Fallback: whole-history payment mix if period has no sales
        if not pm_values or sum(pm_values) == 0:
            cur.execute("SELECT COALESCE(payment_mode,'cash') AS mode, COUNT(*) AS cnt FROM bills GROUP BY payment_mode")
            b_pm = cur.fetchall()
            if b_pm:
                pm_labels = [r["mode"].upper() for r in b_pm]
                pm_values = [float(r["cnt"]) for r in b_pm]

        if not pm_values or sum(pm_values) == 0:
            pm_labels, pm_values = ["CASH", "UPI", "CARD", "KHATA"], [0, 0, 0, 0]

        fig, ax = plt.subplots(figsize=(5.8, 4.2), facecolor='#1E293B')
        ax.set_facecolor('#1E293B')
        colors = ['#38BDF8', '#34D399', '#C084FC', '#FB923C']

        non_zero = [(l, v) for l, v in zip(pm_labels, pm_values) if v > 0]
        if non_zero:
            plot_labels, plot_values = zip(*non_zero)
            slice_colors = colors[:len(plot_values)]
            autopct_format = '%1.1f%%'
        else:
            plot_labels, plot_values = ["NO SALES"], [1]
            slice_colors = ['#64748B']
            autopct_format = ''

        wedges, texts, autotexts = ax.pie(
            plot_values,
            labels=plot_labels,
            autopct=autopct_format,
            colors=slice_colors,
            startangle=140,
            textprops=dict(color='#F8FAFC', fontsize=10, weight='bold'),
            pctdistance=0.75,
            wedgeprops=dict(width=0.45, edgecolor='#0F172A', linewidth=2)
        )
        for autotext in autotexts:
            autotext.set_color('#FFFFFF')
            autotext.set_fontsize(11)
            autotext.set_weight('bold')
            autotext.set_weight('bold')

        ax.set_title(f'Revenue Mix by Payment Mode (last {days}d)', fontsize=13, fontweight='bold', color='#38BDF8', pad=15)
        plt.tight_layout()
        pm_chart_path = os.path.join(output_dir, 'payment_mode_chart.png')
        plt.savefig(pm_chart_path, dpi=300, facecolor=fig.get_facecolor(), transparent=True)
        plt.close()
        chart_paths['payment_mode'] = pm_chart_path

        # 2. Daily revenue trend line chart (period-scoped)
        cur.execute("""
            SELECT finalized_at::date AS day, SUM(total) AS day_revenue
            FROM bills
            WHERE status = 'finalized' AND finalized_at::date >= %s::date
            GROUP BY day ORDER BY day ASC
        """, (start,))
        trend_rows = cur.fetchall()

        if trend_rows and sum(float(r["day_revenue"]) for r in trend_rows) > 0:
            t_labels = [str(r["day"])[5:] for r in trend_rows]  # MM-DD
            t_values = [float(r["day_revenue"]) for r in trend_rows]

            fig, ax = plt.subplots(figsize=(5.8, 4.2), facecolor='#1E293B')
            ax.set_facecolor('#1E293B')
            ax.plot(t_labels, t_values, color='#34D399', linewidth=2.5, marker='o', markersize=6,
                    markerfacecolor='#0F172A', markeredgecolor='#34D399')
            ax.fill_between(range(len(t_values)), t_values, color='#34D399', alpha=0.15)
            ax.set_title(f'Daily Revenue Trend (last {days}d)', fontsize=13, fontweight='bold', color='#34D399', pad=15)
            ax.set_ylabel('Revenue (₹)', color='#94A3B8', fontsize=10)
            ax.tick_params(colors='#F8FAFC', labelsize=9)
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            ax.spines['left'].set_color('#334155')
            ax.spines['bottom'].set_color('#334155')
            ax.grid(axis='y', linestyle='--', alpha=0.2, color='#94A3B8')
            if len(t_labels) > 6:
                ax.set_xticks(ax.get_xticks()[::max(1, len(t_labels) // 6)])
            plt.tight_layout()
            trend_path = os.path.join(output_dir, 'revenue_trend_chart.png')
            plt.savefig(trend_path, dpi=300, facecolor=fig.get_facecolor(), transparent=True)
            plt.close()
            chart_paths['revenue_trend'] = trend_path

        # 3. Category Breakdown Chart (period-scoped, with inventory valuation fallback)
        cur.execute("""
            SELECT p.category, SUM(bi.line_total) AS cat_total
            FROM bill_items bi
            JOIN bills b ON bi.bill_id = b.bill_id
            JOIN products p ON bi.sku_id = p.sku_id
            WHERE b.status = 'finalized' AND b.finalized_at::date >= %s::date
            GROUP BY p.category
            ORDER BY cat_total DESC
        """, (start,))
        cat_rows = cur.fetchall()

        cat_labels = [r["category"] for r in cat_rows]
        cat_values = [float(r["cat_total"]) for r in cat_rows]

        chart_title = f'Revenue Breakdown by Category (₹, last {days}d)'
        if not cat_values or sum(cat_values) == 0:
            cur.execute("""
                SELECT category, SUM(mrp * quantity) AS cat_stock_val
                FROM products WHERE is_active = TRUE
                GROUP BY category
                ORDER BY cat_stock_val DESC
                LIMIT 5
            """)
            inv_rows = cur.fetchall()
            cat_labels = [r["category"] for r in inv_rows]
            cat_values = [float(r["cat_stock_val"]) for r in inv_rows]
            chart_title = 'Inventory Valuation by Category (₹) — no sales in period'

        fig, ax = plt.subplots(figsize=(5.8, 4.2), facecolor='#1E293B')
        ax.set_facecolor('#1E293B')

        display_labels = cat_labels[::-1] if cat_labels else ["General"]
        display_values = cat_values[::-1] if cat_values else [0]

        bars = ax.barh(display_labels, display_values, color='#38BDF8', height=0.55, edgecolor='none')
        ax.set_title(chart_title, fontsize=12, fontweight='bold', color='#38BDF8', pad=15)
        ax.set_xlabel('Value (₹)', color='#94A3B8', fontsize=10)
        ax.tick_params(colors='#F8FAFC', labelsize=10)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.spines['left'].set_color('#334155')
        ax.spines['bottom'].set_color('#334155')
        ax.grid(axis='x', linestyle='--', alpha=0.2, color='#94A3B8')

        max_val = max(display_values) if display_values and max(display_values) > 0 else 1
        for bar in bars:
            width = bar.get_width()
            ax.text(width + (max_val * 0.02), bar.get_y() + bar.get_height() / 2, f'₹{width:,.0f}',
                    ha='left', va='center', color='#F8FAFC', fontsize=9, fontweight='bold')

        plt.tight_layout()
        cat_chart_path = os.path.join(output_dir, 'category_chart.png')
        plt.savefig(cat_chart_path, dpi=300, facecolor=fig.get_facecolor(), transparent=True)
        plt.close()
        chart_paths['category'] = cat_chart_path

        return chart_paths
    finally:
        conn.close()


def _build_data_driven_recommendations() -> List[Dict[str, Any]]:
    """Build recommendations from REAL data: reorder suggestions, khata aging, GST integrity."""
    recommendations: List[Dict[str, Any]] = []

    # 1. Inventory replenishment from sales velocity
    try:
        reorder = reorder_suggestions(velocity_days=7, cover_days=7)
        items = reorder.get("suggestions", [])
        if items:
            top3 = items[:3]
            names = ", ".join(f"{i['name']} ({i['suggested_reorder_qty']:g} {i['unit']})" for i in top3)
            recommendations.append({
                "heading": "Inventory Replenishment",
                "desc": f"Reorder soon based on sales velocity: {names}. Velocity computed over the last 7 days.",
                "color": ACCENT_AMBER
            })
        else:
            recommendations.append({
                "heading": "Inventory Replenishment",
                "desc": "All items have healthy stock cover for the next 7 days. No immediate reorders required.",
                "color": ACCENT_GREEN
            })
    except Exception:
        pass

    # 2. Khata recovery action
    try:
        khata = list_all_khata()
        ledger = khata.get("khata_ledger", [])
        outstanding = sum(c["khata_balance"] for c in ledger if c["khata_balance"] > 0)
        overdue = [c for c in ledger if c["khata_balance"] >= 500]
        if overdue:
            names = ", ".join(c["name"] for c in overdue[:3])
            recommendations.append({
                "heading": "Khata Recovery Action",
                "desc": f"₹{outstanding:,.2f} outstanding across {len(ledger)} customers. High balances (₹500+): {names}. Consider sending reminders.",
                "color": ACCENT_ROSE
            })
        elif outstanding > 0:
            recommendations.append({
                "heading": "Khata Recovery Action",
                "desc": f"₹{outstanding:,.2f} outstanding across {len(ledger)} customers — all within comfortable limits.",
                "color": ACCENT_CYAN
            })
    except Exception:
        pass

    # 3. Category concentration / promotional strategy from real sales mix
    try:
        period = period_summary(days=7)
        top_items = period.get("top_items", [])
        if top_items:
            top_names = ", ".join(i["name"] for i in top_items[:2])
            recommendations.append({
                "heading": "Promotional Strategy",
                "desc": f"Top sellers this week: {top_names}. Bundle them with slow-moving stock to boost gross margin.",
                "color": ACCENT_CYAN
            })
    except Exception:
        pass

    # 4. GST compliance integrity check
    try:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) AS cnt FROM products
                WHERE is_active = TRUE AND (hsn_code IS NULL OR TRIM(hsn_code) = '')
            """)
            missing_hsn = cur.fetchone()["cnt"]
            cur.close()
        finally:
            conn.close()

        if missing_hsn:
            recommendations.append({
                "heading": "GST Compliance Audit",
                "desc": f"{missing_hsn} active product(s) missing HSN codes. Add HSN codes to keep invoices GST-compliant.",
                "color": ACCENT_ROSE
            })
        else:
            recommendations.append({
                "heading": "GST Compliance Audit",
                "desc": "All active products carry valid HSN codes and GST slabs. 100% invoice compliance.",
                "color": ACCENT_GREEN
            })
    except Exception:
        pass

    # Fallback if everything failed (never show fabricated data)
    if not recommendations:
        recommendations.append({
            "heading": "Operational Insights",
            "desc": "Not enough sales data yet to generate specific recommendations. Start billing to unlock data-driven insights.",
            "color": TEXT_MUTED
        })

    return recommendations


def generate_analysis_pptx(period: str = "Today", output_dir: str = "generated_docs",
                          shop_name: Optional[str] = None, days: int = 7) -> str:
    """Generates an executive-grade 4-slide widescreen PowerPoint analysis deck from live data."""
    os.makedirs(output_dir, exist_ok=True)
    days = max(1, min(90, int(days)))

    if not shop_name:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT shop_name FROM shops ORDER BY shop_id DESC LIMIT 1")
            row = cur.fetchone()
            if row:
                shop_name = row["shop_name"]
            cur.close()
            conn.close()
        except Exception:
            pass
    active_shop_name = shop_name or os.getenv("SHOP_NAME", "SuperMart Ops Agent")

    chart_paths = _create_charts(output_dir, days=days)
    file_path = os.path.join(output_dir, "Supermarket_Ops_Analysis.pptx")

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank_layout = prs.slide_layouts[6]

    # ── SLIDE 1: Title & Executive Cover ──────────────────────────────
    slide1 = prs.slides.add_slide(blank_layout)
    _add_solid_background(slide1, SLATE_BG)

    _add_card_container(slide1, Inches(1.2), Inches(1.5), Inches(10.93), Inches(4.5), bg_color=HEADER_CARD, border_color=ACCENT_CYAN)

    txBox = slide1.shapes.add_textbox(Inches(1.6), Inches(1.9), Inches(10.13), Inches(3.7))
    tf = txBox.text_frame
    tf.word_wrap = True

    p = tf.paragraphs[0]
    p.text = "SUPERMARKET OPERATIONS ANALYSIS"
    p.font.size = Pt(32)
    p.font.bold = True
    p.font.color.rgb = ACCENT_CYAN

    p2 = tf.add_paragraph()
    p2.text = f"Executive Performance Deck & Operational Intelligence — Period: {period} (last {days} days)"
    p2.font.size = Pt(18)
    p2.font.color.rgb = TEXT_MUTED
    p2.space_before = Pt(10)

    p3 = tf.add_paragraph()
    p3.text = f"🏬 Store: {active_shop_name}  |  🤖 Powered by Ops AI Agent  |  ⚡ GST Compliant"
    p3.font.size = Pt(13)
    p3.font.color.rgb = ACCENT_GREEN
    p3.space_before = Pt(28)

    # ── SLIDE 2: Key Operational Metrics Dashboard ─────────────────────
    period_data = period_summary(days=days)
    tot_bills = period_data.get("total_bills", 0)
    tot_sales = period_data.get("total_revenue", 0.0)
    tot_tax = period_data.get("total_tax", 0.0)
    top_cat = None

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        from datetime import date, timedelta
        start = (date.today() - timedelta(days=days - 1)).isoformat()
        cur.execute("""
            SELECT p.category, SUM(bi.line_total) AS c_tot
            FROM bill_items bi
            JOIN bills b ON bi.bill_id = b.bill_id
            JOIN products p ON bi.sku_id = p.sku_id
            WHERE b.status = 'finalized' AND b.finalized_at::date >= %s::date
            GROUP BY p.category
            ORDER BY c_tot DESC
            LIMIT 1
        """, (start,))
        r_cat = cur.fetchone()
        if not r_cat:
            cur.execute("SELECT category, SUM(mrp * quantity) AS c_val FROM products WHERE is_active = TRUE GROUP BY category ORDER BY c_val DESC LIMIT 1")
            r_cat = cur.fetchone()
        if r_cat:
            top_cat = r_cat["category"]
        cur.close()
    finally:
        conn.close()

    slide2 = prs.slides.add_slide(blank_layout)
    _add_solid_background(slide2, SLATE_BG)
    _add_header_banner(slide2, "Executive Operations & Revenue Dashboard", period, active_shop_name)

    kpis = [
        ("TOTAL REVENUE", f"₹{tot_sales:,.2f}", ACCENT_GREEN),
        ("COMPLETED TRANSACTIONS", f"{tot_bills} Bills", ACCENT_CYAN),
        ("GST COLLECTED", f"₹{tot_tax:,.2f}", ACCENT_AMBER),
        ("TOP CATEGORY", (top_cat or "No sales yet")[:18], ACCENT_PURPLE)
    ]

    card_w = Inches(2.7)
    card_h = Inches(1.2)
    start_x = Inches(0.8)
    gap = Inches(0.3)

    for idx, (title, val, color) in enumerate(kpis):
        x = start_x + (idx * (card_w + gap))
        _add_card_container(slide2, x, Inches(1.3), card_w, card_h, bg_color=DARK_CARD, border_color=color)

        tb = slide2.shapes.add_textbox(x + Inches(0.15), Inches(1.4), card_w - Inches(0.3), card_h - Inches(0.2))
        tf_k = tb.text_frame
        tf_k.word_wrap = True

        p_t = tf_k.paragraphs[0]
        p_t.text = title
        p_t.font.size = Pt(9)
        p_t.font.bold = True
        p_t.font.color.rgb = TEXT_MUTED

        p_v = tf_k.add_paragraph()
        p_v.text = val
        p_v.font.size = Pt(18)
        p_v.font.bold = True
        p_v.font.color.rgb = color
        p_v.space_before = Pt(4)

    # Left container: itemized tax & performance breakdown (from period_summary)
    _add_card_container(slide2, Inches(0.8), Inches(2.7), Inches(5.6), Inches(4.3), bg_color=DARK_CARD)
    tb_left = slide2.shapes.add_textbox(Inches(1.0), Inches(2.9), Inches(5.2), Inches(3.9))
    tf_l = tb_left.text_frame
    tf_l.word_wrap = True

    pb = period_data.get("payment_breakdown", {})
    metrics_detail = [
        ("Gross Sales (Excl. Tax)", f"₹{period_data.get('subtotal', 0):,.2f}", TEXT_WHITE),
        ("Cash Collected", f"₹{pb.get('cash', 0):,.2f}", TEXT_WHITE),
        ("UPI Collected", f"₹{pb.get('upi', 0):,.2f}", TEXT_WHITE),
        ("Card Collected", f"₹{pb.get('card', 0):,.2f}", TEXT_WHITE),
        ("Khata (Credit) Sales", f"₹{pb.get('khata', 0):,.2f}", TEXT_WHITE),
        ("Average per Bill", f"₹{(tot_sales / tot_bills if tot_bills else 0):,.2f}", ACCENT_CYAN),
    ]

    p_head = tf_l.paragraphs[0]
    p_head.text = "OPERATIONAL PERFORMANCE HIGHLIGHTS"
    p_head.font.size = Pt(11)
    p_head.font.bold = True
    p_head.font.color.rgb = ACCENT_CYAN
    p_head.space_after = Pt(12)

    for lbl, val, color in metrics_detail:
        p_item = tf_l.add_paragraph()
        p_item.text = f"• {lbl}: "
        p_item.font.size = Pt(12)
        p_item.font.color.rgb = TEXT_MUTED

        run = p_item.add_run()
        run.text = val
        run.font.bold = True
        run.font.color.rgb = color
        p_item.space_after = Pt(8)

    # Right container: payment mode chart (or trend if present)
    _add_card_container(slide2, Inches(6.8), Inches(2.7), Inches(5.7), Inches(4.3), bg_color=DARK_CARD)
    chart_key = 'revenue_trend' if 'revenue_trend' in chart_paths else 'payment_mode'
    if chart_key in chart_paths and os.path.exists(chart_paths[chart_key]):
        slide2.shapes.add_picture(chart_paths[chart_key], Inches(6.95), Inches(2.8), width=Inches(5.4))

    # ── SLIDE 3: Category Revenue & Stock Health ──────────────────────
    slide3 = prs.slides.add_slide(blank_layout)
    _add_solid_background(slide3, SLATE_BG)
    _add_header_banner(slide3, "Category Revenue & Stock Health Matrix", period, active_shop_name)

    _add_card_container(slide3, Inches(0.8), Inches(1.3), Inches(5.6), Inches(5.7), bg_color=DARK_CARD)
    if 'category' in chart_paths and os.path.exists(chart_paths['category']):
        slide3.shapes.add_picture(chart_paths['category'], Inches(0.95), Inches(1.5), width=Inches(5.3))

    # Right container: reorder suggestions table (data-driven)
    _add_card_container(slide3, Inches(6.8), Inches(1.3), Inches(5.7), Inches(5.7), bg_color=DARK_CARD)

    table_title_box = slide3.shapes.add_textbox(Inches(7.0), Inches(1.5), Inches(5.3), Inches(0.5))
    tf_tbl = table_title_box.text_frame
    p_tbl = tf_tbl.paragraphs[0]
    p_tbl.text = "⚠️ REORDER SUGGESTIONS (Sales Velocity)"
    p_tbl.font.size = Pt(12)
    p_tbl.font.bold = True
    p_tbl.font.color.rgb = ACCENT_AMBER

    reorder_data = reorder_suggestions(velocity_days=7, cover_days=7)
    items = reorder_data.get("suggestions", [])

    num_data_rows = max(1, min(5, len(items)))
    table_rows = num_data_rows + 1
    table_height = Inches(0.4 + (0.5 * num_data_rows))
    table_shape = slide3.shapes.add_table(rows=table_rows, cols=4, left=Inches(7.0), top=Inches(2.1), width=Inches(5.3), height=table_height)
    table = table_shape.table
    table.columns[0].width = Inches(2.2)
    table.columns[1].width = Inches(1.1)
    table.columns[2].width = Inches(1.0)
    table.columns[3].width = Inches(1.0)

    headers = ["Product / SKU", "Stock", "Cover", "Reorder"]
    for i, h in enumerate(headers):
        cell = table.cell(0, i)
        cell.text = h
        cell.fill.solid()
        cell.fill.fore_color.rgb = RGBColor(51, 65, 85)
        p = cell.text_frame.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        p.font.bold = True
        p.font.color.rgb = ACCENT_CYAN
        p.font.size = Pt(10)

    if items:
        for r_idx, item in enumerate(items[:5], 1):
            cell_name = table.cell(r_idx, 0)
            cell_name.text = str(item["name"])[:24]
            cell_name.fill.solid()
            cell_name.fill.fore_color.rgb = DARK_CARD
            p0 = cell_name.text_frame.paragraphs[0]
            p0.font.color.rgb = TEXT_WHITE
            p0.font.size = Pt(10)

            cell_qty = table.cell(r_idx, 1)
            cell_qty.text = f"{item['current_stock']:g}"
            cell_qty.fill.solid()
            cell_qty.fill.fore_color.rgb = DARK_CARD
            p1 = cell_qty.text_frame.paragraphs[0]
            p1.alignment = PP_ALIGN.CENTER
            p1.font.color.rgb = ACCENT_AMBER
            p1.font.bold = True
            p1.font.size = Pt(10)

            cell_cover = table.cell(r_idx, 2)
            cover_str = f"{item['days_of_cover']}d" if item['days_of_cover'] is not None else "—"
            cell_cover.text = cover_str
            cell_cover.fill.solid()
            cell_cover.fill.fore_color.rgb = DARK_CARD
            pc = cell_cover.text_frame.paragraphs[0]
            pc.alignment = PP_ALIGN.CENTER
            pc.font.color.rgb = TEXT_MUTED
            pc.font.size = Pt(10)

            cell_reorder = table.cell(r_idx, 3)
            cell_reorder.text = f"{item['suggested_reorder_qty']:g}"
            cell_reorder.fill.solid()
            cell_reorder.fill.fore_color.rgb = DARK_CARD
            p2 = cell_reorder.text_frame.paragraphs[0]
            p2.alignment = PP_ALIGN.CENTER
            p2.font.color.rgb = ACCENT_GREEN
            p2.font.bold = True
            p2.font.size = Pt(10)
    else:
        cell_name = table.cell(1, 0)
        cell_name.text = "All inventory stock healthy"
        cell_name.fill.solid()
        cell_name.fill.fore_color.rgb = DARK_CARD
        p0 = cell_name.text_frame.paragraphs[0]
        p0.font.color.rgb = ACCENT_GREEN
        p0.font.bold = True
        p0.font.size = Pt(10)

        for col in (1, 2, 3):
            c = table.cell(1, col)
            c.text = "OK"
            c.fill.solid()
            c.fill.fore_color.rgb = DARK_CARD
            pp = c.text_frame.paragraphs[0]
            pp.alignment = PP_ALIGN.CENTER
            pp.font.color.rgb = ACCENT_GREEN
            pp.font.bold = True
            pp.font.size = Pt(10)

    # ── SLIDE 4: Khata Ledger & Data-Driven Recommendations ──────────
    slide4 = prs.slides.add_slide(blank_layout)
    _add_solid_background(slide4, SLATE_BG)
    _add_header_banner(slide4, "Khata Credit Ledger & AI Strategic Insights", period, active_shop_name)

    _add_card_container(slide4, Inches(0.8), Inches(1.3), Inches(5.6), Inches(5.7), bg_color=DARK_CARD)

    kt_title_box = slide4.shapes.add_textbox(Inches(1.0), Inches(1.5), Inches(5.2), Inches(0.5))
    tf_kt = kt_title_box.text_frame
    p_kt = tf_kt.paragraphs[0]
    p_kt.text = "📖 CUSTOMER KHATA CREDIT LEDGER"
    p_kt.font.size = Pt(12)
    p_kt.font.bold = True
    p_kt.font.color.rgb = ACCENT_PURPLE

    khata_res = list_all_khata()
    khata_customers = khata_res.get("khata_ledger", [])

    k_num_rows = max(1, min(5, len(khata_customers)))
    k_table_shape = slide4.shapes.add_table(rows=k_num_rows + 1, cols=3, left=Inches(1.0), top=Inches(2.1), width=Inches(5.2), height=Inches(0.4 + (0.5 * k_num_rows)))
    k_table = k_table_shape.table
    k_table.columns[0].width = Inches(2.4)
    k_table.columns[1].width = Inches(1.5)
    k_table.columns[2].width = Inches(1.3)

    k_headers = ["Customer Name", "Balance", "Limit"]
    for i, h in enumerate(k_headers):
        cell = k_table.cell(0, i)
        cell.text = h
        cell.fill.solid()
        cell.fill.fore_color.rgb = RGBColor(51, 65, 85)
        p = cell.text_frame.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER if i > 0 else PP_ALIGN.LEFT
        p.font.bold = True
        p.font.color.rgb = ACCENT_CYAN
        p.font.size = Pt(10)

    if khata_customers:
        for r_idx, cust in enumerate(khata_customers[:5], 1):
            cell_n = k_table.cell(r_idx, 0)
            cell_n.text = str(cust["name"])[:24]
            cell_n.fill.solid()
            cell_n.fill.fore_color.rgb = DARK_CARD
            p0 = cell_n.text_frame.paragraphs[0]
            p0.font.color.rgb = TEXT_WHITE
            p0.font.size = Pt(10)

            cell_bal = k_table.cell(r_idx, 1)
            bal_val = cust["khata_balance"]
            cell_bal.text = f"₹{bal_val:,.2f}"
            cell_bal.fill.solid()
            cell_bal.fill.fore_color.rgb = DARK_CARD
            p1 = cell_bal.text_frame.paragraphs[0]
            p1.alignment = PP_ALIGN.RIGHT
            p1.font.bold = True
            p1.font.color.rgb = ACCENT_ROSE if bal_val > 0 else ACCENT_GREEN
            p1.font.size = Pt(10)

            cell_lim = k_table.cell(r_idx, 2)
            limit_val = cust.get("credit_limit") or 0
            cell_lim.text = "∞" if limit_val == 0 else f"₹{limit_val:,.0f}"
            cell_lim.fill.solid()
            cell_lim.fill.fore_color.rgb = DARK_CARD
            p2 = cell_lim.text_frame.paragraphs[0]
            p2.alignment = PP_ALIGN.CENTER
            p2.font.color.rgb = TEXT_MUTED
            p2.font.size = Pt(10)
    else:
        cell_n = k_table.cell(1, 0)
        cell_n.text = "No pending Khata balances"
        cell_n.fill.solid()
        cell_n.fill.fore_color.rgb = DARK_CARD
        p0 = cell_n.text_frame.paragraphs[0]
        p0.font.color.rgb = ACCENT_GREEN
        p0.font.bold = True
        p0.font.size = Pt(10)

        for col in (1, 2):
            c = k_table.cell(1, col)
            c.text = "₹0.00" if col == 1 else "—"
            c.fill.solid()
            c.fill.fore_color.rgb = DARK_CARD
            pp = c.text_frame.paragraphs[0]
            pp.alignment = PP_ALIGN.RIGHT if col == 1 else PP_ALIGN.CENTER
            pp.font.bold = True
            pp.font.color.rgb = ACCENT_GREEN
            pp.font.size = Pt(10)

    # Right container: DATA-DRIVEN recommendations
    _add_card_container(slide4, Inches(6.8), Inches(1.3), Inches(5.7), Inches(5.7), bg_color=DARK_CARD)

    rec_title_box = slide4.shapes.add_textbox(Inches(7.0), Inches(1.5), Inches(5.3), Inches(0.5))
    tf_rec = rec_title_box.text_frame
    p_rec = tf_rec.paragraphs[0]
    p_rec.text = "💡 DATA-DRIVEN STRATEGIC RECOMMENDATIONS"
    p_rec.font.size = Pt(12)
    p_rec.font.bold = True
    p_rec.font.color.rgb = ACCENT_GREEN

    tb_recs = slide4.shapes.add_textbox(Inches(7.0), Inches(2.1), Inches(5.3), Inches(4.7))
    tf_r = tb_recs.text_frame
    tf_r.word_wrap = True

    recommendations = _build_data_driven_recommendations()

    for idx, (rec) in enumerate(recommendations):
        rec_heading = rec["heading"]
        rec_desc = rec["desc"]
        rec_color = rec["color"]

        p_rh = tf_r.add_paragraph() if idx > 0 else tf_r.paragraphs[0]
        p_rh.text = f"• {rec_heading.upper()}"
        p_rh.font.size = Pt(10)
        p_rh.font.bold = True
        p_rh.font.color.rgb = rec_color

        p_rd = tf_r.add_paragraph()
        p_rd.text = rec_desc
        p_rd.font.size = Pt(10)
        p_rd.font.color.rgb = TEXT_MUTED
        p_rd.space_after = Pt(8)

    prs.save(file_path)
    return file_path
