"""
AI Voice Stock Reconcile & Physical Variance Audit Skill.

Allows store owners to audit physical store stock via voice notes or spoken text:
  - Parses counts from natural voice transcripts (e.g. "Counted 12 Atta, 5 Sunlite oil, 20 Maggi")
  - Fuzzy matches spoken names to catalog products
  - Calculates physical vs system stock variance (Shortage / Shrinkage vs Surplus)
  - Evaluates financial impact of stock variance based on cost price
  - One-click atomic reconciliation to synchronize system inventory with audit trail
"""

import re
from typing import Dict, Any, List, Optional, Union
from db.models import get_db_connection, immediate_transaction


def _parse_voice_stock_text(text: str) -> List[Dict[str, Any]]:
    """
    Extract item names and counts from natural language voice transcript.
    Examples:
      - 'Counted 12 Aashirvaad Atta, 5 Fortune oil, and 20 Maggi'
      - '10 Britannia bread, 15 Milk 500ml'
    """
    cleaned = re.sub(r'(?i)\b(counted|checked|found|have|got|and|packets?|packs?|bottles?|pcs|nos|kg|items?)\b', ' ', text)
    segments = re.split(r'[,;\n]+', cleaned)
    
    parsed = []
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        
        m1 = re.match(r'^(\d+(?:\.\d+)?)\s+(.+)$', seg)
        m2 = re.match(r'^(.+?)\s+(\d+(?:\.\d+)?)$', seg)
        
        if m1:
            qty = float(m1.group(1))
            name = m1.group(2).strip()
            parsed.append({"name": name, "quantity": qty})
        elif m2:
            name = m2.group(1).strip()
            qty = float(m2.group(2))
            parsed.append({"name": name, "quantity": qty})
            
    return parsed


def audit_physical_stock(
    counts_input: Union[str, Dict[str, float], List[Dict[str, Any]]]
) -> Dict[str, Any]:
    """
    Compares physical counted inventory against current system stock.
    Returns variance report with financial impact.
    """
    items_to_audit = []
    if isinstance(counts_input, str):
        items_to_audit = _parse_voice_stock_text(counts_input)
    elif isinstance(counts_input, dict):
        for k, v in counts_input.items():
            try:
                items_to_audit.append({"name": str(k).strip(), "quantity": float(v)})
            except (ValueError, TypeError):
                continue
    elif isinstance(counts_input, list):
        for it in counts_input:
            if isinstance(it, dict) and "quantity" in it:
                name = it.get("name") or it.get("sku_id") or ""
                items_to_audit.append({"name": str(name).strip(), "quantity": float(it["quantity"])})

    if not items_to_audit:
        return {"status": "error", "message": "No valid stock items or counts could be parsed from input."}

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        audit_results = []
        total_shortage_value = 0.0
        total_surplus_value = 0.0
        shortage_items_count = 0
        surplus_items_count = 0

        for item in items_to_audit:
            raw_name = item["name"]
            counted_qty = item["quantity"]

            cur.execute("""
                SELECT sku_id, name, cost_price, mrp, quantity, unit
                FROM products 
                WHERE sku_id = %s OR name ILIKE %s OR barcode = %s
                ORDER BY (name ILIKE %s) DESC LIMIT 1;
            """, (raw_name, f"%{raw_name}%", raw_name, raw_name))
            prod = cur.fetchone()

            if not prod:
                audit_results.append({
                    "queried_item": raw_name,
                    "matched": False,
                    "physical_count": counted_qty,
                    "message": "Product not found in database catalog."
                })
                continue

            system_qty = float(prod["quantity"] or 0.0)
            cost_price = float(prod["cost_price"] or 0.0)
            variance_qty = round(counted_qty - system_qty, 2)
            variance_value = round(variance_qty * cost_price, 2)

            status = "matched"
            if variance_qty < 0:
                status = "shortage"
                shortage_items_count += 1
                total_shortage_value += abs(variance_value)
            elif variance_qty > 0:
                status = "surplus"
                surplus_items_count += 1
                total_surplus_value += variance_value

            audit_results.append({
                "sku_id": prod["sku_id"],
                "product_name": prod["name"],
                "unit": prod["unit"] or "pcs",
                "matched": True,
                "system_stock": system_qty,
                "physical_count": counted_qty,
                "variance_quantity": variance_qty,
                "unit_cost_price": cost_price,
                "variance_financial_impact": variance_value,
                "status": status
            })

        net_variance_value = round(total_surplus_value - total_shortage_value, 2)

        return {
            "status": "success",
            "items_audited": len(items_to_audit),
            "matched_products": len([r for r in audit_results if r.get("matched")]),
            "summary": {
                "shortage_count": shortage_items_count,
                "total_shortage_loss": round(total_shortage_value, 2),
                "surplus_count": surplus_items_count,
                "total_surplus_value": round(total_surplus_value, 2),
                "net_financial_variance": net_variance_value
            },
            "audit_details": audit_results
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to perform stock audit: {str(e)}"}
    finally:
        conn.close()


def apply_stock_reconciliation(
    reconciled_items: List[Dict[str, Any]],
    reason: str = "Physical stock audit reconciliation"
) -> Dict[str, Any]:
    """
    Atomically updates system inventory quantities to match verified physical counts.
    """
    if not reconciled_items:
        return {"status": "error", "message": "No reconciled items provided."}

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        updated = 0
        with immediate_transaction(conn):
            for item in reconciled_items:
                sku_id = item.get("sku_id")
                physical_count = item.get("physical_count")

                if not sku_id or physical_count is None:
                    continue

                cur.execute("SELECT quantity, name FROM products WHERE sku_id = %s", (sku_id,))
                prod = cur.fetchone()
                if not prod:
                    continue

                old_qty = float(prod["quantity"] or 0.0)
                new_qty = float(physical_count)

                cur.execute("""
                    UPDATE products SET quantity = %s WHERE sku_id = %s;
                """, (new_qty, sku_id))

                from skills.audit import _log_event
                _log_event(
                    conn,
                    event_type="STOCK_RECONCILE",
                    entity_type="product",
                    entity_id=sku_id,
                    details={"name": prod["name"], "reason": reason},
                    old_value=str(old_qty),
                    new_value=str(new_qty)
                )

                updated += 1


        return {
            "status": "success",
            "message": f"Inventory successfully synchronized: {updated} items updated to physical count.",
            "reconciled_count": updated
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to apply stock reconciliation: {str(e)}"}
    finally:
        conn.close()
