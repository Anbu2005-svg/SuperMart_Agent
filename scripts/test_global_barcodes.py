"""Bulk validation of real-world global barcodes using the OpenFoodFacts local dataset.

Reads directly from en.openfoodfacts.org.products.csv.gz (streamed via gzip).
Uses a multithreaded worker pool (max_workers=6) matching PostgreSQL pool limits.
For each product:
  1. Inserts a temporary test SKU in PostgreSQL.
  2. Maps and validates the barcode using skills.barcode.assign_barcode_to_product.
  3. Queries and verifies full product data using skills.barcode.lookup_product_by_barcode.
  4. Deletes the temporary test SKU to leave the database clean.
  5. Records execution metrics into a CSV report in generated_reports/.
"""

import os
import sys
import csv
import gzip
import time
import argparse
import datetime
import threading
from pathlib import Path
from typing import List, Dict, Any, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

# Reconfigure stdout/stderr for Windows console unicode support
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Tell pytest not to collect this benchmark script as a unit test
__test__ = False

from db.models import get_db_connection, immediate_transaction
from skills.barcode import assign_barcode_to_product, lookup_product_by_barcode

DEFAULT_GZ_PATH = PROJECT_ROOT / "en.openfoodfacts.org.products.csv.gz"
REPORTS_DIR = PROJECT_ROOT / "generated_reports"
REPORTS_DIR.mkdir(exist_ok=True)


def stream_barcodes_from_gz(gz_path: Path, max_count: int) -> List[Tuple[str, str, str]]:
    """Stream valid (barcode, product_name, category) tuples from OpenFoodFacts gzip TSV."""
    if not gz_path.exists():
        raise FileNotFoundError(f"OpenFoodFacts archive not found at: {gz_path}")

    items: List[Tuple[str, str, str]] = []
    seen_barcodes = set()

    with gzip.open(gz_path, "rt", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f, delimiter="\t")
        try:
            header = next(reader)
        except StopIteration:
            return []

        col_map = {col.strip().lower(): idx for idx, col in enumerate(header)}
        code_idx = col_map.get("code", 0)
        name_idx = col_map.get("product_name", -1)
        cat_idx = col_map.get("categories", -1)

        for row in reader:
            if len(row) <= code_idx:
                continue

            code = row[code_idx].strip()
            # Standard numeric barcodes with length between 8 and 14 digits
            if not code or not code.isdigit() or len(code) < 8 or len(code) > 14:
                continue

            if code in seen_barcodes:
                continue

            name = ""
            if name_idx != -1 and len(row) > name_idx:
                name = row[name_idx].strip()
            if not name:
                name = f"Global Product {code}"
            name = name[:90]

            cat = "General"
            if cat_idx != -1 and len(row) > cat_idx and row[cat_idx].strip():
                cat = row[cat_idx].split(",")[0].strip()[:40]

            seen_barcodes.add(code)
            items.append((code, name, cat))

            if len(items) >= max_count:
                break

    return items


def test_single_barcode(item_data: Tuple[int, str, str, str]) -> Dict[str, Any]:
    """Worker task: create temp product, assign, lookup, verify, delete."""
    idx, barcode, name, category = item_data
    sku = f"SKU-OFF-{idx:06d}"

    conn = get_db_connection()
    try:
        # 1. Create temporary product
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO products (sku_id, name, category, unit, is_loose,
                                      cost_price, mrp, gst_slab, hsn_code,
                                      quantity, reorder_level, barcode)
                VALUES (%s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, NULL)
                ON CONFLICT (sku_id) DO UPDATE SET barcode = NULL
                """,
                (sku, name, category, "unit", False, 80.0, 100.0, 18.0, "2106", 50.0, 10.0),
            )
            cur.close()
        conn.commit()

        # 2. Assign barcode via skill
        assign_res = assign_barcode_to_product(sku, barcode)
        assign_ok = assign_res.get("status") == "success"

        # 3. Lookup product by barcode
        t_start = time.time()
        lookup_res = lookup_product_by_barcode(barcode)
        lookup_ms = (time.time() - t_start) * 1000
        lookup_ok = lookup_res.get("status") == "success"

        retrieved_sku = lookup_res.get("product", {}).get("sku_id")
        verified = assign_ok and lookup_ok and (retrieved_sku == sku)

        # 4. Clean up temporary record
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("DELETE FROM products WHERE sku_id = %s", (sku,))
            cur.close()
        conn.commit()

        return {
            "index": idx,
            "barcode": barcode,
            "name": name,
            "category": category,
            "sku": sku,
            "assign_status": assign_res.get("status"),
            "lookup_status": lookup_res.get("status"),
            "verified": verified,
            "lookup_ms": round(lookup_ms, 2),
            "error": None,
        }

    except Exception as e:
        # Emergency cleanup
        try:
            with immediate_transaction(conn):
                cur = conn.cursor()
                cur.execute("DELETE FROM products WHERE sku_id = %s", (sku,))
                cur.close()
            conn.commit()
        except Exception:
            pass

        return {
            "index": idx,
            "barcode": barcode,
            "name": name,
            "category": category,
            "sku": sku,
            "assign_status": "error",
            "lookup_status": "error",
            "verified": False,
            "lookup_ms": 0.0,
            "error": str(e),
        }
    finally:
        conn.close()


def cleanup_all_temp_skus() -> None:
    """Ensure no leftover test SKUs in products table."""
    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("DELETE FROM products WHERE sku_id LIKE 'SKU-OFF-%' OR sku_id LIKE 'SKU-TEST-%'")
            count = cur.rowcount
            cur.close()
        conn.commit()
        if count > 0:
            print(f"🧹 Cleaned up {count} leftover test product(s).")
    finally:
        conn.close()


def run_benchmark(sample_size: int = 1000, gz_file: Path = DEFAULT_GZ_PATH, workers: int = 6) -> None:
    print("=" * 65)
    print("📦 SuperMart Global Barcode Benchmark")
    print(f"• Dataset Source : {gz_file.name}")
    print(f"• Target Samples : {sample_size:,} real-world products")
    print(f"• Concurrency    : {workers} parallel database workers")
    print("=" * 65)

    cleanup_all_temp_skus()

    print(f"🔍 Streaming {sample_size:,} distinct barcodes from OpenFoodFacts dump...")
    t0 = time.time()
    barcode_items = stream_barcodes_from_gz(gz_file, sample_size)
    t_extract = time.time() - t0

    if not barcode_items:
        print("[ERROR] No valid barcodes found in dataset.")
        return

    print(f"✅ Extracted {len(barcode_items):,} distinct products in {t_extract:.2f}s.")
    print("🚀 Running live database validations in parallel...")

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = REPORTS_DIR / f"barcode_global_test_{timestamp}.csv"

    task_payloads = [
        (idx, item[0], item[1], item[2])
        for idx, item in enumerate(barcode_items, start=1)
    ]

    success_count = 0
    fail_count = 0
    processed_count = 0
    t_bench_start = time.time()

    write_lock = threading.Lock()

    with open(report_file, "w", newline="", encoding="utf-8") as f_out:
        writer = csv.writer(f_out)
        writer.writerow([
            "index",
            "barcode",
            "product_name",
            "category",
            "sku_id",
            "assign_status",
            "lookup_status",
            "verified",
            "lookup_time_ms",
            "error",
        ])

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_item = {executor.submit(test_single_barcode, p): p for p in task_payloads}

            for future in as_completed(future_to_item):
                res = future.result()
                processed_count += 1

                if res["verified"]:
                    success_count += 1
                else:
                    fail_count += 1

                with write_lock:
                    writer.writerow([
                        res["index"],
                        res["barcode"],
                        res["name"],
                        res["category"],
                        res["sku"],
                        res["assign_status"],
                        res["lookup_status"],
                        res["verified"],
                        res["lookup_ms"],
                        res.get("error") or "",
                    ])

                if processed_count % 50 == 0 or processed_count == len(barcode_items):
                    elapsed = time.time() - t_bench_start
                    speed = processed_count / elapsed if elapsed > 0 else 0
                    print(
                        f"  [{processed_count:>5}/{len(barcode_items)}] "
                        f"({processed_count/len(barcode_items)*100:>5.1f}%) | "
                        f"Passed: {success_count} | Failed: {fail_count} | "
                        f"Speed: {speed:.1f} ops/sec"
                    )

    cleanup_all_temp_skus()

    total_time = time.time() - t_bench_start
    avg_speed = len(barcode_items) / total_time if total_time > 0 else 0
    accuracy = (success_count / len(barcode_items) * 100) if barcode_items else 0

    print("\n" + "=" * 65)
    print("🎯 GLOBAL BARCODE VALIDATION SUMMARY")
    print(f"• Products Tested       : {len(barcode_items):,}")
    print(f"• Successfully Verified : {success_count:,} ({accuracy:.2f}%)")
    print(f"• Failed / Conflicts    : {fail_count:,}")
    print(f"• Total Elapsed Time    : {total_time:.2f} seconds")
    print(f"• Average Throughput    : {avg_speed:.1f} validations/sec")
    print(f"• Report Generated At   : {report_file}")
    print("=" * 65)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test global barcodes from OpenFoodFacts")
    parser.add_argument("--count", type=int, default=1000, help="Number of barcodes to test (default: 1000)")
    parser.add_argument("--file", type=str, default=str(DEFAULT_GZ_PATH), help="Path to products csv.gz file")
    parser.add_argument("--workers", type=int, default=6, help="Parallel worker threads (default: 6)")
    args = parser.parse_args()

    run_benchmark(sample_size=args.count, gz_file=Path(args.file), workers=args.workers)
