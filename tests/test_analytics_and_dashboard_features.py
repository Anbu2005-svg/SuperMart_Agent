import os
import pytest
from skills.seasonal_demand import get_seasonal_demand_insights, get_upcoming_festival_projections
from skills.demand_anomaly import detect_sales_anomalies
from skills.dashboard import get_dashboard_summary, export_dashboard_html


def test_seasonal_demand_insights():
    # 1. Test Diwali profile
    diwali = get_seasonal_demand_insights(festival_or_season="diwali")
    assert diwali["status"] == "success"
    assert "Diwali" in diwali["festival_season"]
    assert "typical_sales_lift" in diwali
    assert diwali["total_items_analyzed"] >= 1

    # 2. Test Pongal profile
    pongal = get_seasonal_demand_insights(festival_or_season="pongal")
    assert pongal["status"] == "success"
    assert "Pongal" in pongal["festival_season"]

    # 3. Upcoming festival calendar overview
    calendar = get_upcoming_festival_projections()
    assert calendar["status"] == "success"
    assert calendar["festivals_count"] >= 5


def test_demand_anomaly_detection():
    # Test anomaly detection engine execution
    res = detect_sales_anomalies(days=14, z_threshold=2.0)
    assert res["status"] == "success"
    assert "total_products_monitored" in res
    assert "anomalies_detected_count" in res
    assert isinstance(res["anomalies"], list)


def test_executive_web_dashboard():
    # 1. Test dashboard data compilation
    summary = get_dashboard_summary()
    assert summary["status"] == "success"
    assert "today_kpis" in summary
    assert "cash_drawer" in summary
    assert "payment_modes" in summary

    # 2. Test HTML dashboard generation
    export_res = export_dashboard_html()
    assert export_res["status"] == "success"
    file_path = export_res["file_path"]
    assert os.path.exists(file_path)
    assert os.path.getsize(file_path) > 1000

    with open(file_path, "r", encoding="utf-8") as f:
        html = f.read()
    assert "SuperMart AI Operations Command" in html
    assert "Today's Revenue" in html
    assert "LIVE SYSTEM" in html
