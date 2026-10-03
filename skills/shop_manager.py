"""
Multi-Shop / Multi-Branch Management Skill.

Enables owners to operate multiple supermarkets or branch locations from a single Telegram interface:
  - List all registered shops and branches
  - Switch active operating shop context seamlessly (/shop <name>)
  - View branch performance summary
  - Register new branches
"""

from typing import Dict, Any, List, Optional
from db.models import get_db_connection, immediate_transaction
from skills.auth import _hash_password, _verify_password
from skills.security import sanitize_input


def list_shops() -> Dict[str, Any]:
    """
    List all registered shops / supermarket branches in the system.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.shop_id, s.shop_name, s.shop_address, s.shop_gstin, s.created_at,
                   COUNT(DISTINCT u.telegram_id) as active_sessions
            FROM shops s
            LEFT JOIN user_sessions u ON s.shop_id = u.shop_id
            GROUP BY s.shop_id, s.shop_name, s.shop_address, s.shop_gstin, s.created_at
            ORDER BY s.shop_name ASC
        """)
        rows = cur.fetchall()
        cur.close()

        shops = []
        for r in rows:
            shops.append({
                "shop_id": r["shop_id"],
                "shop_name": r["shop_name"],
                "address": r["shop_address"] or "Not Set",
                "gstin": r["shop_gstin"] or "Unregistered",
                "active_operators": r["active_sessions"],
                "created_at": str(r["created_at"])[:10] if r["created_at"] else "N/A"
            })

        lines = [f"🏬 **Registered Supermarket Branches ({len(shops)}):**\n"]
        for s in shops:
            lines.append(
                f"• **{s['shop_name']}** (ID: {s['shop_id']})\n"
                f"  📍 Address: {s['address']} | GSTIN: {s['gstin']}\n"
                f"  👥 Active Operators: {s['active_operators']}"
            )

        return {
            "status": "success",
            "count": len(shops),
            "shops": shops,
            "message": "\n\n".join(lines) if shops else "No shops registered yet."
        }
    finally:
        conn.close()


def get_active_shop(telegram_id: str) -> Dict[str, Any]:
    """
    Get the currently active shop context for a given Telegram user ID.
    """
    if not telegram_id:
        return {"status": "error", "message": "telegram_id is required."}

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.shop_id, s.shop_name, s.shop_address, s.shop_gstin, u.authenticated_at
            FROM user_sessions u
            JOIN shops s ON u.shop_id = s.shop_id
            WHERE u.telegram_id = %s
        """, (str(telegram_id),))
        row = cur.fetchone()
        cur.close()

        if not row:
            return {
                "status": "not_authenticated",
                "message": "No active shop session found. Please log in using /login <ShopName> <Password>."
            }

        return {
            "status": "success",
            "shop_id": row["shop_id"],
            "shop_name": row["shop_name"],
            "address": row["shop_address"] or "Not Set",
            "gstin": row["shop_gstin"] or "Unregistered",
            "authenticated_at": str(row["authenticated_at"]),
            "message": f"🏬 Active Shop: **{row['shop_name']}** (ID: {row['shop_id']})"
        }
    finally:
        conn.close()


def switch_active_shop(telegram_id: str, target_shop_name: str) -> Dict[str, Any]:
    """
    Switch user's current session to a different shop / branch.
    """
    if not telegram_id or not target_shop_name:
        return {"status": "error", "message": "Both telegram_id and target_shop_name are required."}

    clean_name = sanitize_input(target_shop_name.strip(), max_length=100)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM shops WHERE LOWER(shop_name) = %s", (clean_name.lower(),))
        shop = cur.fetchone()
        if not shop:
            return {"status": "error", "message": f"Shop '{clean_name}' not found. Use list_shops to see available branches."}

        with immediate_transaction(conn):
            cur.execute("""
                INSERT INTO user_sessions (telegram_id, shop_id, authenticated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    shop_id = EXCLUDED.shop_id,
                    authenticated_at = CURRENT_TIMESTAMP
            """, (str(telegram_id), shop["shop_id"]))
            cur.close()

        return {
            "status": "success",
            "shop_id": shop["shop_id"],
            "shop_name": shop["shop_name"],
            "message": f"✅ Successfully switched active shop context to **{shop['shop_name']}**! All subsequent commands will operate on this branch."
        }
    finally:
        conn.close()


def create_branch_shop(
    telegram_id: str,
    shop_name: str,
    password: str,
    address: Optional[str] = None,
    gstin: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a new supermarket branch and automatically switch session to it.
    """
    clean_name = sanitize_input(shop_name.strip(), max_length=100)
    if not clean_name:
        return {"status": "error", "message": "Shop name cannot be empty."}
    if not password or len(password.strip()) < 4:
        return {"status": "error", "message": "Password must be at least 4 characters long."}

    password_hash = _hash_password(password.strip())

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO shops (shop_name, password_hash, shop_address, shop_gstin)
                VALUES (%s, %s, %s, %s)
                RETURNING shop_id
            """, (clean_name, password_hash, address, gstin))
            new_id = cur.fetchone()["shop_id"]

            cur.execute("""
                INSERT INTO user_sessions (telegram_id, shop_id, authenticated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    shop_id = EXCLUDED.shop_id,
                    authenticated_at = CURRENT_TIMESTAMP
            """, (str(telegram_id), new_id))
            cur.close()

        return {
            "status": "success",
            "shop_id": new_id,
            "shop_name": clean_name,
            "message": f"🎉 New branch **{clean_name}** successfully registered! Switched active context to it."
        }
    except Exception as e:
        if "unique" in str(e).lower():
            return {"status": "error", "message": f"Shop name '{clean_name}' already exists."}
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()
