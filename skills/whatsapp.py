"""
WhatsApp Digital Receipt & Khata Reminder Skill for SuperMart AI Ops Agent.

Generates one-tap WhatsApp deep links (https://wa.me/) for:
1. Itemized digital GST tax receipts sent immediately to customer WhatsApp numbers.
2. Polite, professional customer Khata (credit ledger) balance payment reminders with UPI ID.
"""

import os
import re
import urllib.parse
from typing import Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()

DEFAULT_SHOP_NAME = os.getenv("SHOP_NAME", "SuperMart Supermarket").strip()
DEFAULT_UPI_VPA = os.getenv("UPI_VPA", "supermart@upi").strip()


def sanitize_phone_for_whatsapp(phone: Optional[str]) -> Optional[str]:
    """
    Format phone number to international E.164 digits without '+' or spaces for wa.me.
    Defaults Indian 10-digit mobile numbers to '91' country code prefix.
    """
    if not phone or not isinstance(phone, str):
        return None

    # Strip everything except digits
    digits = re.sub(r'\D', '', phone)
    if not digits:
        return None

    # If 10 digits (standard Indian mobile), prepend 91
    if len(digits) == 10:
        return f"91{digits}"
    return digits


def generate_whatsapp_bill_link(
    bill_id: str,
    phone: Optional[str] = None
) -> Dict[str, Any]:
    """
    Generate an itemized digital receipt formatted for WhatsApp,
    with a one-tap wa.me link to share directly with the customer.
    """
    if not bill_id or not isinstance(bill_id, str):
        return {"status": "error", "message": "bill_id is required."}

    from skills.billing import preview_bill
    bill_data = preview_bill(bill_id.strip())
    if bill_data.get("status") == "error":
        return bill_data

    shop_name = DEFAULT_SHOP_NAME
    summary = bill_data["summary"]
    items = bill_data.get("items", [])
    customer_name = bill_data.get("customer_name") or "Valued Customer"
    payment_mode = (bill_data.get("payment_mode") or "CASH").upper()

    # Build WhatsApp message with native formatting (*bold*, _italic_)
    separator = "────────────────────────────"
    msg_lines = [
        f"🧾 *{shop_name.upper()}*",
        f"📍 _Supermarket Tax Invoice & Receipt_",
        separator,
        f"🔖 *Bill No:* `{bill_id}`",
        f"👤 *Customer:* {customer_name}",
        f"💳 *Payment Mode:* {payment_mode}",
        separator,
        "*Purchased Items:*"
    ]

    for idx, item in enumerate(items, 1):
        qty_val = item['qty']
        qty_str = f"{qty_val:.0f}" if qty_val == int(qty_val) else f"{qty_val:.2f}"
        msg_lines.append(
            f"• {item['name']}\n"
            f"   {qty_str} {item.get('unit', 'unit')} × ₹{item['unit_price']:.2f} = *₹{item['line_total']:.2f}*"
        )

    msg_lines.extend([
        separator,
        f"• Subtotal: ₹{summary['subtotal']:.2f}",
        f"• Total GST: ₹{summary['total_gst']:.2f}",
        f"💰 *GRAND TOTAL: ₹{summary['grand_total']:.2f}*",
        separator,
        f"Thank you for shopping at *{shop_name}*! 🙏",
        "Visit again soon! 🛒"
    ])

    full_message = "\n".join(msg_lines)
    clean_phone = sanitize_phone_for_whatsapp(phone)

    # If phone is known, direct link; otherwise open WhatsApp chat picker
    encoded_text = urllib.parse.quote(full_message)
    if clean_phone:
        wa_url = f"https://wa.me/{clean_phone}?text={encoded_text}"
    else:
        wa_url = f"https://wa.me/?text={encoded_text}"

    return {
        "status": "success",
        "bill_id": bill_id,
        "customer_name": customer_name,
        "grand_total": summary["grand_total"],
        "whatsapp_url": wa_url,
        "message_text": full_message,
        "phone": clean_phone,
        "message": (
            f"📱 **WhatsApp Digital Receipt Ready**\n\n"
            f"• **Bill:** `{bill_id}`\n"
            f"• **Customer:** {customer_name}\n"
            f"• **Amount:** ₹{summary['grand_total']:.2f}\n\n"
            f"[👉 Tap here to send on WhatsApp]({wa_url})"
        )
    }


def generate_whatsapp_khata_reminder_link(
    customer_name: str,
    phone: Optional[str] = None
) -> Dict[str, Any]:
    """
    Generate a polite, professional WhatsApp Khata payment reminder with shop UPI ID.
    Returns status, formatted text, and one-tap wa.me link.
    """
    if not customer_name or not isinstance(customer_name, str):
        return {"status": "error", "message": "customer_name is required."}

    from skills.credit import get_khata_balance
    bal_res = get_khata_balance(customer_name.strip())
    if bal_res.get("status") == "error":
        return bal_res

    balance = float(bal_res.get("khata_balance") if bal_res.get("khata_balance") is not None else bal_res.get("balance", 0.0))
    cust_name = bal_res.get("customer_name", customer_name)
    cust_phone = phone or bal_res.get("phone")

    if balance <= 0:
        return {
            "status": "no_due",
            "customer_name": cust_name,
            "balance": 0.0,
            "message": f"✅ Customer **{cust_name}** has NO pending Khata balance (All dues are cleared)!"
        }

    shop_name = DEFAULT_SHOP_NAME
    upi_vpa = DEFAULT_UPI_VPA

    # Construct polite, respectful Khata reminder message
    msg = (
        f"Namaste {cust_name} Ji 🙏,\n\n"
        f"This is a gentle payment reminder from *{shop_name}* regarding your store credit (Khata) account.\n\n"
        f"📌 *Outstanding Balance: ₹{balance:.2f}*\n\n"
        f"💳 *Quick Payment Options:*\n"
        f"1. *UPI (GPay / PhonePe / Paytm):* `{upi_vpa}`\n"
        f"2. *Cash:* You can clear your balance at our store billing counter.\n\n"
        f"Thank you for being our valued customer! Have a wonderful day. 😊"
    )

    clean_phone = sanitize_phone_for_whatsapp(cust_phone)
    encoded_text = urllib.parse.quote(msg)

    if clean_phone:
        wa_url = f"https://wa.me/{clean_phone}?text={encoded_text}"
    else:
        wa_url = f"https://wa.me/?text={encoded_text}"

    return {
        "status": "success",
        "customer_name": cust_name,
        "balance": balance,
        "whatsapp_url": wa_url,
        "message_text": msg,
        "phone": clean_phone,
        "message": (
            f"💬 **WhatsApp Khata Reminder Generated**\n\n"
            f"• **Customer:** {cust_name}\n"
            f"• **Pending Balance:** ₹{balance:.2f}\n"
            f"• **UPI ID:** `{upi_vpa}`\n\n"
            f"[👉 Tap here to send WhatsApp reminder]({wa_url})"
        )
    }
