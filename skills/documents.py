from typing import Dict, Any, Optional
from docgen.invoice_template import generate_pdf_invoice
from docgen.deck_builder import generate_analysis_pptx


def generate_invoice_pdf(bill_id: str) -> Dict[str, Any]:
    """
    Generate a GST-compliant PDF invoice (sequential invoice number, slab-wise tax
    breakup, amount in words, place of supply, signature block) for a given bill ID.
    """
    try:
        pdf_path = generate_pdf_invoice(bill_id)
        return {
            "status": "success",
            "message": f"PDF Invoice for Bill '{bill_id}' generated successfully.",
            "file_path": pdf_path,
            "bill_id": bill_id
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to generate PDF invoice: {str(e)}"
        }


def generate_analysis_deck(period: str = "Today", shop_name: Optional[str] = None,
                           days: Optional[int] = None) -> Dict[str, Any]:
    """
    Generate a PowerPoint (.pptx) executive sales analysis presentation deck
    with real charts and data-driven recommendations.

    `days`: aggregation window (defaults: 1 for 'Today', 7 weekly, 30 monthly).
    """
    try:
        # Infer window from the period string when not given explicitly
        if days is None:
            p = (period or "").lower()
            if "today" in p or "daily" in p:
                days = 1
            elif "month" in p:
                days = 30
            else:
                days = 7

        pptx_path = generate_analysis_pptx(period, shop_name=shop_name, days=days)
        return {
            "status": "success",
            "message": f"PowerPoint Analysis Deck generated successfully for period '{period}' (last {days} days).",
            "file_path": pptx_path,
            "period": period,
            "days": days
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to generate analysis deck: {str(e)}"
        }
