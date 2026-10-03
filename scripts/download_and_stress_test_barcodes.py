"""Stress Testing and Extended Benchmark Suite for Barcode Engine.

Tests extended public datasets:
  1. ZXing Extended Symbologies: Code 128 (Logistics), Code 39 (Inventory), PDF417 (2D Document/Shipping)
  2. ZXing Challenging Real-World Packaging: ean13-2 (Reflections, wrinkles, angles)
  3. ZXing False Positives Rejection: falsepositives (Checking if non-barcode images cause phantom scans)
  4. OpenFoodFacts Global Products: 50 Real International FMCG items extracted from local gzip dump.

All test images are physically downloaded and saved to disk in test_assets/.
Database operations use local SQLite clone (ZERO Prisma cloud quota used).
"""

import os
import sys
import csv
import gzip
import time
import httpx
import urllib.request
from pathlib import Path
from typing import List, Dict, Any, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from db.local_clone import get_local_db_connection
import skills.barcode
skills.barcode.get_db_connection = get_local_db_connection

from skills.barcode import scan_barcode_from_image, lookup_product_by_barcode

ASSETS_DIR = PROJECT_ROOT / "test_assets"
STRESS_DIR = ASSETS_DIR / "stress_test_photos"
STRESS_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR = PROJECT_ROOT / "generated_reports"
REPORTS_DIR.mkdir(exist_ok=True)

BASE_RAW = "https://raw.githubusercontent.com/zxing/zxing/master/core/src/test/resources/blackbox"


def download_file(url: str, dest_path: Path) -> bool:
    if dest_path.exists() and dest_path.stat().st_size > 0:
        return True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 SuperMart/2026"})
        with urllib.request.urlopen(req, timeout=12) as response:
            if response.status == 200:
                dest_path.write_bytes(response.read())
                return True
    except Exception as e:
        # print(f"    Download error {url}: {e}")
        return False
    return False


# ==============================================================================
# 1. EXTENDED SYMBOLOGIES (Code 128, Code 39, PDF417)
# ==============================================================================
def test_extended_symbologies() -> Dict[str, Any]:
    print("\n" + "=" * 75)
    print("🏷️  SUITE 1: EXTENDED SYMBOLOGIES (Code 128 Logistics, Code 39 Inventory, PDF417)")
    print("=" * 75)

    targets = [
        ("code128-1", "1.png", "1.txt"),
        ("code128-1", "2.png", "2.txt"),
        ("code128-1", "3.png", "3.txt"),
        ("code39-1", "1.png", "1.txt"),
        ("code39-1", "2.png", "2.txt"),
        ("code39-1", "3.png", "3.txt"),
        ("pdf417-1", "01.png", "01.txt"),
        ("pdf417-1", "02.png", "02.txt"),
        ("pdf417-1", "03.png", "03.txt"),
    ]

    passed = 0
    errors = []
    results = []

    for folder, img_name, txt_name in targets:
        local_img = STRESS_DIR / f"{folder}_{img_name}"
        local_txt = STRESS_DIR / f"{folder}_{txt_name}"

        # Download if needed
        download_file(f"{BASE_RAW}/{folder}/{img_name}", local_img)
        download_file(f"{BASE_RAW}/{folder}/{txt_name}", local_txt)

        if not local_img.exists() or not local_txt.exists():
            errors.append(f"Could not download {folder}/{img_name}")
            continue

        expected = local_txt.read_text(encoding="utf-8").strip()
        img_bytes = local_img.read_bytes()

        t0 = time.time()
        scan = scan_barcode_from_image(img_bytes)
        latency = (time.time() - t0) * 1000

        decoded = scan.get("primary_barcode")
        status = scan.get("status")
        detected_fmt = scan.get("barcodes", [{}])[0].get("format", "UNKNOWN") if scan.get("barcodes") else "None"

        # Check match
        ok = (status == "success" and decoded and (decoded == expected or decoded.strip() == expected.strip()))
        if ok:
            passed += 1
            status_tag = "✅ PASS"
        else:
            status_tag = "❌ FAIL"
            errors.append({
                "suite": "Extended Symbologies",
                "sample": f"{folder}/{img_name}",
                "expected": expected,
                "decoded": decoded,
                "reason": scan.get("message", "Mismatch with ground truth")
            })

        print(f"  {status_tag} | {folder}/{img_name:<8} | Exp: {expected:<18} | Decoded: {str(decoded):<18} | Fmt: {detected_fmt:<10} | {latency:>5.1f}ms")
        results.append({"sample": f"{folder}/{img_name}", "expected": expected, "decoded": decoded, "passed": ok, "latency": latency})

    return {"total": len(targets), "passed": passed, "errors": errors, "results": results}


# ==============================================================================
# 2. CHALLENGING / DISTORTED REAL PACKAGING (EAN13-2)
# ==============================================================================
def test_challenging_packaging() -> Dict[str, Any]:
    print("\n" + "=" * 75)
    print("🔥 SUITE 2: CHALLENGING PACKAGING (ean13-2: Wrinkles, Glare, Curved Bottles)")
    print("=" * 75)

    targets = [
        ("ean13-2", f"{i:02d}.png", f"{i:02d}.txt") for i in range(1, 11)
    ]

    passed = 0
    errors = []
    results = []

    for folder, img_name, txt_name in targets:
        local_img = STRESS_DIR / f"{folder}_{img_name}"
        local_txt = STRESS_DIR / f"{folder}_{txt_name}"

        download_file(f"{BASE_RAW}/{folder}/{img_name}", local_img)
        download_file(f"{BASE_RAW}/{folder}/{txt_name}", local_txt)

        if not local_img.exists() or not local_txt.exists():
            continue

        expected = local_txt.read_text(encoding="utf-8").strip()
        img_bytes = local_img.read_bytes()

        t0 = time.time()
        scan = scan_barcode_from_image(img_bytes)
        latency = (time.time() - t0) * 1000

        decoded = scan.get("primary_barcode")
        status = scan.get("status")
        detected_fmt = scan.get("barcodes", [{}])[0].get("format", "UNKNOWN") if scan.get("barcodes") else "None"

        # Check match (allow leading 0 equivalence for UPC-A in EAN-13)
        matched = (decoded == expected) or (
            decoded and expected and (
                decoded == "0" + expected or
                decoded.lstrip("0") == expected.lstrip("0")
            )
        )
        ok = (status == "success" and matched)
        if ok:
            passed += 1
            status_tag = "✅ PASS"
        else:
            status_tag = "⚠️ MISSED"
            errors.append({
                "suite": "Challenging Packaging (ean13-2)",
                "sample": f"{folder}/{img_name}",
                "expected": expected,
                "decoded": decoded,
                "reason": "Extreme packaging reflection / curve occlusion (No barcode detected)" if not decoded else f"Decoded mismatch ({decoded} != {expected})"
            })

        print(f"  {status_tag} | {folder}/{img_name:<8} | Exp: {expected:<14} | Decoded: {str(decoded):<14} | Fmt: {detected_fmt:<8} | {latency:>5.1f}ms")
        results.append({"sample": f"{folder}/{img_name}", "expected": expected, "decoded": decoded, "passed": ok, "latency": latency})

    return {"total": len(targets), "passed": passed, "errors": errors, "results": results}


# ==============================================================================
# 3. FALSE POSITIVE REJECTION TEST
# ==============================================================================
def test_false_positive_rejection() -> Dict[str, Any]:
    print("\n" + "=" * 75)
    print("🛡️  SUITE 3: ADVERSARIAL FALSE POSITIVE REJECTION (Non-Barcode Images)")
    print("   Goal: The scanner must NOT hallucinate barcodes from plain photos / patterns")
    print("=" * 75)

    targets = [
        ("falsepositives", f"{i:02d}.png") for i in range(1, 9)
    ]

    passed = 0
    errors = []
    results = []

    for folder, img_name in targets:
        local_img = STRESS_DIR / f"{folder}_{img_name}"
        download_file(f"{BASE_RAW}/{folder}/{img_name}", local_img)

        if not local_img.exists():
            continue

        img_bytes = local_img.read_bytes()
        t0 = time.time()
        scan = scan_barcode_from_image(img_bytes)
        latency = (time.time() - t0) * 1000

        # PASS means status is 'not_found' or count is 0! (No hallucinated barcode)
        count = scan.get("count", 0)
        decoded = scan.get("primary_barcode")
        status = scan.get("status")

        rejected_properly = (count == 0 and decoded is None)
        if rejected_properly:
            passed += 1
            status_tag = "✅ PASS"
            verdict = "Correctly Rejected (No False Positive)"
        else:
            status_tag = "❌ FALSE POSITIVE"
            verdict = f"Hallucinated phantom barcode: {decoded}"
            errors.append({
                "suite": "False Positive Rejection",
                "sample": f"{folder}/{img_name}",
                "expected": "None (Rejection)",
                "decoded": decoded,
                "reason": f"Scanner hallucinated phantom barcode '{decoded}' from background texture"
            })

        print(f"  {status_tag} | {folder}/{img_name:<8} | Count: {count} | Decoded: {str(decoded):<12} | {verdict} | {latency:>5.1f}ms")
        results.append({"sample": f"{folder}/{img_name}", "expected": "None", "decoded": decoded, "passed": rejected_properly, "latency": latency})

    return {"total": len(targets), "passed": passed, "errors": errors, "results": results}


# ==============================================================================
# 4. OPENFOODFACTS EXTRACTION & RETAIL METADATA TEST
# ==============================================================================
def test_openfoodfacts_catalog_extraction(target_count: int = 50) -> Dict[str, Any]:
    print("\n" + "=" * 75)
    print(f"🌍 SUITE 4: REAL OPENFOODFACTS GLOBAL PRODUCT CATALOG EXTRACTION ({target_count} Items)")
    print("   Source: Local en.openfoodfacts.org.products.csv.gz (1.27 GB Internet Archive)")
    print("=" * 75)

    gz_path = PROJECT_ROOT / "en.openfoodfacts.org.products.csv.gz"
    out_csv = ASSETS_DIR / f"openfoodfacts_sample_{target_count}.csv"

    extracted_items = []

    if out_csv.exists():
        with open(out_csv, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            extracted_items = list(reader)
        print(f"  📂 Loaded {len(extracted_items)} items from existing {out_csv.name}")
    else:
        print("  ⏳ Extracting 50 verified international items with barcodes from 1.27GB gzip file...")
        with gzip.open(gz_path, "rt", encoding="utf-8", errors="ignore") as f:
            reader = csv.reader(f, delimiter="\t")
            header = next(reader)
            code_idx = header.index("code")
            name_idx = header.index("product_name")
            cat_idx = header.index("categories") if "categories" in header else -1
            brand_idx = header.index("brands") if "brands" in header else -1

            for row in reader:
                if len(row) > max(code_idx, name_idx):
                    code = row[code_idx].strip()
                    name = row[name_idx].strip()
                    brand = row[brand_idx].strip() if brand_idx != -1 and len(row) > brand_idx else ""
                    category = row[cat_idx].strip() if cat_idx != -1 and len(row) > cat_idx else "General"

                    # Only valid GTIN lengths (8, 12, 13) and non-empty clean names
                    if code.isdigit() and len(code) in (8, 12, 13) and len(name) > 3 and not name.startswith("http"):
                        extracted_items.append({
                            "barcode": code,
                            "name": name[:60],
                            "brand": brand[:30] if brand else "Generic",
                            "category": (category.split(",")[0] if category else "Packaged Food")[:30],
                        })
                        if len(extracted_items) >= target_count:
                            break

        # Save to test_assets/
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["barcode", "name", "brand", "category"])
            writer.writeheader()
            writer.writerows(extracted_items)
        print(f"  💾 Saved {len(extracted_items)} extracted international products to {out_csv.name}")

    # Validate against local database catalog lookup / insertion
    conn = get_local_db_connection()
    cur = conn.cursor()
    passed = 0
    errors = []
    results = []

    for idx, item in enumerate(extracted_items, start=1):
        barcode = item["barcode"]
        name = item["name"]
        cat = item["category"]

        # Insert into local clone to test database binding and lookup
        sku = f"SKU-OFF-{idx:03d}"
        cur.execute("""
            INSERT INTO products (sku_id, name, category, unit, cost_price, mrp, gst_slab, quantity, reorder_level, barcode)
            VALUES (%s, %s, %s, 'unit', 50.0, 75.0, 12.0, 25.0, 5.0, %s)
            ON CONFLICT (sku_id) DO UPDATE SET barcode = %s
        """, (sku, name, cat, barcode, barcode))
        conn.commit()

        t0 = time.time()
        lookup = lookup_product_by_barcode(barcode)
        latency = (time.time() - t0) * 1000

        matched = (
            lookup.get("status") == "success"
            and lookup.get("product", {}).get("sku_id") == sku
            and lookup.get("product", {}).get("barcode") == barcode
        )

        if matched:
            passed += 1
            status_tag = "✅ PASS"
        else:
            status_tag = "❌ FAIL"
            errors.append({
                "suite": "OpenFoodFacts Catalog Lookup",
                "sample": barcode,
                "expected": sku,
                "decoded": lookup.get("message", "Lookup failed"),
                "reason": "Database lookup failed for valid GTIN"
            })

        if idx <= 5 or idx % 10 == 0 or idx == len(extracted_items):
            print(f"  {status_tag} | [{idx:>2}/{len(extracted_items)}] {barcode:<14} | {name:<35} | {cat:<18} | {latency:>4.1f}ms")

        # Clean up
        cur.execute("DELETE FROM products WHERE sku_id = %s", (sku,))
        conn.commit()
        results.append({"sample": barcode, "name": name, "passed": matched, "latency": latency})

    conn.close()
    return {"total": len(extracted_items), "passed": passed, "errors": errors, "results": results}


def main():
    print("=" * 75)
    print("🚀 EXTENDED BARCODE TEST DATASET SUITE & ERROR AUDIT")
    print(f"📅 Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 75)

    s1 = test_extended_symbologies()
    s2 = test_challenging_packaging()
    s3 = test_false_positive_rejection()
    s4 = test_openfoodfacts_catalog_extraction(50)

    total_tests = s1["total"] + s2["total"] + s3["total"] + s4["total"]
    total_passed = s1["passed"] + s2["passed"] + s3["passed"] + s4["passed"]
    all_errors = s1["errors"] + s2["errors"] + s3["errors"] + s4["errors"]

    print("\n" + "=" * 75)
    print("📊 OVERALL SUMMARY & ERROR ANALYSIS")
    print("=" * 75)
    print(f"• Suite 1 (Extended Symbologies - Code128, Code39, PDF417) : {s1['passed']}/{s1['total']} Passed ({(s1['passed']/s1['total']*100):.1f}%)")
    print(f"• Suite 2 (Challenging Packaging - ean13-2 Distortions)     : {s2['passed']}/{s2['total']} Passed ({(s2['passed']/s2['total']*100):.1f}%)")
    print(f"• Suite 3 (Adversarial False Positive Rejection)           : {s3['passed']}/{s3['total']} Passed ({(s3['passed']/s3['total']*100):.1f}%)")
    print(f"• Suite 4 (OpenFoodFacts Real Global Products Extracted)   : {s4['passed']}/{s4['total']} Passed ({(s4['passed']/s4['total']*100):.1f}%)")
    print("-" * 75)
    print(f"⭐ TOTAL TESTS : {total_tests}")
    print(f"⭐ TOTAL PASSED: {total_passed} ({(total_passed/total_tests*100):.1f}%)")
    print(f"⭐ TOTAL ERRORS / MISSES: {len(all_errors)}")

    if all_errors:
        print("\n" + "⚠️ " * 15 + " DETAILED ERROR REPORT " + "⚠️ " * 15)
        for idx, err in enumerate(all_errors, start=1):
            if isinstance(err, dict):
                print(f"[{idx}] Suite: {err.get('suite')} | Sample: {err.get('sample')}")
                print(f"    Expected : {err.get('expected')}")
                print(f"    Decoded  : {err.get('decoded')}")
                print(f"    Reason   : {err.get('reason')}\n")
            else:
                print(f"[{idx}] {err}\n")
    else:
        print("\n🎉 ZERO ERRORS! All tests passed flawlessly with 100% precision.")

    # Save summary report
    report_file = REPORTS_DIR / f"extended_stress_test_report_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(f"Extended Barcode Stress Test Report\nGenerated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"Total: {total_tests}, Passed: {total_passed}, Errors: {len(all_errors)}\n\n")
        for err in all_errors:
            f.write(str(err) + "\n")
    print(f"📄 Report written to: {report_file}")


if __name__ == "__main__":
    main()
