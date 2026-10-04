import pytest
from skills.cash_drawer import (
    open_cash_drawer,
    record_petty_cash,
    get_cash_drawer_status,
    close_cash_drawer,
    list_drawer_sessions
)
from skills.gst_itc import get_gst_itc_summary
from skills.reorder_alerts import check_low_stock_reorder_alerts, list_recent_reorder_notifications
from skills.billing import start_bill, add_item_to_bill, finalize_bill


def test_cash_drawer_lifecycle():
    # Make sure drawer is clean or close existing
    status = get_cash_drawer_status()
    if status.get("status") == "success":
        close_cash_drawer(closing_cash_counted=status["reconciliation"]["expected_cash_in_drawer"], closed_by="Setup")

    # 1. Open drawer with 2000 float
    res_open = open_cash_drawer(opening_cash=2000.0, opened_by="Cashier Anbu")
    assert res_open["status"] == "success"
    session_id = res_open["session_id"]

    # Opening again while open should fail
    dup_open = open_cash_drawer(opening_cash=1000.0)
    assert dup_open["status"] == "error"

    # 2. Record cash sales via finalized bill
    b = start_bill("Cash Drawer Customer")
    add_item_to_bill(b["bill_id"], "Atta", 1.0)
    final_res = finalize_bill(b["bill_id"], payment_mode="cash")
    bill_total = final_res["total"]

    # 3. Record petty cash expense (tea: ₹50)
    res_petty = record_petty_cash(amount=50.0, category="tea_snacks", expense_type="expense", paid_to="Chai Stall")
    assert res_petty["status"] == "success"

    # 4. Record petty cash in (owner float add: ₹500)
    res_in = record_petty_cash(amount=500.0, category="misc", expense_type="cash_in", paid_to="Drawer")
    assert res_in["status"] == "success"

    # 5. Check live status
    status = get_cash_drawer_status()
    assert status["status"] == "success"
    rec = status["reconciliation"]
    assert rec["opening_float"] == 2000.0
    assert rec["cash_sales_revenue"] >= bill_total
    assert rec["petty_cash_expenses"] == 50.0
    assert rec["cash_in"] == 500.0

    expected = 2000.0 + rec["cash_sales_revenue"] + 500.0 - 50.0
    assert round(rec["expected_cash_in_drawer"], 2) == round(expected, 2)

    # 6. Close drawer with exact expected amount
    res_close = close_cash_drawer(closing_cash_counted=expected, closed_by="Cashier Anbu")
    assert res_close["status"] == "success"
    assert res_close["variance"] == 0.0
    assert res_close["variance_status"] == "balanced"

    # 7. Check drawer sessions history
    sess_list = list_drawer_sessions(limit=5)
    assert sess_list["status"] == "success"
    assert any(s["session_id"] == session_id for s in sess_list["sessions"])


def test_gst_itc_summary():
    summary = get_gst_itc_summary()
    assert summary["status"] == "success"
    assert "outward_supplies" in summary
    assert "inward_supplies_itc" in summary
    assert "net_tax_computation" in summary
    assert "slab_breakdown" in summary["outward_supplies"]
    assert "net_payable" in summary["net_tax_computation"]


def test_low_stock_reorder_alerts_no_auto_reorder():
    res = check_low_stock_reorder_alerts()
    assert res["status"] == "success"
    assert "message" in res
    assert "alerts" in res

    # Verify no auto-reorder orders were generated
    assert "Auto-reorder is disabled" in res["message"] or res["count"] == 0

    # History audit
    history = list_recent_reorder_notifications(limit=5)
    assert history["status"] == "success"
