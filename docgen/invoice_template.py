import os
import re
from typing import Dict, Any
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from skills.billing import preview_bill

# ── Indian number-to-words (for "Rupees in words" on the invoice) ──
_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
         "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two_digit(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return f"{_TENS[n // 10]}{_ONES[n % 10] if n % 10 else ''}"


def _three_digit(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]}Hundred")
        n %= 100
    if n:
        parts.append(_two_digit(n))
    return " ".join(parts)


def amount_to_indian_words(amount: float) -> str:
    """Convert a rupee amount to Indian-style words (crore/lakh/thousand), e.g. 'One Thousand Two Hundred Thirty Four Rupees and Fifty Paise Only'."""
    amount = round(amount + 1e-9, 2)
    rupees = int(amount)
    paise = int(round((amount - rupees) * 100))

    units = [
        (10000000, "Crore"),
        (100000, "Lakh"),
        (1000, "Thousand"),
    ]
    words_parts = []
    remaining = rupees
    for value, label in units:
        if remaining >= value:
            count = remaining // value
            remaining %= value
            words_parts.append(f"{_three_digit(count) if count >= 100 else (_two_digit(count) if count >= 20 else _ONES[count])} {label}")
    if remaining:
        words_parts.append(_three_digit(remaining) if remaining >= 100 else (_two_digit(remaining) if remaining >= 20 else _ONES[remaining]))

    rupee_words = " ".join(p for p in words_parts if p) if words_parts else "Zero"
    result = f"{rupee_words} Rupees"
    if paise:
        result += f" and {_two_digit(paise) if paise >= 20 else _ONES[paise]} Paise"
    return f"{result} Only"


def _fetch_shop_details() -> Dict[str, str]:
    """Fetch shop header details from DB (falls back to env, then defaults)."""
    shop_name = os.getenv("SHOP_NAME", "SuperMart")
    shop_address = os.getenv("SHOP_ADDRESS", "123 Main Street, Chennai, TN - 600001")
    shop_gstin = os.getenv("SHOP_GSTIN", "33AABCU9603R1ZM")
    place_of_supply = os.getenv("PLACE_OF_SUPPLY", "Tamil Nadu (33)")

    try:
        from db.models import get_db_connection
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT shop_name, shop_address, shop_gstin, place_of_supply FROM shops ORDER BY shop_id LIMIT 1")
            shop_row = cur.fetchone()
            if shop_row:
                shop_name = shop_row["shop_name"] or shop_name
                shop_address = shop_row["shop_address"] or shop_address
                shop_gstin = shop_row["shop_gstin"] or shop_gstin
                place_of_supply = shop_row.get("place_of_supply") or place_of_supply
            cur.close()
        finally:
            conn.close()
    except Exception:
        pass

    return {
        "shop_name": shop_name,
        "shop_address": shop_address,
        "shop_gstin": shop_gstin,
        "place_of_supply": place_of_supply,
    }


def generate_pdf_invoice(bill_id: str, output_dir: str = "generated_docs") -> str:
    """Generates a GST-compliant ReportLab PDF tax invoice for a given bill_id."""
    clean_bill_id = re.sub(r'[^a-zA-Z0-9_-]', '', bill_id)
    bill_data = preview_bill(clean_bill_id)
    if bill_data.get("status") == "error":
        raise ValueError(bill_data.get("message", "Bill not found"))

    os.makedirs(output_dir, exist_ok=True)
    file_path = os.path.join(output_dir, f"invoice_{clean_bill_id}.pdf")

    doc = SimpleDocTemplate(
        file_path,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'DocTitle', parent=styles['Heading1'],
        fontName='Helvetica-Bold', fontSize=20, leading=24,
        textColor=colors.HexColor('#1A365D')
    )
    subtitle_style = ParagraphStyle(
        'SubTitle', parent=styles['Normal'],
        fontName='Helvetica', fontSize=9, leading=12,
        textColor=colors.HexColor('#4A5568')
    )
    table_header_style = ParagraphStyle(
        'TableHeader', parent=styles['Normal'],
        fontName='Helvetica-Bold', fontSize=9, leading=11,
        textColor=colors.white, alignment=1
    )
    cell_style = ParagraphStyle(
        'TableCell', parent=styles['Normal'],
        fontName='Helvetica', fontSize=9, leading=11,
        textColor=colors.HexColor('#2D3748')
    )
    right_cell_style = ParagraphStyle('RightTableCell', parent=cell_style, alignment=2)
    bold_right_cell_style = ParagraphStyle('BoldRightTableCell', parent=cell_style, fontName='Helvetica-Bold', alignment=2)

    story = []
    shop = _fetch_shop_details()

    # ── Header ──
    story.append(Paragraph(f"<b>{shop['shop_name']}</b>", title_style))
    story.append(Paragraph(f"{shop['shop_address']} | GSTIN: {shop['shop_gstin']}", subtitle_style))
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor('#2B6CB0'), spaceAfter=15))

    # ── Invoice meta ──
    invoice_label = f"Invoice #{bill_data.get('invoice_number') or '-'}" if bill_data.get('invoice_number') else "Tax Invoice (Draft)"
    payment_mode_str = (bill_data.get('payment_mode') or 'Pending').upper()
    final_or_draft = bill_data.get('bill_status', '').upper()

    info_data = [
        [
            Paragraph("<b>Tax Invoice</b>", ParagraphStyle('H2', fontName='Helvetica-Bold', fontSize=14, textColor=colors.HexColor('#2B6CB0'))),
            Paragraph(f"<b>{invoice_label}</b>", right_cell_style)
        ],
        [
            Paragraph(f"<b>Bill ID:</b> {bill_data['bill_id']}", cell_style),
            Paragraph(f"<b>Status:</b> {final_or_draft}", right_cell_style)
        ],
        [
            Paragraph(f"<b>Customer Name:</b> {bill_data['customer_name']}", cell_style),
            Paragraph(f"<b>Payment Mode:</b> {payment_mode_str}", right_cell_style)
        ],
        [
            Paragraph(f"<b>Place of Supply:</b> {bill_data.get('place_of_supply') or shop['place_of_supply']}", cell_style),
            Paragraph(f"<b>Date:</b> {bill_data.get('finalized_at') or bill_data.get('created_at') or 'N/A'}", right_cell_style)
        ]
    ]
    if bill_data.get('payment_ref'):
        info_data.append([
            Paragraph(f"<b>Payment Ref:</b> {bill_data['payment_ref']}", cell_style),
            Paragraph("", right_cell_style)
        ])

    info_table = Table(info_data, colWidths=[330, 210])
    info_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 15))

    # ── Line items ──
    headers = [
        Paragraph("<b>S.No</b>", table_header_style),
        Paragraph("<b>Product Name</b>", table_header_style),
        Paragraph("<b>HSN</b>", table_header_style),
        Paragraph("<b>Qty</b>", table_header_style),
        Paragraph("<b>Rate (₹)</b>", table_header_style),
        Paragraph("<b>Taxable (₹)</b>", table_header_style),
        Paragraph("<b>GST%</b>", table_header_style),
        Paragraph("<b>CGST (₹)</b>", table_header_style),
        Paragraph("<b>SGST (₹)</b>", table_header_style),
        Paragraph("<b>Total (₹)</b>", table_header_style)
    ]

    table_rows = [headers]
    for idx, item in enumerate(bill_data['items'], 1):
        table_rows.append([
            Paragraph(str(idx), cell_style),
            Paragraph(item['name'], cell_style),
            Paragraph(item.get('hsn_code') or "-", cell_style),
            Paragraph(f"{item['qty']} {item['unit']}", cell_style),
            Paragraph(f"{item['unit_price']:.2f}", right_cell_style),
            Paragraph(f"{item.get('line_subtotal', item['qty'] * item['unit_price']):.2f}", right_cell_style),
            Paragraph(f"{item['gst_slab']}%", right_cell_style),
            Paragraph(f"{item['cgst']:.2f}", right_cell_style),
            Paragraph(f"{item['sgst']:.2f}", right_cell_style),
            Paragraph(f"{item['line_total']:.2f}", right_cell_style)
        ])

    items_table = Table(table_rows, colWidths=[24, 150, 34, 44, 42, 48, 34, 42, 42, 48])
    items_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2B6CB0')),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F7FAFC')]),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 15))

    # ── Summary + tax breakup side by side ──
    summary = bill_data['summary']

    # Per-slab tax breakup (HSN-wise summary, GST-compliant)
    slab_totals: Dict[float, Dict[str, float]] = {}
    for item in bill_data['items']:
        slab = float(item['gst_slab'])
        taxable = item.get('line_subtotal', item['qty'] * item['unit_price'])
        bucket = slab_totals.setdefault(slab, {"taxable": 0.0, "cgst": 0.0, "sgst": 0.0})
        bucket["taxable"] += taxable
        bucket["cgst"] += item['cgst']
        bucket["sgst"] += item['sgst']

    breakup_title_style = ParagraphStyle('BreakupTitle', parent=cell_style, fontName='Helvetica-Bold', textColor=colors.HexColor('#2B6CB0'))

    breakup_data = [[Paragraph("<b>Tax Breakup by Slab</b>", breakup_title_style), Paragraph("", cell_style), Paragraph("", cell_style)]]
    if slab_totals:
        breakup_data.append([
            Paragraph("<b>GST Rate</b>", ParagraphStyle('Bh', parent=cell_style, fontName='Helvetica-Bold')),
            Paragraph("<b>Taxable Amt (₹)</b>", ParagraphStyle('Bh2', parent=right_cell_style, fontName='Helvetica-Bold')),
            Paragraph("<b>CGST + SGST (₹)</b>", ParagraphStyle('Bh3', parent=right_cell_style, fontName='Helvetica-Bold'))
        ])
        for slab in sorted(slab_totals.keys()):
            b = slab_totals[slab]
            breakup_data.append([
                Paragraph(f"{slab:g}%", cell_style),
                Paragraph(f"{b['taxable']:.2f}", right_cell_style),
                Paragraph(f"{b['cgst'] + b['sgst']:.2f}", right_cell_style)
            ])

    breakup_table = Table(breakup_data, colWidths=[110, 130, 150])
    breakup_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('GRID', (0, 1), (-1, -1), 0.5, colors.HexColor('#CBD5E0')),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))

    summary_data = [
        [Paragraph("Subtotal:", cell_style), Paragraph(f"₹ {summary['subtotal']:.2f}", right_cell_style)],
        [Paragraph("Total CGST:", cell_style), Paragraph(f"₹ {summary['cgst']:.2f}", right_cell_style)],
        [Paragraph("Total SGST:", cell_style), Paragraph(f"₹ {summary['sgst']:.2f}", right_cell_style)],
        [Paragraph("Total Tax (GST):", cell_style), Paragraph(f"₹ {summary['total_gst']:.2f}", right_cell_style)],
        [Paragraph("<b>Grand Total:</b>", ParagraphStyle('GT', parent=cell_style, fontName='Helvetica-Bold', fontSize=11)),
         Paragraph(f"<b>₹ {summary['grand_total']:.2f}</b>", ParagraphStyle('GTR', parent=right_cell_style, fontName='Helvetica-Bold', fontSize=11, textColor=colors.HexColor('#2B6CB0')))]
    ]
    summary_table = Table(summary_data, colWidths=[120, 110])
    summary_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E0')),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#EDF2F7')),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))

    words_style = ParagraphStyle('Words', parent=cell_style, fontName='Helvetica-Oblique', fontSize=8.5)
    words_para = Paragraph(f"<b>Amount in Words:</b> {amount_to_indian_words(summary['grand_total'])}", words_style)

    bottom_wrapper = Table(
        [[breakup_table, summary_table], [words_para, Paragraph("", cell_style)]],
        colWidths=[400, 240]
    )
    bottom_wrapper.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 1), (-1, 1), 8),
    ]))
    story.append(bottom_wrapper)

    # ── Signature block & footer ──
    story.append(Spacer(1, 30))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#CBD5E0'), spaceAfter=10))

    sign_data = [
        [Paragraph("Customer's Signature", ParagraphStyle('SignL', parent=cell_style, fontSize=8.5, textColor=colors.HexColor('#718096'))),
         Paragraph(f"For <b>{shop['shop_name']}</b><br/><br/><br/>Authorised Signatory", ParagraphStyle('SignR', parent=right_cell_style, fontSize=8.5, textColor=colors.HexColor('#718096')))]
    ]
    sign_table = Table(sign_data, colWidths=[270, 270])
    sign_table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'BOTTOM'), ('TOPPADDING', (0, 0), (-1, -1), 12)]))
    story.append(sign_table)

    story.append(Spacer(1, 12))
    story.append(Paragraph(
        "This is a computer-generated invoice. Goods once sold will only be replaced as per store policy. "
        "Subject to local jurisdiction.",
        ParagraphStyle('Footer', parent=styles['Normal'], alignment=1, fontSize=8, textColor=colors.HexColor('#718096'))
    ))

    doc.build(story)
    return file_path
