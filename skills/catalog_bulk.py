"""
Bulk CSV / Excel Catalog Import & Export Skill.

Empowers store owners to manage thousands of products in bulk:
  - Export the full store inventory into structured CSV format
  - Bulk import or update product prices, stocks, barcodes, and HSN codes
  - Intelligent field resolution and validation
  - Auto-assigns SKUs if missing and reconciles existing barcodes
"""

import io
import os
import csv
import uuid
from typing import Dict, Any, List, Optional
from db.models import get_db_connection, immediate_transaction


def export_catalog_csv(file_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Export all store products to CSV format.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                sku_id, name, barcode, category, mrp, cost_price,
                quantity, unit, hsn_code, gst_slab, reorder_level
            FROM products
            ORDER BY category ASC, name ASC;
        """)
        products = cur.fetchall()

        fieldnames = [
            "sku_id", "name", "barcode", "category", "mrp",
            "cost_price", "quantity", "unit", "hsn_code", "gst_slab", "reorder_level"
        ]

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()

        for p in products:
            writer.writerow({
                "sku_id": p["sku_id"],
                "name": p["name"],
                "barcode": p["barcode"] or "",
                "category": p["category"] or "General",
                "mrp": float(p["mrp"] or 0.0),
                "cost_price": float(p["cost_price"] or 0.0),
                "quantity": float(p["quantity"] or 0.0),
                "unit": p["unit"] or "pcs",
                "hsn_code": p["hsn_code"] or "",
                "gst_slab": float(p["gst_slab"] or 0.0),
                "reorder_level": float(p["reorder_level"] or 5.0)
            })

        csv_text = output.getvalue()
        saved_path = None

        if file_path:
            os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(csv_text)
            saved_path = os.path.abspath(file_path)

        return {
            "status": "success",
            "total_products": len(products),
            "csv_content": csv_text,
            "saved_to_path": saved_path
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to export catalog: {str(e)}"}
    finally:
        conn.close()


def import_catalog_csv(
    csv_content: Optional[str] = None,
    file_path: Optional[str] = None,
    mode: str = "upsert"  # "upsert" or "add_stock"
) -> Dict[str, Any]:
    """
    Bulk import or update products from CSV text or a file path.
    """
    raw_text = csv_content
    if not raw_text and file_path:
        if not os.path.exists(file_path):
            return {"status": "error", "message": f"File '{file_path}' does not exist."}
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            raw_text = f.read()

    if not raw_text or not raw_text.strip():
        return {"status": "error", "message": "No CSV content provided to import."}

    stream = io.StringIO(raw_text.strip())
    reader = csv.DictReader(stream)

    if not reader.fieldnames:
        return {"status": "error", "message": "CSV header row is missing or empty."}

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        inserted_count = 0
        updated_count = 0
        errors = []

        with immediate_transaction(conn):
            for row_idx, row in enumerate(reader, start=2):
                clean_row = {k.strip().lower(): (v.strip() if v else "") for k, v in row.items() if k}

                name = clean_row.get("name") or clean_row.get("product_name") or clean_row.get("item")
                if not name:
                    errors.append(f"Row {row_idx}: Missing product name.")
                    continue

                sku_id = clean_row.get("sku_id") or clean_row.get("sku")
                barcode = clean_row.get("barcode") or clean_row.get("upc") or clean_row.get("ean")
                category = clean_row.get("category") or "General"
                unit = clean_row.get("unit") or "pcs"
                hsn_code = clean_row.get("hsn_code") or clean_row.get("hsn") or ""

                try:
                    mrp = float(clean_row.get("mrp") or clean_row.get("selling_price") or clean_row.get("price") or 0.0)
                    cost_price = float(clean_row.get("cost_price") or clean_row.get("cost") or 0.0)
                    qty = float(clean_row.get("quantity") or clean_row.get("stock_quantity") or clean_row.get("stock") or clean_row.get("qty") or 0.0)
                    gst_slab = float(clean_row.get("gst_slab") or clean_row.get("gst_rate") or clean_row.get("gst") or 0.0)
                    reorder_level = float(clean_row.get("reorder_level") or clean_row.get("min_stock_threshold") or clean_row.get("min_stock") or 5.0)
                except ValueError as ve:
                    errors.append(f"Row {row_idx} ({name}): Invalid numeric value ({str(ve)}).")
                    continue

                # Check if item exists by sku_id, barcode, or exact name
                existing = None
                if sku_id:
                    cur.execute("SELECT sku_id, quantity FROM products WHERE sku_id = %s", (sku_id,))
                    existing = cur.fetchone()
                if not existing and barcode:
                    cur.execute("SELECT sku_id, quantity FROM products WHERE barcode = %s", (barcode,))
                    existing = cur.fetchone()
                if not existing:
                    cur.execute("SELECT sku_id, quantity FROM products WHERE LOWER(name) = LOWER(%s)", (name,))
                    existing = cur.fetchone()

                if existing:
                    target_sku = existing["sku_id"]
                    new_qty = (float(existing["quantity"]) + qty) if mode == "add_stock" else qty
                    cur.execute("""
                        UPDATE products
                        SET name = %s,
                            barcode = COALESCE(NULLIF(%s, ''), barcode),
                            category = %s,
                            mrp = %s,
                            cost_price = %s,
                            quantity = %s,
                            unit = %s,
                            hsn_code = %s,
                            gst_slab = %s,
                            reorder_level = %s
                        WHERE sku_id = %s;
                    """, (name, barcode, category, mrp, cost_price, new_qty, unit, hsn_code, gst_slab, reorder_level, target_sku))
                    updated_count += 1
                else:
                    new_sku = sku_id if sku_id else f"SKU-{uuid.uuid4().hex[:6].upper()}"
                    cur.execute("""
                        INSERT INTO products (
                            sku_id, name, barcode, category, mrp, cost_price,
                            quantity, unit, hsn_code, gst_slab, reorder_level
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """, (new_sku, name, barcode if barcode else None, category, mrp, cost_price, qty, unit, hsn_code, gst_slab, reorder_level))
                    inserted_count += 1

        return {
            "status": "success",
            "message": f"Bulk import complete: {inserted_count} added, {updated_count} updated.",
            "inserted": inserted_count,
            "updated": updated_count,
            "errors_count": len(errors),
            "errors": errors[:10]
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed during CSV import: {str(e)}"}
    finally:
        conn.close()
