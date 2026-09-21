import threading
from skills.billing import start_bill, add_item_to_bill, finalize_bill, preview_bill
from skills.analytics import daily_summary, close_day, period_summary, reorder_suggestions
from skills.inventory import get_stock


def test_daily_summary_and_close_day():
    initial_summary = daily_summary()
    initial_bills = initial_summary["total_bills"]
    initial_sales = initial_summary["total_sales"]

    # Cut a cash bill (Maggi 10 pkts)
    b1 = start_bill("Customer 1")["bill_id"]
    add_item_to_bill(b1, "Maggi", 10)
    finalize_bill(b1, payment_mode="cash")
    b1_total = preview_bill(b1)["summary"]["grand_total"]

    # Cut a UPI bill (Butter 2 pkts)
    b2 = start_bill("Customer 2")["bill_id"]
    add_item_to_bill(b2, "Butter", 2)
    finalize_bill(b2, payment_mode="upi")
    b2_total = preview_bill(b2)["summary"]["grand_total"]

    summary = daily_summary()
    assert summary["status"] == "success"
    assert summary["total_bills"] == initial_bills + 2
    assert round(summary["total_sales"], 2) == round(initial_sales + b1_total + b2_total, 2)

    closed = close_day()
    assert closed["status"] == "success"
    assert closed["total_bills"] == initial_bills + 2


def test_concurrency_transaction_lock():
    """5 concurrent finalize transactions must all succeed without corrupting stock."""
    initial_qty = get_stock("SKU-SALT-01")["product"]["quantity"]
    results = []

    def make_sale(_i):
        try:
            b_id = start_bill("Ravi Kumar")["bill_id"]
            add_item_to_bill(b_id, "Salt", 1)
            res = finalize_bill(b_id, payment_mode="cash")
            results.append(res)
        except Exception as e:
            results.append({"status": "exception", "message": str(e)})

    threads = []
    for i in range(5):
        t = threading.Thread(target=make_sale, args=(i,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 5
    finalized = [r for r in results if r.get("bill_status") == "finalized"]
    assert len(finalized) == 5

    # Stock decremented exactly 5 times — no corruption, no loss
    final_qty = get_stock("SKU-SALT-01")["product"]["quantity"]
    assert final_qty == initial_qty - 5


def test_concurrent_sale_vs_restock_no_oversell_corruption():
    """A sale and a stock-in racing each other must never produce negative stock."""
    # Drain Salt stock to 3 so oversell is easy to trigger
    from db.models import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE products SET quantity = 3 WHERE sku_id = 'SKU-SALT-01'")
        conn.commit()
        cur.close()
    finally:
        conn.close()

    results = []

    def attempt_sale(qty):
        try:
            b_id = start_bill()["bill_id"]
            add_item_to_bill(b_id, "SKU-SALT-01", qty)
            res = finalize_bill(b_id, payment_mode="cash")
            results.append(res)
        except Exception as e:
            results.append({"status": "exception", "message": str(e)})

    sale_t = threading.Thread(target=attempt_sale, args=(2,))
    sale_t2 = threading.Thread(target=attempt_sale, args=(2,))
    sale_t.start(); sale_t2.start()
    sale_t.join(); sale_t2.join()

    # Stock must never go negative
    assert get_stock("SKU-SALT-01")["product"]["quantity"] >= 0


def test_period_summary_aggregates():
    p = period_summary(days=7)
    assert p["status"] == "success"
    assert p["period_days"] == 7
    assert "payment_breakdown" in p
    assert "top_items" in p
    assert "daily_trend" in p


def test_reorder_suggestions_from_velocity():
    """After selling items, reorder suggestions must be data-driven."""
    # Make Milk low-stock artificially + sell some Maggi
    from db.models import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE products SET quantity = 2 WHERE sku_id = 'SKU-MILK-1L'")
        conn.commit()
        cur.close()
    finally:
        conn.close()

    b_id = start_bill()["bill_id"]
    add_item_to_bill(b_id, "Milk", 1)
    add_item_to_bill(b_id, "Maggi", 85)
    finalize_bill(b_id, payment_mode="upi")

    sugg = reorder_suggestions(velocity_days=7, cover_days=7)
    assert sugg["status"] == "success"
    names = [s["name"] for s in sugg["suggestions"]]
    assert any("Milk" in n for n in names)   # below reorder level
    assert any("Maggi" in n for n in names)   # high velocity → low cover
    milk = next(s for s in sugg["suggestions"] if "Milk" in s["name"])
    assert milk["avg_daily_velocity"] > 0
    assert milk["suggested_reorder_qty"] > 0
    maggi = next(s for s in sugg["suggestions"] if "Maggi" in s["name"])
    assert maggi["avg_daily_velocity"] > 0
    assert maggi["suggested_reorder_qty"] > 0
