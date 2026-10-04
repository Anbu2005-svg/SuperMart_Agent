import pytest
from skills.billing import start_bill, add_item_to_bill, finalize_bill, void_bill, search_bills, get_bill_details
from skills.inventory import get_stock
from skills.credit import get_khata_balance, set_credit_limit


def test_search_and_get_bill_details():
    # 1. Create and finalize a bill
    res_b = start_bill("Search Test Customer")
    bill_id = res_b["bill_id"]
    add_item_to_bill(bill_id, "Atta", 2.0)
    finalize_bill(bill_id, payment_mode="cash")

    # 2. Search by query (bill_id)
    search_res = search_bills(query=bill_id)
    assert search_res["status"] == "success"
    assert search_res["matched_bills_count"] >= 1
    found = any(b["bill_id"] == bill_id for b in search_res["bills"])
    assert found

    # 3. Search by payment_mode
    search_cash = search_bills(payment_mode="cash", limit=5)
    assert search_cash["status"] == "success"
    assert all(b["payment_mode"] == "cash" for b in search_cash["bills"])

    # 4. Get bill details
    details = get_bill_details(bill_id)
    assert details["status"] == "success"
    assert details["bill_id"] == bill_id
    assert details["financials"]["payment_mode"] == "cash"
    assert len(details["items"]) >= 1
    assert "gst_slab_breakup" in details


def test_void_draft_bill():
    # Start a draft bill and void it
    res = start_bill("Draft Void Customer")
    bill_id = res["bill_id"]
    add_item_to_bill(bill_id, "Atta", 1.0)

    # Void draft bill
    void_res = void_bill(bill_id, reason="Customer cancelled before checkout")
    assert void_res["status"] == "success"
    assert void_res["new_status"] == "voided"

    # Voiding again should return error
    second_void = void_bill(bill_id)
    assert second_void["status"] == "error"
    assert "already been voided" in second_void["message"]


def test_void_finalized_bill_restores_inventory_and_khata():
    cust_name = "Ravi Kumar"
    set_credit_limit(cust_name, 10000.0)

    # Check stock before
    stock_before = get_stock("Atta")["product"]["quantity"]

    # Check khata balance before
    khata_before = get_khata_balance(cust_name)["khata_balance"]

    # 1. Create and finalize a khata bill for 2 units of Atta
    res_b = start_bill(cust_name)
    bill_id = res_b["bill_id"]
    add_item_to_bill(bill_id, "Atta", 2.0)
    final_res = finalize_bill(bill_id, payment_mode="khata")
    assert final_res["status"] == "success"

    # Stock should be decremented by 2
    stock_after_finalize = get_stock("Atta")["product"]["quantity"]
    assert stock_after_finalize == stock_before - 2.0

    # Khata balance should have increased by bill total
    details = get_bill_details(bill_id)
    bill_total = details["financials"]["total"]
    khata_after_finalize = get_khata_balance(cust_name)["khata_balance"]
    assert round(khata_after_finalize, 2) == round(khata_before + bill_total, 2)

    # 2. Void the finalized bill
    void_res = void_bill(bill_id, reason="Wrong item picked by cashier")
    assert void_res["status"] == "success"
    assert void_res["khata_reversal_amount"] == bill_total
    assert len(void_res["items_restored"]) >= 1

    # 3. Check that stock is fully restored
    stock_after_void = get_stock("Atta")["product"]["quantity"]
    assert stock_after_void == stock_before

    # 4. Check that khata balance is fully reversed
    khata_after_void = get_khata_balance(cust_name)["khata_balance"]
    assert round(khata_after_void, 2) == round(khata_before, 2)
