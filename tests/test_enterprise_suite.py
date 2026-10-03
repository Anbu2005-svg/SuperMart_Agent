"""
Unit & Integration Tests for Advanced Enterprise Suite:
- Tier 1: Customer Purchase History & Suggestions (01), Daily Profit Dashboard (04)
- Tier 2: Multi-Shop Management (08), Category Sales Heatmap (09), Auto Purchase Order (10)
- Tier 3: GSTR-1 JSON Export (11), Dead Stock Detector (13), Customer Feedback (14), Role-Based Access (15)
- Tier 4: Business Health Score (18), EMI Installment Khata (19), End-of-Day Report (20)
"""

import os
import pytest
from datetime import date
from db.models import get_db_connection, immediate_transaction
from skills import (
    customer_history,
    analytics,
    shop_manager,
    purchase_orders,
    gst_export,
    inventory,
    feedback,
    roles,
    installments,
    eod_report
)


def _seed_customer_and_bill():
    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO customers (name, phone, khata_balance)
                VALUES ('Ravi Kumar', '+919876543210', 500.0)
                ON CONFLICT (name) DO UPDATE SET phone = EXCLUDED.phone
                RETURNING customer_id
            """)
            cid = cur.fetchone()["customer_id"]

            test_bill_id = "BILL-TEST-ENT-01"
            cur.execute("""
                INSERT INTO bills (bill_id, customer_id, status, payment_mode, subtotal, cgst, sgst, total, finalized_at)
                VALUES (%s, %s, 'finalized', 'upi', 500.0, 25.0, 25.0, 550.0, CURRENT_TIMESTAMP)
                ON CONFLICT (bill_id) DO UPDATE SET status = 'finalized'
            """, (test_bill_id, cid))

            cur.execute("SELECT sku_id, cost_price, mrp FROM products LIMIT 1")
            prod = cur.fetchone()
            if prod:
                cur.execute("""
                    INSERT INTO bill_items (bill_id, sku_id, qty, unit_price, gst_slab, line_total)
                    VALUES (%s, %s, 2.0, %s, 5.0, %s)
                    ON CONFLICT DO NOTHING
                """, (test_bill_id, prod["sku_id"], prod["mrp"], prod["mrp"] * 2))

            cur.close()
    finally:
        conn.close()


# ── Tier 1 Tests ──

def test_customer_purchase_history():
    _seed_customer_and_bill()
    res = customer_history.get_customer_purchase_history("Ravi Kumar")
    assert res["status"] == "success"
    assert res["customer_name"] == "Ravi Kumar"
    assert res["total_bills"] >= 1
    assert "lifetime_spend" in res


def test_customer_smart_suggestions():
    _seed_customer_and_bill()
    res = customer_history.get_customer_smart_suggestions("Ravi Kumar")
    assert res["status"] == "success"
    assert "whatsapp_reminder_template" in res


def test_all_customer_repurchase_alerts():
    _seed_customer_and_bill()
    res = customer_history.get_all_customer_repurchase_alerts()
    assert res["status"] == "success"
    assert "alert_count" in res


def test_daily_profit_dashboard():
    _seed_customer_and_bill()
    res = analytics.daily_profit_dashboard()
    assert res["status"] == "success"
    assert "today" in res
    assert "gross_profit" in res["today"]
    assert "margin_pct" in res["today"]
    assert "category_margins" in res


# ── Tier 2 Tests ──

def test_list_shops():
    res = shop_manager.list_shops()
    assert res["status"] == "success"
    assert res["count"] >= 1


def test_switch_active_shop():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT shop_name FROM shops LIMIT 1")
        row = cur.fetchone()
        cur.close()
        shop_name = row["shop_name"] if row else "Default Shop"
    finally:
        conn.close()

    res = shop_manager.switch_active_shop("test_user_123", shop_name)
    assert res["status"] == "success"
    assert res["shop_name"] == shop_name


def test_category_sales_heatmap():
    _seed_customer_and_bill()
    res = analytics.category_sales_heatmap(30)
    assert res["status"] == "success"
    assert "heatmap" in res


def test_generate_purchase_order():
    res = purchase_orders.generate_purchase_order(supplier_name="Metro Cash & Carry Wholesale")
    assert res["status"] == "success"
    if res.get("file_path"):
        assert os.path.exists(res["file_path"])
        assert res["file_path"].endswith(".pdf")


def test_list_purchase_orders():
    res = purchase_orders.list_purchase_orders()
    assert res["status"] == "success"
    assert "purchase_orders" in res


# ── Tier 3 Tests ──

def test_export_gstr1_json():
    _seed_customer_and_bill()
    res = gst_export.export_gstr1_json()
    assert res["status"] == "success"
    assert "file_path" in res
    assert os.path.exists(res["file_path"])
    assert res["file_path"].endswith(".json")


def test_detect_dead_stock():
    res = inventory.detect_dead_stock(no_sales_days=15)
    assert res["status"] == "success"
    assert "dead_stock_count" in res
    assert "total_locked_capital" in res


def test_customer_feedback():
    rec = feedback.record_customer_feedback(
        customer_name="Ravi Kumar",
        rating=5,
        feedback_text="Exceptional customer service and fresh groceries!"
    )
    assert rec["status"] == "success"
    assert rec["rating"] == 5

    summary = feedback.get_feedback_summary(30)
    assert summary["status"] == "success"
    assert summary["total_reviews"] >= 1
    assert summary["average_rating"] > 0

    link_res = feedback.generate_feedback_request_link("Ravi Kumar", bill_id="BILL-123")
    assert link_res["status"] == "success"
    assert "wa.me" in link_res["whatsapp_link"]


def test_role_based_access():
    owner_tid = "owner_test_99"
    staff_tid = "staff_test_88"

    roles.set_user_role(owner_tid, owner_tid, "owner")
    set_staff = roles.set_user_role(owner_tid, staff_tid, "staff")
    assert set_staff["status"] == "success"
    assert set_staff["role"] == "staff"

    allowed, err = roles.is_action_allowed(staff_tid, "get_stock")
    assert allowed is True

    denied, err = roles.is_action_allowed(staff_tid, "daily_profit_dashboard")
    assert denied is False
    assert "restricted" in err.lower() or "denied" in err.lower()


# ── Tier 4 Tests ──

def test_business_health_score():
    res = analytics.business_health_score()
    assert res["status"] == "success"
    assert 0 <= res["overall_score"] <= 100
    assert "grade" in res
    assert "pillars" in res
    assert len(res["recommendations"]) > 0


def test_installment_khata():
    plan = installments.create_installment_plan(
        customer_name="Ravi Kumar",
        total_amount=3000.0,
        num_installments=3,
        frequency_days=15
    )
    assert plan["status"] == "success"
    plan_id = plan["plan_id"]
    assert plan["installment_amount"] == 1000.0

    pay = installments.record_installment_payment(plan_id, 1000.0)
    assert pay["status"] == "success"
    assert pay["remaining_balance"] == 2000.0

    active = installments.list_active_installments()
    assert active["status"] == "success"

    rem = installments.generate_installment_reminder_link(plan_id)
    assert rem["status"] == "success"
    assert "wa.me" in rem["whatsapp_link"]


def test_end_of_day_report():
    _seed_customer_and_bill()
    res = eod_report.generate_end_of_day_report()
    assert res["status"] == "success"
    assert "gross_turnover" in res
    assert "collections" in res
    assert "cash" in res["collections"]
    assert "upi" in res["collections"]
