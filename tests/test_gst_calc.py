# Pure logic tests — no database required.
NO_DB = True

import pytest
from skills.billing import _calculate_gst, _price_line


def test_gst_calculation_zero_percent():
    res = _calculate_gst(100.0, 0.0)
    assert res["subtotal"] == 100.0
    assert res["cgst"] == 0.0
    assert res["sgst"] == 0.0
    assert res["total_tax"] == 0.0
    assert res["line_total"] == 100.0


def test_gst_calculation_five_percent():
    res = _calculate_gst(200.0, 5.0)
    assert res["subtotal"] == 200.0
    assert res["cgst"] == 5.0
    assert res["sgst"] == 5.0
    assert res["total_tax"] == 10.0
    assert res["line_total"] == 210.0


def test_gst_calculation_twelve_percent():
    res = _calculate_gst(275.0, 12.0)
    assert res["subtotal"] == 275.0
    assert res["cgst"] == 16.5
    assert res["sgst"] == 16.5
    assert res["total_tax"] == 33.0
    assert res["line_total"] == 308.0


def test_gst_calculation_eighteen_percent():
    res = _calculate_gst(14.0, 18.0)  # Maggi 70g
    assert res["subtotal"] == 14.0
    # 14 * 0.18 = 2.52 -> CGST = 1.26, SGST = 1.26
    assert res["cgst"] == 1.26
    assert res["sgst"] == 1.26
    assert res["total_tax"] == 2.52
    assert res["line_total"] == 16.52


def test_gst_rounding_is_rupee_correct():
    """Odd amounts must round CGST/SGST to 2 decimals and the total must balance."""
    res = _calculate_gst(99.99, 18.0)
    assert res["cgst"] == round(99.99 * 0.18 / 2, 2)
    assert res["sgst"] == res["cgst"]
    # Total = subtotal + cgst + sgst exactly (no drift)
    assert res["line_total"] == round(res["subtotal"] + res["cgst"] + res["sgst"], 2)


def test_price_line_packaged_uses_mrp():
    product = {"is_loose": False, "mrp": 14.0, "unit": "packet", "base_unit": "piece",
               "conversion_factor": 1.0, "price_per_base_unit": None}
    res = _price_line(product, 4)
    assert res["unit_price"] == 14.0


def test_price_line_loose_per_kg():
    product = {"is_loose": True, "mrp": 48.0, "unit": "kg", "base_unit": "kg",
               "conversion_factor": 1.0, "price_per_base_unit": 48.0}
    res = _price_line(product, 2)
    assert res["unit_price"] == 48.0  # per kg


def test_price_line_loose_base_conversion():
    """Product sold in grams but priced per kg: unit price must convert (48/1000)."""
    product = {"is_loose": True, "mrp": 48.0, "unit": "g", "base_unit": "kg",
               "conversion_factor": 1000.0, "price_per_base_unit": 48.0}
    res = _price_line(product, 500)
    assert res["unit_price"] == 0.05  # ₹48/kg → ₹0.05/g (rounded 4dp then 2dp)


def test_price_line_loose_without_base_price_falls_back_to_mrp():
    product = {"is_loose": True, "mrp": 80.0, "unit": "kg", "base_unit": "kg",
               "conversion_factor": 1.0, "price_per_base_unit": None}
    res = _price_line(product, 3)
    assert res["unit_price"] == 80.0
