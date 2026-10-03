"""
EMI / Installment Khata (Credit) Management Skill.

Enables customers to purchase large grocery baskets or festive packs on EMI / installments:
  - Create installment plans with custom frequencies (e.g. 3 installments every 15 days)
  - Record periodic installment payments
  - Track upcoming and overdue installment deadlines
  - Generate WhatsApp EMI reminder links with dynamic UPI payment instructions
"""

import math
import uuid
import urllib.parse
from datetime import date, datetime, timedelta
from typing import Dict, Any, List, Optional
from db.models import get_db_connection, immediate_transaction
from skills.whatsapp import sanitize_phone_for_whatsapp, DEFAULT_UPI_VPA, DEFAULT_SHOP_NAME


def create_installment_plan(
    customer_name: str,
    total_amount: float,
    num_installments: int = 3,
    frequency_days: int = 15
) -> Dict[str, Any]:
    """
    Set up a structured installment / EMI repayment plan for a customer.
    """
    if not customer_name:
        return {"status": "error", "message": "customer_name is required."}

    try:
        amt = float(total_amount)
        if not math.isfinite(amt) or amt <= 0:
            return {"status": "error", "message": "total_amount must be a positive number."}
        num_inst = max(2, min(12, int(num_installments)))
        freq = max(7, min(60, int(frequency_days)))
    except (ValueError, TypeError):
        return {"status": "error", "message": "Invalid numeric inputs for amount or installments."}

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT customer_id, name, phone, khata_balance FROM customers WHERE name ILIKE %s", (f"%{customer_name.strip()}%",))
        customer = cur.fetchone()
        if not customer:
            return {"status": "error", "message": f"Customer '{customer_name}' not found. Please create their customer account first."}

        cid = customer["customer_id"]
        plan_id = f"EMI-{uuid.uuid4().hex[:6].upper()}"
        installment_amt = round(amt / num_inst, 2)
        first_due_date = date.today() + timedelta(days=freq)

        with immediate_transaction(conn):
            cur.execute("""
                INSERT INTO khata_installments (
                    plan_id, customer_id, total_amount, installment_amount,
                    num_installments, installments_paid, amount_paid, status,
                    due_date, frequency_days
                ) VALUES (%s, %s, %s, %s, %s, 0, 0.0, 'active', %s, %s)
            """, (plan_id, cid, amt, installment_amt, num_inst, first_due_date, freq))
            cur.close()

        lines = [
            f"🤝 **EMI Installment Plan Activated: {plan_id}**",
            f"👤 Customer: **{customer['name']}** (📞 {customer.get('phone') or 'Not Registered'})",
            f"💰 Total Amount: **₹{amt:,.2f}**",
            f"📅 Structure: **{num_inst} installments** of **₹{installment_amt:,.2f}** every {freq} days",
            f"⏰ 1st Installment Due: **{first_due_date.strftime('%d %B %Y')}**"
        ]

        return {
            "status": "success",
            "plan_id": plan_id,
            "customer_name": customer["name"],
            "total_amount": amt,
            "num_installments": num_inst,
            "installment_amount": installment_amt,
            "first_due_date": str(first_due_date),
            "message": "\n".join(lines)
        }
    finally:
        conn.close()


def record_installment_payment(
    plan_id: str,
    amount: float
) -> Dict[str, Any]:
    """
    Record an EMI payment towards an active installment plan.
    """
    if not plan_id:
        return {"status": "error", "message": "plan_id is required."}

    try:
        amt = float(amount)
        if not math.isfinite(amt) or amt <= 0:
            return {"status": "error", "message": "amount must be a positive number."}
    except (ValueError, TypeError):
        return {"status": "error", "message": "amount must be a valid number."}

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("""
                SELECT ki.*, c.name as customer_name, c.phone
                FROM khata_installments ki
                JOIN customers c ON ki.customer_id = c.customer_id
                WHERE ki.plan_id = %s FOR UPDATE
            """, (plan_id.strip(),))
            plan = cur.fetchone()

            if not plan:
                return {"status": "error", "message": f"Installment plan '{plan_id}' not found."}

            if plan["status"] == "completed":
                return {"status": "error", "message": f"Plan '{plan_id}' is already fully paid and completed."}

            new_amount_paid = round(float(plan["amount_paid"]) + amt, 2)
            new_installments_paid = int(plan["installments_paid"]) + 1
            total_amount = float(plan["total_amount"])
            remaining = max(0.0, round(total_amount - new_amount_paid, 2))

            if remaining <= 0:
                new_status = "completed"
                next_due = plan["due_date"]
            else:
                new_status = "active"
                next_due = plan["due_date"] + timedelta(days=plan["frequency_days"])

            cur.execute("""
                UPDATE khata_installments
                SET amount_paid = %s,
                    installments_paid = %s,
                    status = %s,
                    due_date = %s
                WHERE plan_id = %s
            """, (new_amount_paid, new_installments_paid, new_status, next_due, plan_id.strip()))

            # Also record in khata transactions for audit trail
            cur.execute("""
                INSERT INTO khata_transactions (customer_id, type, amount)
                VALUES (%s, 'payment', %s)
            """, (plan["customer_id"], amt))
            cur.close()

        status_msg = "🎉 Fully paid off!" if new_status == "completed" else f"Next due: {next_due.strftime('%d %B %Y')} (Remaining: ₹{remaining:,.2f})"
        return {
            "status": "success",
            "plan_id": plan_id,
            "customer_name": plan["customer_name"],
            "amount_paid_now": amt,
            "total_paid": new_amount_paid,
            "remaining_balance": remaining,
            "plan_status": new_status,
            "message": f"✅ Payment of ₹{amt:,.2f} recorded for {plan['customer_name']} on plan {plan_id}! {status_msg}"
        }
    finally:
        conn.close()


def list_active_installments() -> Dict[str, Any]:
    """
    List all ongoing EMI plans with upcoming due dates and overdue statuses.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT ki.*, c.name as customer_name, c.phone
            FROM khata_installments ki
            JOIN customers c ON ki.customer_id = c.customer_id
            WHERE ki.status = 'active'
            ORDER BY ki.due_date ASC
        """)
        rows = cur.fetchall()
        cur.close()

        today = date.today()
        plans = []
        for r in rows:
            due = r["due_date"]
            days_diff = (due - today).days
            if days_diff < 0:
                urgency = f"🔴 OVERDUE ({abs(days_diff)} days ago)"
            elif days_diff <= 3:
                urgency = f"🟡 DUE IN {days_diff} DAYS"
            else:
                urgency = f"🟢 Due in {days_diff} days"

            rem = round(float(r["total_amount"]) - float(r["amount_paid"]), 2)
            plans.append({
                "plan_id": r["plan_id"],
                "customer_name": r["customer_name"],
                "phone": r["phone"] or "N/A",
                "installment_amount": float(r["installment_amount"]),
                "paid_installments": f"{r['installments_paid']}/{r['num_installments']}",
                "remaining_balance": rem,
                "due_date": str(due),
                "urgency": urgency
            })

        lines = [f"📅 **Active EMI Installment Plans ({len(plans)}):**\n"]
        for p in plans:
            lines.append(
                f"• **{p['plan_id']}** — {p['customer_name']} (📞 {p['phone']})\n"
                f"  Installment: ₹{p['installment_amount']:,.2f} ({p['paid_installments']} paid) | Remaining: ₹{p['remaining_balance']:,.2f}\n"
                f"  Status: {p['urgency']} ({p['due_date']})"
            )

        return {
            "status": "success",
            "count": len(plans),
            "plans": plans,
            "message": "\n\n".join(lines) if plans else "✅ No active installment plans currently open."
        }
    finally:
        conn.close()


def generate_installment_reminder_link(plan_id: str) -> Dict[str, Any]:
    """
    Generate a personalized WhatsApp EMI reminder link with UPI payment details.
    """
    if not plan_id:
        return {"status": "error", "message": "plan_id is required."}

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT ki.*, c.name as customer_name, c.phone
            FROM khata_installments ki
            JOIN customers c ON ki.customer_id = c.customer_id
            WHERE ki.plan_id = %s
        """, (plan_id.strip(),))
        plan = cur.fetchone()
        cur.close()

        if not plan:
            return {"status": "error", "message": f"Installment plan '{plan_id}' not found."}

        rem = round(float(plan["total_amount"]) - float(plan["amount_paid"]), 2)
        inst_amt = float(plan["installment_amount"])
        due_str = plan["due_date"].strftime("%d %B %Y")

        upi_intent = f"upi://pay?pa={DEFAULT_UPI_VPA}&pn={urllib.parse.quote(DEFAULT_SHOP_NAME)}&am={inst_amt:.2f}&cu=INR&tn={plan_id}"

        text = (
            f"Namaste {plan['customer_name']} ji! 🙏\n\n"
            f"This is a gentle payment reminder from *{DEFAULT_SHOP_NAME}* for your EMI plan *{plan_id}*.\n\n"
            f"📋 *Installment Details:*\n"
            f"• Due Amount: *₹{inst_amt:,.2f}*\n"
            f"• Due Date: *{due_str}*\n"
            f"• Installment: {plan['installments_paid'] + 1} of {plan['num_installments']}\n"
            f"• Total Remaining Balance: ₹{rem:,.2f}\n\n"
            f"📲 *Pay via UPI:*\n"
            f"• UPI ID: `{DEFAULT_UPI_VPA}`\n"
            f"• One-tap Pay: {upi_intent}\n\n"
            f"Thank you for being a valued customer! 🙏"
        )

        encoded = urllib.parse.quote(text)
        sanitized_phone = sanitize_phone_for_whatsapp(plan["phone"])

        if sanitized_phone:
            link = f"https://wa.me/{sanitized_phone}?text={encoded}"
        else:
            link = f"https://wa.me/?text={encoded}"

        return {
            "status": "success",
            "plan_id": plan_id,
            "customer_name": plan["customer_name"],
            "whatsapp_link": link,
            "message": f"📱 WhatsApp EMI Reminder Link generated for **{plan['customer_name']}**:\n👉 [Open WhatsApp Reminder]({link})"
        }
    finally:
        conn.close()
