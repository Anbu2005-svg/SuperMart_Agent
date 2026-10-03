"""
Barcode & QR Code Scanning Skill for SuperMart AI Ops Agent.

Powered by zxing-cpp for ultrafast, accurate decoding of retail barcodes:
EAN-13, EAN-8, UPC-A, UPC-E, Code 128, Code 39, and QR Codes.
Supports photo scanning, direct number lookup, barcode generation, and draft bill insertion.
"""

import io
import os
import re
import logging
from typing import Dict, Any, List, Optional, Union
import httpx
from PIL import Image, ImageEnhance, ImageOps, ImageFilter

try:
    import zxingcpp
except ImportError:
    zxingcpp = None

from db.models import get_db_connection, immediate_transaction
from skills.audit import _log_event

logger = logging.getLogger(__name__)


def scan_barcode_from_image(image_input: Union[str, bytes, io.BytesIO, Image.Image]) -> Dict[str, Any]:
    """
    Scan and decode one or more barcodes from an image file path, raw bytes, or PIL Image.
    Applies image preprocessing (grayscale, contrast normalization) for real-world phone photos.
    """
    if zxingcpp is None:
        return {
            "status": "error",
            "message": "zxing-cpp is not installed in the current environment. Please install it using pip."
        }

    try:
        # Safety: set decompression bomb pixel limit (50M pixels, ~200MB uncompressed)
        Image.MAX_IMAGE_PIXELS = 50_000_000

        # Load image
        if isinstance(image_input, Image.Image):
            pil_img = image_input
        elif isinstance(image_input, (bytes, bytearray)):
            pil_img = Image.open(io.BytesIO(image_input))
        elif isinstance(image_input, io.BytesIO):
            pil_img = Image.open(image_input)
        elif isinstance(image_input, str):
            if not os.path.exists(image_input):
                return {"status": "error", "message": f"Image file not found: {image_input}"}
            pil_img = Image.open(image_input)
        else:
            return {"status": "error", "message": f"Unsupported image input type: {type(image_input)}"}

        # Validate image dimensions (reject absurdly large images)
        max_dimension = 8000  # 8000x8000 is more than enough for any barcode photo
        if pil_img.width > max_dimension or pil_img.height > max_dimension:
            return {"status": "error", "message": f"Image too large ({pil_img.width}x{pil_img.height}). Maximum dimension is {max_dimension}px."}

        # Auto-orient smartphone photos according to EXIF metadata
        try:
            pil_img = ImageOps.exif_transpose(pil_img)
        except Exception:
            pass

        # Convert palette or transparency modes to RGB
        if pil_img.mode in ("P", "RGBA", "LA"):
            pil_img = pil_img.convert("RGB")

        # Pass 1: Direct multi-directional decode with local average binarizer
        results = zxingcpp.read_barcodes(pil_img, try_rotate=True, try_downscale=True, try_invert=True)

        # Pass 2: De-blurring UnsharpMask (restores out-of-focus or motion-blurred photos)
        if not results:
            unsharp_rgb = pil_img.filter(ImageFilter.UnsharpMask(radius=2, percent=200, threshold=3))
            results = zxingcpp.read_barcodes(unsharp_rgb, try_rotate=True, try_downscale=True)

        # Pass 3: Grayscale Contrast & Edge Sharpening (solves dim lighting, shadowy or wrinkled packaging)
        if not results:
            gray = pil_img.convert("L")
            for c_val in (1.5, 2.0):
                c_im = ImageEnhance.Contrast(gray).enhance(c_val)
                sh_gray = c_im.filter(ImageFilter.UnsharpMask(radius=2, percent=200, threshold=2))
                results = zxingcpp.read_barcodes(sh_gray, try_rotate=True, try_downscale=True)
                if results:
                    break

        # Pass 4: Anti-glare Gamma Correction (darkens overexposed highlights from flash reflections)
        if not results:
            gray = pil_img.convert("L") if "gray" not in locals() else gray
            gamma_table = [int(((i / 255.0) ** (1.0 / 0.5)) * 255) for i in range(256)]
            gamma_img = gray.point(gamma_table)
            results = zxingcpp.read_barcodes(gamma_img, try_rotate=True, try_downscale=True)

        # Pass 5: Multi-slice scanning (decodes torn, scratched, or cut-in-half barcodes via vertical redundancy)
        if not results:
            w, h = pil_img.size
            slices = [
                pil_img.crop((0, 0, w, int(h * 0.55))),             # Top half
                pil_img.crop((0, int(h * 0.22), w, int(h * 0.78))), # Middle strip
                pil_img.crop((0, int(h * 0.45), w, h)),             # Bottom half
            ]
            for s in slices:
                results = zxingcpp.read_barcodes(s, try_rotate=True, try_downscale=True)
                if results:
                    break
                sh_s = s.filter(ImageFilter.UnsharpMask(radius=2, percent=200, threshold=3))
                results = zxingcpp.read_barcodes(sh_s, try_rotate=True, try_downscale=True)
                if results:
                    break

        # Pass 6: Downscale high-resolution smartphone photos (> 1800px) where dense barcodes blur
        if not results and max(pil_img.size) > 1800:
            scale = 1400 / max(pil_img.size)
            new_size = (int(pil_img.size[0] * scale), int(pil_img.size[1] * scale))
            img_resized = pil_img.resize(new_size, Image.Resampling.LANCZOS)
            results = zxingcpp.read_barcodes(img_resized, try_rotate=True, try_downscale=True)

        # Pass 7: Global histogram binarizer + error-tolerant recovery fallback
        if not results:
            results = zxingcpp.read_barcodes(
                pil_img,
                binarizer=zxingcpp.Binarizer.GlobalHistogram,
                try_rotate=True,
                try_downscale=True,
                return_errors=True
            )

        if not results:
            return {
                "status": "not_found",
                "count": 0,
                "barcodes": [],
                "primary_barcode": None,
                "message": "No barcode detected in the provided image. Ensure the barcode is well-lit and clearly in focus."
            }

        barcodes_list: List[Dict[str, Any]] = []
        for r in results:
            fmt_str = str(r.format).replace("BarcodeFormat.", "")
            barcodes_list.append({
                "text": r.text.strip(),
                "format": fmt_str,
                "valid": getattr(r, "valid", True)
            })

        primary = barcodes_list[0]["text"] if barcodes_list else None

        return {
            "status": "success",
            "count": len(barcodes_list),
            "primary_barcode": primary,
            "barcodes": barcodes_list,
            "message": f"Detected {len(barcodes_list)} barcode(s). Primary: {primary} ({barcodes_list[0]['format']})"
        }

    except Exception as e:
        logger.error(f"Barcode decoding error: {e}", exc_info=True)
        return {"status": "error", "message": f"Failed to decode image: {str(e)}"}


def get_gs1_country(barcode: str) -> str:
    """Identify the GS1 member organization or origin country from barcode digits."""
    clean = re.sub(r"\D", "", barcode)
    if not clean:
        return "Unknown"
    digits = clean.zfill(13)
    try:
        p3 = int(digits[:3])
    except ValueError:
        return "Unknown"

    if p3 <= 19 or (30 <= p3 <= 39) or (60 <= p3 <= 139):
        return "United States & Canada (UPC)"
    if 300 <= p3 <= 379:
        return "France"
    if 400 <= p3 <= 440:
        return "Germany"
    if (450 <= p3 <= 459) or (490 <= p3 <= 499):
        return "Japan"
    if 460 <= p3 <= 469:
        return "Russia"
    if p3 == 471:
        return "Taiwan"
    if p3 == 489:
        return "Hong Kong"
    if 500 <= p3 <= 509:
        return "United Kingdom"
    if 540 <= p3 <= 549:
        return "Belgium & Luxembourg"
    if p3 == 590:
        return "Poland"
    if 690 <= p3 <= 699:
        return "China"
    if 730 <= p3 <= 739:
        return "Sweden"
    if 760 <= p3 <= 769:
        return "Switzerland"
    if 800 <= p3 <= 839:
        return "Italy"
    if 840 <= p3 <= 849:
        return "Spain"
    if 870 <= p3 <= 879:
        return "Netherlands"
    if p3 == 880:
        return "South Korea"
    if p3 == 885:
        return "Thailand"
    if p3 == 888:
        return "Singapore"
    if p3 == 890:
        return "India (GS1 India)"
    if p3 == 893:
        return "Vietnam"
    if p3 == 899:
        return "Indonesia"
    if 930 <= p3 <= 939:
        return "Australia"
    if 940 <= p3 <= 949:
        return "New Zealand"
    if p3 == 955:
        return "Malaysia"
    if p3 == 977:
        return "ISSN (Periodicals / Magazines)"
    if p3 in (978, 979):
        return "International ISBN (Books)"
    return "International (GS1 Global)"


def fetch_global_product_info(barcode: str) -> Dict[str, Any]:
    """
    Query Open Food Facts (global product registry covering 3M+ items across 150+ countries)
    to identify international products not yet in the local supermarket database.
    """
    clean = re.sub(r"\D", "", barcode)
    if not clean:
        return {"status": "error", "message": "Invalid barcode digits."}

    country = get_gs1_country(clean)

    try:
        url = f"https://world.openfoodfacts.org/api/v2/product/{clean}.json"
        with httpx.Client(timeout=3.0, headers={"User-Agent": "SuperMartOpsAgent/1.0"}) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == 1:
                    p = data.get("product", {})
                    name = p.get("product_name") or p.get("product_name_en") or p.get("generic_name") or "Global Product"
                    brand = p.get("brands") or "Unknown Brand"
                    category = p.get("categories", "").split(",")[0].strip() or "General Packaged Food"
                    return {
                        "status": "success",
                        "product": {
                            "name": name,
                            "brand": brand,
                            "category": category,
                            "origin_country": country,
                            "barcode": clean,
                            "image_url": p.get("image_front_small_url") or p.get("image_url")
                        },
                        "message": f"Global Product Found: {name} ({brand}) from {country}"
                    }
    except Exception as e:
        logger.debug(f"Open Food Facts lookup error for {clean}: {e}")

    return {"status": "not_found", "origin_country": country, "barcode": clean}


def lookup_product_by_barcode(barcode: str) -> Dict[str, Any]:
    """
    Look up a product from the database using its barcode string or SKU ID.
    Returns product details, pricing, available stock, and earliest-expiring batch (FEFO).
    Supports EAN-13, UPC-A leading zero normalization, and stripped punctuation.
    If not in local DB, queries Open Food Facts (3M+ products globally) and GS1 country prefixes.
    """
    if not barcode or not isinstance(barcode, str):
        return {"status": "error", "message": "Barcode must be a non-empty string."}

    raw_input = barcode.strip()
    clean_barcode = re.sub(r"[\s\-_:]+", "", raw_input)

    # Build search candidates (raw SKU/barcode, cleaned barcode, plus UPC-A/EAN-13 conversions)
    candidates = list(dict.fromkeys([raw_input, clean_barcode]))
    if len(clean_barcode) == 12 and clean_barcode.isdigit():
        candidates.append(f"0{clean_barcode}")
    elif len(clean_barcode) == 13 and clean_barcode.startswith("0") and clean_barcode.isdigit():
        candidates.append(clean_barcode[1:])

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # Query products by candidate barcodes or sku_id
        cur.execute("""
            SELECT p.*
            FROM products p
            WHERE p.barcode = ANY(%s) OR p.sku_id = ANY(%s)
            LIMIT 1
        """, (candidates, candidates))
        prod = cur.fetchone()

        if not prod:
            cur.close()

            # Global product registry query (Open Food Facts + GS1 Origin)
            global_res = fetch_global_product_info(clean_barcode)
            if global_res.get("status") == "success":
                gp = global_res["product"]
                return {
                    "status": "global_recognized",
                    "barcode": clean_barcode,
                    "global_product": gp,
                    "message": (
                        f"🌍 **Global Product Recognized:** {gp['name']}\n"
                        f"• Brand: {gp['brand']}\n"
                        f"• Category: {gp['category']}\n"
                        f"• Origin: {gp['origin_country']}\n"
                        f"• Barcode: `{clean_barcode}`\n\n"
                        f"This product is recognized in the global database but not yet in your local store inventory."
                    )
                }

            origin_country = global_res.get("origin_country") or get_gs1_country(clean_barcode)
            origin_hint = f" (Origin: {origin_country})" if origin_country != "Unknown" else ""
            return {
                "status": "not_found",
                "barcode": clean_barcode,
                "origin_country": origin_country,
                "message": f"Barcode '{clean_barcode}'{origin_hint} is not registered in your supermarket. You can map it to an existing product using assign_barcode."
            }

        sku_id = prod["sku_id"]

        # Fetch earliest expiring batch for this product
        cur.execute("""
            SELECT batch_code, qty_remaining, expiry_date
            FROM stock_batches
            WHERE sku_id = %s AND qty_remaining > 0
            ORDER BY expiry_date ASC NULLS LAST
            LIMIT 1
        """, (sku_id,))
        batch = cur.fetchone()
        cur.close()

        product_data = {
            "sku_id": prod["sku_id"],
            "name": prod["name"],
            "category": prod["category"],
            "unit": prod["unit"],
            "is_loose": prod.get("is_loose", False),
            "cost_price": float(prod["cost_price"]),
            "mrp": float(prod["mrp"]),
            "gst_slab": float(prod.get("gst_slab", 0.0)),
            "hsn_code": prod.get("hsn_code"),
            "quantity": float(prod["quantity"]),
            "reorder_level": float(prod.get("reorder_level", 10.0)),
            "barcode": prod.get("barcode") or clean_barcode,
            "earliest_batch": {
                "batch_code": batch["batch_code"],
                "qty_remaining": float(batch["qty_remaining"]),
                "expiry_date": str(batch["expiry_date"]) if batch["expiry_date"] else None
            } if batch else None
        }

        # Formatted display message
        lines = [
            f"🏷️ **Product Found: {product_data['name']}**",
            f"• **Barcode:** `{product_data['barcode']}`",
            f"• **SKU:** `{product_data['sku_id']}`",
            f"• **MRP:** ₹{product_data['mrp']:.2f} (GST: {product_data['gst_slab']}%)",
            f"• **Stock Available:** {product_data['quantity']} {product_data['unit']}",
        ]
        if product_data["earliest_batch"] and product_data["earliest_batch"]["expiry_date"]:
            lines.append(f"• **Nearest Expiry:** {product_data['earliest_batch']['expiry_date']} (Batch: {product_data['earliest_batch']['batch_code']})")

        return {
            "status": "success",
            "barcode": clean_barcode,
            "product": product_data,
            "message": "\n".join(lines)
        }

    finally:
        conn.close()


def assign_barcode_to_product(sku_id: str, barcode: str) -> Dict[str, Any]:
    """
    Assign or map a barcode to an existing product SKU.
    Validates SKU existence and ensures barcode uniqueness.
    """
    if not sku_id or not isinstance(sku_id, str):
        return {"status": "error", "message": "sku_id is required."}
    if not barcode or not isinstance(barcode, str):
        return {"status": "error", "message": "barcode is required."}

    clean_sku = sku_id.strip()
    clean_barcode = barcode.strip()

    conn = get_db_connection()
    try:
        with immediate_transaction(conn):
            cur = conn.cursor()

            # Check if product exists
            cur.execute("SELECT * FROM products WHERE sku_id = %s FOR UPDATE", (clean_sku,))
            prod = cur.fetchone()
            if not prod:
                cur.close()
                return {"status": "error", "message": f"Product with SKU '{clean_sku}' does not exist."}

            # Check if barcode is already used by a DIFFERENT product
            cur.execute("SELECT sku_id, name FROM products WHERE barcode = %s AND sku_id != %s", (clean_barcode, clean_sku))
            conflict = cur.fetchone()
            if conflict:
                cur.close()
                return {
                    "status": "error",
                    "message": f"Barcode '{clean_barcode}' is already mapped to product '{conflict['name']}' ({conflict['sku_id']})."
                }

            # Update product barcode
            old_bc = prod.get("barcode")
            cur.execute("UPDATE products SET barcode = %s WHERE sku_id = %s", (clean_barcode, clean_sku))

            _log_event(conn, "BARCODE_ASSIGNED", "product", clean_sku,
                       details={"barcode": clean_barcode, "product_name": prod["name"]},
                       old_value=old_bc, new_value=clean_barcode)
            cur.close()

        return {
            "status": "success",
            "sku_id": clean_sku,
            "product_name": prod["name"],
            "barcode": clean_barcode,
            "message": f"✅ Barcode `{clean_barcode}` successfully linked to **{prod['name']}** (`{clean_sku}`)."
        }

    finally:
        conn.close()


def quick_bill_by_barcode(bill_id: str, barcode: str, qty: float = 1.0) -> Dict[str, Any]:
    """
    Convenience method: resolve a scanned barcode and add it directly to an active draft bill.
    """
    lookup = lookup_product_by_barcode(barcode)
    if lookup.get("status") != "success":
        return lookup

    prod = lookup["product"]
    from skills.billing import add_item_to_bill
    res = add_item_to_bill(bill_id=bill_id, sku_or_name=prod["sku_id"], qty=qty)

    if res.get("status") == "success":
        res["scanned_product"] = prod["name"]
        res["scanned_barcode"] = barcode

    return res


def generate_barcode_image(barcode_text: str, format_name: str = "EAN13", output_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Generate a barcode image for a product. Supports EAN13, Code128, QRCode.
    Returns path to the generated image file.
    """
    if zxingcpp is None:
        return {"status": "error", "message": "zxing-cpp not installed."}

    format_map = {
        "EAN13": zxingcpp.BarcodeFormat.EAN13,
        "CODE128": zxingcpp.BarcodeFormat.Code128,
        "QR": zxingcpp.BarcodeFormat.QRCode,
        "UPCA": zxingcpp.BarcodeFormat.UPCA
    }

    fmt = format_map.get(format_name.upper(), zxingcpp.BarcodeFormat.Code128)

    try:
        clean_text = barcode_text.strip()
        bc = zxingcpp.create_barcode(clean_text, fmt)
        img_view = zxingcpp.write_barcode_to_image(bc)

        # zxingcpp Image provides .shape as (height, width)
        h, w = img_view.shape[0], img_view.shape[1]
        img_pil = Image.frombuffer("L", (w, h), bytes(img_view))

        if not output_path:
            os.makedirs("generated_docs", exist_ok=True)
            safe_filename = re.sub(r'[^a-zA-Z0-9_-]', '_', clean_text)[:40]
            output_path = os.path.join("generated_docs", f"barcode_{safe_filename}.png")

        img_pil.save(output_path)

        return {
            "status": "success",
            "barcode": clean_text,
            "format": str(fmt),
            "file_path": output_path,
            "message": f"Barcode image generated at {output_path}"
        }

    except Exception as e:
        return {"status": "error", "message": f"Failed to generate barcode: {str(e)}"}
