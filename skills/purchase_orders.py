"""
Auto Purchase Order Generator Skill for SuperMart AI Ops Agent.

Scans items below reorder level or with low sales coverage days.
Auto-computes supplier order quantities, estimated procurement costs,
records the PO in PostgreSQL, and generates a formatted PDF Purchase Order.
"""

import os
import json
import uuid
from datetime import date, datetime
from typing import Dict, Any, List, Optional

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

from db.models import get_db_connection, immediate_transaction
from skills.security import escape_xml_text


def generate_purchase_order(
    supplier_name: Optional[str] = None,
    cover_days: int = 14
) -> Dict[str, Any]:
    """
    Auto-detect products needing replenishment, calculate order quantities,
    generate a Purchase Order with estimated cost, and export a PDF PO document.
    """
    supplier = (supplier_name or "General Wholesale Distributor").strip()
    cover_days = max(7, min(60, int(cover_days)))

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Find products at or below reorder level or with low stock
        cur.execute("""
            SELECT sku_id, name, category, unit, cost_price, mrp, quantity, reorder_level
            FROM products
            WHERE is_active = TRUE AND quantity <= (reorder_level * 1.5)
            ORDER BY (quantity / NULLIF(reorder_level, 0)) ASC, quantity ASC
        """)
        low_items = cur.fetchall()

        if not low_items:
            # Fallback: check all active products if stock is low
            cur.execute("""
                SELECT sku_id, name, category, unit, cost_price, mrp, quantity, reorder_level
                FROM products
                WHERE is_active = TRUE AND quantity <= 10
                ORDER BY quantity ASC
                LIMIT 5
            """)
            low_items = cur.fetchall()

        if not low_items:
            return {
                "status": "success",
                "count": 0,
                "items": [],
                "message": "✅ All products are sufficiently stocked above their reorder levels! No Purchase Order needed."
            }

        po_id = f"PO-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
        po_items = []
        total_estimated_cost = 0.0

        for it in low_items:
            qty_current = float(it["quantity"])
            reorder_lvl = float(it["reorder_level"])
            cost_p = float(it["cost_price"])

            # Target stock = max(reorder_lvl * 2.5, 20.0)
            target_stock = max(reorder_lvl * 2.5, 20.0)
            suggested_order_qty = max(5.0, round(target_stock - qty_current, 1))
            line_cost = round(suggested_order_qty * cost_p, 2)
            total_estimated_cost += line_cost

            po_items.append({
                "sku_id": it["sku_id"],
                "name": it["name"],
                "category": it["category"],
                "unit": it["unit"],
                "current_stock": qty_current,
                "reorder_level": reorder_lvl,
                "cost_price": cost_p,
                "order_qty": suggested_order_qty,
                "line_cost": line_cost
            })

        total_estimated_cost = round(total_estimated_cost, 2)

        # Generate PDF document
        output_dir = os.path.join("generated_docs")
        os.makedirs(output_dir, exist_ok=True)
        pdf_path = os.path.join(output_dir, f"{po_id}.pdf")

        _create_po_pdf(pdf_path, po_id, supplier, po_items, total_estimated_cost)

        # Save to database
        with immediate_transaction(conn):
            cur.execute("""
                INSERT INTO purchase_orders (po_id, supplier_name, status, total_estimated_cost, items_json, pdf_path)
                VALUES (%s, %s, 'draft', %s, %s, %s)
            """, (po_id, supplier, total_estimated_cost, json.dumps(po_items), pdf_path))
            cur.close()

        lines = [
            f"📋 **Purchase Order Generated: {po_id}**",
            f"🏢 Supplier: **{supplier}**",
            f"📦 Items to Order: {len(po_items)} product(s)",
            f"💰 Total Estimated Cost: **₹{total_estimated_cost:,.2f}**\n",
            "🛒 **Order Line Items:**"
        ]
        for item in po_items:
            lines.append(f"  • {item['name']} [{item['sku_id']}] — Order: **{item['order_qty']} {item['unit']}** @ ₹{item['cost_price']:.2f} = ₹{item['line_cost']:,.2f} (Current: {item['current_stock']})")

        lines.append(f"\n📄 PDF Export: `{pdf_path}`")

        return {
            "status": "success",
            "po_id": po_id,
            "supplier_name": supplier,
            "total_items": len(po_items),
            "total_estimated_cost": total_estimated_cost,
            "items": po_items,
            "file_path": pdf_path,
            "message": "\n".join(lines)
        }
    finally:
        conn.close()


def _create_po_pdf(
    pdf_path: str,
    po_id: str,
    supplier_name: str,
    items: List[Dict[str, Any]],
    total_cost: float
) -> None:
    """Generate clean professional PDF purchase order document using ReportLab."""
    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "POTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=colors.HexColor("#1A365D")
    )
    subtitle_style = ParagraphStyle(
        "POSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#4A5568")
    )
    cell_style = ParagraphStyle(
        "POCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=11
    )
    header_style = ParagraphStyle(
        "POHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=11,
        textColor=colors.white
    )

    story = []
    story.append(Paragraph("SUPERMART SUPERMARKET", title_style))
    story.append(Paragraph("<b>PURCHASE ORDER</b>", ParagraphStyle("POHeading", parent=title_style, fontSize=14, textColor=colors.HexColor("#2B6CB0"))))
    story.append(Spacer(1, 8))

    meta_data = [
        [
            Paragraph(f"<b>PO Number:</b> {escape_xml_text(po_id)}<br/><b>Date:</b> {date.today().strftime('%d %B %Y')}", subtitle_style),
            Paragraph(f"<b>Supplier:</b> {escape_xml_text(supplier_name)}<br/><b>Payment Terms:</b> Net 30 Days", subtitle_style)
        ]
    ]
    meta_table = Table(meta_data, colWidths=[270, 270])
    meta_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
    ]))
    story.append(meta_table)
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#CBD5E0"), spaceAfter=12))

    # Table of products
    table_data = [[
        Paragraph("#", header_style),
        Paragraph("Item Description", header_style),
        Paragraph("Unit", header_style),
        Paragraph("Current", header_style),
        Paragraph("Order Qty", header_style),
        Paragraph("Unit Cost", header_style),
        Paragraph("Line Total", header_style)
    ]]

    for idx, it in enumerate(items, 1):
        table_data.append([
            Paragraph(str(idx), cell_style),
            Paragraph(f"<b>{escape_xml_text(it['name'])}</b><br/><font color='#718096' size=7>{escape_xml_text(it['sku_id'])}</font>", cell_style),
            Paragraph(escape_xml_text(it["unit"]), cell_style),
            Paragraph(str(it["current_stock"]), cell_style),
            Paragraph(f"<b>{it['order_qty']}</b>", cell_style),
            Paragraph(f"₹{it['cost_price']:.2f}", cell_style),
            Paragraph(f"₹{it['line_cost']:,.2f}", cell_style)
        ])

    table_data.append([
        "", "", "", "", "",
        Paragraph("<b>Total Estimated:</b>", cell_style),
        Paragraph(f"<b>₹{total_cost:,.2f}</b>", ParagraphStyle("POTotal", parent=cell_style, fontName="Helvetica-Bold", textColor=colors.HexColor("#2B6CB0")))
    ])

    po_table = Table(table_data, colWidths=[25, 200, 45, 50, 60, 70, 90])
    po_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#2B6CB0")),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('ALIGN', (3,1), (-1,-1), 'RIGHT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('GRID', (0,0), (-1,-2), 0.5, colors.HexColor("#E2E8F0")),
        ('LINEBELOW', (0,-1), (-1,-1), 1.5, colors.HexColor("#2B6CB0")),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
    ]))
    story.append(po_table)

    story.append(Spacer(1, 20))
    story.append(Paragraph("<i>Authorized Signature & Stamp: ____________________________</i>", subtitle_style))

    doc.build(story)


def list_purchase_orders() -> Dict[str, Any]:
    """
    List all generated purchase orders and their status.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT po_id, supplier_name, status, total_estimated_cost, created_at, pdf_path FROM purchase_orders ORDER BY created_at DESC LIMIT 10")
        rows = cur.fetchall()
        cur.close()

        pos = []
        for r in rows:
            pos.append({
                "po_id": r["po_id"],
                "supplier": r["supplier_name"] or "N/A",
                "status": (r["status"] or "draft").upper(),
                "cost": round(float(r["total_estimated_cost"]), 2),
                "created_at": str(r["created_at"])[:16] if r["created_at"] else "N/A",
                "pdf_path": r["pdf_path"]
            })

        lines = [f"📋 **Recent Purchase Orders ({len(pos)}):**\n"]
        for p in pos:
            lines.append(f"• **{p['po_id']}** — ₹{p['cost']:,.2f} ({p['status']}) for {p['supplier']} on {p['created_at']}")

        return {
            "status": "success",
            "count": len(pos),
            "purchase_orders": pos,
            "message": "\n".join(lines) if pos else "No purchase orders generated yet."
        }
    finally:
        conn.close()
