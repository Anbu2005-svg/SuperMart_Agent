"""
Comprehensive Test Suite for Tier 3 and Tier 4 Supermarket Intelligence Suite:
  - Feature 09: Supplier Ledger & Accounts Payable (Vendor Khata)
  - Feature 10: Purchase Cost & Price Inflation Tracker
  - Feature 11: Bulk CSV / Excel Catalog Import & Export
  - Feature 12: ABC Inventory Classification (Pareto Revenue Analysis)
  - Feature 13: Smart Cross-Sell & Market Basket Recommendations
  - Feature 15: Customer Self-Service Digital Catalog & Offline HTML Generator
  - Feature 16: AI Voice Stock Reconcile & Variance Audit
  - Tool Dispatch Verification in Harness
"""

import os
import uuid
import pytest
from datetime import date, timedelta
from db.models import get_db_connection, immediate_transaction, init_db
from skills import (
    supplier_ledger, price_tracker, catalog_bulk, abc_analysis,
    cross_sell, digital_catalog, stock_reconcile
)
from agent.harness import TOOL_DISPATCH, TOOLS_SCHEMA


@pytest.fixture(scope="module", autouse=True)
def setup_database():
    """Ensure database schema is initialized before running tests."""
    init_db()


def test_feature_09_supplier_ledger():
    """Verify supplier registration, purchase bills, payments, and ledger."""
    unique_name = f"Sri Krishna Traders {uuid.uuid4().hex[:4]}"
    add_res = supplier_ledger.add_supplier(
        name=unique_name,
        phone="9876501234",
        gstin="33AAAAA0000A1Z5",
        company_name="Krishna Wholesale FMCG",
        address="12 Big Bazaar Street, Madurai"
    )
    assert add_res["status"] == "success"
    supplier_id = add_res["supplier"]["supplier_id"]

    # Record purchase bill
    bill_res = supplier_ledger.record_supplier_bill(
        supplier=supplier_id,
        total_amount=5000.0,
        vendor_bill_no=f"INV-{uuid.uuid4().hex[:4]}",
        due_date=str(date.today() + timedelta(days=10)),
        notes="Monthly bulk pulses & rice shipment"
    )
    assert bill_res["status"] == "success"
    bill_id = bill_res["bill"]["bill_id"]

    # Record payment to supplier
    pay_res = supplier_ledger.record_supplier_payment(
        supplier=supplier_id,
        amount=2000.0,
        payment_mode="upi",
        reference_no="UPI-REF-998877",
        bill_id=bill_id,
        notes="Part payment via phonepe"
    )
    assert pay_res["status"] == "success"

    # Get supplier ledger
    ledger = supplier_ledger.get_supplier_ledger(supplier_id)
    assert ledger["status"] == "success"
    assert ledger["summary"]["total_billed"] >= 5000.0
    assert ledger["summary"]["total_paid"] >= 2000.0
    assert ledger["summary"]["balance_payable"] >= 3000.0

    # Get pending payables list
    payables = supplier_ledger.get_pending_payables()
    assert payables["status"] == "success"
    assert payables["total_pending_payables"] > 0


def test_feature_10_price_tracker():
    """Verify price changes, audit trail, and inflation detection."""
    # Find or create a test product
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT sku_id, cost_price, mrp FROM products LIMIT 1;")
    prod = cur.fetchone()
    conn.close()

    assert prod is not None, "Products must exist in test DB"
    sku_id = prod["sku_id"]

    # Log initial lower cost
    price_tracker.log_price_change(sku_id, new_cost_price=40.0, new_selling_price=50.0, source="test_init")

    # Log increased cost (inflation surge)
    update_res = price_tracker.log_price_change(sku_id, new_cost_price=48.0, new_selling_price=52.0, source="supplier_hike")
    assert update_res["status"] == "success"
    assert update_res["new_cost_price"] == 48.0

    # View price history
    hist = price_tracker.get_product_price_history(sku_id)
    assert hist["status"] == "success"
    assert hist["total_revisions"] >= 2

    # Check price inflation
    inf_res = price_tracker.check_price_inflation(days=30, target_margin_pct=20.0)
    assert inf_res["status"] == "success"
    assert "inflated_products_count" in inf_res


def test_feature_11_catalog_bulk():
    """Verify CSV export and tolerant bulk CSV import."""
    # Export catalog to CSV string
    export_res = catalog_bulk.export_catalog_csv()
    assert export_res["status"] == "success"
    assert "sku_id,name,barcode" in export_res["csv_content"]
    assert export_res["total_products"] > 0

    # Test importing new product and updating existing
    sample_sku = f"BULK-{uuid.uuid4().hex[:5]}"
    csv_data = (
        "sku_id,name,category,mrp,cost_price,quantity,unit\n"
        f"{sample_sku},Bulk Organic Honey,Groceries,250.0,180.0,25,bottle\n"
    )
    import_res = catalog_bulk.import_catalog_csv(csv_content=csv_data, mode="upsert")
    assert import_res["status"] == "success"
    assert import_res["inserted"] >= 1

    # Verify inserted
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT name, mrp, quantity FROM products WHERE sku_id = %s", (sample_sku,))
    inserted_item = cur.fetchone()
    conn.close()
    assert inserted_item is not None
    assert inserted_item["name"] == "Bulk Organic Honey"
    assert float(inserted_item["quantity"]) == 25.0


def test_feature_12_abc_analysis():
    """Verify Pareto ABC inventory categorization."""
    abc_res = abc_analysis.compute_abc_classification(days=90)
    assert abc_res["status"] == "success"
    assert "class_a" in abc_res
    assert "class_b" in abc_res
    assert "class_c" in abc_res
    assert abc_res["total_products_evaluated"] > 0
    assert "strategic_recommendations" in abc_res


def test_feature_13_cross_sell():
    """Verify market basket recommendations and fallback suggestions."""
    # Find a product name
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT name FROM products WHERE quantity > 0 LIMIT 1;")
    prod = cur.fetchone()
    conn.close()

    assert prod is not None
    rec_res = cross_sell.get_cross_sell_suggestions(prod["name"], top_n=3)
    assert rec_res["status"] == "success"
    assert "suggestions" in rec_res
    assert len(rec_res["suggestions"]) > 0

    # Top overall market baskets
    baskets = cross_sell.get_top_market_baskets(top_n=5)
    assert baskets["status"] == "success"
    assert "top_pairs" in baskets


def test_feature_15_digital_catalog(tmp_path):
    """Verify categorized digital catalog generation and HTML exporter."""
    cat_res = digital_catalog.generate_digital_catalog(in_stock_only=False)
    assert cat_res["status"] == "success"
    assert cat_res["total_items"] > 0
    assert "🏬" in cat_res["formatted_message"]

    # Export HTML file
    test_html_path = str(tmp_path / "test_catalog.html")
    html_res = digital_catalog.export_html_catalog(file_path=test_html_path)
    assert html_res["status"] == "success"
    assert os.path.exists(test_html_path)
    with open(test_html_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "<title>" in content
    assert "Digital Store Catalog" in content or "Online Catalog" in content


def test_feature_16_stock_reconcile():
    """Verify spoken text stock count parsing, variance analysis, and reconciliation."""
    # Find existing product
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT sku_id, name, quantity FROM products LIMIT 1;")
    prod = cur.fetchone()
    conn.close()

    assert prod is not None
    sku = prod["sku_id"]
    name = prod["name"]
    current_stock = float(prod["quantity"] or 0.0)

    # Simulate voice note transcript: "Counted 45 <Product Name>"
    voice_note = f"Counted 45 {name}"
    audit_res = stock_reconcile.audit_physical_stock(voice_note)
    assert audit_res["status"] == "success"
    assert audit_res["items_audited"] >= 1
    assert "summary" in audit_res

    # Apply reconciliation to synchronize
    reconcile_items = [{"sku_id": sku, "physical_count": 45.0}]
    apply_res = stock_reconcile.apply_stock_reconciliation(reconcile_items, reason="Physical monthly audit test")
    assert apply_res["status"] == "success"
    assert apply_res["reconciled_count"] == 1

    # Verify in DB
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT quantity FROM products WHERE sku_id = %s", (sku,))
    updated_stock = float(cur.fetchone()["quantity"])
    # Revert to original stock to maintain pristine state
    cur.execute("UPDATE products SET quantity = %s WHERE sku_id = %s", (current_stock, sku))
    conn.commit()
    conn.close()


    assert updated_stock == 45.0


def test_harness_tier3_tier4_dispatch():
    """Verify all new tools are registered in harness dispatch and have valid schemas."""
    expected_tools = [
        "add_supplier", "list_suppliers", "record_supplier_bill", "record_supplier_payment",
        "get_supplier_ledger", "get_pending_payables", "log_price_change", "check_price_inflation",
        "get_product_price_history", "export_catalog_csv", "import_catalog_csv",
        "compute_abc_classification", "get_cross_sell_suggestions", "get_top_market_baskets",
        "generate_digital_catalog", "export_html_catalog", "audit_physical_stock",
        "apply_stock_reconciliation"
    ]

    schema_tool_names = {t["function"]["name"] for t in TOOLS_SCHEMA}

    for tool in expected_tools:
        assert tool in TOOL_DISPATCH, f"Tool '{tool}' missing from TOOL_DISPATCH"
        assert callable(TOOL_DISPATCH[tool]), f"Tool '{tool}' in dispatch is not callable"
        assert tool in schema_tool_names, f"Tool '{tool}' missing from TOOLS_SCHEMA"
