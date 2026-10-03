"""
Automated tests for Barcode & QR Code Scanning Feature.
Covers decoding, product lookup, duplicate protection, assignment,
quick billing, and Telegram bot handlers.
"""

import os
import io
import pytest
from unittest.mock import AsyncMock, MagicMock
from PIL import Image

import zxingcpp
from skills.barcode import (
    scan_barcode_from_image,
    lookup_product_by_barcode,
    assign_barcode_to_product,
    quick_bill_by_barcode,
    generate_barcode_image
)
from skills.billing import start_bill, preview_bill


class TestBarcodeEngine:
    """Test synthetic barcode generation and decoding across multiple symbologies."""

    def test_decode_ean13(self):
        """EAN-13 retail barcode correctly encodes and decodes."""
        barcode_val = "8901030383458"
        gen = generate_barcode_image(barcode_val, "EAN13")
        assert gen["status"] == "success"
        assert os.path.exists(gen["file_path"])

        # Scan back from file
        scan = scan_barcode_from_image(gen["file_path"])
        assert scan["status"] == "success"
        assert scan["primary_barcode"] == barcode_val
        assert any(b["text"] == barcode_val and "EAN" in b["format"] for b in scan["barcodes"])

    def test_decode_code128(self):
        """Code-128 alphanumeric barcode correctly encodes and decodes."""
        code_val = "SUPERMART-PROMO-2026"
        gen = generate_barcode_image(code_val, "CODE128")
        assert gen["status"] == "success"

        scan = scan_barcode_from_image(gen["file_path"])
        assert scan["status"] == "success"
        assert scan["primary_barcode"] == code_val

    def test_decode_qrcode(self):
        """QR Code correctly encodes and decodes."""
        qr_val = "https://t.me/SuperMart_Ops_Agent_bot"
        gen = generate_barcode_image(qr_val, "QR")
        assert gen["status"] == "success"

        scan = scan_barcode_from_image(gen["file_path"])
        assert scan["status"] == "success"
        assert scan["primary_barcode"] == qr_val

    def test_scan_blank_image(self):
        """Blank image returns clean not_found status without crashing."""
        blank = Image.new("RGB", (300, 300), color="white")
        buf = io.BytesIO()
        blank.save(buf, format="PNG")
        buf.seek(0)

        scan = scan_barcode_from_image(buf)
        assert scan["status"] == "not_found"
        assert scan["count"] == 0


class TestBarcodeDatabaseOperations:
    """Test product lookup, barcode assignment, and billing."""

    def test_lookup_existing_product(self):
        """Look up known seeded barcode returns correct product details."""
        # Tata Salt barcode
        res = lookup_product_by_barcode("8901030383458")
        assert res["status"] == "success"
        prod = res["product"]
        assert prod["sku_id"] == "SKU-SALT-01"
        assert "Salt" in prod["name"]
        assert prod["mrp"] == 28.0

    def test_lookup_by_sku_fallback(self):
        """Passing SKU ID directly to lookup resolves the product."""
        res = lookup_product_by_barcode("SKU-MAGGI-70")
        assert res["status"] == "success"
        assert "Maggi" in res["product"]["name"]

    def test_lookup_unknown_barcode(self):
        """Unknown barcode returns not_found with helpful message."""
        res = lookup_product_by_barcode("0000000000000")
        assert res["status"] == "not_found"
        assert "0000000000000" in res["message"]

    def test_assign_and_reassign_barcode(self):
        """Assigning a new barcode to an SKU links it correctly."""
        sku = "SKU-PARLEG-80"
        new_bc = "8901719109999"

        assign_res = assign_barcode_to_product(sku_id=sku, barcode=new_bc)
        assert assign_res["status"] == "success"

        # Verify lookup now resolves Parle-G via the new barcode
        lookup = lookup_product_by_barcode(new_bc)
        assert lookup["status"] == "success"
        assert lookup["product"]["sku_id"] == sku

    def test_assign_duplicate_barcode_conflict(self):
        """Attempting to assign a barcode already owned by another product is refused."""
        # Tata Salt already has 8901030383458
        tata_bc = "8901030383458"
        conflict_res = assign_barcode_to_product(sku_id="SKU-BUTTER-100", barcode=tata_bc)
        assert conflict_res["status"] == "error"
        assert "already mapped" in conflict_res["message"]

    def test_quick_bill_by_barcode(self):
        """Resolve barcode and add item directly to draft bill."""
        b = start_bill(customer_name="Barcode Shopper")
        b_id = b["bill_id"]

        # Add 2x Maggi via barcode
        maggi_bc = "8901058852301"
        add_res = quick_bill_by_barcode(bill_id=b_id, barcode=maggi_bc, qty=2.0)
        assert add_res["status"] == "success"

        # Verify bill items
        prev = preview_bill(b_id)
        assert len(prev["items"]) >= 1
        assert any(it["sku_id"] == "SKU-MAGGI-70" for it in prev["items"])


class TestTelegramBarcodeHandlers:
    """Test Telegram bot commands and photo message handler."""

    @pytest.mark.asyncio
    async def test_barcode_command_with_arg(self):
        """/barcode 8901030383458 returns product card."""
        import bot
        from skills.auth import register_shop, login_shop

        user_id = "test_bc_user_1"
        shop_name = "BC Test Shop"
        register_shop(shop_name, "Pass123!")
        login_shop(user_id, shop_name, "Pass123!")

        update = MagicMock()
        update.effective_user.id = user_id
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.args = ["8901030383458"]

        await bot.barcode_command(update, context)
        assert update.message.reply_text.called
        reply = update.message.reply_text.call_args[0][0]
        assert "Barcode Scanned" in reply
        assert "Tata Iodized Salt" in reply

    @pytest.mark.asyncio
    async def test_barcode_command_empty_help(self):
        """/barcode without args shows help on how to scan."""
        import bot
        from skills.auth import register_shop, login_shop

        user_id = "test_bc_user_2"
        shop_name = "BC Test Shop 2"
        register_shop(shop_name, "Pass123!")
        login_shop(user_id, shop_name, "Pass123!")

        update = MagicMock()
        update.effective_user.id = user_id
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.args = []

        await bot.barcode_command(update, context)
        assert update.message.reply_text.called
        reply = update.message.reply_text.call_args[0][0]
        assert "Send a photo" in reply
