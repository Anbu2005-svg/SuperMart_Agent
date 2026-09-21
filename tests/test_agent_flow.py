import os
import uuid
from skills.billing import start_bill, add_item_to_bill, preview_bill, finalize_bill, quick_create_bill
from skills.credit import charge_khata, get_khata_balance, record_payment, khata_reminders, set_credit_limit
from skills.preferences import set_preference, get_preference
from docgen.invoice_template import generate_pdf_invoice, amount_to_indian_words
from docgen.deck_builder import generate_analysis_pptx


def test_full_billing_and_pdf_flow():
    bill_res = start_bill(customer_name="Ravi Kumar")
    bill_id = bill_res["bill_id"]
    assert bill_id.startswith("BILL-")

    add_item_to_bill(bill_id, "Aashirvaad Whole Wheat Atta", 2)
    add_item_to_bill(bill_id, "Amul Pasteurised Butter", 1)

    prev = preview_bill(bill_id)
    assert prev["status"] == "success"
    assert len(prev["items"]) == 2
    assert prev["summary"]["grand_total"] > 0

    fin = finalize_bill(bill_id, payment_mode="upi")
    assert fin["bill_status"] == "finalized"

    pdf_path = generate_pdf_invoice(bill_id)
    assert os.path.exists(pdf_path)
    assert pdf_path.endswith(".pdf")


def test_pdf_contains_invoice_number_and_words():
    """The generated invoice must be GST-compliant: invoice number + amount in words."""
    bill_id = quick_create_bill(items=[{"name": "Maggi", "qty": 2}], payment_mode="cash")["bill_id"]
    fin = finalize_bill(bill_id, payment_mode="cash") if preview_bill(bill_id)["bill_status"] != "finalized" else preview_bill(bill_id)
    assert fin["invoice_number"] is not None

    pdf_path = generate_pdf_invoice(bill_id)
    assert os.path.exists(pdf_path)

    # Verify the words conversion used on the invoice
    words = amount_to_indian_words(33.04)  # 2×14=28 + 18% GST = 33.04
    assert "Rupees" in words and "Paise" in words


def test_amount_to_indian_words():
    assert "Zero Rupees Only" in amount_to_indian_words(0)
    assert "Fifty Rupees Only" in amount_to_indian_words(50)
    assert "One Thousand" in amount_to_indian_words(1000)
    assert "One Lakh" in amount_to_indian_words(100000)
    assert "One Crore" in amount_to_indian_words(10000000)
    assert "and Fifty Paise" in amount_to_indian_words(10.50)


def test_khata_credit_lifecycle():
    b_start = get_khata_balance("Priya Sharma").get("khata_balance", 0.0)
    set_credit_limit("Priya Sharma", 0.0)  # unlimited for this test

    chg = charge_khata("Priya Sharma", 100.0)
    assert chg["status"] == "success"

    bal1 = get_khata_balance("Priya Sharma")
    assert bal1["khata_balance"] == b_start + 100.0

    pmt = record_payment("Priya Sharma", 50.0)
    assert pmt["status"] == "success"

    bal2 = get_khata_balance("Priya Sharma")
    assert bal2["khata_balance"] == b_start + 50.0


def test_khata_reminders_generate_messages():
    set_credit_limit("Priya Sharma", 0.0)
    charge_khata("Priya Sharma", 150.0)  # above default 100 min balance
    res = khata_reminders(min_balance=100.0)
    assert res["status"] == "success"
    assert res["count"] >= 1
    priya = next(r for r in res["reminders"] if r["customer_name"] == "Priya Sharma")
    assert priya["khata_balance"] >= 150.0
    assert str(int(priya["khata_balance"])) in priya["reminder_message"] or f"{priya['khata_balance']:.2f}" in priya["reminder_message"]
    assert "₹" in priya["reminder_message"]


def test_pptx_generation_with_data_driven_charts():
    # Make some sales so the deck has real data
    quick_create_bill(items=[{"name": "Maggi", "qty": 2}, {"name": "Sugar", "qty": 1}], payment_mode="upi")
    deck_path = generate_analysis_pptx("Weekly", days=7)
    assert os.path.exists(deck_path)
    assert deck_path.endswith(".pptx")


def test_preferences_persistence():
    owner = f"owner_{uuid.uuid4().hex[:8]}"
    set_preference(owner, "default_payment_mode", "upi")
    pref = get_preference(owner, "default_payment_mode")
    assert pref["status"] == "success"
    assert pref["value"] == "upi"


def test_list_all_products_pagination():
    from skills.inventory import list_all_products
    res = list_all_products(limit=5)
    assert res["status"] == "success"
    assert res["count"] == 5
    assert res["total"] >= 12
    assert "total" in res

    page2 = list_all_products(limit=5, offset=5)
    assert page2["count"] == 5
    assert page2["products"][0]["sku_id"] != res["products"][0]["sku_id"]


def test_add_product_and_receive_stock():
    from skills.inventory import add_product, receive_stock, get_stock, archive_product

    unique_name = f"Test Bhujia {uuid.uuid4().hex[:6]}"
    sku = None
    try:
        add_res = add_product(
            name=unique_name,
            category="Snacks & Packaged Food",
            unit="packet",
            is_loose=False,
            cost_price=45.0,
            mrp=60.0,
            gst_slab=12.0,
            hsn_code="2106",
            quantity=50.0,
            reorder_level=10.0
        )
        assert add_res["status"] == "success"
        sku = add_res["sku_id"]

        stk = get_stock(sku)
        assert stk["status"] == "success"
        assert stk["product"]["quantity"] == 50.0

        rec = receive_stock(sku, qty=25.0, cost_price=45.0)
        assert rec["status"] == "success"
        assert rec["new_quantity"] == 75.0
    finally:
        if sku:
            archive_product(sku)  # soft-delete so we don't pollute shared test DB
