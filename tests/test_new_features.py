"""
Tests for new features: Proactive Notifications, Sales Forecast, GSTR-1 Export,
Profit & Loss, Returns & Refunds, Customer Insights, Digital Receipt, and
tool registration in the agent harness.
"""
import os
import math
from datetime import date, timedelta

# ── helpers to create bills deterministically ──────────────────────────
def _create_product(sku_id, name, category="Grocery", unit="packet",
                    cost_price=30.0, mrp=45.0, gst_slab=5, hsn_code="1234",
                    quantity=100):
    from skills.inventory import add_product
    return add_product(
        name=name, category=category, unit=unit, is_loose=False,
        cost_price=cost_price, mrp=mrp, gst_slab=gst_slab,
        hsn_code=hsn_code, quantity=quantity
    )


def _create_and_finalize_bill(items, payment_mode="cash", customer_name=None):
    """Create a bill, add items, and finalize it. Returns finalized bill result."""
    from skills.billing import start_bill, add_item_to_bill, finalize_bill
    bill = start_bill(customer_name=customer_name)
    bill_id = bill["bill_id"]
    for sku_id, qty in items:
        add_item_to_bill(bill_id=bill_id, sku_or_name=sku_id, qty=qty)
    result = finalize_bill(bill_id=bill_id, payment_mode=payment_mode)
    return result


# ═══════════════════════════════════════════════════════════════════
# Feature 1: Proactive Smart Notifications
# ═══════════════════════════════════════════════════════════════════

class TestExpiryAlerts:
    """Test expiry alert detection from stock_batches."""

    def test_no_expiry_alerts_when_clean(self):
        """No alerts when no batches have expiry dates set."""
        from skills.notifications import check_expiring_stock
        result = check_expiring_stock(days_ahead=7)
        assert result["status"] == "success"
        assert result["count"] == 0

    def test_expiry_alert_within_window(self):
        """Alert raised when a batch expires within the window."""
        from skills.inventory import receive_stock
        from skills.notifications import check_expiring_stock

        # Receive stock with expiry 1 day from now
        expiry = (date.today() + timedelta(days=1)).isoformat()
        receive_stock(sku_id="SKU-MILK-1L", qty=20, batch_code="B-EXP-TEST",
                      expiry_date=expiry)

        result = check_expiring_stock(days_ahead=7)
        assert result["status"] == "success"
        assert result["count"] >= 1
        alert = next(a for a in result["alerts"] if a["batch_code"] == "B-EXP-TEST")
        assert alert["days_left"] == 1
        assert "CRITICAL" in alert["urgency"]

    def test_expiry_alert_outside_window_not_shown(self):
        """No alert for batches expiring beyond the window."""
        from skills.inventory import receive_stock
        from skills.notifications import check_expiring_stock

        expiry = (date.today() + timedelta(days=30)).isoformat()
        receive_stock(sku_id="SKU-MILK-1L", qty=20, batch_code="B-FAR",
                      expiry_date=expiry)

        result = check_expiring_stock(days_ahead=7)
        far_alerts = [a for a in result["alerts"] if a["batch_code"] == "B-FAR"]
        assert len(far_alerts) == 0


class TestLowStockAlerts:
    """Test critically low stock detection."""

    def test_low_stock_detection(self):
        """Items at or below reorder level are flagged."""
        from skills.notifications import check_critical_low_stock
        from db.models import get_db_connection

        # Force a product to 0 stock
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE products SET quantity = 0 WHERE sku_id = 'SKU-MILK-1L'")
            conn.commit()
            cur.close()
        finally:
            conn.close()

        result = check_critical_low_stock()
        assert result["status"] == "success"
        assert result["count"] >= 1
        names = [a["name"] for a in result["alerts"]]
        assert any("Milk" in n or "milk" in n.lower() for n in names)

    def test_healthy_stock_no_alerts(self):
        """No alerts when all stock is above reorder level."""
        from skills.notifications import check_critical_low_stock
        from db.models import get_db_connection

        # Set all products to high stock
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE products SET quantity = 999 WHERE is_active = TRUE")
            conn.commit()
            cur.close()
        finally:
            conn.close()

        result = check_critical_low_stock()
        assert result["count"] == 0


class TestDailyCloseoutMessage:
    """Test daily closeout summary generation."""

    def test_closeout_with_no_sales(self):
        """Closeout works even with zero sales."""
        from skills.notifications import generate_daily_closeout_message
        result = generate_daily_closeout_message()
        assert result["status"] == "success"
        assert result["total_bills"] == 0
        assert result["revenue"] == 0
        assert "message" in result

    def test_closeout_with_sales(self):
        """Closeout includes revenue from today's finalized bills."""
        from skills.notifications import generate_daily_closeout_message

        _create_and_finalize_bill([("SKU-MILK-1L", 5)], payment_mode="cash")

        result = generate_daily_closeout_message()
        assert result["status"] == "success"
        assert result["total_bills"] >= 1
        assert result["revenue"] > 0


# ═══════════════════════════════════════════════════════════════════
# Feature 2: AI Sales Forecasting
# ═══════════════════════════════════════════════════════════════════

class TestSalesForecast:
    """Test demand prediction and stockout risk detection."""

    def test_forecast_with_no_sales(self):
        """Forecast returns empty when no sales exist."""
        from skills.analytics import sales_forecast
        result = sales_forecast(days_history=30, forecast_days=7)
        assert result["status"] == "success"
        assert result["product_count"] == 0

    def test_forecast_with_sales(self):
        """Forecast includes products that were sold."""
        from skills.analytics import sales_forecast

        _create_and_finalize_bill([("SKU-MILK-1L", 10)])

        result = sales_forecast(days_history=30, forecast_days=7)
        assert result["status"] == "success"
        assert result["product_count"] >= 1
        milk_forecast = next((f for f in result["forecasts"] if "Milk" in f["name"] or "milk" in f["name"].lower()), None)
        assert milk_forecast is not None
        assert milk_forecast["daily_velocity"] > 0


# ═══════════════════════════════════════════════════════════════════
# Feature 3: GST Return Export (GSTR-1)
# ═══════════════════════════════════════════════════════════════════

class TestGSTR1Export:
    """Test GSTR-1 CSV generation."""

    def test_gstr1_empty_month(self):
        """GSTR-1 export works with no invoices in the month."""
        from skills.gst_export import export_gstr1
        result = export_gstr1(month=1, year=2020)
        assert result["status"] == "success"
        assert result["total_invoices"] == 0

    def test_gstr1_with_invoices(self):
        """GSTR-1 generates CSV files with invoice data."""
        from skills.gst_export import export_gstr1

        _create_and_finalize_bill([("SKU-MILK-1L", 5), ("SKU-RICE-5K", 2)])

        today = date.today()
        result = export_gstr1(month=today.month, year=today.year)
        assert result["status"] == "success"
        assert result["total_invoices"] >= 1
        assert result["total_revenue"] > 0
        assert len(result["files"]) == 2
        # Check CSV files exist
        for f_path in result["files"]:
            assert os.path.exists(f_path)
        assert len(result["b2c_summary"]) > 0
        assert len(result["hsn_summary"]) > 0


# ═══════════════════════════════════════════════════════════════════
# Feature 4: Profit & Loss Reporting
# ═══════════════════════════════════════════════════════════════════

class TestProfitLoss:
    """Test P&L report generation."""

    def test_pnl_no_sales(self):
        """P&L works with zero sales."""
        from skills.analytics import profit_loss_report
        result = profit_loss_report(days=30)
        assert result["status"] == "success"
        assert result["total_revenue"] == 0
        assert result["gross_profit"] == 0

    def test_pnl_with_sales(self):
        """P&L correctly calculates revenue, COGS, and gross profit."""
        from skills.analytics import profit_loss_report

        _create_and_finalize_bill([("SKU-MILK-1L", 10)])

        result = profit_loss_report(days=30)
        assert result["status"] == "success"
        assert result["total_revenue"] > 0
        assert result["total_cogs"] > 0
        assert result["gross_profit"] > 0
        assert 0 < result["gross_margin_pct"] <= 100
        assert result["bill_count"] >= 1


# ═══════════════════════════════════════════════════════════════════
# Feature 5: Returns & Refund Management
# ═══════════════════════════════════════════════════════════════════

class TestReturns:
    """Test product return processing."""

    def test_return_against_finalized_bill(self):
        """Return successfully reverses stock and records refund."""
        from skills.returns import process_return, list_returns
        from skills.inventory import get_stock

        # Check initial stock
        initial = get_stock("SKU-MILK-1L")
        initial_qty = initial["product"]["quantity"]

        # Create and finalize a bill
        result = _create_and_finalize_bill([("SKU-MILK-1L", 5)])
        bill_id = result["bill_id"]

        # Stock should be reduced by 5
        after_sale = get_stock("SKU-MILK-1L")
        assert after_sale["product"]["quantity"] == initial_qty - 5

        # Process return of 2 units
        ret = process_return(bill_id=bill_id, sku_or_name="SKU-MILK-1L",
                             qty=2, reason="Damaged packaging")
        assert ret["status"] == "success"
        assert ret["qty_returned"] == 2
        assert ret["refund_amount"] > 0
        assert "RET-" in ret["return_id"]

        # Stock should be restored by 2
        after_return = get_stock("SKU-MILK-1L")
        assert after_return["product"]["quantity"] == initial_qty - 3

        # List returns
        returns = list_returns(bill_id=bill_id)
        assert returns["count"] == 1

    def test_return_exceeds_billed_qty(self):
        """Cannot return more than was billed."""
        from skills.returns import process_return

        result = _create_and_finalize_bill([("SKU-MILK-1L", 3)])
        bill_id = result["bill_id"]

        ret = process_return(bill_id=bill_id, sku_or_name="SKU-MILK-1L", qty=10)
        assert ret["status"] == "error"
        assert "Cannot return" in ret["message"]

    def test_return_against_draft_bill_fails(self):
        """Returns only work against finalized bills."""
        from skills.returns import process_return
        from skills.billing import start_bill, add_item_to_bill

        bill = start_bill()
        add_item_to_bill(bill_id=bill["bill_id"], sku_or_name="SKU-MILK-1L", qty=5)

        ret = process_return(bill_id=bill["bill_id"], sku_or_name="SKU-MILK-1L", qty=2)
        assert ret["status"] == "error"
        assert "not finalized" in ret["message"]

    def test_return_wrong_product_fails(self):
        """Cannot return a product not in the bill."""
        from skills.returns import process_return

        result = _create_and_finalize_bill([("SKU-MILK-1L", 3)])
        bill_id = result["bill_id"]

        ret = process_return(bill_id=bill_id, sku_or_name="NONEXISTENT-PRODUCT", qty=1)
        assert ret["status"] == "error"
        assert "not found" in ret["message"]

    def test_double_return_tracked(self):
        """Multiple partial returns are tracked against the same bill."""
        from skills.returns import process_return

        result = _create_and_finalize_bill([("SKU-MILK-1L", 10)])
        bill_id = result["bill_id"]

        # First return: 3 units
        ret1 = process_return(bill_id=bill_id, sku_or_name="SKU-MILK-1L", qty=3)
        assert ret1["status"] == "success"

        # Second return: 5 units (total = 8, still within 10)
        ret2 = process_return(bill_id=bill_id, sku_or_name="SKU-MILK-1L", qty=5)
        assert ret2["status"] == "success"

        # Third return: 5 units (total would = 13, exceeds 10)
        ret3 = process_return(bill_id=bill_id, sku_or_name="SKU-MILK-1L", qty=5)
        assert ret3["status"] == "error"


# ═══════════════════════════════════════════════════════════════════
# Feature 6: Top Customers & Loyalty Insights
# ═══════════════════════════════════════════════════════════════════

class TestCustomerInsights:
    """Test customer ranking and loyalty tier assignment."""

    def test_insights_with_no_sales(self):
        """Customer insights returns empty when no bills are tied to customers."""
        from skills.analytics import customer_insights
        result = customer_insights(days=30, top_n=5)
        assert result["status"] == "success"
        assert result["count"] == 0

    def test_insights_with_customer_sales(self):
        """Customer insights ranks customers by spend."""
        from skills.analytics import customer_insights

        _create_and_finalize_bill([("SKU-MILK-1L", 5)],
                                  payment_mode="cash", customer_name="Ravi Kumar")

        result = customer_insights(days=30, top_n=5)
        assert result["status"] == "success"
        assert result["count"] >= 1
        top = result["customers"][0]
        assert top["total_spend"] > 0
        assert top["visit_count"] >= 1
        assert "loyalty_tier" in top


# ═══════════════════════════════════════════════════════════════════
# Feature 7: WhatsApp-Style Digital Receipt
# ═══════════════════════════════════════════════════════════════════

class TestDigitalReceipt:
    """Test formatted receipt generation."""

    def test_receipt_for_finalized_bill(self):
        """Digital receipt includes all line items and totals."""
        from skills.billing import generate_digital_receipt

        result = _create_and_finalize_bill([("SKU-MILK-1L", 3), ("SKU-RICE-5K", 1)])
        bill_id = result["bill_id"]

        receipt = generate_digital_receipt(bill_id=bill_id)
        assert receipt["status"] == "success"
        assert "RECEIPT" in receipt["receipt"]
        assert bill_id in receipt["receipt"]
        assert "₹" in receipt["receipt"]
        assert "Thank you" in receipt["receipt"]

    def test_receipt_invalid_bill(self):
        """Receipt returns error for nonexistent bill."""
        from skills.billing import generate_digital_receipt
        result = generate_digital_receipt(bill_id="BILL-NONEXISTENT")
        assert result["status"] == "error"


# ═══════════════════════════════════════════════════════════════════
# Tool Registration Verification
# ═══════════════════════════════════════════════════════════════════

class TestNewToolRegistration:
    """Verify all new tools are registered in the agent harness."""

    def test_new_tools_in_dispatch(self):
        """All new feature functions are in TOOL_DISPATCH."""
        from agent.harness import TOOL_DISPATCH
        new_tools = [
            "check_expiring_stock", "check_low_stock_alerts",
            "sales_forecast", "profit_loss_report", "customer_insights",
            "export_gstr1", "process_return", "list_returns",
            "generate_digital_receipt"
        ]
        for tool_name in new_tools:
            assert tool_name in TOOL_DISPATCH, f"{tool_name} missing from TOOL_DISPATCH"
            assert callable(TOOL_DISPATCH[tool_name]), f"{tool_name} is not callable"

    def test_new_tools_in_schema(self):
        """All new feature tools have TOOLS_SCHEMA entries."""
        from agent.harness import TOOLS_SCHEMA
        schema_names = {t["function"]["name"] for t in TOOLS_SCHEMA}
        new_tools = [
            "check_expiring_stock", "check_low_stock_alerts",
            "sales_forecast", "profit_loss_report", "customer_insights",
            "export_gstr1", "process_return", "list_returns",
            "generate_digital_receipt"
        ]
        for tool_name in new_tools:
            assert tool_name in schema_names, f"{tool_name} missing from TOOLS_SCHEMA"

    def test_total_tool_count(self):
        """Verify total registered tool count (was 30, now should be 39)."""
        from agent.harness import TOOL_DISPATCH, TOOLS_SCHEMA
        # Existing 30 tools + 9 new tools = 39
        assert len(TOOL_DISPATCH) >= 39, f"Expected ≥39 tools, got {len(TOOL_DISPATCH)}"
        assert len(TOOLS_SCHEMA) >= 39, f"Expected ≥39 schemas, got {len(TOOLS_SCHEMA)}"
