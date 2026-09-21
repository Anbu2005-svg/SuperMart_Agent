import uuid
from datetime import datetime
from skills.auth import (
    register_shop,
    login_shop,
    get_user_session,
    is_user_authenticated,
    logout_user_session,
    logout_all_sessions,
    get_latest_morning_cutoff_ist,
    IST
)


def test_multi_shop_auth_lifecycle():
    run_id = uuid.uuid4().hex[:8]
    user_id = f"test_telegram_owner_{run_id}"
    shop_name = f"SuperMart Test {run_id}"
    password = "SecretPassword123"

    logout_user_session(user_id)
    assert is_user_authenticated(user_id) is False

    reg_res = register_shop(shop_name=shop_name, password=password, shop_address="456 Main St", shop_gstin="33AABCU9603R1ZM")
    assert reg_res["status"] == "success"

    login_res = login_shop(telegram_id=user_id, shop_name=shop_name, password=password)
    assert login_res["status"] == "success"
    assert is_user_authenticated(user_id) is True

    session = get_user_session(user_id)
    assert session["shop_name"] == shop_name

    logout_user_session(user_id)
    assert is_user_authenticated(user_id) is False


def test_logout_and_chat_clear_preserves_database_inventory():
    run_id = uuid.uuid4().hex[:8]
    user_id = f"test_user_persistent_{run_id}"
    shop_name = f"Persistent Store {run_id}"
    password = "Pass123Password"

    register_shop(shop_name=shop_name, password=password)
    login_shop(telegram_id=user_id, shop_name=shop_name, password=password)

    from skills.inventory import list_all_products
    prods_before = list_all_products()
    assert prods_before["status"] == "success"
    initial_count = prods_before["count"]
    assert initial_count > 0

    from agent.control_loop import clear_conversation
    clear_conversation(12345678)

    logout_user_session(user_id)

    prods_after = list_all_products()
    assert prods_after["status"] == "success"
    assert prods_after["count"] == initial_count


def test_24h_session_expiration():
    user_id = f"test_user_expiry_{uuid.uuid4().hex[:8]}"
    shop_name = f"Expiry Test Shop {uuid.uuid4().hex[:8]}"
    password = "Password777"

    register_shop(shop_name=shop_name, password=password)
    login_shop(telegram_id=user_id, shop_name=shop_name, password=password)

    assert is_user_authenticated(user_id) is True

    from db.models import get_db_connection, immediate_transaction
    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute(
                "UPDATE user_sessions SET authenticated_at = (CURRENT_TIMESTAMP - INTERVAL '25 hours') WHERE telegram_id = %s",
                (str(user_id),)
            )
            cur.close()
    finally:
        conn.close()

    session = get_user_session(user_id)
    assert session is None
    assert is_user_authenticated(user_id) is False


def test_daily_morning_logout_cutoff_calculation():
    after_cutoff = datetime(2026, 9, 12, 10, 0, tzinfo=IST)
    cutoff = get_latest_morning_cutoff_ist(after_cutoff, reset_hour=4, reset_minute=30)
    assert cutoff == datetime(2026, 9, 12, 4, 30, tzinfo=IST)

    before_cutoff = datetime(2026, 9, 12, 2, 15, tzinfo=IST)
    cutoff_prev = get_latest_morning_cutoff_ist(before_cutoff, reset_hour=4, reset_minute=30)
    assert cutoff_prev == datetime(2026, 9, 11, 4, 30, tzinfo=IST)


def test_logout_all_sessions_purges_all_users():
    run_id = uuid.uuid4().hex[:6]
    u1 = f"user_all_1_{run_id}"
    u2 = f"user_all_2_{run_id}"
    s1 = f"Shop All 1 {run_id}"
    s2 = f"Shop All 2 {run_id}"

    register_shop(s1, "Pass12345!")
    register_shop(s2, "Pass12345!")

    login_shop(u1, s1, "Pass12345!")
    login_shop(u2, s2, "Pass12345!")

    assert is_user_authenticated(u1) is True
    assert is_user_authenticated(u2) is True

    deleted = logout_all_sessions()
    assert deleted >= 2

    assert is_user_authenticated(u1) is False
    assert is_user_authenticated(u2) is False


def test_wrong_password_rejected():
    run_id = uuid.uuid4().hex[:6]
    shop_name = f"Security Shop {run_id}"
    user_id = f"sec_user_{run_id}"

    register_shop(shop_name=shop_name, password="CorrectPassword1")
    res = login_shop(telegram_id=user_id, shop_name=shop_name, password="WrongPassword")
    assert res["status"] == "error"
    assert is_user_authenticated(user_id) is False
