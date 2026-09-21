import uuid
from skills.billing import start_bill, add_item_to_bill, edit_item_qty, remove_item_from_bill, preview_bill, finalize_bill
from skills.credit import charge_khata, record_payment, set_credit_limit


def test_edit_and_remove_item_from_bill():
    bill_res = start_bill("Anita Roy")
    bill_id = bill_res["bill_id"]

    add_item_to_bill(bill_id, "Sugar", 5)
    add_item_to_bill(bill_id, "Salt", 2)

    edit_res = edit_item_qty(bill_id, "Sugar", new_qty=10)
    assert edit_res["status"] == "success"

    prev1 = preview_bill(bill_id)
    # Sugar is ₹48/kg loose (0% GST) and Salt ₹28 (0% GST)
    assert prev1["summary"]["subtotal"] == (10 * 48.0) + (2 * 28.0)

    rem_res = remove_item_from_bill(bill_id, "Salt")
    assert rem_res["status"] == "success"

    prev2 = preview_bill(bill_id)
    assert len(prev2["items"]) == 1
    assert "Refined White Sugar" in prev2["items"][0]["name"]


def test_finalize_bill_invalid_payment_mode():
    bill_res = start_bill("Walk-in")
    bill_id = bill_res["bill_id"]
    add_item_to_bill(bill_id, "Sugar", 1)

    fin = finalize_bill(bill_id, payment_mode="bitcoin")
    assert fin["status"] == "error"
    assert "Invalid payment mode" in fin["message"]


def test_finalize_nonexistent_bill():
    fin = finalize_bill("BILL-INVALID-999", payment_mode="cash")
    assert fin["status"] == "error"
    assert "not found" in fin["message"]


def test_finalize_empty_bill_refused():
    bill_id = start_bill()["bill_id"]
    fin = finalize_bill(bill_id, payment_mode="cash")
    assert fin["status"] == "error"
    assert "empty bill" in fin["message"].lower()


def test_sequential_invoice_numbers_assigned():
    b1 = start_bill()["bill_id"]
    add_item_to_bill(b1, "Maggi", 1)
    f1 = finalize_bill(b1, payment_mode="cash")

    b2 = start_bill()["bill_id"]
    add_item_to_bill(b2, "Maggi", 1)
    f2 = finalize_bill(b2, payment_mode="cash")

    n1, n2 = f1["invoice_number"], f2["invoice_number"]
    assert n1 is not None and n2 is not None
    assert n2 == n1 + 1


def test_below_cost_sale_refused():
    from db.models import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Force MRP below cost for a fresh product to trigger the guard
        cur.execute("UPDATE products SET mrp = 5.0, cost_price = 10.0 WHERE sku_id = 'SKU-PARLEG-80'")
        conn.commit()
        cur.close()
    finally:
        conn.close()

    bill_id = start_bill()["bill_id"]
    res = add_item_to_bill(bill_id, "Parle-G", 1)
    assert res["status"] == "error"
    assert "below cost price" in res["message"]


def test_khata_payment_requires_customer():
    bill_id = start_bill()["bill_id"]  # no customer
    add_item_to_bill(bill_id, "Maggi", 1)
    res = finalize_bill(bill_id, payment_mode="khata")
    assert res["status"] == "error"
    assert res.get("error_type") == "KhataCustomerRequired"


def test_credit_limit_enforced_on_khata_bill():
    # Priya Sharma seed: balance 250, limit 500 → a ₹600 bill must be refused
    set_credit_limit("Priya Sharma", 500.0)
    bill_id = start_bill("Priya Sharma")["bill_id"]
    # 5 × Basmati rice @80 = 400 taxable → still under 650 projected? 250+400=650 > 500 → refused
    add_item_to_bill(bill_id, "SKU-RICE-1K", 5)
    res = finalize_bill(bill_id, payment_mode="khata")
    assert res["status"] == "error"
    assert res.get("error_type") == "CreditLimitExceeded"

    # A smaller bill within the limit succeeds and charges khata
    bill2 = start_bill("Priya Sharma")["bill_id"]
    add_item_to_bill(bill2, "SKU-MILK-1L", 1)  # ₹56, 0% GST → 250+56=306 < 500
    res2 = finalize_bill(bill2, payment_mode="khata")
    assert res2["bill_status"] == "finalized"


def test_charge_khata_credit_limit_direct():
    set_credit_limit("Priya Sharma", 500.0)
    # Balance is 250 after seed; a ₹400 charge would exceed
    res = charge_khata("Priya Sharma", 400.0)
    assert res["status"] == "error"
    assert res.get("error_type") == "CreditLimitExceeded"

    ok = charge_khata("Priya Sharma", 100.0)  # 250+100=350 < 500
    assert ok["status"] == "success"


def test_overpayment_blocked():
    res = record_payment("Priya Sharma", 99999.0)
    assert res["status"] == "error"
    assert res.get("error_type") == "OverpaymentBlocked"
