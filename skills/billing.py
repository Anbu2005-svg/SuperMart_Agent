import math
import uuid
from typing import Dict, Any, List, Optional
from db.models import get_db_connection, immediate_transaction
from skills.audit import _log_event

# Unit conversion helpers for loose-item billing (kg <-> g, litre <-> ml)
_UNIT_CONVERSIONS = {
    ("kg", "g"): 1000.0,
    ("g", "kg"): 0.001,
    ("litre", "ml"): 1000.0,
    ("ml", "litre"): 0.001,
}


def _calculate_gst(line_subtotal: float, gst_slab: float) -> Dict[str, float]:
    """
    Pure function for GST calculation.
    Returns dict with rounded line_subtotal, cgst, sgst, line_gst, and line_total.
    Intra-state GST split: CGST = SGST = (line_subtotal * slab / 100) / 2.
    """
    subtotal = round(line_subtotal, 2)
    gst_total = subtotal * (gst_slab / 100.0)
    cgst = round(gst_total / 2.0, 2)
    sgst = round(gst_total / 2.0, 2)
    total_tax = cgst + sgst
    line_total = round(subtotal + total_tax, 2)

    return {
        "subtotal": subtotal,
        "cgst": cgst,
        "sgst": sgst,
        "total_tax": total_tax,
        "line_total": line_total
    }


def start_bill(customer_name: Optional[str] = None) -> Dict[str, Any]:
    """Start a new draft bill. Optionally associate with a customer name."""
    conn = get_db_connection()
    try:
        bill_id = f"BILL-{uuid.uuid4().hex[:8].upper()}"
        customer_id = None
        with immediate_transaction(conn):
            cur = conn.cursor()
            if customer_name:
                cur.execute("SELECT customer_id FROM customers WHERE name ILIKE %s", (f"%{customer_name.strip()}%",))
                cust = cur.fetchone()
                if cust:
                    customer_id = cust["customer_id"]
                else:
                    cur.execute("INSERT INTO customers (name) VALUES (%s) RETURNING customer_id", (customer_name.strip(),))
                    customer_id = cur.fetchone()["customer_id"]

            cur.execute("""
                INSERT INTO bills (bill_id, status, customer_id, subtotal, cgst, sgst, total)
                VALUES (%s, 'draft', %s, 0.0, 0.0, 0.0, 0.0)
            """, (bill_id, customer_id))

            _log_event(conn, "BILL_CREATED", "bill", bill_id,
                       details={"customer_name": customer_name})
            cur.close()

        return {
            "status": "success",
            "message": f"Draft bill created successfully with ID: {bill_id}",
            "bill_id": bill_id,
            "customer_name": customer_name
        }
    finally:
        conn.close()


def _resolve_sku(conn, sku_or_name: str) -> Dict[str, Any]:
    """Helper to resolve SKU ID or product name to a product record or multiple matches."""
    cur = conn.cursor()
    # 1. Search by exact SKU ID
    cur.execute("SELECT * FROM products WHERE sku_id = %s AND is_active = TRUE", (sku_or_name.strip(),))
    product = cur.fetchone()
    if product:
        cur.close()
        return {"status": "single", "product": product}

    # 2. Search by exact name (case-insensitive)
    cur.execute("SELECT * FROM products WHERE name ILIKE %s AND is_active = TRUE", (sku_or_name.strip(),))
    product = cur.fetchone()
    if product:
        cur.close()
        return {"status": "single", "product": product}

    # 3. Search by substring/prefix
    cur.execute("SELECT * FROM products WHERE name ILIKE %s AND is_active = TRUE ORDER BY name ASC", (f"%{sku_or_name.strip()}%",))
    matches = cur.fetchall()
    cur.close()

    if not matches:
        return {"status": "not_found", "product": None}
    if len(matches) == 1:
        return {"status": "single", "product": matches[0]}

    return {"status": "multiple", "matches": matches, "product": None}


def _price_line(product: Dict[str, Any], qty: float) -> Dict[str, Any]:
    """
    Compute unit_price (price for ONE unit of the product's `unit`) and the line subtotal.

    Loose items with price_per_base_unit set:
      unit_price = price_per_base_unit converted into product.unit terms.
      e.g. sugar priced ₹48/kg sold in 'kg' units -> unit_price = 48.0.
           If a product's unit is 'g' but base pricing is per 'kg', unit_price = 48/1000.

    Packaged items: unit_price = MRP (price of one packet/piece/dozen).
    """
    is_loose = bool(product.get("is_loose", False))
    price_per_base = product.get("price_per_base_unit")
    unit = (product.get("unit") or "piece").lower()
    base_unit = (product.get("base_unit") or unit).lower()

    if is_loose and price_per_base is not None:
        unit_price = float(price_per_base)
        if unit != base_unit:
            # _UNIT_CONVERSIONS[(a, b)] = how many b-units are in ONE a-unit
            # e.g. ("kg", "g") = 1000  → one kg is 1000 g
            factor = _UNIT_CONVERSIONS.get((base_unit, unit))
            if factor is not None:
                # price per ONE base-unit → price per ONE selling-unit:
                # one selling unit = 1/factor base units, so divide.
                unit_price = round(float(price_per_base) / factor, 4)
        return {"unit_price": round(unit_price, 2), "price_basis": f"₹{price_per_base}/{base_unit}"}

    return {"unit_price": float(product["mrp"]), "price_basis": "MRP per unit"}


def add_item_to_bill(bill_id: str, sku_or_name: str, qty: float) -> Dict[str, Any]:
    """Add an item to a draft bill. Performs stock warning check and cost price guard check."""
    if not isinstance(qty, (int, float)) or not math.isfinite(qty) or qty <= 0:
        return {"status": "error", "message": "Item quantity must be a positive finite number"}

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("SELECT * FROM bills WHERE bill_id = %s", (bill_id.strip(),))
            bill = cur.fetchone()
            if not bill:
                return {"status": "error", "message": f"Bill '{bill_id}' not found."}
            if bill["status"] != "draft":
                return {"status": "error", "message": f"Bill '{bill_id}' is already {bill['status']} and cannot be edited."}

            res_sku = _resolve_sku(conn, sku_or_name)
            if res_sku["status"] == "not_found":
                return {"status": "error", "message": f"Product matching '{sku_or_name}' not found."}
            if res_sku["status"] == "multiple":
                matches = res_sku["matches"]
                match_list = [f"• {p['name']} [{p['sku_id']}] – MRP: ₹{p['mrp']} | Stock: {p['quantity']} {p['unit']}" for p in matches]
                return {
                    "status": "multiple_matches",
                    "message": f"Found multiple products matching '{sku_or_name}'. Please specify which brand/variety you want:\n" + "\n".join(match_list),
                    "matches": [{"sku_id": p["sku_id"], "name": p["name"], "mrp": p["mrp"], "quantity": p["quantity"], "unit": p["unit"]} for p in matches]
                }

            product = res_sku["product"]

            # Lock the product row so concurrent drafts / stock receipts see a consistent quantity
            cur.execute("SELECT quantity FROM products WHERE sku_id = %s FOR UPDATE", (product["sku_id"],))
            locked = cur.fetchone()
            if not locked:
                return {"status": "error", "message": f"Product '{product['name']}' not found after lock."}
            available_qty = locked["quantity"]

            # Oversell soft check during draft addition
            if qty > available_qty:
                return {
                    "status": "oversell_warning",
                    "message": f"Cannot add {qty} {product['unit']} of {product['name']}. Only {available_qty} available in stock.",
                    "available_stock": available_qty,
                    "requested_qty": qty
                }

            pricing = _price_line(product, qty)
            unit_price = pricing["unit_price"]

            # Below-cost guard check
            if unit_price < product["cost_price"]:
                return {
                    "status": "error",
                    "message": f"Selling price ({unit_price}) is below cost price ({product['cost_price']}) for product '{product['name']}'."
                }

            line_subtotal = round(qty * unit_price, 2)
            gst_info = _calculate_gst(line_subtotal, product["gst_slab"])

            # Check if item already exists in bill
            cur.execute("SELECT * FROM bill_items WHERE bill_id = %s AND sku_id = %s", (bill_id.strip(), product["sku_id"]))
            existing = cur.fetchone()

            if existing:
                new_qty = existing["qty"] + qty
                if new_qty > available_qty:
                    return {
                        "status": "oversell_warning",
                        "message": f"Updating total item qty to {new_qty} exceeds available stock ({available_qty}).",
                        "available_stock": available_qty
                    }
                new_subtotal = round(new_qty * unit_price, 2)
                new_gst = _calculate_gst(new_subtotal, product["gst_slab"])
                cur.execute("""
                    UPDATE bill_items
                    SET qty = %s, unit_price = %s, line_total = %s
                    WHERE id = %s
                """, (new_qty, unit_price, new_gst["line_total"], existing["id"]))
            else:
                cur.execute("""
                    INSERT INTO bill_items (bill_id, sku_id, qty, unit_price, gst_slab, line_total)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (bill_id.strip(), product["sku_id"], qty, unit_price, product["gst_slab"], gst_info["line_total"]))

            effective_qty = (existing["qty"] + qty) if existing else qty
            _log_event(conn, "ITEM_ADDED", "bill", bill_id,
                       details={"product_name": product["name"], "sku_id": product["sku_id"],
                                "qty": effective_qty, "unit_price": unit_price,
                                "price_basis": pricing["price_basis"]})
            cur.close()

        return {
            "status": "success",
            "message": f"Added {qty} {product['unit']} of {product['name']} to bill {bill_id}.",
            "bill_id": bill_id,
            "product_name": product["name"],
            "qty": qty,
            "unit_price": unit_price,
            "price_basis": pricing["price_basis"],
            "line_total": gst_info["line_total"]
        }
    finally:
        conn.close()


def remove_item_from_bill(bill_id: str, sku_or_name: str) -> Dict[str, Any]:
    """Remove a line item from a draft bill."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM bills WHERE bill_id = %s", (bill_id.strip(),))
        bill = cur.fetchone()
        if not bill or bill["status"] != "draft":
            return {"status": "error", "message": f"Bill '{bill_id}' not found or not in draft state."}

        res_sku = _resolve_sku(conn, sku_or_name)
        product = res_sku.get("product") if res_sku.get("status") == "single" else (res_sku.get("matches")[0] if res_sku.get("matches") else None)
        if not product:
            return {"status": "error", "message": f"Product matching '{sku_or_name}' not found."}

        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("DELETE FROM bill_items WHERE bill_id = %s AND sku_id = %s", (bill_id.strip(), product["sku_id"]))
            _log_event(conn, "ITEM_REMOVED", "bill", bill_id,
                       details={"product_name": product["name"], "sku_id": product["sku_id"]})
            cur.close()

        return {"status": "success", "message": f"Removed '{product['name']}' from bill {bill_id}."}
    finally:
        conn.close()


def edit_item_qty(bill_id: str, sku_or_name: str, new_qty: float) -> Dict[str, Any]:
    """Edit the quantity of an existing line item in a draft bill."""
    if not isinstance(new_qty, (int, float)) or not math.isfinite(new_qty):
        return {"status": "error", "message": "New quantity must be a valid finite number"}
    if new_qty <= 0:
        return remove_item_from_bill(bill_id, sku_or_name)

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("SELECT * FROM bills WHERE bill_id = %s", (bill_id.strip(),))
            bill = cur.fetchone()
            if not bill or bill["status"] != "draft":
                return {"status": "error", "message": f"Bill '{bill_id}' not found or not in draft state."}

            res_sku = _resolve_sku(conn, sku_or_name)
            product = res_sku.get("product") if res_sku.get("status") == "single" else (res_sku.get("matches")[0] if res_sku.get("matches") else None)
            if not product:
                return {"status": "error", "message": f"Product matching '{sku_or_name}' not found."}

            # Lock the product row for this transaction
            cur.execute("SELECT quantity FROM products WHERE sku_id = %s FOR UPDATE", (product["sku_id"],))
            locked = cur.fetchone()
            if not locked:
                return {"status": "error", "message": f"Product '{product['name']}' not found after lock."}
            available_qty = locked["quantity"]

            if new_qty > available_qty:
                return {
                    "status": "oversell_warning",
                    "message": f"Requested quantity {new_qty} exceeds available stock ({available_qty}).",
                    "available_stock": available_qty
                }

            pricing = _price_line(product, new_qty)
            unit_price = pricing["unit_price"]

            new_subtotal = round(new_qty * unit_price, 2)
            gst_info = _calculate_gst(new_subtotal, product["gst_slab"])

            cur.execute("SELECT qty FROM bill_items WHERE bill_id = %s AND sku_id = %s",
                        (bill_id.strip(), product["sku_id"]))
            existing = cur.fetchone()
            if not existing:
                return {"status": "error", "message": f"Item '{product['name']}' not found in bill {bill_id}."}

            cur.execute("""
                UPDATE bill_items
                SET qty = %s, unit_price = %s, line_total = %s
                WHERE bill_id = %s AND sku_id = %s
            """, (new_qty, unit_price, gst_info["line_total"], bill_id.strip(), product["sku_id"]))

            _log_event(conn, "ITEM_QTY_UPDATED", "bill", bill_id,
                       details={"product_name": product["name"], "sku_id": product["sku_id"],
                                "unit_price": unit_price},
                       old_value=existing["qty"], new_value=new_qty)
            cur.close()

        return {
            "status": "success",
            "message": f"Updated quantity of '{product['name']}' to {new_qty} in bill {bill_id}.",
            "new_qty": new_qty,
            "unit_price": unit_price,
            "price_basis": pricing["price_basis"],
            "line_total": gst_info["line_total"]
        }
    finally:
        conn.close()


def preview_bill(bill_id: str) -> Dict[str, Any]:
    """Preview bill calculations (subtotal, CGST, SGST, total) without finalizing."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT b.*, c.name as customer_name
            FROM bills b
            LEFT JOIN customers c ON b.customer_id = c.customer_id
            WHERE b.bill_id = %s
        """, (bill_id.strip(),))
        bill = cur.fetchone()
        if not bill:
            return {"status": "error", "message": f"Bill '{bill_id}' not found."}

        cur.execute("""
            SELECT bi.*, p.name as product_name, p.hsn_code, p.unit
            FROM bill_items bi
            JOIN products p ON bi.sku_id = p.sku_id
            WHERE bi.bill_id = %s
        """, (bill_id.strip(),))
        items = cur.fetchall()
        cur.close()

        subtotal = 0.0
        cgst_total = 0.0
        sgst_total = 0.0

        item_previews = []
        for item in items:
            line_subtotal = round(item["qty"] * item["unit_price"], 2)
            gst_info = _calculate_gst(line_subtotal, item["gst_slab"])

            subtotal += gst_info["subtotal"]
            cgst_total += gst_info["cgst"]
            sgst_total += gst_info["sgst"]

            item_previews.append({
                "sku_id": item["sku_id"],
                "name": item["product_name"],
                "hsn_code": item["hsn_code"],
                "unit": item["unit"],
                "qty": item["qty"],
                "unit_price": item["unit_price"],
                "gst_slab": item["gst_slab"],
                "line_subtotal": gst_info["subtotal"],
                "cgst": gst_info["cgst"],
                "sgst": gst_info["sgst"],
                "line_total": gst_info["line_total"]
            })

        grand_total = round(subtotal + cgst_total + sgst_total, 2)

        return {
            "status": "success",
            "bill_id": bill_id,
            "bill_status": bill["status"],
            "invoice_number": bill.get("invoice_number"),
            "payment_mode": (bill["payment_mode"] or "Pending").upper() if bill.get("payment_mode") else "Pending",
            "payment_ref": bill.get("payment_ref"),
            "customer_name": bill["customer_name"] or "Walk-in Customer",
            "created_at": str(bill["created_at"]) if bill.get("created_at") else None,
            "finalized_at": str(bill["finalized_at"]) if bill.get("finalized_at") else None,
            "items": item_previews,
            "summary": {
                "subtotal": round(subtotal, 2),
                "cgst": round(cgst_total, 2),
                "sgst": round(sgst_total, 2),
                "total_gst": round(cgst_total + sgst_total, 2),
                "grand_total": grand_total
            }
        }
    finally:
        conn.close()


def finalize_bill(
    bill_id: str,
    payment_mode: str,
    payment_ref: Optional[str] = None
) -> Dict[str, Any]:
    """
    Finalize a draft bill atomically:
    1. Enforces oversell guard with SELECT FOR UPDATE row locks inside one transaction.
    2. Decrements stock (and FEFO-consumes stock_batches when batch data exists).
    3. Assigns a sequential invoice number + snapshots place of supply.
    4. Handles Khata ledger charge (with credit-limit enforcement) if payment_mode is 'khata'.
    Telegram-level idempotency is enforced in the control loop before any tool runs.
    Re-finalizing the same bill is a no-op that simply returns the stored preview.
    """
    payment_mode = payment_mode.lower().strip()
    if payment_mode not in ["cash", "upi", "card", "khata"]:
        return {"status": "error", "message": "Invalid payment mode. Must be cash, upi, card, or khata."}

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()
            cur.execute("SELECT * FROM bills WHERE bill_id = %s", (bill_id.strip(),))
            bill = cur.fetchone()
            if not bill:
                return {"status": "error", "message": f"Bill '{bill_id}' not found."}
            if bill["status"] == "finalized":
                cur.close()
                return preview_bill(bill_id)
            if bill["status"] == "voided":
                return {"status": "error", "message": f"Bill '{bill_id}' has been voided."}

            cur.execute("SELECT * FROM bill_items WHERE bill_id = %s", (bill_id.strip(),))
            items = cur.fetchall()
            if not items:
                return {"status": "error", "message": "Cannot finalize an empty bill. Add items first."}

            # ── 1. Oversell guard with row locks ──
            subtotal = 0.0
            cgst_total = 0.0
            sgst_total = 0.0
            item_stock_before: Dict[str, Any] = {}

            for item in items:
                cur.execute("SELECT * FROM products WHERE sku_id = %s FOR UPDATE", (item["sku_id"],))
                product = cur.fetchone()
                if not product:
                    raise ValueError(f"Product SKU {item['sku_id']} missing during finalization.")

                if product["quantity"] < item["qty"]:
                    return {
                        "status": "error",
                        "error_type": "OversellGuardError",
                        "message": f"Oversell Guard Triggered: Cannot sell {item['qty']} units of '{product['name']}'. Current stock is only {product['quantity']}."
                    }

                item_stock_before[item["sku_id"]] = {"qty": product["quantity"], "name": product["name"]}

                line_subtotal = round(item["qty"] * item["unit_price"], 2)
                gst_info = _calculate_gst(line_subtotal, item["gst_slab"])
                subtotal += gst_info["subtotal"]
                cgst_total += gst_info["cgst"]
                sgst_total += gst_info["sgst"]

            grand_total = round(subtotal + cgst_total + sgst_total, 2)

            # ── 2. Khata validation (customer + credit limit) ──
            cust = None
            if payment_mode == "khata":
                if not bill["customer_id"]:
                    return {
                        "status": "error",
                        "error_type": "KhataCustomerRequired",
                        "message": "Cannot finalize bill with payment mode 'khata' without an associated customer."
                    }
                cur.execute("SELECT * FROM customers WHERE customer_id = %s FOR UPDATE", (bill["customer_id"],))
                cust = cur.fetchone()
                if not cust:
                    return {"status": "error", "message": "Khata customer not found in customer ledger."}

                credit_limit = cust.get("credit_limit") or 0
                if credit_limit > 0 and (cust["khata_balance"] + grand_total) > credit_limit:
                    return {
                        "status": "error",
                        "error_type": "CreditLimitExceeded",
                        "message": (f"Credit limit exceeded for {cust['name']}. Limit: ₹{credit_limit:.2f}, "
                                    f"Current: ₹{cust['khata_balance']:.2f}, This bill: ₹{grand_total:.2f}, "
                                    f"Would become: ₹{cust['khata_balance'] + grand_total:.2f}")
                    }

            # ── 3. Decrement stock + FEFO batch consumption ──
            for item in items:
                cur.execute("""
                    UPDATE products
                    SET quantity = quantity - %s, updated_at = CURRENT_TIMESTAMP
                    WHERE sku_id = %s
                """, (item["qty"], item["sku_id"]))

                # FEFO: consume batch rows nearest-expiry-first if batch data exists for this SKU
                remaining = item["qty"]
                cur.execute("""
                    SELECT batch_id FROM stock_batches
                    WHERE sku_id = %s AND qty_remaining > 0
                    ORDER BY expiry_date NULLS LAST, received_at ASC
                    FOR UPDATE
                """, (item["sku_id"],))
                batch_rows = cur.fetchall()
                for row in batch_rows:
                    if remaining <= 0:
                        break
                    cur.execute("SELECT qty_remaining FROM stock_batches WHERE batch_id = %s FOR UPDATE", (row["batch_id"],))
                    b = cur.fetchone()
                    take = min(b["qty_remaining"], remaining)
                    cur.execute("UPDATE stock_batches SET qty_remaining = qty_remaining - %s WHERE batch_id = %s",
                                (take, row["batch_id"]))
                    remaining -= take

                before = item_stock_before[item["sku_id"]]
                _log_event(conn, "STOCK_DECREMENTED", "product", item["sku_id"],
                           details={"product_name": before["name"], "bill_id": bill_id.strip(),
                                    "qty_sold": item["qty"]},
                           old_value=before["qty"], new_value=before["qty"] - item["qty"])

            # ── 4. Khata ledger charge ──
            if payment_mode == "khata" and cust:
                cur.execute("""
                    INSERT INTO khata_transactions (customer_id, type, amount, bill_id)
                    VALUES (%s, 'charge', %s, %s)
                """, (bill["customer_id"], grand_total, bill_id.strip()))

                cur.execute("""
                    UPDATE customers
                    SET khata_balance = khata_balance + %s, updated_at = CURRENT_TIMESTAMP
                    WHERE customer_id = %s
                """, (grand_total, bill["customer_id"]))

                _log_event(conn, "KHATA_CHARGED", "customer", cust["name"],
                           details={"amount": grand_total, "bill_id": bill_id.strip()},
                           old_value=cust["khata_balance"],
                           new_value=cust["khata_balance"] + grand_total)

            # ── 5. Sequential invoice number (guaranteed race-free via PostgreSQL sequence) ──
            try:
                cur.execute("SELECT nextval('invoice_number_seq') AS next_num")
                next_invoice_number = cur.fetchone()["next_num"]
            except Exception:
                # Fallback if sequence is not yet initialized on existing database
                cur.execute("SELECT COALESCE(MAX(invoice_number), 0) + 1 AS next_num FROM bills")
                next_invoice_number = cur.fetchone()["next_num"]

            shop_pos = None
            try:
                cur.execute("SELECT place_of_supply FROM shops ORDER BY shop_id LIMIT 1")
                pos_row = cur.fetchone()
                if pos_row:
                    shop_pos = pos_row.get("place_of_supply")
            except Exception:
                pass

            # ── 6. Update Bill Status to Finalized ──
            cur.execute("""
                UPDATE bills
                SET status = 'finalized',
                    payment_mode = %s,
                    payment_ref = %s,
                    subtotal = %s,
                    cgst = %s,
                    sgst = %s,
                    total = %s,
                    invoice_number = %s,
                    place_of_supply = %s,
                    finalized_at = CURRENT_TIMESTAMP
                WHERE bill_id = %s
            """, (payment_mode, payment_ref, round(subtotal, 2), round(cgst_total, 2),
                  round(sgst_total, 2), grand_total, next_invoice_number, shop_pos, bill_id.strip()))

            customer_name = None
            if bill["customer_id"]:
                cur.execute("SELECT name FROM customers WHERE customer_id = %s", (bill["customer_id"],))
                cust_row = cur.fetchone()
                customer_name = cust_row["name"] if cust_row else None

            _log_event(conn, "BILL_FINALIZED", "bill", bill_id,
                       details={"payment_mode": payment_mode, "grand_total": grand_total,
                                "customer_name": customer_name, "invoice_number": next_invoice_number})
            cur.close()

        # Post-commit: low stock alerts (separate read-only query, safe outside txn)
        low_stock_alerts = _fetch_low_stock_alerts()

        final_preview = preview_bill(bill_id)
        final_preview["message"] = f"Bill {bill_id} finalized successfully! Invoice #{next_invoice_number}. Total: ₹{grand_total} ({payment_mode.upper()})."
        if low_stock_alerts:
            alert_items_str = ", ".join([f"{item['name']} ({item['current_qty']} {item['unit']} left)" for item in low_stock_alerts])
            final_preview["low_stock_warning"] = (
                f"⚠️ LOW STOCK INTIMATION ALERT: {len(low_stock_alerts)} item(s) are at or below reorder level: {alert_items_str}. Please reorder soon!"
            )
            final_preview["low_stock_alerts"] = low_stock_alerts
        return final_preview

    finally:
        conn.close()


def _fetch_low_stock_alerts() -> List[Dict[str, Any]]:
    """Read-only low stock scan used after billing. Never raises."""
    try:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT sku_id, name, quantity, reorder_level, unit
                FROM products
                WHERE is_active = TRUE AND quantity <= reorder_level
                ORDER BY name ASC
            """)
            low_items = cur.fetchall()
            cur.close()
            return [{
                "sku_id": p["sku_id"],
                "name": p["name"],
                "current_qty": p["quantity"],
                "reorder_level": p["reorder_level"],
                "unit": p["unit"]
            } for p in low_items]
        finally:
            conn.close()
    except Exception:
        return []


def quick_create_bill(
    items: List[Dict[str, Any]],
    customer_name: Optional[str] = None,
    payment_mode: Optional[str] = None
) -> Dict[str, Any]:
    """
    ULTRAFAST Single-Turn Billing Tool:
    Creates draft bill, adds all items (name/sku & qty), and optionally finalizes in 1 single call!

    `items` format: [{"name": "sugar", "qty": 2}, {"name": "Maggi", "qty": 4}]
    `payment_mode`: Optional "upi", "cash", "card", or "khata". If omitted, leaves bill as draft.
    """
    # 1. Start bill
    start_res = start_bill(customer_name=customer_name)
    if start_res.get("status") != "success":
        return start_res

    bill_id = start_res["bill_id"]
    added_summary = []
    warnings = []

    # 2. Add all items
    for item in items:
        name = item.get("name") or item.get("sku_or_name") or item.get("sku")
        if not name:
            continue
        try:
            qty = float(item.get("qty", 1))
            if not math.isfinite(qty) or qty <= 0:
                warnings.append(f"Invalid quantity for '{name}': must be a positive finite number")
                continue
        except (ValueError, TypeError):
            warnings.append(f"Invalid quantity format for '{name}'")
            continue
        res = add_item_to_bill(bill_id=bill_id, sku_or_name=name, qty=qty)
        if res.get("status") == "success":
            added_summary.append(f"{qty} {res.get('product_name')}")
        elif res.get("status") == "oversell_warning":
            warnings.append(res.get("message"))
        else:
            warnings.append(f"Failed to add '{name}': {res.get('message')}")

    # 3. Finalize if payment mode provided
    if payment_mode:
        fin_res = finalize_bill(bill_id=bill_id, payment_mode=payment_mode)
        if warnings:
            fin_res["warnings"] = warnings
        return fin_res
    else:
        preview = preview_bill(bill_id=bill_id)
        if warnings:
            preview["warnings"] = warnings
        return preview
