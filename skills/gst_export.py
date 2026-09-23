"""
GST Return Export — GSTR-1 Ready CSV Generation.

Generates GSTR-1 formatted data from finalized bills for Indian tax compliance:
  - B2C (Small) Summary: Aggregated by GST slab rate
  - HSN-wise Summary: Grouped by HSN code with taxable value, CGST, SGST
  - Exports as downloadable CSV file
"""
import csv
import os
import logging
from datetime import date
from typing import Dict, Any, List

from db.models import get_db_connection

logger = logging.getLogger(__name__)


def _sanitize_csv_cell(val: Any) -> Any:
    """Neutralize spreadsheet formula injection characters (=, +, -, @, tab, cr)."""
    if isinstance(val, str) and val:
        stripped = val.lstrip(' ')
        if stripped and stripped[0] in ("=", "+", "-", "@", "\t", "\r"):
            return f"'{val}"
    return val


def export_gstr1(month: int = None, year: int = None) -> Dict[str, Any]:
    """
    Generate GSTR-1 compliant export data for a given month/year.
    Defaults to current month if omitted.
    Returns structured data + path to generated CSV files.
    """
    try:
        month_val = date.today().month if month is None else int(month)
        year_val = date.today().year if year is None else int(year)
    except (ValueError, TypeError):
        return {"status": "error", "message": "Month and year must be valid integers."}

    month = max(1, min(12, month_val))
    year = max(2020, min(2099, year_val))

    month_name = date(year, month, 1).strftime("%B %Y")

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # ── B2C Summary: aggregate by GST slab ──
        cur.execute("""
            SELECT bi.gst_slab,
                   COUNT(DISTINCT b.bill_id) AS invoice_count,
                   SUM(bi.qty * bi.unit_price) AS taxable_value,
                   SUM(bi.qty * bi.unit_price * bi.gst_slab / 200.0) AS cgst,
                   SUM(bi.qty * bi.unit_price * bi.gst_slab / 200.0) AS sgst,
                   SUM(bi.line_total) AS total_value
            FROM bill_items bi
            JOIN bills b ON bi.bill_id = b.bill_id
            WHERE b.status = 'finalized'
              AND EXTRACT(MONTH FROM b.finalized_at) = %s
              AND EXTRACT(YEAR FROM b.finalized_at) = %s
            GROUP BY bi.gst_slab
            ORDER BY bi.gst_slab ASC
        """, (month, year))
        b2c_rows = cur.fetchall()

        # ── HSN-wise Summary ──
        cur.execute("""
            SELECT COALESCE(p.hsn_code, 'N/A') AS hsn_code,
                   p.name AS product_name,
                   p.unit AS uqc,
                   SUM(bi.qty) AS total_qty,
                   SUM(bi.qty * bi.unit_price) AS taxable_value,
                   bi.gst_slab AS rate,
                   SUM(bi.qty * bi.unit_price * bi.gst_slab / 200.0) AS cgst,
                   SUM(bi.qty * bi.unit_price * bi.gst_slab / 200.0) AS sgst,
                   SUM(bi.line_total) AS total_value
            FROM bill_items bi
            JOIN bills b ON bi.bill_id = b.bill_id
            JOIN products p ON bi.sku_id = p.sku_id
            WHERE b.status = 'finalized'
              AND EXTRACT(MONTH FROM b.finalized_at) = %s
              AND EXTRACT(YEAR FROM b.finalized_at) = %s
            GROUP BY p.hsn_code, p.name, p.unit, bi.gst_slab
            ORDER BY p.hsn_code ASC
        """, (month, year))
        hsn_rows = cur.fetchall()

        # ── Invoice Summary ──
        cur.execute("""
            SELECT COUNT(*) AS total_invoices,
                   COALESCE(SUM(total), 0) AS total_revenue,
                   COALESCE(SUM(cgst), 0) AS total_cgst,
                   COALESCE(SUM(sgst), 0) AS total_sgst
            FROM bills
            WHERE status = 'finalized'
              AND EXTRACT(MONTH FROM finalized_at) = %s
              AND EXTRACT(YEAR FROM finalized_at) = %s
        """, (month, year))
        invoice_summary = cur.fetchone()
        cur.close()

        # ── Generate CSV files ──
        output_dir = os.path.join("generated_docs")
        os.makedirs(output_dir, exist_ok=True)

        # B2C Summary CSV
        b2c_path = os.path.join(output_dir, f"GSTR1_B2C_{year}_{month:02d}.csv")
        with open(b2c_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["GST Slab (%)", "Invoice Count", "Taxable Value (₹)",
                             "CGST (₹)", "SGST (₹)", "Total Value (₹)"])
            for r in b2c_rows:
                writer.writerow([
                    f"{r['gst_slab']}%", r["invoice_count"],
                    f"{r['taxable_value']:.2f}", f"{r['cgst']:.2f}",
                    f"{r['sgst']:.2f}", f"{r['total_value']:.2f}"
                ])

        # HSN Summary CSV
        hsn_path = os.path.join(output_dir, f"GSTR1_HSN_{year}_{month:02d}.csv")
        with open(hsn_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["HSN Code", "Product", "UQC", "Total Qty",
                             "Taxable Value (₹)", "Rate (%)", "CGST (₹)",
                             "SGST (₹)", "Total Value (₹)"])
            for r in hsn_rows:
                writer.writerow([
                    _sanitize_csv_cell(r["hsn_code"]),
                    _sanitize_csv_cell(r["product_name"]),
                    _sanitize_csv_cell(r["uqc"]),
                    f"{r['total_qty']:.2f}", f"{r['taxable_value']:.2f}",
                    f"{r['rate']}%", f"{r['cgst']:.2f}",
                    f"{r['sgst']:.2f}", f"{r['total_value']:.2f}"
                ])

        # Build response
        b2c_data = [{
            "gst_slab": f"{r['gst_slab']}%",
            "invoice_count": r["invoice_count"],
            "taxable_value": round(r["taxable_value"], 2),
            "cgst": round(r["cgst"], 2),
            "sgst": round(r["sgst"], 2),
            "total_value": round(r["total_value"], 2)
        } for r in b2c_rows]

        hsn_data = [{
            "hsn_code": r["hsn_code"],
            "product": r["product_name"],
            "total_qty": round(r["total_qty"], 2),
            "taxable_value": round(r["taxable_value"], 2),
            "rate": f"{r['rate']}%",
            "cgst": round(r["cgst"], 2),
            "sgst": round(r["sgst"], 2),
            "total_value": round(r["total_value"], 2)
        } for r in hsn_rows]

        total_invoices = invoice_summary["total_invoices"]
        total_revenue = round(invoice_summary["total_revenue"], 2)
        total_cgst = round(invoice_summary["total_cgst"], 2)
        total_sgst = round(invoice_summary["total_sgst"], 2)

        return {
            "status": "success",
            "period": month_name,
            "total_invoices": total_invoices,
            "total_revenue": total_revenue,
            "total_cgst": total_cgst,
            "total_sgst": total_sgst,
            "total_gst": round(total_cgst + total_sgst, 2),
            "b2c_summary": b2c_data,
            "hsn_summary": hsn_data,
            "files": [b2c_path, hsn_path],
            "message": (
                f"📋 GSTR-1 Export for {month_name}:\n"
                f"📄 Total Invoices: {total_invoices}\n"
                f"💰 Total Revenue: ₹{total_revenue:,.2f}\n"
                f"🧾 CGST: ₹{total_cgst:,.2f} | SGST: ₹{total_sgst:,.2f}\n"
                f"📁 CSV files generated:\n"
                f"  • B2C Summary: {b2c_path}\n"
                f"  • HSN Summary: {hsn_path}"
            )
        }
    finally:
        conn.close()
