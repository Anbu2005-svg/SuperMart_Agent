import os
import time
import hmac
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, Tuple
from db.models import get_db_connection, immediate_transaction

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# In-memory tracking for failed login attempts to prevent brute-force attacks
# {identifier: {"count": int, "locked_until": float}}
_FAILED_LOGIN_ATTEMPTS: Dict[str, Dict[str, Any]] = {}
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION_SECONDS = 300  # 5 minutes


def _hash_password(password: str) -> str:
    """Hash password using PBKDF2-HMAC-SHA256 with 100,000 iterations and a cryptographically secure random salt."""
    salt = os.urandom(16).hex()
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000).hex()
    return f"pbkdf2${salt}${derived}"


def _verify_password(password: str, stored_hash: str) -> Tuple[bool, bool]:
    """
    Verify password against stored hash using constant-time comparison.
    Returns (is_valid, needs_upgrade).
    Supports backward compatibility with legacy SHA-256 hashes and indicates when upgrade is needed.
    """
    if not stored_hash or not password:
        return False, False

    if stored_hash.startswith("pbkdf2$"):
        try:
            parts = stored_hash.split("$")
            if len(parts) != 3:
                return False, False
            salt = parts[1]
            expected_derived = parts[2]
            computed_derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000).hex()
            is_valid = hmac.compare_digest(computed_derived, expected_derived)
            return is_valid, False
        except Exception:
            return False, False
    else:
        # Legacy SHA-256 with static salt
        legacy_salt = "supermarket_ops_salt_2026"
        legacy_hash = hashlib.sha256((password + legacy_salt).encode("utf-8")).hexdigest()
        is_valid = hmac.compare_digest(stored_hash, legacy_hash)
        return is_valid, True


MAX_TRACKED_IDENTIFIERS = 1000


def _prune_expired_failed_attempts(now: float) -> None:
    """Evict expired failed login tracking entries to bound memory usage."""
    expired = [k for k, v in _FAILED_LOGIN_ATTEMPTS.items() if v.get("locked_until", 0) <= now]
    for k in expired:
        _FAILED_LOGIN_ATTEMPTS.pop(k, None)


def _check_rate_limit(identifier: str) -> Optional[str]:
    """Check if identifier is currently locked out from login attempts."""
    now = time.time()
    record = _FAILED_LOGIN_ATTEMPTS.get(identifier)
    if record and record.get("locked_until", 0) > now:
        remaining = int(record["locked_until"] - now)
        return f"Too many failed login attempts. Account temporarily locked for {remaining} seconds. Please try again later."
    return None


def _record_failed_attempt(identifier: str) -> None:
    """Record a failed login attempt and apply lockout if threshold exceeded with memory bounds."""
    now = time.time()
    if len(_FAILED_LOGIN_ATTEMPTS) >= MAX_TRACKED_IDENTIFIERS:
        _prune_expired_failed_attempts(now)
        if len(_FAILED_LOGIN_ATTEMPTS) >= MAX_TRACKED_IDENTIFIERS:
            # Force prune oldest entries to prevent memory exhaustion
            excess = len(_FAILED_LOGIN_ATTEMPTS) - MAX_TRACKED_IDENTIFIERS + 100
            for k in list(_FAILED_LOGIN_ATTEMPTS.keys())[:excess]:
                _FAILED_LOGIN_ATTEMPTS.pop(k, None)

    record = _FAILED_LOGIN_ATTEMPTS.get(identifier, {"count": 0, "locked_until": 0})
    if record.get("locked_until", 0) <= now:
        record["count"] = record.get("count", 0) + 1
        if record["count"] >= MAX_LOGIN_ATTEMPTS:
            record["locked_until"] = now + LOCKOUT_DURATION_SECONDS
    _FAILED_LOGIN_ATTEMPTS[identifier] = record


def _reset_failed_attempts(identifier: str) -> None:
    """Clear failed login attempts on successful authentication."""
    _FAILED_LOGIN_ATTEMPTS.pop(identifier, None)


def register_shop(
    shop_name: str,
    password: str,
    shop_address: Optional[str] = None,
    shop_gstin: Optional[str] = None
) -> Dict[str, Any]:
    """Register a new supermarket shop in the system."""
    name = shop_name.strip()
    if not name or not password.strip():
        return {"status": "error", "message": "Shop name and password are mandatory fields!"}

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT shop_id FROM shops WHERE LOWER(shop_name) = %s", (name.lower(),))
        if cur.fetchone():
            cur.close()
            return {"status": "error", "message": f"Shop '{name}' already exists. Please choose Login instead."}

        pwd_hash = _hash_password(password.strip())
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO shops (shop_name, password_hash, shop_address, shop_gstin)
                VALUES (%s, %s, %s, %s)
                RETURNING shop_id
            """, (name, pwd_hash,
                  shop_address.strip() if shop_address else None,
                  shop_gstin.strip() if shop_gstin else None))
            shop_id = cur.fetchone()["shop_id"]
            cur.close()

        return {
            "status": "success",
            "message": f"Shop '{name}' registered successfully!",
            "shop_id": shop_id,
            "shop_name": name
        }
    finally:
        conn.close()


def login_shop(telegram_id: str, shop_name: str, password: str) -> Dict[str, Any]:
    """Authenticate Telegram user to an existing shop using credentials with rate-limit brute-force protection."""
    name = shop_name.strip()
    lockout_msg = _check_rate_limit(f"{telegram_id}:{name.lower()}")
    if lockout_msg:
        return {"status": "error", "message": lockout_msg}

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM shops WHERE LOWER(shop_name) = %s", (name.lower(),))
        shop = cur.fetchone()
        cur.close()
        if not shop:
            _record_failed_attempt(f"{telegram_id}:{name.lower()}")
            return {"status": "error", "message": f"Shop '{name}' not found. Please check spelling or sign up as a New Shop."}

        is_valid, needs_upgrade = _verify_password(password.strip(), shop["password_hash"])
        if not is_valid:
            _record_failed_attempt(f"{telegram_id}:{name.lower()}")
            return {"status": "error", "message": "Invalid password for this shop!"}

        # Clear failed attempt counter on success
        _reset_failed_attempts(f"{telegram_id}:{name.lower()}")

        # Auto-upgrade legacy SHA-256 hash to modern PBKDF2-HMAC-SHA256
        if needs_upgrade:
            new_hash = _hash_password(password.strip())
            with immediate_transaction(conn):
                cur_up = conn.cursor()
                cur_up.execute("UPDATE shops SET password_hash = %s WHERE shop_id = %s", (new_hash, shop["shop_id"]))
                cur_up.close()

        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO user_sessions (telegram_id, shop_id)
                VALUES (%s, %s)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    shop_id = EXCLUDED.shop_id,
                    authenticated_at = CURRENT_TIMESTAMP
            """, (str(telegram_id), shop["shop_id"]))
            cur.close()

        return {
            "status": "success",
            "message": f"Successfully logged into '{shop['shop_name']}'!",
            "shop_id": shop["shop_id"],
            "shop_name": shop["shop_name"]
        }
    finally:
        conn.close()


SESSION_EXPIRY_HOURS = 24


def get_latest_morning_cutoff_ist(now_dt: Optional[datetime] = None, reset_hour: int = 4, reset_minute: int = 30) -> datetime:
    """
    Returns the datetime of the latest morning reset cutoff in Indian Standard Time (IST).
    Defaults to 4:30 AM IST (between 4:00 AM and 5:00 AM IST).
    """
    if now_dt is None:
        now_dt = datetime.now(IST)
    elif now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc).astimezone(IST)
    else:
        now_dt = now_dt.astimezone(IST)

    today_cutoff = now_dt.replace(hour=reset_hour, minute=reset_minute, second=0, microsecond=0)
    if now_dt >= today_cutoff:
        return today_cutoff
    else:
        return today_cutoff - timedelta(days=1)


def logout_all_sessions() -> int:
    """
    Log out all active user sessions across all shops.
    Invoked for the daily morning reset between 4:00 AM and 5:00 AM IST.
    """
    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("DELETE FROM user_sessions")
            deleted_count = cur.rowcount
            cur.close()
        return deleted_count
    except Exception:
        return 0
    finally:
        conn.close()


def cleanup_expired_sessions() -> int:
    """
    Purge user sessions from PostgreSQL that are expired:
    1. Authenticated prior to the most recent daily morning cutoff (4:30 AM IST).
    2. Or older than 24 hours.
    """
    reset_hour = int(os.getenv("DAILY_LOGOUT_HOUR_IST", "4"))
    reset_minute = int(os.getenv("DAILY_LOGOUT_MINUTE_IST", "30"))
    cutoff = get_latest_morning_cutoff_ist(reset_hour=reset_hour, reset_minute=reset_minute)
    cutoff_utc = cutoff.astimezone(timezone.utc)

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("""
                DELETE FROM user_sessions
                WHERE authenticated_at < %s
                   OR authenticated_at < (CURRENT_TIMESTAMP - INTERVAL '24 hours')
            """, (cutoff_utc,))
            deleted_count = cur.rowcount
            cur.close()
        return deleted_count
    except Exception:
        return 0
    finally:
        conn.close()


def get_user_session(telegram_id: str) -> Optional[Dict[str, Any]]:
    """Fetch active shop session for a Telegram user. Purges and returns None if expired (> 24 hours)."""
    cleanup_expired_sessions()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.*, us.authenticated_at FROM user_sessions us
            JOIN shops s ON us.shop_id = s.shop_id
            WHERE us.telegram_id = %s
        """, (str(telegram_id),))
        shop = cur.fetchone()
        cur.close()
        if shop:
            return {
                "shop_id": shop["shop_id"],
                "shop_name": shop["shop_name"],
                "shop_address": shop["shop_address"],
                "shop_gstin": shop["shop_gstin"],
                "authenticated_at": shop["authenticated_at"]
            }
        return None
    finally:
        conn.close()


def is_user_authenticated(telegram_id: str) -> bool:
    """Check if a Telegram user has an active shop session."""
    return get_user_session(telegram_id) is not None


def logout_user_session(telegram_id: str) -> bool:
    """End active shop session for Telegram user."""
    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("DELETE FROM user_sessions WHERE telegram_id = %s", (str(telegram_id),))
            cur.close()
        return True
    finally:
        conn.close()
