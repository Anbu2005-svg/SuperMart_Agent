"""
GST Input Tax Credit (ITC) & Net Tax Liability Tracker for SuperMart AI Ops Agent.

Computes:
  - Output GST: Collected on retail sales bills (CGST + SGST)
  - Input GST (ITC): Paid on supplier / vendor stock invoices (CGST + SGST + IGST)
  - Net GST Tax Liability: Output Tax minus Eligible Input Tax Credit
  - GSTR-3B Table 4 summary with slab-wise breakdown (0%, 5%, 12%, 18%, 28%)
"""

from typing import Dict, Any, List, Optional
from datetime import datetime
from db.models import get_db_connection


def get_gst_itc_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
) -> Dict[str, Any]:
    """
    Generate complete GST Input Tax Credit (ITC) and Net Liability report.
    Compares outward taxable supplies (sales bills) vs inward supplies (supplier invoices).
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # ── 1. OUTWARD SUPPLIES (Retail Sales Bills) ──
        bill_conditions = ["status = 'finalized'"]
        bill_params: List[Any] = []
        if date_from:
            bill_conditions.append("DATE(finalized_at) >= %s::date")
            bill_params.append(date_from)
        if date_to:
            bill_conditions.append("DATE(finalized_at) <= %s::date")
            bill_params.append(date_to)

        where_bills = " AND ".join(bill_conditions)
        cur.execute(f"""
            SELECT 
                COUNT(*) AS bill_count,
                COALESCE(SUM(subtotal), 0) AS total_taxable_sales,
                COALESCE(SUM(cgst), 0) AS output_cgst,
                COALESCE(SUM(sgst), 0) AS output_sgst,
                COALESCE(SUM(cgst + sgst), 0) AS total_output_gst,
                COALESCE(SUM(total), 0) AS gross_sales_value
            FROM bills
            WHERE {where_bills}
        """, bill_params)
        sales_row = cur.fetchone()

        output_cgst = float(sales_row["output_cgst"] or 0)
        output_sgst = float(sales_row["output_sgst"] or 0)
        total_output_gst = float(sales_row["total_output_gst"] or 0)
        total_taxable_sales = float(sales_row["total_taxable_sales"] or 0)
        gross_sales = float(sales_row["gross_sales_value"] or 0)
        sales_bill_count = int(sales_row["bill_count"] or 0)

        # Sales slab-wise breakdown
        cur.execute(f"""
            SELECT 
                bi.gst_slab,
                COALESCE(SUM(bi.qty * bi.unit_price), 0) AS taxable_val,
                COALESCE(SUM((bi.qty * bi.unit_price) * (bi.gst_slab / 100.0)), 0) AS tax_val
            FROM bill_items bi
            JOIN bills b ON bi.bill_id = b.bill_id
            WHERE {where_bills}
            GROUP BY bi.gst_slab
            ORDER BY bi.gst_slab ASC
        """, bill_params)
        sales_slabs = cur.fetchall()

        outward_by_slab = {}
        for s in sales_slabs:
            slab_rate = float(s["gst_slab"])
            taxable = float(s["taxable_val"])
            tax = float(s["tax_val"])
            outward_by_slab[f"{slab_rate:.1f}%"] = {
                "taxable_turnover": round(taxable, 2),
                "cgst": round(tax / 2.0, 2),
                "sgst": round(tax / 2.0, 2),
                "total_output_gst": round(tax, 2)
            }

        # ── 2. INWARD SUPPLIES (Supplier Purchase Bills / Invoices) ──
        supp_conditions = ["1=1"]
        supp_params: List[Any] = []
        if date_from:
            supp_conditions.append("DATE(created_at) >= %s::date")
            supp_params.append(date_from)
        if date_to:
            supp_conditions.append("DATE(created_at) <= %s::date")
            supp_params.append(date_to)

        where_supp = " AND ".join(supp_conditions)
        cur.execute(f"""
            SELECT 
                COUNT(*) AS invoice_count,
                COALESCE(SUM(taxable_amount), 0) AS total_purchase_taxable,
                COALESCE(SUM(cgst), 0) AS input_cgst,
                COALESCE(SUM(sgst), 0) AS input_sgst,
                COALESCE(SUM(igst), 0) AS input_igst,
                COALESCE(SUM(COALESCE(gst_amount, cgst + sgst + igst)), 0) AS total_input_gst,
                COALESCE(SUM(total_amount), 0) AS total_purchases
            FROM supplier_bills
            WHERE {where_supp}
        """, supp_params)
        supp_row = cur.fetchone()

        input_cgst = float(supp_row["input_cgst"] or 0)
        input_sgst = float(supp_row["input_sgst"] or 0)
        input_igst = float(supp_row["input_igst"] or 0)
        total_input_gst = float(supp_row["total_input_gst"] or 0)
        total_purchases = float(supp_row["total_purchases"] or 0)
        supp_invoice_count = int(supp_row["invoice_count"] or 0)

        cur.close()

        # ── 3. NET TAX LIABILITY CALCULATION ──
        # CGST balance = Output CGST - Input CGST - (offset from excess IGST if any)
        # SGST balance = Output SGST - Input SGST
        total_itc_available = round(input_cgst + input_sgst + input_igst, 2)
        net_liability = round(total_output_gst - total_itc_available, 2)

        net_cgst = round(output_cgst - input_cgst, 2)
        net_sgst = round(output_sgst - input_sgst, 2)

        if net_liability > 0:
            tax_position = f"🔴 Net GST Payable: ₹{net_liability:.2f}"
            action_advice = f"You have collected more GST than paid on purchases. Pay ₹{net_liability:.2f} via GSTR-3B challan."
        elif net_liability < 0:
            tax_position = f"🟢 ITC Credit Available (Carry Forward): ₹{abs(net_liability):.2f}"
            action_advice = f"Input Tax Credit exceeds output liability by ₹{abs(net_liability):.2f}. This amount carries forward to offset next month's tax."
        else:
            tax_position = "⚪ GST Neutral (₹0.00)"
            action_advice = "Input GST exactly equals Output GST."

        period_label = f"{date_from or 'All time'} to {date_to or 'Present'}"

        return {
            "status": "success",
            "period": period_label,
            "outward_supplies": {
                "sales_bills_count": sales_bill_count,
                "gross_turnover": round(gross_sales, 2),
                "taxable_turnover": round(total_taxable_sales, 2),
                "output_cgst": round(output_cgst, 2),
                "output_sgst": round(output_sgst, 2),
                "total_output_tax_collected": round(total_output_gst, 2),
                "slab_breakdown": outward_by_slab
            },
            "inward_supplies_itc": {
                "supplier_invoices_count": supp_invoice_count,
                "total_purchases": round(total_purchases, 2),
                "eligible_input_cgst": round(input_cgst, 2),
                "eligible_input_sgst": round(input_sgst, 2),
                "eligible_input_igst": round(input_igst, 2),
                "total_itc_claimed": total_itc_available
            },
            "net_tax_computation": {
                "total_output_tax": round(total_output_gst, 2),
                "total_input_itc": total_itc_available,
                "net_payable": max(0.0, net_liability),
                "itc_carried_forward": max(0.0, -net_liability),
                "net_cgst_position": net_cgst,
                "net_sgst_position": net_sgst,
                "tax_position": tax_position,
                "advice": action_advice
            }
        }
    finally:
        conn.close()
