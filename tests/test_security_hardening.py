import os
import math
import uuid
from skills.auth import (
    _hash_password,
    _verify_password,
    _check_rate_limit,
    _record_failed_attempt,
    _reset_failed_attempts,
)
from skills.billing import add_item_to_bill, edit_item_qty, start_bill
from skills.credit import charge_khata, record_payment
from skills.inventory import add_product, receive_stock
from skills.analytics import daily_summary
from agent.harness import validate_gstin


def test_pbkdf2_hashing_and_verification():
    pwd = "SecureSupermarketPassword2026!"
    h = _hash_password(pwd)
    assert h.startswith("pbkdf2$")
    assert len(h.split("$")) == 3

    valid, needs_upgrade = _verify_password(pwd, h)
    assert valid is True
    assert needs_upgrade is False

    valid_wrong, _ = _verify_password("WrongPassword", h)
    assert valid_wrong is False


def test_legacy_hash_backward_compatibility_and_upgrade():
    import hashlib
    pwd = "LegacyShopPassword"
    legacy_salt = "supermarket_ops_salt_2026"
    legacy_hash = hashlib.sha256((pwd + legacy_salt).encode("utf-8")).hexdigest()

    valid, needs_upgrade = _verify_password(pwd, legacy_hash)
    assert valid is True
    assert needs_upgrade is True

    valid_wrong, _ = _verify_password("WrongPwd", legacy_hash)
    assert valid_wrong is False


def test_brute_force_login_lockout():
    identifier = f"test_bf_{uuid.uuid4().hex[:6]}"
    _reset_failed_attempts(identifier)

    for _ in range(4):
        _record_failed_attempt(identifier)
        assert _check_rate_limit(identifier) is None

    _record_failed_attempt(identifier)
    lockout_msg = _check_rate_limit(identifier)
    assert lockout_msg is not None
    assert "Too many failed login attempts" in lockout_msg

    _reset_failed_attempts(identifier)
    assert _check_rate_limit(identifier) is None


def test_billing_numeric_injection_and_nan_guards():
    assert add_item_to_bill("BILL-FAKE", "SKU-SALT-01", -5.0)["status"] == "error"
    assert add_item_to_bill("BILL-FAKE", "SKU-SALT-01", 0.0)["status"] == "error"
    assert add_item_to_bill("BILL-FAKE", "SKU-SALT-01", float("nan"))["status"] == "error"
    assert add_item_to_bill("BILL-FAKE", "SKU-SALT-01", float("inf"))["status"] == "error"
    assert edit_item_qty("BILL-FAKE", "SKU-SALT-01", float("nan"))["status"] == "error"


def test_credit_numeric_injection_and_nan_guards():
    assert charge_khata("Test Cust", float("nan"))["status"] == "error"
    assert charge_khata("Test Cust", float("inf"))["status"] == "error"
    assert charge_khata("Test Cust", -100.0)["status"] == "error"
    assert charge_khata("Test Cust", 0.0)["status"] == "error"

    assert record_payment("Test Cust", float("nan"))["status"] == "error"
    assert record_payment("Test Cust", float("inf"))["status"] == "error"
    assert record_payment("Test Cust", -50.0)["status"] == "error"
    assert record_payment("Test Cust", 0.0)["status"] == "error"


def test_inventory_numeric_injection_and_nan_guards():
    res = add_product("SecItem", "General", "piece", False, -10.0, 50.0, 5.0, "1234")
    assert res["status"] == "error"

    res_nan = add_product("SecItem", "General", "piece", False, float("nan"), 50.0, 5.0, "1234")
    assert res_nan["status"] == "error"

    res_zero_mrp = add_product("SecItem", "General", "piece", False, 10.0, 0.0, 5.0, "1234")
    assert res_zero_mrp["status"] == "error"

    assert receive_stock("SKU-SALT-01", float("nan"))["status"] == "error"
    assert receive_stock("SKU-SALT-01", -10.0)["status"] == "error"


def test_analytics_invalid_date_validation():
    res = daily_summary("invalid-date-format")
    assert res["status"] == "error"
    assert "Invalid date format" in res["message"]

    res2 = daily_summary("2026/13/45")
    assert res2["status"] == "error"


def test_gstin_validation():
    assert validate_gstin("33AABCU9603R1ZM") is True
    assert validate_gstin("33AABCU9603R1ZM".lower()) is False  # must be uppercase pattern per regex
    assert validate_gstin("33AABCU9603R1Z") is False   # 14 chars
    assert validate_gstin("XXAABCU9603R1ZM") is False   # bad state code chars
    assert validate_gstin("") is False
    assert validate_gstin(None) is False


def test_path_traversal_defense():
    from bot import is_safe_generated_file
    os.makedirs("generated_docs", exist_ok=True)
    test_doc = os.path.join("generated_docs", "test_safe_invoice.pdf")
    with open(test_doc, "w") as f:
        f.write("safe")

    try:
        assert is_safe_generated_file(test_doc) is True
        assert is_safe_generated_file("../.env") is False
        assert is_safe_generated_file("../../boot.ini") is False
        assert is_safe_generated_file("generated_docs/../../.env") is False
        assert is_safe_generated_file("") is False
        assert is_safe_generated_file(None) is False
    finally:
        if os.path.exists(test_doc):
            os.remove(test_doc)


def test_message_rate_limiter(monkeypatch):
    import time
    fixed_time = 1770000000.0
    monkeypatch.setattr(time, "time", lambda: fixed_time)

    from bot import is_rate_limited, _RATE_LIMIT_MAX, _USER_RATE_BUCKETS
    user = f"rate_user_{uuid.uuid4().hex[:8]}"
    _USER_RATE_BUCKETS.pop(user, None)

    # First N messages pass
    for _ in range(_RATE_LIMIT_MAX):
        assert is_rate_limited(user) is False

    # The next one is throttled
    assert is_rate_limited(user) is True
    _USER_RATE_BUCKETS.pop(user, None)


def test_audit_log_sensitive_data_redaction():
    from skills.audit import _sanitize_audit_details
    payload = {
        "user": "admin",
        "password": "supersecretpassword123",
        "nested": {
            "api_key": "sk-1234567890",
            "pin_code": "9999",
            "safe_field": "public_data"
        },
        "list_items": [
            {"auth_token": "bearer xyz"},
            {"item_name": "Milk 1L"}
        ]
    }
    sanitized = _sanitize_audit_details(payload)
    assert sanitized["password"] == "***REDACTED***"
    assert sanitized["nested"]["api_key"] == "***REDACTED***"
    assert sanitized["nested"]["pin_code"] == "***REDACTED***"
    assert sanitized["nested"]["safe_field"] == "public_data"
    assert sanitized["list_items"][0]["auth_token"] == "***REDACTED***"
    assert sanitized["list_items"][1]["item_name"] == "Milk 1L"


def test_csv_formula_injection_defense():
    from skills.gst_export import _sanitize_csv_cell
    # Malicious spreadsheet formula payloads
    assert _sanitize_csv_cell("=cmd|'/C calc'!A0") == "'=cmd|'/C calc'!A0"
    assert _sanitize_csv_cell("+2+3+cmd|...") == "'+2+3+cmd|..."
    assert _sanitize_csv_cell("-100") == "'-100"
    assert _sanitize_csv_cell("@SUM(A1:A10)") == "'@SUM(A1:A10)"
    assert _sanitize_csv_cell("\tTAB_INJECT") == "'\tTAB_INJECT"
    assert _sanitize_csv_cell("\rCR_INJECT") == "'\rCR_INJECT"
    # Normal cells should not be altered
    assert _sanitize_csv_cell("Amul Butter 100g") == "Amul Butter 100g"
    assert _sanitize_csv_cell("12345") == "12345"
    assert _sanitize_csv_cell(50.0) == 50.0


def test_auth_rate_limiter_memory_bounds():
    import time
    from skills.auth import (
        _FAILED_LOGIN_ATTEMPTS,
        _record_failed_attempt,
        MAX_TRACKED_IDENTIFIERS
    )
    _FAILED_LOGIN_ATTEMPTS.clear()

    # Prepopulate with expired entries
    now = time.time()
    for i in range(MAX_TRACKED_IDENTIFIERS):
        _FAILED_LOGIN_ATTEMPTS[f"user_{i}"] = {"count": 1, "locked_until": now - 10}

    assert len(_FAILED_LOGIN_ATTEMPTS) == MAX_TRACKED_IDENTIFIERS

    # Next record should trigger pruning and keep size below or at limit
    _record_failed_attempt("new_attacker")
    assert len(_FAILED_LOGIN_ATTEMPTS) <= MAX_TRACKED_IDENTIFIERS
    _FAILED_LOGIN_ATTEMPTS.clear()


def test_analytics_and_notification_nan_guards():
    from skills.notifications import check_expiring_stock
    from skills.analytics import sales_forecast, profit_loss_report, customer_insights
    from skills.gst_export import export_gstr1

    # NaN / invalid types should return structured error responses, not crash
    assert check_expiring_stock(days_ahead=float("nan"))["status"] == "error"
    assert check_expiring_stock(days_ahead="not_an_int")["status"] == "error"

    assert sales_forecast(days_history=float("nan"))["status"] == "error"
    assert sales_forecast(days_history="bad")["status"] == "error"

    assert profit_loss_report(days=float("nan"))["status"] == "error"
    assert profit_loss_report(days="invalid")["status"] == "error"

    assert customer_insights(days=float("nan"))["status"] == "error"
    assert customer_insights(top_n="bad")["status"] == "error"

    assert export_gstr1(month=float("nan"))["status"] == "error"
    assert export_gstr1(month="invalid")["status"] == "error"


def test_update_gst_slab_whitespace_defense():
    from skills.inventory import update_gst_slab
    res = update_gst_slab(18, sku_or_name="   ", category="   ", hsn_code="   ")
    assert res["status"] == "error"
    assert "Specify at least one target" in res["message"]

