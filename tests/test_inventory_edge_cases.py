import uuid
import pytest
from skills.inventory import add_product, receive_stock, search_products, list_low_stock, get_stock, \
    update_gst_slab, archive_product, clarify_product_match, expiring_stock
from skills.billing import start_bill, add_item_to_bill, finalize_bill
from db.models import get_db_connection


def test_add_product_cost_exceeds_mrp():
    res = add_product("Overpriced Item", "Pantry", "packet", False, cost_price=150.0, mrp=100.0, gst_slab=5.0, hsn_code="1234")
    assert res["status"] == "error"
    assert "cannot exceed MRP" in res["message"]


def test_add_product_invalid_gst_slab():
    res = add_product("Invalid GST Item", "Pantry", "packet", False, cost_price=50.0, mrp=100.0, gst_slab=8.0, hsn_code="1234")
    assert res["status"] == "error"
    assert "GST slab must be one of" in res["message"]


def test_add_product_requires_valid_hsn():
    res = add_product("No HSN Item", "Pantry", "packet", False, cost_price=10.0, mrp=20.0, gst_slab=5.0, hsn_code="x1")
    assert res["status"] == "error"
    assert "HSN" in res["message"]


def test_receive_stock_negative_qty():
    res = receive_stock("SKU-MILK-1L", qty=-5.0, cost_price=40.0)
    assert res["status"] == "error"
    assert "positive" in res["message"]


def test_receive_stock_cost_cannot_exceed_mrp():
    res = receive_stock("SKU-MAGGI-70", qty=5.0, cost_price=20.0, mrp=14.0)
    assert res["status"] == "error"
    assert "cannot exceed MRP" in res["message"]


def test_search_products_pagination():
    res = search_products("Amul", limit=1, offset=0)
    assert res["status"] == "success"
    assert res["count"] == 1
    assert res["total"] >= 2  # total ignores pagination

    res2 = search_products("Amul", limit=1, offset=1)
    assert res2["count"] == 1
    assert res2["products"][0]["name"] != res["products"][0]["name"]


def test_list_low_stock():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE products SET quantity = 2 WHERE sku_id = 'SKU-RICE-1K'")
        conn.commit()
        cur.close()
    finally:
        conn.close()

    res = list_low_stock()
    assert res["status"] == "success"
    assert any("Rice" in item["name"] for item in res["low_stock_items"])


def test_update_gst_slab_and_revert():
    res = update_gst_slab(new_gst_slab=5.0, sku_or_name="Sugar")
    assert res["status"] == "success"
    assert res["new_gst_slab"] == 5.0
    st = get_stock("Sugar")
    assert st["product"]["gst_slab"] == 5.0
    # revert to seed value for other tests
    update_gst_slab(new_gst_slab=0.0, sku_or_name="Sugar")


def test_clarify_product_match_formats_options():
    res = clarify_product_match(
        matches=[
            {"sku_id": "SKU-A", "name": "Aavin Milk 500ml", "mrp": 25.0, "quantity": 10, "unit": "packet"},
            {"sku_id": "SKU-B", "name": "Amul Milk 500ml", "mrp": 27.0, "quantity": 8, "unit": "packet"},
        ],
        query="milk"
    )
    assert res["status"] == "clarification_required"
    assert "Aavin Milk" in res["message"]
    assert "Amul Milk" in res["message"]


def test_archive_product_soft_delete():
    unique = f"Archive Test Item {uuid.uuid4().hex[:6]}"
    add_res = add_product(unique, "Pantry", "packet", False, cost_price=10.0, mrp=15.0, gst_slab=5.0,
                          hsn_code="9999", quantity=5.0)
    assert add_res["status"] == "success"
    sku = add_res["sku_id"]

    # Archived product disappears from search & billing but row remains
    arch = archive_product(sku)
    assert arch["status"] == "success"

    search_res = search_products(unique)
    assert all(p["sku_id"] != sku for p in search_res["products"])

    bill_id = start_bill()["bill_id"]
    add_bill_res = add_item_to_bill(bill_id, sku, 1)
    assert add_bill_res["status"] == "error"

    # Row still physically exists (soft delete)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT is_active FROM products WHERE sku_id = %s", (sku,))
        row = cur.fetchone()
        cur.close()
        assert row is not None and row["is_active"] is False
    finally:
        conn.close()


def test_batch_and_felo_expiry_tracking():
    """Receiving stock with batch code + expiry registers a batch; FEFO consumes it at billing."""
    from datetime import date, timedelta
    expiry_soon = (date.today() + timedelta(days=10)).isoformat()
    expiry_later = (date.today() + timedelta(days=60)).isoformat()

    r1 = receive_stock("SKU-MAGGI-70", qty=10, batch_code="B-EARLY", expiry_date=expiry_soon)
    assert r1["status"] == "success"
    assert r1["batch"]["batch_code"] == "B-EARLY"

    r2 = receive_stock("SKU-MAGGI-70", qty=10, batch_code="B-LATE", expiry_date=expiry_later)
    assert r2["status"] == "success"

    # Expiring-stock report sees the early batch
    exp = expiring_stock(within_days=15)
    codes = [b["batch_code"] for b in exp["expiring_batches"]]
    assert "B-EARLY" in codes
    assert "B-LATE" not in codes
    # soonest-first ordering
    assert codes.index("B-EARLY") == 0 or exp["expiring_batches"][0]["batch_code"] == "B-EARLY"

    # FEFO: selling 5 Maggi must consume from the earliest-expiry batch first
    bill_id = start_bill()["bill_id"]
    add_item_to_bill(bill_id, "SKU-MAGGI-70", 5)
    fin = finalize_bill(bill_id, payment_mode="cash")
    assert fin["bill_status"] == "finalized"

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT batch_code, qty_remaining FROM stock_batches WHERE sku_id = 'SKU-MAGGI-70' ORDER BY expiry_date")
        rows = cur.fetchall()
        cur.close()
        by_code = {r["batch_code"]: float(r["qty_remaining"]) for r in rows}
        assert by_code["B-EARLY"] == 5.0   # consumed FEFO-first
        assert by_code["B-LATE"] == 10.0   # untouched
    finally:
        conn.close()


def test_loose_item_pricing_per_kg():
    """Loose sugar at ₹48/kg: 2kg must bill as 2 × 48 = 96 taxable."""
    from skills.billing import preview_bill
    bill_id = start_bill()["bill_id"]
    add_item_to_bill(bill_id, "SKU-SUGAR-1K", 2)

    prev = preview_bill(bill_id)
    item = prev["items"][0]
    assert item["unit_price"] == 48.0
    assert prev["summary"]["subtotal"] == 96.0  # 0% GST on loose sugar
