import pytest
import uuid
from skills.billing import start_bill, add_item_to_bill, finalize_bill, preview_bill, quick_create_bill
from skills.inventory import get_stock


def test_oversell_guard_refusal_on_draft():
    stock_info = get_stock("SKU-SALT-01")
    curr_qty = stock_info["product"]["quantity"]

    bill_res = start_bill()
    bill_id = bill_res["bill_id"]

    # Attempting to add more items than available (seed stock = 50 salt)
    add_res = add_item_to_bill(bill_id, "SKU-SALT-01", curr_qty + 50)
    assert add_res["status"] == "oversell_warning"
    assert "exceeds available stock" in add_res["message"] or "Only" in add_res["message"]
    assert add_res["available_stock"] == curr_qty


def test_oversell_guard_refusal_at_finalize_layer():
    from db.models import get_db_connection
    from skills.billing import add_item_to_bill

    # Seed plenty of stock, draft a bill, then secretly shrink stock below the draft qty
    bill_res = start_bill()
    bill_id = bill_res["bill_id"]
    add_item_to_bill(bill_id, "SKU-MAGGI-70", 100)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE products SET quantity = 5 WHERE sku_id = 'SKU-MAGGI-70'")
        conn.commit()
        cur.close()
    finally:
        conn.close()

    fin = finalize_bill(bill_id, payment_mode="cash")
    assert fin["status"] == "error"
    assert fin.get("error_type") == "OversellGuardError"
    assert "Oversell Guard Triggered" in fin["message"]


def test_stock_decrements_on_finalization():
    stock_info = get_stock("SKU-SALT-01")
    initial_qty = stock_info["product"]["quantity"]

    bill_res = start_bill()
    bill_id = bill_res["bill_id"]

    add_item_to_bill(bill_id, "SKU-SALT-01", 5)
    fin_res = finalize_bill(bill_id, payment_mode="cash")
    assert fin_res["bill_status"] == "finalized"

    stock_after = get_stock("SKU-SALT-01")
    assert stock_after["product"]["quantity"] == initial_qty - 5


def test_refinalize_is_noop_no_double_decrement():
    """Re-finalizing the same bill must not double-decrement (Telegram retry safety)."""
    stock_info = get_stock("SKU-MILK-1L")
    initial_qty = stock_info["product"]["quantity"]

    bill_id = start_bill()["bill_id"]
    add_item_to_bill(bill_id, "SKU-MILK-1L", 2)

    res1 = finalize_bill(bill_id, payment_mode="upi")
    assert res1["bill_status"] == "finalized"
    assert get_stock("SKU-MILK-1L")["product"]["quantity"] == initial_qty - 2

    # Retried finalize on the SAME bill — no second decrement
    res2 = finalize_bill(bill_id, payment_mode="upi")
    assert res2["bill_status"] == "finalized"
    assert get_stock("SKU-MILK-1L")["product"]["quantity"] == initial_qty - 2


def test_control_loop_idempotency_claims_update_once():
    """The control loop's claim step must be atomic — second claim returns cached reply."""
    from agent.control_loop import _check_and_claim_update, _store_idempotency_reply

    key = f"TEST_IDEM_{uuid.uuid4().hex}"
    first = _check_and_claim_update(key)
    assert first is None  # claimed — processing should proceed

    # Simulate a completed turn's cached reply
    _store_idempotency_reply(key, "cached answer", ["/tmp/x.pdf"])

    # Redelivery of the same update_id returns the cached reply
    second = _check_and_claim_update(key)
    assert second is not None
    assert second[0] == "cached answer"
    assert second[1] == ["/tmp/x.pdf"]


def test_quick_create_bill_finalizes_atomically():
    stock_before = get_stock("SKU-MAGGI-70")["product"]["quantity"]

    res = quick_create_bill(items=[{"name": "Maggi", "qty": 3}, {"name": "Parle-G", "qty": 2}], payment_mode="upi")
    assert res["bill_status"] == "finalized"
    assert res["summary"]["grand_total"] > 0

    stock_after = get_stock("SKU-MAGGI-70")["product"]["quantity"]
    assert stock_after == stock_before - 3
