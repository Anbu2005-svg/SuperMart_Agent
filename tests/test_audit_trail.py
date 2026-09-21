import os
import json
import pytest
from db.seed import seed_database
from db.models import get_db_connection
from skills.billing import start_bill, add_item_to_bill, edit_item_qty, remove_item_from_bill, finalize_bill, preview_bill
from skills.inventory import receive_stock, add_product, get_stock
from skills.credit import charge_khata, record_payment
from skills.preferences import set_preference
from skills.audit import get_audit_trail

TEST_DB = "test_audit_trail.db"

@pytest.fixture(autouse=True)
def setup_test_db():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    seed_database(TEST_DB)
    import db.models
    orig_path = db.models.DEFAULT_DB_PATH
    db.models.DEFAULT_DB_PATH = TEST_DB
    yield
    db.models.DEFAULT_DB_PATH = orig_path
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

def _fetch_events():
    """Read raw audit_log rows ordered oldest first."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM audit_log ORDER BY id ASC")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

def test_audit_logs_bill_lifecycle():
    from skills.inventory import get_stock
    
    # Get current Maggi stock before test
    maggi_before = get_stock("SKU-MAGGI-70")["product"]["quantity"]
    
    bill_id = start_bill(customer_name="Ravi Kumar")["bill_id"]
    add_item_to_bill(bill_id, "Maggi", 3)
    add_item_to_bill(bill_id, "Salt", 2)
    edit_item_qty(bill_id, "Maggi", new_qty=5)
    remove_item_from_bill(bill_id, "Salt")
    fin = finalize_bill(bill_id, payment_mode="cash")
    assert fin["bill_status"] == "finalized"

    # Filter only events for this specific bill (by entity_id or bill_id in details)
    all_events = _fetch_events()
    events = [e for e in all_events if
              e["entity_id"] == bill_id or
              (e["details"] and bill_id in e["details"])]

    # Check event types are present
    event_types = [e["event_type"] for e in events]
    assert "BILL_CREATED" in event_types
    assert event_types.count("ITEM_ADDED") >= 2
    assert "ITEM_QTY_UPDATED" in event_types
    assert "ITEM_REMOVED" in event_types
    assert "STOCK_DECREMENTED" in event_types
    assert "BILL_FINALIZED" in event_types

    # BILL_CREATED details
    created = next(e for e in events if e["event_type"] == "BILL_CREATED")
    assert created["entity_type"] == "bill" and created["entity_id"] == bill_id
    assert json.loads(created["details"])["customer_name"] == "Ravi Kumar"

    # ITEM_ADDED for Maggi
    maggi_added = next(e for e in events if e["event_type"] == "ITEM_ADDED" and
                       json.loads(e["details"]).get("sku_id") == "SKU-MAGGI-70")
    d = json.loads(maggi_added["details"])
    assert d["qty"] == 3 and d["unit_price"] == 14.0

    # ITEM_QTY_UPDATED old -> new
    upd = next(e for e in events if e["event_type"] == "ITEM_QTY_UPDATED")
    assert upd["old_value"] == 3 and upd["new_value"] == 5

    # ITEM_REMOVED for Salt
    rem = next(e for e in events if e["event_type"] == "ITEM_REMOVED")
    assert json.loads(rem["details"])["sku_id"] == "SKU-SALT-01"

    # STOCK_DECREMENTED: Maggi qty - 5, references bill
    dec = next(e for e in events if e["event_type"] == "STOCK_DECREMENTED" and e["entity_id"] == "SKU-MAGGI-70")
    assert dec["entity_type"] == "product" and dec["entity_id"] == "SKU-MAGGI-70"
    assert dec["old_value"] == maggi_before
    assert dec["new_value"] == maggi_before - 5
    assert json.loads(dec["details"])["bill_id"] == bill_id

    # No decrement for the removed Salt item in this bill
    assert all(
        json.loads(e.get("details") or "{}").get("bill_id") != bill_id
        for e in all_events if e["event_type"] == "STOCK_DECREMENTED" and e["entity_id"] == "SKU-SALT-01"
    )

    # BILL_FINALIZED details
    fin_ev = next(e for e in events if e["event_type"] == "BILL_FINALIZED")
    fd = json.loads(fin_ev["details"])
    assert fd["payment_mode"] == "cash" and fd["customer_name"] == "Ravi Kumar"

def test_audit_no_log_on_oversell_rejection():
    from skills.inventory import get_stock
    
    # Get current Maggi stock from DB
    maggi_info = get_stock("SKU-MAGGI-70")
    maggi_stock = maggi_info["product"]["quantity"] if maggi_info["status"] == "success" else 0
    
    bill_id = start_bill("Walk-in")["bill_id"]

    # Attempt to add way more than current stock (3x current stock)
    oversell_qty = int(maggi_stock) + 9999
    res = add_item_to_bill(bill_id, "SKU-MAGGI-70", oversell_qty)
    assert res["status"] == "oversell_warning"

    # Bill2 is empty (nothing was added), so finalizing it returns an error
    bill2 = start_bill("Another")["bill_id"]
    fin = finalize_bill(bill2, payment_mode="cash")  # empty bill rejected
    assert fin["status"] == "error"

    events = _fetch_events()
    b2_events = [e for e in events if e["entity_id"] == bill2 and e["event_type"] in ("ITEM_ADDED", "STOCK_DECREMENTED")]
    assert b2_events == []

def test_audit_logs_stock_receipt_and_product():
    # receive_stock on Butter 100g (SKU-BUTTER-100, stock 20 -> 45)
    rec = receive_stock("SKU-BUTTER-100", qty=25.0, cost_price=52.0)
    assert rec["status"] == "success"

    events = _fetch_events()
    rcv = [e for e in events if e["event_type"] == "STOCK_RECEIVED"]
    assert len(rcv) >= 1
    assert any(e["entity_id"] == "SKU-BUTTER-100" for e in rcv)
    matching = next(e for e in rcv if e["entity_id"] == "SKU-BUTTER-100")
    assert matching["new_value"] == matching["old_value"] + 25.0

    # add_product
    add_res = add_product(name="Haldiram Bhujia 200g", category="Snacks & Packaged Food",
                          unit="packet", is_loose=False, cost_price=45.0, mrp=60.0,
                          gst_slab=12.0, hsn_code="2106", quantity=50.0, reorder_level=10.0)
    assert add_res["status"] == "success"

    events = _fetch_events()
    add_ev = [e for e in events if e["event_type"] == "PRODUCT_ADDED"]
    assert len(add_ev) >= 1
    assert any(e["entity_id"] == add_res["sku_id"] for e in add_ev)
    matching_add = next(e for e in add_ev if e["entity_id"] == add_res["sku_id"])
    assert matching_add["old_value"] == 0 and matching_add["new_value"] == 50.0

def test_audit_logs_khata_events():
    from skills.credit import get_khata_balance, set_credit_limit
    set_credit_limit("Priya Sharma", 0.0)
    initial_bal = get_khata_balance("Priya Sharma").get("khata_balance", 0.0)
    
    # Direct charge
    res_chg = charge_khata("Priya Sharma", 350.0)
    assert res_chg["status"] == "success"
    # Payment
    res_pmt = record_payment("Priya Sharma", 200.0)
    assert res_pmt["status"] == "success"
    # Finalize-path khata charge
    bill_id = start_bill(customer_name="Priya Sharma")["bill_id"]
    add_item_to_bill(bill_id, "Maggi", 2)
    fin = finalize_bill(bill_id, payment_mode="khata")
    assert fin["bill_status"] == "finalized"

    events = _fetch_events()
    khata_events = [e for e in events if e["event_type"] in ("KHATA_CHARGED", "KHATA_PAYMENT_RECORDED")]

    assert len(khata_events) >= 3
    charged_events = [e for e in khata_events if e["event_type"] == "KHATA_CHARGED"]
    paid_events = [e for e in khata_events if e["event_type"] == "KHATA_PAYMENT_RECORDED"]
    
    assert len(charged_events) >= 1
    assert len(paid_events) >= 1

    # Verify charge delta
    c1 = charged_events[0]
    assert round(c1["new_value"] - c1["old_value"], 2) == 350.0

    # Verify payment delta
    p1 = paid_events[0]
    assert round(p1["old_value"] - p1["new_value"], 2) == 200.0

    # Finalize-path KHATA_CHARGED: verify bill linkage
    fin_charge = charged_events[-1]
    assert json.loads(fin_charge["details"])["bill_id"] == bill_id
    grand_total = fin["summary"]["grand_total"]
    assert round(fin_charge["new_value"] - fin_charge["old_value"], 2) == round(grand_total, 2)

def test_get_audit_trail_filtering():
    # Sell Maggi and Butter
    b1 = start_bill("Customer 1")["bill_id"]
    add_item_to_bill(b1, "Maggi", 10)
    finalize_bill(b1, payment_mode="cash")
    b2 = start_bill("Customer 2")["bill_id"]
    add_item_to_bill(b2, "Butter", 1)
    finalize_bill(b2, payment_mode="cash")

    # Filter by product + event type
    res = get_audit_trail(query="Maggi", event_type="STOCK_DECREMENTED")
    assert res["status"] == "success"
    assert res["count"] >= 1
    for e in res["events"]:
        assert e["event_type"] == "STOCK_DECREMENTED"
        assert "Maggi" in e["entity_id"] or "Maggi" in json.dumps(e["details"])

    # Filter by bill id
    res_bill = get_audit_trail(query=b1)
    assert res_bill["status"] == "success"
    assert res_bill["count"] >= 1
    assert all(e["entity_id"] == b1 or b1 in json.dumps(e["details"] or {}) for e in res_bill["events"])

    # Empty result shape
    res_empty = get_audit_trail(query="NonExistentXYZ123")
    assert res_empty["status"] == "success"
    assert res_empty["count"] == 0
    assert res_empty["events"] == []

    # Limit clamping
    res_all = get_audit_trail()
    assert res_all["status"] == "success"
    assert res_all["count"] <= 100
    res_clamped_hi = get_audit_trail(limit=500)
    assert res_clamped_hi["status"] == "success" and res_clamped_hi["count"] <= 100
    res_clamped_lo = get_audit_trail(limit=0)
    assert res_clamped_lo["status"] == "success" and res_clamped_lo["count"] <= 1
