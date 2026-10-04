"""
Scheduled Auto-Notifications & Operations Heartbeat Skill for SuperMart AI Ops Agent.

Manages scheduled notification rules for:
  - 'low_stock': Daily inventory low-stock alerts
  - 'expiry_warning': Items expiring within next 7 days
  - 'eod_summary': Daily sales, cash drawer closing & revenue summary
  - 'pending_payables': Supplier outstanding bills due for payment
"""

from typing import Dict, Any, List, Optional
from datetime import datetime
from db.models import get_db_connection, immediate_transaction
from skills.audit import _log_event
from skills.reorder_alerts import check_low_stock_reorder_alerts
from skills.notifications import check_expiring_stock, generate_daily_closeout_message
from skills.supplier_ledger import get_pending_payables


def create_scheduled_alert(
    alert_type: str,
    recipient_id: str,
    channel: str = "telegram",
    schedule_time: str = "09:00"
) -> Dict[str, Any]:
    """
    Configure a scheduled automated notification.
    - alert_type: 'low_stock', 'expiry_warning', 'eod_summary', or 'pending_payables'
    - recipient_id: Telegram Chat ID or phone number
    - channel: 'telegram' or 'whatsapp'
    - schedule_time: Time of day in 24-hr format HH:MM (e.g. '09:00', '21:30')
    """
    valid_types = {"low_stock", "expiry_warning", "eod_summary", "pending_payables"}
    atype = alert_type.strip().lower()
    if atype not in valid_types:
        return {"status": "error", "message": f"Invalid alert_type. Must be one of: {', '.join(valid_types)}"}

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO scheduled_alerts (alert_type, recipient_id, channel, schedule_time, is_active)
                VALUES (%s, %s, %s, %s, TRUE)
                RETURNING alert_id, created_at
            """, (atype, recipient_id.strip(), channel.strip().lower(), schedule_time.strip()))
            row = cur.fetchone()
            alert_id = row["alert_id"]

            _log_event(conn, "SCHEDULED_ALERT_CREATED", "alert", str(alert_id),
                       details={"type": atype, "recipient": recipient_id, "time": schedule_time})
            cur.close()

        return {
            "status": "success",
            "message": f"Scheduled {atype} alert created successfully (ID: #{alert_id}) for {schedule_time} daily.",
            "alert_id": alert_id,
            "alert_type": atype,
            "recipient_id": recipient_id,
            "schedule_time": schedule_time
        }
    finally:
        conn.close()


def list_scheduled_alerts() -> Dict[str, Any]:
    """List all configured scheduled notification rules."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT alert_id, alert_type, recipient_id, channel, schedule_time,
                   is_active, last_sent_at, created_at
            FROM scheduled_alerts
            ORDER BY schedule_time ASC, alert_id ASC
        """)
        rows = cur.fetchall()
        cur.close()

        alerts = []
        for r in rows:
            alerts.append({
                "alert_id": r["alert_id"],
                "alert_type": r["alert_type"],
                "recipient_id": r["recipient_id"],
                "channel": r["channel"],
                "schedule_time": r["schedule_time"],
                "is_active": r["is_active"],
                "last_sent_at": str(r["last_sent_at"]) if r["last_sent_at"] else "Never",
                "created_at": str(r["created_at"])[:16]
            })

        return {
            "status": "success",
            "count": len(alerts),
            "alerts": alerts
        }
    finally:
        conn.close()


def toggle_scheduled_alert(alert_id: int, is_active: bool) -> Dict[str, Any]:
    """Enable or disable a scheduled alert rule."""
    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("""
                UPDATE scheduled_alerts
                SET is_active = %s
                WHERE alert_id = %s
                RETURNING alert_type
            """, (is_active, alert_id))
            row = cur.fetchone()
            if not row:
                cur.close()
                return {"status": "error", "message": f"Alert rule #{alert_id} not found."}
            cur.close()

        state = "activated" if is_active else "paused"
        return {
            "status": "success",
            "message": f"Scheduled alert #{alert_id} ({row['alert_type']}) has been {state}.",
            "alert_id": alert_id,
            "is_active": is_active
        }
    finally:
        conn.close()


def trigger_due_scheduled_alerts(alert_type: Optional[str] = None) -> Dict[str, Any]:
    """
    Execute scheduled notifications and generate their message payloads.
    Can be run by background cron jobs or invoked directly on demand.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        query = "SELECT * FROM scheduled_alerts WHERE is_active = TRUE"
        params: List[Any] = []
        if alert_type:
            query += " AND alert_type = %s"
            params.append(alert_type.strip().lower())

        cur.execute(query, params)
        rules = cur.fetchall()
        cur.close()

        dispatched = []
        with immediate_transaction(conn):
            upd_cur = conn.cursor()
            for rule in rules:
                atype = rule["alert_type"]
                content = None

                if atype == "low_stock":
                    res = check_low_stock_reorder_alerts(notify_recipient=rule["recipient_id"])
                    if res.get("count", 0) > 0:
                        content = res["message"]
                elif atype == "expiry_warning":
                    res = check_expiring_stock(days_ahead=7)
                    if res.get("count", 0) > 0:
                        content = res["message"]
                elif atype == "eod_summary":
                    res = generate_daily_closeout_message()
                    content = res.get("message")
                elif atype == "pending_payables":
                    res = get_pending_payables()
                    if res.get("count", 0) > 0:
                        lines = [f"⚠️ *Pending Supplier Payables Due ({res['count']} invoices)*\n"]
                        for b in res.get("pending_bills", [])[:5]:
                            lines.append(f"• {b['supplier_name']}: Bill #{b['vendor_bill_no']} – Due ₹{b['balance_due']:.2f}")
                        content = "\n".join(lines)

                if content:
                    upd_cur.execute("""
                        UPDATE scheduled_alerts
                        SET last_sent_at = CURRENT_TIMESTAMP
                        WHERE alert_id = %s
                    """, (rule["alert_id"],))

                    dispatched.append({
                        "alert_id": rule["alert_id"],
                        "alert_type": atype,
                        "recipient_id": rule["recipient_id"],
                        "channel": rule["channel"],
                        "content_preview": content[:120] + "..." if len(content) > 120 else content
                    })
            upd_cur.close()

        return {
            "status": "success",
            "triggered_count": len(dispatched),
            "dispatched_alerts": dispatched
        }
    finally:
        conn.close()
