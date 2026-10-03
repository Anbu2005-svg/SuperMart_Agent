"""
Role-Based Access Control (RBAC) Module for SuperMart AI Ops Agent.

Enforces role separation between:
  - 'owner': Full operational and financial authority (price edits, profit analytics, GST, audit logs, credit limits).
  - 'staff' (Cashier): Allowed to perform everyday counter operations (billing, stock checks, customer lookups, returns).
Restricts sensitive actions with helpful security explanations.
"""

import os
from typing import Dict, Any, List, Optional, Tuple
from db.models import get_db_connection, immediate_transaction
from skills.security import is_admin_user

# Actions strictly reserved for Owner
OWNER_ONLY_ACTIONS = {
    "profit_loss_report",
    "daily_profit_dashboard",
    "business_health_score",
    "get_audit_trail",
    "export_gstr1",
    "export_gstr1_json",
    "update_gst_slab",
    "archive_product",
    "set_credit_limit",
    "set_user_role",
    "generate_purchase_order"
}


def get_user_role(telegram_id: str) -> str:
    """
    Get effective role of Telegram user: 'owner' or 'staff'.
    """
    if not telegram_id:
        return "staff"

    tid_str = str(telegram_id).strip()

    # Check if configured in environment admin list
    admin_env = os.getenv("ADMIN_TELEGRAM_IDS", "").strip()
    if admin_env:
        admin_ids = [a.strip() for a in admin_env.split(",") if a.strip()]
        if tid_str in admin_ids:
            return "owner"

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT role FROM user_roles WHERE telegram_id = %s", (tid_str,))
        row = cur.fetchone()
        cur.close()

        if row:
            return row["role"].lower()
        
        # If no explicit role configured and no admin whitelist defined, default to 'owner'
        if not admin_env:
            return "owner"
        return "staff"
    finally:
        conn.close()


def set_user_role(
    requester_telegram_id: str,
    target_telegram_id: str,
    new_role: str
) -> Dict[str, Any]:
    """
    Assign a role ('owner' or 'staff') to a Telegram user. Only current Owners can invoke this.
    """
    if not requester_telegram_id or not target_telegram_id:
        return {"status": "error", "message": "Both requester_telegram_id and target_telegram_id are required."}

    req_role = get_user_role(requester_telegram_id)
    if req_role != "owner":
        return {
            "status": "forbidden",
            "message": "⛔ Access Denied: Only a verified Supermarket Owner can assign or modify user roles."
        }

    role_clean = new_role.strip().lower()
    if role_clean not in ("owner", "staff"):
        return {"status": "error", "message": "Role must be either 'owner' or 'staff'."}

    target_clean = str(target_telegram_id).strip()

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO user_roles (telegram_id, role, assigned_by)
                VALUES (%s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    role = EXCLUDED.role,
                    assigned_by = EXCLUDED.assigned_by,
                    created_at = CURRENT_TIMESTAMP
            """, (target_clean, role_clean, str(requester_telegram_id)))
            cur.close()

        return {
            "status": "success",
            "target_telegram_id": target_clean,
            "role": role_clean,
            "message": f"✅ Successfully set role of user `{target_clean}` to **{role_clean.upper()}**."
        }
    finally:
        conn.close()


def list_user_roles(requester_telegram_id: str) -> Dict[str, Any]:
    """
    List all staff and owner user role assignments.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT telegram_id, role, assigned_by, created_at FROM user_roles ORDER BY role ASC, created_at DESC")
        rows = cur.fetchall()
        cur.close()

        users = [{
            "telegram_id": r["telegram_id"],
            "role": r["role"].upper(),
            "assigned_by": r["assigned_by"] or "System",
            "date": str(r["created_at"])[:10] if r["created_at"] else "N/A"
        } for r in rows]

        lines = [f"👥 **Team Roles & Permissions ({len(users)} assigned):**\n"]
        for u in users:
            emoji = "👑" if u["role"] == "OWNER" else "👨‍💼"
            lines.append(f"• {emoji} User `{u['telegram_id']}`: **{u['role']}** (Assigned by {u['assigned_by']})")

        return {
            "status": "success",
            "count": len(users),
            "users": users,
            "message": "\n".join(lines) if users else "ℹ️ No custom staff roles configured yet. All authenticated users currently have default owner permissions."
        }
    finally:
        conn.close()


def is_action_allowed(telegram_id: str, action_name: str) -> Tuple[bool, Optional[str]]:
    """
    Check if a specific tool/action is permitted for the given Telegram user.
    """
    if action_name not in OWNER_ONLY_ACTIONS:
        return True, None

    role = get_user_role(telegram_id)
    if role == "owner":
        return True, None

    return False, (
        f"⛔ **Permission Denied:** Action `{action_name}` is restricted to Supermarket **Owner** accounts.\n"
        f"Your current role is **Staff / Cashier**. Please ask the store owner for authorization."
    )
