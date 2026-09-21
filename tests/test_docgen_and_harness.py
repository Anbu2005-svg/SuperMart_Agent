import os
from skills.billing import start_bill, add_item_to_bill, finalize_bill, quick_create_bill
from skills.credit import charge_khata, record_payment, get_khata_balance, list_all_khata, set_credit_limit
from skills.inventory import search_products
from docgen.invoice_template import generate_pdf_invoice
from docgen.deck_builder import generate_analysis_pptx
from agent.harness import TOOL_DISPATCH, TOOLS_SCHEMA


def test_invoice_pdf_creation_and_size():
    b_res = start_bill("Sita Lakshmi")
    bill_id = b_res["bill_id"]
    add_item_to_bill(bill_id, "Atta", 1)
    add_item_to_bill(bill_id, "Butter", 1)
    add_item_to_bill(bill_id, "Soap", 2)
    finalize_bill(bill_id, payment_mode="cash")

    pdf_file = generate_pdf_invoice(bill_id)
    assert os.path.exists(pdf_file)
    assert os.path.getsize(pdf_file) > 1000

    from pypdf import PdfReader
    invoice_text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_file).pages)
    assert "HSN" in invoice_text
    assert "1101" in invoice_text
    # GST-compliance additions
    assert "Invoice #" in invoice_text
    assert "Amount in Words" in invoice_text
    assert "Place of Supply" in invoice_text
    assert "Authorised Signatory" in invoice_text


def test_invoice_pdf_tax_breakup_by_slab():
    """Invoice must render the slab-wise GST breakup table (0/5/12/18 items)."""
    res = quick_create_bill(
        items=[{"name": "Sugar", "qty": 1}, {"name": "Atta", "qty": 1}, {"name": "Butter", "qty": 1}],
        payment_mode="upi"
    )
    bill_id = res["bill_id"]
    pdf_file = generate_pdf_invoice(bill_id)

    from pypdf import PdfReader
    invoice_text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_file).pages)
    assert "Tax Breakup by Slab" in invoice_text
    assert "GST Rate" in invoice_text


def test_pptx_deck_slides_creation():
    deck_file = generate_analysis_pptx("August 2026", days=7)
    assert os.path.exists(deck_file)
    assert os.path.getsize(deck_file) > 5000


def test_llm_tool_dispatch_completeness():
    schema_names = [t["function"]["name"] for t in TOOLS_SCHEMA]
    assert len(schema_names) == len(TOOL_DISPATCH)
    for name in schema_names:
        assert name in TOOL_DISPATCH
        assert callable(TOOL_DISPATCH[name])


def test_khata_repayment_lifecycle():
    set_credit_limit("Ravi Kumar", 0.0)  # unlimited for this test
    init_bal = get_khata_balance("Ravi Kumar")["khata_balance"]

    charge_khata("Ravi Kumar", 500.0)
    bal1 = get_khata_balance("Ravi Kumar")
    assert bal1["khata_balance"] == init_bal + 500.0

    record_payment("Ravi Kumar", 200.0)
    bal2 = get_khata_balance("Ravi Kumar")
    assert bal2["khata_balance"] == init_bal + 300.0

    khata_list = list_all_khata()
    assert khata_list["status"] == "success"
    names = [c["name"] for c in khata_list["khata_ledger"]]
    assert "Ravi Kumar" in names


def test_search_products_no_match():
    res = search_products("NonExistentItemXYZ")
    assert res["status"] == "success"
    assert res["count"] == 0
    assert len(res["products"]) == 0
