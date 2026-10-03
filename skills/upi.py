"""
Dynamic UPI QR Code Generator Skill for SuperMart AI Ops Agent.

Generates NPCI-compliant dynamic UPI payment links and high-resolution QR codes
for exact bill amounts, Khata settlements, and customer payments.
Compatible with Google Pay, PhonePe, Paytm, BHIM, and all Indian banking apps.
"""

import os
import re
import urllib.parse
from typing import Dict, Any, Optional
import qrcode
from dotenv import load_dotenv

load_dotenv()

# Shop UPI configuration
DEFAULT_UPI_VPA = os.getenv("UPI_VPA", "supermart@upi").strip()
DEFAULT_MERCHANT_NAME = os.getenv("SHOP_NAME", "SuperMart Supermarket").strip()


def build_upi_uri(
    amount: float,
    vpa: Optional[str] = None,
    merchant_name: Optional[str] = None,
    transaction_note: Optional[str] = None,
    transaction_ref: Optional[str] = None
) -> str:
    """
    Construct an NPCI-compliant UPI deep link URI string:
    upi://pay?pa={vpa}&pn={merchant_name}&am={amount:.2f}&cu=INR&tn={note}&tr={ref}
    """
    payee_vpa = (vpa or DEFAULT_UPI_VPA or "supermart@upi").strip()
    payee_name = (merchant_name or DEFAULT_MERCHANT_NAME or "SuperMart").strip()
    note = (transaction_note or "Supermarket Bill Payment").strip()
    
    params = {
        "pa": payee_vpa,
        "pn": payee_name,
        "am": f"{amount:.2f}",
        "cu": "INR",
        "tn": note
    }
    if transaction_ref:
        params["tr"] = re.sub(r'[^a-zA-Z0-9_-]', '', transaction_ref)

    return f"upi://pay?{urllib.parse.urlencode(params)}"


def generate_upi_qr_code(
    amount: float,
    bill_id: Optional[str] = None,
    vpa: Optional[str] = None,
    merchant_name: Optional[str] = None,
    note: Optional[str] = None,
    output_dir: str = "generated_docs"
) -> Dict[str, Any]:
    """
    Generate a dynamic UPI QR Code image file for the given amount/bill.
    Returns status, image file path, UPI URI, and formatted payment details.
    """
    if amount <= 0:
        return {"status": "error", "message": "Amount must be greater than zero."}

    os.makedirs(output_dir, exist_ok=True)

    txn_note = note or (f"Bill {bill_id}" if bill_id else "Supermarket Payment")
    upi_uri = build_upi_uri(
        amount=amount,
        vpa=vpa,
        merchant_name=merchant_name,
        transaction_note=txn_note,
        transaction_ref=bill_id
    )

    clean_id = re.sub(r'[^a-zA-Z0-9_-]', '', bill_id) if bill_id else f"pay_{int(amount*100)}"
    file_name = f"upi_qr_{clean_id}.png"
    file_path = os.path.join(output_dir, file_name)

    # Render QR code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=3,
    )
    qr.add_data(upi_uri)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    img.save(file_path)

    payee_vpa = (vpa or DEFAULT_UPI_VPA).strip()
    payee_name = (merchant_name or DEFAULT_MERCHANT_NAME).strip()

    return {
        "status": "success",
        "file_path": file_path,
        "upi_uri": upi_uri,
        "amount": round(amount, 2),
        "vpa": payee_vpa,
        "merchant_name": payee_name,
        "bill_id": bill_id,
        "message": (
            f"📱 **Dynamic UPI QR Generated**\n\n"
            f"• **Amount:** ₹{amount:.2f}\n"
            f"• **Payee VPA:** `{payee_vpa}`\n"
            f"• **Merchant:** {payee_name}\n"
            f"• **Note:** {txn_note}\n\n"
            f"⚡ Scan with GPay, PhonePe, Paytm, or BHIM to pay instantly!"
        )
    }


def generate_upi_qr_for_bill(bill_id: str, vpa: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetch finalized bill details from DB and generate a dynamic UPI QR Code.
    """
    if not bill_id or not isinstance(bill_id, str):
        return {"status": "error", "message": "bill_id is required."}

    from db.models import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT bill_id, grand_total, customer_id FROM bills WHERE bill_id = %s", (bill_id.strip(),))
        row = cur.fetchone()
        if not row:
            cur.close()
            return {"status": "error", "message": f"Bill '{bill_id}' not found."}

        grand_total = float(row["grand_total"] or 0.0)
        cur.close()
    finally:
        conn.close()

    if grand_total <= 0:
        return {"status": "error", "message": f"Bill '{bill_id}' has a zero total."}

    return generate_upi_qr_code(
        amount=grand_total,
        bill_id=bill_id,
        vpa=vpa,
        note=f"Payment for Bill {bill_id}"
    )
