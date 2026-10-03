"""Offline Comprehensive Barcode Test Suite using Local Cloned Database.

Zero calls to Prisma Cloud — runs 100% locally against supermarket_local.db.
Tests all 3 categories requested:
  1. GS1 Standard Modulo-10 Checksum & Integrity Validation
  2. Indian Supermarket Retail Barcodes (890... prefix) on Local Cloned DB
  3. Real Barcode Image & QR Code Decoding with Image Preprocessing (zxing-cpp)
"""

import os
import sys
import csv
import time
import datetime
from pathlib import Path
from typing import List, Dict, Any, Tuple
from PIL import Image, ImageEnhance

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from db.local_clone import get_local_db_connection, local_immediate_transaction
import skills.barcode
# Patch skills.barcode to use the local cloned database
skills.barcode.get_db_connection = get_local_db_connection

from skills.barcode import (
    scan_barcode_from_image,
    lookup_product_by_barcode,
    assign_barcode_to_product,
    quick_bill_by_barcode,
    generate_barcode_image,
)

REPORTS_DIR = PROJECT_ROOT / "generated_reports"
REPORTS_DIR.mkdir(exist_ok=True)


# ==============================================================================
# 1. GS1 Modulo-10 Standard Checksum Verification
# ==============================================================================
def calculate_gs1_check_digit(number_without_check: str) -> int:
    """Calculates the official GS1 Modulo-10 check digit for any standard barcode length."""
    digits = [int(d) for d in number_without_check if d.isdigit()]
    reversed_digits = digits[::-1]
    total = 0
    for idx, d in enumerate(reversed_digits):
        weight = 3 if idx % 2 == 0 else 1
        total += d * weight
    remainder = total % 10
    return (10 - remainder) % 10


def make_gs1_ean13(prefix12: str) -> str:
    """Generates a valid 13-digit EAN with exact GS1 check digit."""
    cd = calculate_gs1_check_digit(prefix12[:12])
    return prefix12[:12] + str(cd)


def verify_gs1_barcode(barcode: str) -> bool:
    """Validates barcode format and GS1 modulo-10 checksum."""
    if not barcode.isdigit() or len(barcode) not in (8, 12, 13, 14):
        return False
    body = barcode[:-1]
    expected_check = calculate_gs1_check_digit(body)
    return int(barcode[-1]) == expected_check


def test_gs1_compliance() -> Dict[str, Any]:
    print("\n" + "=" * 65, flush=True)
    print("🧪 SUITE 1: GS1 MODULO-10 CHECKSUM & INTEGRITY VALIDATION", flush=True)
    print("=" * 65, flush=True)

    test_vectors = [
        # Authentic valid GS1 test vectors
        (make_gs1_ean13("890172513123"), "Aashirvaad Atta 5kg (EAN-13, India)", True),
        (make_gs1_ean13("890103038345"), "Tata Iodized Salt 1kg (EAN-13, India)", True),
        (make_gs1_ean13("890105885230"), "Maggi 2-Minute Noodles (EAN-13, India)", True),
        (make_gs1_ean13("890171910101"), "Parle-G Biscuits (EAN-13, India)", True),
        ("5000159484695", "Snickers Chocolate Bar (EAN-13, UK)", True),
        ("737628064502", "Thai Kitchen Rice Noodles (UPC-A, US)", True),
        ("041196100105", "HEB Organics Whole Milk (UPC-A, US)", True),
        ("96385074", "KitKat Mini (EAN-8, Global)", True),
        # Corrupted / invalid check digits (must be detected as invalid)
        ("8901725131239", "Aashirvaad Atta with corrupted check digit 9", False),
        ("8901030383451", "Tata Salt with corrupted check digit 1", False),
        ("737628064509", "Thai Kitchen Noodles with corrupted check digit 9", False),
        ("12345675", "Corrupted EAN-8 check digit (expecting 0)", False),
    ]

    passed = 0
    for code, desc, expected in test_vectors:
        actual = verify_gs1_barcode(code)
        ok = actual == expected
        status_tag = "✅ PASS" if ok else "❌ FAIL"
        if ok:
            passed += 1
        print(f"  {status_tag} | {code:<14} | Valid: {str(actual):<5} (Exp: {str(expected):<5}) | {desc}", flush=True)

    print(f"\n🎯 GS1 Compliance Score: {passed}/{len(test_vectors)} passed ({(passed/len(test_vectors))*100:.1f}%)", flush=True)
    return {"passed": passed, "total": len(test_vectors)}


# ==============================================================================
# 2. Indian Supermarket Retail Barcodes (890... prefix) on Local Cloned DB
# ==============================================================================
def get_indian_retail_catalog() -> List[Tuple[str, str, str]]:
    """Returns curated Indian supermarket products with GS1 890... barcodes."""
    prefixes = [
        ("890103038345", "Tata Iodized Salt 1kg", "Pantry"),
        ("890105885230", "Maggi 2-Minute Noodles", "Snacks"),
        ("890171910101", "Parle-G Glucose Biscuits", "Biscuits"),
        ("890103001501", "Surf Excel Easy Wash 1kg", "Detergent"),
        ("890103082501", "Brooke Bond Red Label Tea", "Beverages"),
        ("890172513123", "Aashirvaad Atta 5kg", "Grains"),
        ("890126201020", "Amul Butter 100g", "Dairy"),
        ("890600728001", "Fortune Sunflower Oil 1L", "Edible Oils"),
        ("890126201005", "Amul Taaza Toned Milk 1L", "Dairy"),
        ("890100000001", "Madhur Pure Crystal Sugar", "Pantry Basics"),
        ("890100000002", "Daawat Traditional Basmati", "Grains"),
        ("890100000003", "Tata Sampann Unpolished Dal", "Pulses"),
        ("890149900814", "Haldirams Nagpur Aloo Bhujia", "Snacks"),
        ("890120701011", "Dabur Red Ayurvedic Paste", "Personal Care"),
        ("890106301222", "Britannia Good Day Butter", "Biscuits"),
        ("890101211111", "Colgate Strong Teeth 100g", "Personal Care"),
        ("890103000000", "Lifebuoy Total Soap 125g", "Personal Care"),
        ("890123302456", "Vim Dishwash Gel 500ml", "Household"),
        ("890105886600", "Nescafe Classic Coffee 50g", "Beverages"),
        ("890257910001", "MDH Deggi Mirch Powder 100g", "Spices"),
    ]
    catalog = []
    for p, name, cat in prefixes:
        full_barcode = make_gs1_ean13(p)
        catalog.append((full_barcode, name, cat))
    return catalog


def test_indian_retail_barcodes_on_local_db() -> Dict[str, Any]:
    print("\n" + "=" * 65, flush=True)
    print("🇮🇳 SUITE 2: INDIAN RETAIL BARCODES (890...) ON LOCAL CLONED DB", flush=True)
    print("• Database Engine: Local SQLite (supermarket_local.db — ZERO Prisma calls)", flush=True)
    print("=" * 65, flush=True)

    indian_items = get_indian_retail_catalog()
    print(f"✅ Loaded {len(indian_items)} Indian supermarket items (prefix 890...).", flush=True)
    print("🚀 Executing local DB validation (Insert -> Assign -> Lookup -> Conflict -> Cleanup)...", flush=True)

    conn = get_local_db_connection()
    success = 0
    t_start = time.time()

    for idx, (barcode, name, category) in enumerate(indian_items, start=1):
        cur = conn.cursor()
        cur.execute("SELECT sku_id, name, barcode FROM products WHERE barcode = %s", (barcode,))
        existing = cur.fetchone()

        if existing:
            # 1. Product is already permanently seeded: test lookup directly
            lookup_res = lookup_product_by_barcode(barcode)
            lookup_ok = lookup_res.get("status") == "success" and lookup_res.get("product", {}).get("barcode") == barcode

            # 2. Test conflict guard: attempting to reassign this barcode to a different SKU must be blocked
            temp_sku = f"SKU-TEMP-DUP-{idx}"
            cur.execute("""
                INSERT INTO products (sku_id, name, category, unit, cost_price, mrp, quantity, reorder_level)
                VALUES (%s, 'Duplicate Attempt', 'Testing', 'packet', 10, 15, 10, 2)
            """, (temp_sku,))
            conn.commit()
            dup_res = assign_barcode_to_product(temp_sku, barcode)
            cur.execute("DELETE FROM products WHERE sku_id = %s", (temp_sku,))
            conn.commit()

            conflict_ok = dup_res.get("status") == "error"
            item_ok = lookup_ok and conflict_ok
            sku_tag = existing["sku_id"]
        else:
            # 2. New product: create, assign, lookup, verify conflict guard, cleanup
            sku = f"SKU-IND-{idx:04d}"
            cur.execute("""
                INSERT INTO products (sku_id, name, category, unit, cost_price, mrp, quantity, reorder_level)
                VALUES (%s, %s, %s, 'packet', 40.0, 50.0, 25.0, 5.0)
            """, (sku, name, category))
            conn.commit()

            assign_res = assign_barcode_to_product(sku, barcode)
            lookup_res = lookup_product_by_barcode(barcode)

            # Test conflict
            dup_sku = f"SKU-CONFLICT-{idx}"
            cur.execute("""
                INSERT INTO products (sku_id, name, category, unit, cost_price, mrp, quantity, reorder_level)
                VALUES (%s, 'Conflict Test', 'General', 'packet', 10, 15, 10, 2)
            """, (dup_sku,))
            conn.commit()
            conflict_res = assign_barcode_to_product(dup_sku, barcode)

            # Cleanup
            cur.execute("DELETE FROM products WHERE sku_id IN (%s, %s)", (sku, dup_sku))
            conn.commit()

            lookup_ok = lookup_res.get("status") == "success" and lookup_res.get("product", {}).get("sku_id") == sku
            conflict_ok = conflict_res.get("status") == "error"
            item_ok = assign_res.get("status") == "success" and lookup_ok and conflict_ok
            sku_tag = sku

        if item_ok:
            success += 1
            print(f"  ✅ [{idx:>2}/{len(indian_items)}] Barcode: {barcode} | {name:<30} ({sku_tag}) | Verified", flush=True)
        else:
            print(f"  ❌ [{idx:>2}/{len(indian_items)}] Barcode: {barcode} | Failed: {lookup_res.get('message')}", flush=True)

    conn.close()
    elapsed = time.time() - t_start
    rate = len(indian_items) / elapsed if elapsed > 0 else 0

    print(f"\n🎯 Indian Retail Results:", flush=True)
    print(f"• Tested: {len(indian_items)} | Passed: {success} ({(success/len(indian_items))*100:.1f}%)", flush=True)
    print(f"• Execution Time: {elapsed:.2f}s (Speed: {rate:.1f} local ops/sec)", flush=True)
    print(f"• Prisma Operations Consumed: 0 (100% Offline)", flush=True)
    return {"passed": success, "total": len(indian_items)}


# ==============================================================================
# 3. Real Barcode Image & QR Code Decoding with Image Preprocessing
# ==============================================================================
def test_barcode_image_decoding() -> Dict[str, Any]:
    print("\n" + "=" * 65, flush=True)
    print("📸 SUITE 3: BARCODE IMAGE & QR DECODING WITH PREPROCESSING", flush=True)
    print("• Computer Vision Engine: zxing-cpp + Pillow (PIL)", flush=True)
    print("• Preprocessing Tested   : Grayscale, 2.0x Contrast Enhancer, Lanczos Downsampling", flush=True)
    print("=" * 65, flush=True)

    test_cases = [
        # (label, text, format_name, apply_filter)
        ("EAN-13 Indian Product", make_gs1_ean13("890172513123"), "EAN13", None),
        ("EAN-13 Tata Salt", make_gs1_ean13("890103038345"), "EAN13", None),
        ("EAN-13 Maggi Noodles", make_gs1_ean13("890105885230"), "EAN13", None),
        ("Code 128 Invoice Number", "INV-2026-0926-01", "CODE128", None),
        ("Code 128 Warehouse SKU", "SKU-ATTA-AASHIRVAAD-5KG", "CODE128", None),
        ("UPI Payment QR Code", "upi://pay?pa=supermart@okaxis&am=245.00", "QR", None),
        ("GS1 Digital Link QR", "https://id.gs1.org/01/08901725131234/10/BATCH2026", "QR", None),
        # Distorted image test: phone camera low-light / dimmed
        ("Low-Light Phone Photo EAN-13", make_gs1_ean13("890103001501"), "EAN13", "dimmed"),
    ]

    passed = 0
    for label, text, fmt_name, distortion in test_cases:
        gen = generate_barcode_image(text, fmt_name)
        if gen.get("status") != "success":
            print(f"  ❌ FAIL | {label:<30} | Generation Error: {gen.get('message')}", flush=True)
            continue

        img_path = gen["file_path"]

        # Apply simulation filter if requested
        if distortion == "dimmed":
            pil_img = Image.open(img_path)
            enhancer = ImageEnhance.Brightness(pil_img)
            distorted = enhancer.enhance(0.5)  # 50% darker phone shadow
            distorted.save(img_path)

        # Run vision decoding
        scan = scan_barcode_from_image(img_path)
        status = scan.get("status")
        detected = scan.get("primary_barcode")
        ok = (status == "success" and detected == text)

        status_tag = "✅ PASS" if ok else "❌ FAIL"
        if ok:
            passed += 1

        print(f"  {status_tag} | {label:<30} | Decoded: {detected:<38} | Format: {fmt_name}", flush=True)

    print(f"\n🎯 Image Vision Decoding Score: {passed}/{len(test_cases)} passed ({(passed/len(test_cases))*100:.1f}%)", flush=True)
    return {"passed": passed, "total": len(test_cases)}


# ==============================================================================
# Master Benchmark Runner
# ==============================================================================
def main():
    print("=" * 65, flush=True)
    print("🚀 SUPERMART LOCAL OFFLINE BARCODE MASTER TEST RUNNER", flush=True)
    print("• Target Mode: 100% LOCAL & OFFLINE (ZERO Prisma Cloud operations)", flush=True)
    print("• Cloned DB  : supermarket_local.db", flush=True)
    print("=" * 65, flush=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = REPORTS_DIR / f"offline_barcode_master_report_{timestamp}.csv"

    r1 = test_gs1_compliance()
    r2 = test_indian_retail_barcodes_on_local_db()
    r3 = test_barcode_image_decoding()

    total_tests = r1["total"] + r2["total"] + r3["total"]
    total_passed = r1["passed"] + r2["passed"] + r3["passed"]
    overall_pct = (total_passed / total_tests * 100) if total_tests > 0 else 0

    with open(report_file, "w", newline="", encoding="utf-8") as f_out:
        writer = csv.writer(f_out)
        writer.writerow(["suite", "tests_executed", "tests_passed", "pass_percentage", "prisma_queries"])
        writer.writerow(["GS1 Checksum Compliance", r1["total"], r1["passed"], f"{(r1['passed']/r1['total'])*100:.1f}%", 0])
        writer.writerow(["Indian Retail (890...)", r2["total"], r2["passed"], f"{(r2['passed']/r2['total'])*100:.1f}%", 0])
        writer.writerow(["Image & QR Vision Decoding", r3["total"], r3["passed"], f"{(r3['passed']/r3['total'])*100:.1f}%", 0])
        writer.writerow(["TOTAL", total_tests, total_passed, f"{overall_pct:.1f}%", 0])

    print("\n" + "=" * 65, flush=True)
    print("🏆 FINAL COMPREHENSIVE TEST RESULTS", flush=True)
    print(f"• Suite 1 (GS1 Modulo-10 Checksum Tests) : {r1['passed']}/{r1['total']} Passed ({(r1['passed']/r1['total'])*100:.1f}%)", flush=True)
    print(f"• Suite 2 (Indian Retail 890... on Local): {r2['passed']}/{r2['total']} Passed ({(r2['passed']/r2['total'])*100:.1f}%)", flush=True)
    print(f"• Suite 3 (Image & QR Vision Decoding)   : {r3['passed']}/{r3['total']} Passed ({(r3['passed']/r3['total'])*100:.1f}%)", flush=True)
    print("-" * 65, flush=True)
    print(f"• Overall Combined Result                : {total_passed}/{total_tests} ({overall_pct:.1f}% Passed)", flush=True)
    print(f"• Total Prisma Cloud Queries Consumed    : 0 (ZERO)", flush=True)
    print(f"• Detailed Audit Report Saved At         : {report_file}", flush=True)
    print("=" * 65, flush=True)


if __name__ == "__main__":
    main()
