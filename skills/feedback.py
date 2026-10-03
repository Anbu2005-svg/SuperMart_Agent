"""
Customer Feedback & Experience Rating Skill.

Enables supermarkets to:
  - Collect and record customer ratings (1 to 5 stars) and reviews
  - Compute average store satisfaction score and sentiment
  - Generate 1-tap WhatsApp survey links for customers post-billing
"""

import urllib.parse
from datetime import date, timedelta
from typing import Dict, Any, List, Optional
from db.models import get_db_connection, immediate_transaction
from skills.whatsapp import sanitize_phone_for_whatsapp


def record_customer_feedback(
    customer_name: str,
    rating: int,
    feedback_text: Optional[str] = None,
    product_name: Optional[str] = None,
    bill_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Record a customer rating (1-5 stars) and optional comments/product feedback.
    """
    if not customer_name:
        return {"status": "error", "message": "customer_name is required."}

    try:
        rating_int = int(rating)
        if not (1 <= rating_int <= 5):
            return {"status": "error", "message": "Rating must be an integer between 1 and 5 stars."}
    except (ValueError, TypeError):
        return {"status": "error", "message": "Rating must be a valid integer between 1 and 5."}

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Find customer ID if registered
        cur.execute("SELECT customer_id FROM customers WHERE name ILIKE %s", (f"%{customer_name.strip()}%",))
        c_row = cur.fetchone()
        customer_id = c_row["customer_id"] if c_row else None

        # Find SKU ID if product mentioned
        sku_id = None
        if product_name:
            cur.execute("SELECT sku_id FROM products WHERE name ILIKE %s LIMIT 1", (f"%{product_name.strip()}%",))
            p_row = cur.fetchone()
            if p_row:
                sku_id = p_row["sku_id"]

        with immediate_transaction(conn):
            cur.execute("""
                INSERT INTO customer_feedback (customer_id, customer_name, bill_id, sku_id, rating, feedback_text)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (customer_id, customer_name.strip(), bill_id, sku_id, rating_int, feedback_text))
            new_id = cur.fetchone()["id"]
            cur.close()

        stars = "⭐" * rating_int
        return {
            "status": "success",
            "feedback_id": new_id,
            "customer_name": customer_name,
            "rating": rating_int,
            "message": f"✅ Recorded {stars} rating ({rating_int}/5) from {customer_name}! Thank you for capturing customer sentiment."
        }
    finally:
        conn.close()


def get_feedback_summary(days: int = 30) -> Dict[str, Any]:
    """
    Calculate average customer rating, star distribution, and recent comments.
    """
    days = max(1, min(365, int(days)))
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) AS total_reviews,
                   COALESCE(AVG(rating), 0) AS avg_rating,
                   SUM(CASE WHEN rating = 5 THEN 1 ELSE 0 END) AS r5,
                   SUM(CASE WHEN rating = 4 THEN 1 ELSE 0 END) AS r4,
                   SUM(CASE WHEN rating = 3 THEN 1 ELSE 0 END) AS r3,
                   SUM(CASE WHEN rating = 2 THEN 1 ELSE 0 END) AS r2,
                   SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END) AS r1
            FROM customer_feedback
            WHERE created_at::date >= %s::date
        """, (cutoff,))
        summary = cur.fetchone()

        total = summary["total_reviews"] or 0
        avg_rating = round(float(summary["avg_rating"]), 2)

        cur.execute("""
            SELECT customer_name, rating, feedback_text, created_at
            FROM customer_feedback
            WHERE created_at::date >= %s::date AND feedback_text IS NOT NULL AND feedback_text != ''
            ORDER BY created_at DESC
            LIMIT 5
        """, (cutoff,))
        comments = cur.fetchall()
        cur.close()

        r5 = summary["r5"] or 0
        r4 = summary["r4"] or 0
        r3 = summary["r3"] or 0
        r2 = summary["r2"] or 0
        r1 = summary["r1"] or 0

        # Sentiment label
        if avg_rating >= 4.5:
            sentiment = "🌟 Exceptional"
        elif avg_rating >= 3.8:
            sentiment = "🟢 Very Good"
        elif avg_rating >= 3.0:
            sentiment = "🟡 Average"
        else:
            sentiment = "🔴 Needs Improvement"

        lines = [
            f"⭐ **Customer Satisfaction Report (Last {days} Days)**\n",
            f"📊 **Average Rating:** **{avg_rating}/5.0** ({sentiment}) across {total} reviews\n",
            f"📈 **Rating Breakdown:**",
            f"  • 5 ⭐: {r5} ({round(r5/total*100, 1) if total else 0}%)",
            f"  • 4 ⭐: {r4} ({round(r4/total*100, 1) if total else 0}%)",
            f"  • 3 ⭐: {r3} ({round(r3/total*100, 1) if total else 0}%)",
            f"  • 2 ⭐: {r2} ({round(r2/total*100, 1) if total else 0}%)",
            f"  • 1 ⭐: {r1} ({round(r1/total*100, 1) if total else 0}%)"
        ]

        if comments:
            lines.append("\n💬 **Recent Customer Comments:**")
            for c in comments:
                lines.append(f"  • \"{c['feedback_text']}\" — {c['customer_name']} ({c['rating']}⭐)")

        return {
            "status": "success",
            "period_days": days,
            "total_reviews": total,
            "average_rating": avg_rating,
            "sentiment": sentiment,
            "distribution": {"5_star": r5, "4_star": r4, "3_star": r3, "2_star": r2, "1_star": r1},
            "recent_comments": [{
                "customer": c["customer_name"],
                "rating": c["rating"],
                "comment": c["feedback_text"],
                "date": str(c["created_at"])[:10]
            } for c in comments],
            "message": "\n".join(lines)
        }
    finally:
        conn.close()


def generate_feedback_request_link(
    customer_name: str,
    bill_id: Optional[str] = None,
    phone: Optional[str] = None
) -> Dict[str, Any]:
    """
    Generate a 1-tap WhatsApp deep link asking the customer for a quick rating.
    """
    name = (customer_name or "Valued Customer").strip()
    bill_ref = f" (Bill #{bill_id})" if bill_id else ""
    
    text = (
        f"Namaste {name}! 🙏\n"
        f"Thank you for shopping at SuperMart{bill_ref}.\n\n"
        f"How was your shopping experience today?\n"
        f"Please reply with a rating from 1 to 5 stars (⭐⭐⭐⭐⭐) and any feedback so we can serve you even better!\n\n"
        f"Have a wonderful day!"
    )
    encoded = urllib.parse.quote(text)
    sanitized_phone = sanitize_phone_for_whatsapp(phone)

    if sanitized_phone:
        link = f"https://wa.me/{sanitized_phone}?text={encoded}"
    else:
        link = f"https://wa.me/?text={encoded}"

    return {
        "status": "success",
        "customer_name": name,
        "whatsapp_link": link,
        "message": f"📱 WhatsApp Feedback Survey Link generated for **{name}**:\n👉 [Open WhatsApp Survey]({link})"
    }
