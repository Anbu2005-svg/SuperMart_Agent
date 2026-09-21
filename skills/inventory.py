import math
import re
import uuid
from typing import Dict, Any, List, Optional
from db.models import get_db_connection, immediate_transaction
from skills.audit import _log_event


def clarify_product_match(matches: List[Dict[str, Any]], query: str) -> Dict[str, Any]:
    """
    Disambiguation tool: presents multiple product matches to the user.
    The agent MUST call this instead of guessing when multiple products match.
    """
    if not matches:
        return {"status": "error", "message": "No matches provided to clarify."}
    match_list = "\n".join([
        f"• {m.get('name')} [{m.get('sku_id')}] – MRP: ₹{m.get('mrp')} | Stock: {m.get('quantity')} {m.get('unit', '')}"
        for m in matches
    ])
    return {
        "status": "clarification_required",
        "message": f"Found multiple products matching '{query}'. Please specify which brand/variety you want:\n{match_list}",
        "matches": matches,
        "original_query": query
    }


def get_stock(query: str) -> Dict[str, Any]:
    """Get stock information for a product by SKU ID or product name search."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Search by exact SKU first
        cur.execute("SELECT * FROM products WHERE sku_id = %s", (query.strip(),))
        product = cur.fetchone()

        if not product:
            # Search by name (fuzzy case-insensitive using ILIKE)
            cur.execute("SELECT * FROM products WHERE name ILIKE %s AND is_active = TRUE ORDER BY name ASC LIMIT 5", (f"%{query.strip()}%",))
            products = cur.fetchall()
            if not products:
                return {"status": "error", "message": f"No product found matching '{query}'"}
            if len(products) > 1:
                matches = [{
                    "sku_id": p["sku_id"],
                    "name": p["name"],
                    "quantity": p["quantity"],
                    "mrp": p["mrp"],
                    "unit": p["unit"],
                    "is_loose": p["is_loose"],
                    "base_unit": p.get("base_unit") or "piece",
                    "conversion_factor": p.get("conversion_factor") or 1.0,
                    "price_per_base_unit": p.get("price_per_base_unit")
                } for p in products]
                return {
                    "status": "multiple_matches",
                    "message": f"Found multiple products matching '{query}'. Please specify which brand/variety:",
                    "matches": matches
                }
            product = products[0]

        cur.close()
        return {
            "status": "success",
            "product": {
                "sku_id": product["sku_id"],
                "name": product["name"],
                "category": product["category"],
                "unit": product["unit"],
                "base_unit": product.get("base_unit") or "piece",
                "conversion_factor": product.get("conversion_factor") or 1.0,
                "is_loose": product["is_loose"],
                "cost_price": product["cost_price"],
                "mrp": product["mrp"],
                "price_per_base_unit": product.get("price_per_base_unit"),
                "gst_slab": product["gst_slab"],
                "hsn_code": product["hsn_code"],
                "quantity": product["quantity"],
                "reorder_level": product["reorder_level"],
                "is_low_stock": product["quantity"] <= product["reorder_level"]
            }
        }
    finally:
        conn.close()


def receive_stock(sku_id: str, qty: float, cost_price: Optional[float] = None, mrp: Optional[float] = None,
                  price_per_base_unit: Optional[float] = None,
                  batch_code: Optional[str] = None, expiry_date: Optional[str] = None) -> Dict[str, Any]:
    """
    Receive inventory stock (increases stock quantity).
    Optionally updates cost_price / MRP / price_per_base_unit.
    Optionally records a batch with expiry date (enables FEFO consumption on sale).
    `expiry_date` format: YYYY-MM-DD.
    """
    if not isinstance(qty, (int, float)) or not math.isfinite(qty) or qty <= 0:
        return {"status": "error", "message": "Received quantity must be positive"}
    if cost_price is not None and (not isinstance(cost_price, (int, float)) or not math.isfinite(cost_price) or cost_price < 0):
        return {"status": "error", "message": "Cost price must be a non-negative finite number"}
    if mrp is not None and (not isinstance(mrp, (int, float)) or not math.isfinite(mrp) or mrp <= 0):
        return {"status": "error", "message": "MRP must be a positive finite number"}
    if price_per_base_unit is not None and (not isinstance(price_per_base_unit, (int, float)) or not math.isfinite(price_per_base_unit) or price_per_base_unit <= 0):
        return {"status": "error", "message": "Price per base unit must be a positive finite number"}

    parsed_expiry = None
    if expiry_date:
        from datetime import datetime
        try:
            parsed_expiry = datetime.strptime(expiry_date.strip(), "%Y-%m-%d").date().isoformat()
        except ValueError:
            return {"status": "error", "message": "Invalid expiry_date format. Expected YYYY-MM-DD."}

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("SELECT * FROM products WHERE sku_id = %s FOR UPDATE", (sku_id.strip(),))
            product = cur.fetchone()
            if not product:
                cur.execute("SELECT * FROM products WHERE name ILIKE %s AND is_active = TRUE FOR UPDATE", (f"%{sku_id.strip()}%",))
                product = cur.fetchone()
                if not product:
                    return {"status": "error", "message": f"Product SKU '{sku_id}' not found. Add the product first."}

            real_sku = product["sku_id"]
            new_qty = product["quantity"] + qty
            new_cost = cost_price if cost_price is not None else product["cost_price"]
            new_mrp = mrp if mrp is not None else product["mrp"]
            new_price_per_base = price_per_base_unit if price_per_base_unit is not None else product.get("price_per_base_unit")

            if new_cost > new_mrp:
                return {"status": "error", "message": f"Cost price ({new_cost}) cannot exceed MRP ({new_mrp})"}

            if product.get("is_loose") and new_price_per_base is not None:
                conv = product.get("conversion_factor") or 1.0
                if new_price_per_base * conv > new_mrp + 0.01:
                    return {"status": "error", "message": "Price per base unit × conversion factor exceeds MRP"}

            cur.execute("""
                UPDATE products
                SET quantity = %s, cost_price = %s, mrp = %s, price_per_base_unit = %s, updated_at = CURRENT_TIMESTAMP
                WHERE sku_id = %s
            """, (new_qty, new_cost, new_mrp, new_price_per_base, real_sku))

            # Batch / expiry tracking (FEFO)
            batch_info = None
            if batch_code and batch_code.strip():
                batch_code_clean = batch_code.strip()
                cur.execute("""
                    INSERT INTO stock_batches (sku_id, batch_code, qty_received, qty_remaining, cost_price, expiry_date)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (sku_id, batch_code) DO UPDATE SET
                        qty_received = stock_batches.qty_received + EXCLUDED.qty_received,
                        qty_remaining = stock_batches.qty_remaining + EXCLUDED.qty_remaining,
                        expiry_date = COALESCE(EXCLUDED.expiry_date, stock_batches.expiry_date),
                        cost_price = EXCLUDED.cost_price
                    RETURNING batch_id
                """, (real_sku, batch_code_clean, qty, qty, new_cost, parsed_expiry))
                batch_info = {"batch_id": cur.fetchone()["batch_id"], "batch_code": batch_code_clean, "expiry_date": parsed_expiry}

            _log_event(conn, "STOCK_RECEIVED", "product", real_sku,
                       details={"product_name": product["name"], "qty_received": qty,
                                "cost_price": new_cost, "mrp": new_mrp, "price_per_base_unit": new_price_per_base,
                                "batch_code": batch_code_clean if batch_code else None, "expiry_date": parsed_expiry},
                       old_value=product["quantity"], new_value=new_qty)
            cur.close()

        return {
            "status": "success",
            "message": f"Received {qty} {product['unit']} of {product['name']}. New quantity: {new_qty}",
            "sku_id": real_sku,
            "name": product["name"],
            "previous_quantity": product["quantity"],
            "new_quantity": new_qty,
            "cost_price": new_cost,
            "mrp": new_mrp,
            "price_per_base_unit": new_price_per_base,
            "batch": batch_info
        }
    finally:
        conn.close()


def add_product(
    name: str,
    category: str,
    unit: str,
    is_loose: bool,
    cost_price: float,
    mrp: float,
    gst_slab: float,
    hsn_code: str,
    quantity: float = 0.0,
    reorder_level: float = 10.0,
    sku_id: Optional[str] = None,
    base_unit: Optional[str] = None,
    conversion_factor: float = 1.0,
    price_per_base_unit: Optional[float] = None
) -> Dict[str, Any]:
    """Add a new product SKU to the catalog. Supports loose items with base_unit pricing."""
    if not isinstance(cost_price, (int, float)) or not math.isfinite(cost_price) or cost_price < 0:
        return {"status": "error", "message": "Cost price must be a non-negative finite number"}
    if not isinstance(mrp, (int, float)) or not math.isfinite(mrp) or mrp <= 0:
        return {"status": "error", "message": "MRP must be a positive finite number"}
    if cost_price > mrp:
        return {"status": "error", "message": f"Cost price ({cost_price}) cannot exceed MRP ({mrp})"}
    if not isinstance(quantity, (int, float)) or not math.isfinite(quantity) or quantity < 0:
        return {"status": "error", "message": "Quantity must be a non-negative finite number"}
    if not isinstance(reorder_level, (int, float)) or not math.isfinite(reorder_level) or reorder_level < 0:
        return {"status": "error", "message": "Reorder level must be a non-negative finite number"}
    if gst_slab not in [0, 5, 12, 18, 28]:
        return {"status": "error", "message": "GST slab must be one of: 0, 5, 12, 18, 28"}
    if not hsn_code or not hsn_code.strip():
        return {"status": "error", "message": "HSN code is required"}
    if not re.fullmatch(r"[0-9]{2,8}", hsn_code.strip()):
        return {"status": "error", "message": "HSN code must be 2-8 digits (e.g. '1701' for sugar)"}

    valid_units = ["kg", "g", "litre", "ml", "packet", "piece", "dozen"]
    unit_clean = unit.strip().lower()
    if unit_clean not in valid_units:
        return {"status": "error", "message": f"Invalid unit. Must be one of: {', '.join(valid_units)}"}

    # Base unit defaults to the selling unit (e.g. loose sugar sold in kg → base kg)
    base_unit_clean = (base_unit.strip().lower() if base_unit else unit_clean)
    if base_unit_clean not in valid_units:
        return {"status": "error", "message": f"Invalid base_unit. Must be one of: {', '.join(valid_units)}"}
    if not isinstance(conversion_factor, (int, float)) or not math.isfinite(conversion_factor) or conversion_factor <= 0:
        return {"status": "error", "message": "Conversion factor must be a positive finite number"}

    if price_per_base_unit is not None:
        if not isinstance(price_per_base_unit, (int, float)) or not math.isfinite(price_per_base_unit) or price_per_base_unit <= 0:
            return {"status": "error", "message": "Price per base unit must be a positive finite number"}
        if price_per_base_unit * conversion_factor > mrp + 0.01:
            return {"status": "error", "message": "Price per base unit × conversion factor exceeds MRP"}

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            # Check if product with exact or case-insensitive name already exists
            cur.execute("SELECT * FROM products WHERE name ILIKE %s", (name.strip(),))
            existing = cur.fetchone()
            if existing:
                target_sku = existing["sku_id"]
                new_qty = existing["quantity"] + quantity
                cur.execute("""
                    UPDATE products
                    SET category = %s, unit = %s, base_unit = %s, conversion_factor = %s, is_loose = %s,
                        cost_price = %s, mrp = %s, price_per_base_unit = %s, gst_slab = %s, hsn_code = %s,
                        quantity = %s, reorder_level = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE sku_id = %s
                """, (category.strip(), unit_clean, base_unit_clean, conversion_factor, is_loose,
                      cost_price, mrp, price_per_base_unit, gst_slab, hsn_code.strip(), new_qty, reorder_level, target_sku))
                _log_event(conn, "PRODUCT_UPDATED", "product", target_sku,
                           details={"name": name, "mrp": mrp}, old_value=existing["quantity"], new_value=new_qty)
                cur.close()
                return {
                    "status": "success",
                    "message": f"Updated existing product '{name}' (SKU: {target_sku}). New quantity: {new_qty}",
                    "sku_id": target_sku,
                    "name": name,
                    "mrp": mrp,
                    "quantity": new_qty
                }

            generated_sku = sku_id.strip() if sku_id else f"SKU-{uuid.uuid4().hex[:6].upper()}"
            cur.execute("""
                INSERT INTO products (sku_id, name, category, unit, base_unit, conversion_factor, is_loose,
                                     cost_price, mrp, price_per_base_unit, gst_slab, hsn_code, quantity, reorder_level)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (generated_sku, name.strip(), category.strip(), unit_clean, base_unit_clean,
                  conversion_factor, is_loose, cost_price, mrp, price_per_base_unit, gst_slab,
                  hsn_code.strip(), quantity, reorder_level))

            _log_event(conn, "PRODUCT_ADDED", "product", generated_sku,
                       details={"name": name, "category": category, "mrp": mrp, "gst_slab": gst_slab,
                                "is_loose": is_loose, "base_unit": base_unit_clean, "conversion_factor": conversion_factor},
                       old_value=0, new_value=quantity)
            cur.close()

            return {
                "status": "success",
                "message": f"Product '{name}' added successfully with SKU: {generated_sku}",
                "sku_id": generated_sku,
                "name": name,
                "mrp": mrp,
                "quantity": quantity
            }
    except Exception as e:
        return {"status": "error", "message": f"Failed to add product: {str(e)}"}
    finally:
        conn.close()


def archive_product(sku_or_name: str) -> Dict[str, Any]:
    """
    Soft-delete (archive) a product. Archived products are hidden from search/listing/billing
    but their history (bills, audit trail) is preserved. Stock data is retained, not deleted.
    """
    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("SELECT * FROM products WHERE (sku_id = %s OR name ILIKE %s) AND is_active = TRUE",
                        (sku_or_name.strip(), sku_or_name.strip()))
            product = cur.fetchone()
            if not product:
                return {"status": "error", "message": f"No active product matching '{sku_or_name}' found."}

            cur.execute("""
                UPDATE products SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP
                WHERE sku_id = %s
            """, (product["sku_id"],))

            _log_event(conn, "PRODUCT_ARCHIVED", "product", product["sku_id"],
                       details={"name": product["name"], "quantity_at_archive": product["quantity"]},
                       old_value=product["quantity"], new_value=None)
            cur.close()

        return {
            "status": "success",
            "message": f"Product '{product['name']}' archived. It is hidden from billing but history is preserved. Stock retained: {product['quantity']} {product['unit']}.",
            "sku_id": product["sku_id"],
            "name": product["name"]
        }
    finally:
        conn.close()


def list_low_stock() -> Dict[str, Any]:
    """List all products where current stock level is less than or equal to reorder level."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM products WHERE is_active = TRUE AND quantity <= reorder_level ORDER BY quantity ASC")
        products = cur.fetchall()
        cur.close()

        items = [{
            "sku_id": p["sku_id"],
            "name": p["name"],
            "quantity": p["quantity"],
            "unit": p["unit"],
            "reorder_level": p["reorder_level"]
        } for p in products]

        return {"status": "success", "count": len(items), "low_stock_items": items}
    finally:
        conn.close()


def list_all_products(category: Optional[str] = None, limit: int = 100, offset: int = 0,
                      include_inactive: bool = False) -> Dict[str, Any]:
    """List available products with stock levels, MRPs, units, categories. Paginated."""
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if include_inactive:
            base_where = ""
            params = []
        else:
            base_where = "WHERE is_active = TRUE "
            params = []

        if category:
            base_where += ("WHERE " if not base_where.strip() else "AND ") + "category ILIKE %s "
            params.append(f"%{category.strip()}%")

        cur.execute(f"SELECT * FROM products {base_where}ORDER BY category ASC, name ASC LIMIT %s OFFSET %s",
                    (*params, limit, offset))
        products = cur.fetchall()

        # Strip trailing WHERE/AND for count query
        count_clause = base_where.strip()
        if count_clause.startswith("AND"):
            count_clause = "WHERE " + count_clause[3:].strip()
        cur.execute(f"SELECT COUNT(*) AS count FROM products {count_clause}".rstrip(), params)
        total_count = cur.fetchone()["count"]
        cur.close()

        items = [{
            "sku_id": p["sku_id"],
            "name": p["name"],
            "category": p["category"],
            "quantity": p["quantity"],
            "unit": p["unit"],
            "base_unit": p.get("base_unit") or "piece",
            "conversion_factor": p.get("conversion_factor") or 1.0,
            "is_loose": p["is_loose"],
            "mrp": p["mrp"],
            "cost_price": p["cost_price"],
            "price_per_base_unit": p.get("price_per_base_unit"),
            "gst_slab": p["gst_slab"],
            "hsn_code": p["hsn_code"],
            "is_low_stock": p["quantity"] <= p["reorder_level"],
            "is_active": p.get("is_active", True)
        } for p in products]

        return {"status": "success", "count": len(items), "total": total_count, "products": items}
    finally:
        conn.close()


def search_products(query: str, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """Search product catalog by query string. Paginated."""
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        like = f"%{query.strip()}%"
        cur.execute("""
            SELECT * FROM products
            WHERE is_active = TRUE AND (name ILIKE %s OR category ILIKE %s OR sku_id ILIKE %s)
            ORDER BY name ASC LIMIT %s OFFSET %s
        """, (like, like, like, limit, offset))
        products = cur.fetchall()

        cur.execute("""
            SELECT COUNT(*) AS count FROM products
            WHERE is_active = TRUE AND (name ILIKE %s OR category ILIKE %s OR sku_id ILIKE %s)
        """, (like, like, like))
        total_count = cur.fetchone()["count"]
        cur.close()

        items = [{
            "sku_id": p["sku_id"],
            "name": p["name"],
            "category": p["category"],
            "quantity": p["quantity"],
            "unit": p["unit"],
            "base_unit": p.get("base_unit") or "piece",
            "conversion_factor": p.get("conversion_factor") or 1.0,
            "is_loose": p["is_loose"],
            "mrp": p["mrp"],
            "cost_price": p["cost_price"],
            "price_per_base_unit": p.get("price_per_base_unit"),
            "gst_slab": p["gst_slab"],
            "hsn_code": p["hsn_code"]
        } for p in products]

        return {"status": "success", "count": len(items), "total": total_count, "products": items}
    finally:
        conn.close()


def get_product_count() -> int:
    """Return total number of active products in current shop inventory database."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS count FROM products WHERE is_active = TRUE")
        row = cur.fetchone()
        cur.close()
        return row["count"] if row else 0
    except Exception:
        return 0
    finally:
        conn.close()


def populate_default_inventory() -> Dict[str, Any]:
    """Populate empty inventory with the 12 standard problem statement stock items and sample customers."""
    from db.seed import seed_database
    try:
        seed_database()
        count = get_product_count()
        return {
            "status": "success",
            "message": f"Successfully auto-populated inventory with {count} default problem statement stock items and sample customers!",
            "product_count": count
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to populate default stocks: {str(e)}"}


def expiring_stock(within_days: int = 30) -> Dict[str, Any]:
    """
    FEFO intelligence: list batches expiring within `within_days` days (or already expired),
    ordered by expiry date (soonest first). Also flags items with no batch tracking.
    """
    within_days = max(1, min(365, int(within_days)))
    from datetime import date, timedelta
    cutoff = (date.today() + timedelta(days=within_days)).isoformat()

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT sb.sku_id, p.name, p.unit, sb.batch_code, sb.qty_remaining,
                   sb.expiry_date, sb.received_at
            FROM stock_batches sb
            JOIN products p ON sb.sku_id = p.sku_id
            WHERE sb.qty_remaining > 0 AND sb.expiry_date IS NOT NULL
              AND sb.expiry_date <= %s::date
            ORDER BY sb.expiry_date ASC
        """, (cutoff,))
        rows = cur.fetchall()

        today = date.today()
        batches = []
        for r in rows:
            exp = r["expiry_date"]
            expired = exp < today if exp else False
            days_left = (exp - today).days if exp else None
            batches.append({
                "sku_id": r["sku_id"],
                "name": r["name"],
                "unit": r["unit"],
                "batch_code": r["batch_code"],
                "qty_remaining": r["qty_remaining"],
                "expiry_date": str(exp) if exp else None,
                "days_left": days_left,
                "status": "EXPIRED" if expired else ("CRITICAL" if days_left is not None and days_left <= 7 else "EXPIRING_SOON")
            })

        # Products with stock but zero batch coverage (no expiry visibility)
        cur.execute("""
            SELECT p.sku_id, p.name, p.quantity, p.unit
            FROM products p
            WHERE p.is_active = TRUE AND p.quantity > 0
              AND NOT EXISTS (SELECT 1 FROM stock_batches sb WHERE sb.sku_id = p.sku_id AND sb.qty_remaining > 0)
            ORDER BY p.name ASC
        """)
        untracked = cur.fetchall()
        cur.close()

        return {
            "status": "success",
            "count": len(batches),
            "message": f"{len(batches)} batch(es) expiring within {within_days} days. FEFO ordering is active — these are consumed first on sale.",
            "expiring_batches": batches,
            "untracked_items": [{
                "sku_id": u["sku_id"], "name": u["name"],
                "quantity": u["quantity"], "unit": u["unit"]
            } for u in untracked]
        }
    finally:
        conn.close()


def update_gst_slab(
    new_gst_slab: float,
    sku_or_name: Optional[str] = None,
    category: Optional[str] = None,
    hsn_code: Optional[str] = None
) -> Dict[str, Any]:
    """
    Update GST slab for products based on SKU/product name, category, or HSN code.
    Useful when government updates GST slabs for specific items, categories, or HSN codes.
    """
    if new_gst_slab not in [0, 5, 12, 18, 28]:
        return {"status": "error", "message": "GST slab must be one of standard rates: 0, 5, 12, 18, 28"}

    if not sku_or_name and not category and not hsn_code:
        return {"status": "error", "message": "Specify at least one target: sku_or_name, category, or hsn_code"}

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            query_conditions = []
            params = []

            if sku_or_name:
                query_conditions.append("(sku_id ILIKE %s OR name ILIKE %s)")
                params.extend([f"%{sku_or_name.strip()}%", f"%{sku_or_name.strip()}%"])
            if category:
                query_conditions.append("category ILIKE %s")
                params.append(f"%{category.strip()}%")
            if hsn_code:
                query_conditions.append("hsn_code = %s")
                params.append(hsn_code.strip())

            where_clause = " AND ".join(query_conditions)

            cur.execute(f"SELECT sku_id, name, gst_slab FROM products WHERE is_active = TRUE AND {where_clause}", tuple(params))
            products = cur.fetchall()

            if not products:
                return {"status": "error", "message": "No matching products found to update GST slab."}

            updated_items = []
            for p in products:
                old_slab = p["gst_slab"]
                cur.execute("UPDATE products SET gst_slab = %s, updated_at = CURRENT_TIMESTAMP WHERE sku_id = %s", (new_gst_slab, p["sku_id"]))
                _log_event(conn, "GST_SLAB_UPDATED", "product", p["sku_id"],
                           details={"name": p["name"], "old_gst": old_slab, "new_gst": new_gst_slab},
                           old_value=old_slab, new_value=new_gst_slab)
                updated_items.append(f"• {p['name']} [{p['sku_id']}]: GST {old_slab}% ➔ {new_gst_slab}%")

            cur.close()

            return {
                "status": "success",
                "message": f"Successfully updated GST slab to {new_gst_slab}% for {len(updated_items)} product(s).",
                "updated_count": len(updated_items),
                "new_gst_slab": new_gst_slab,
                "updated_items": updated_items
            }
    finally:
        conn.close()
