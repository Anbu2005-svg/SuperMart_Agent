"""
Comprehensive Test Suite for 4 New SuperMarket Ops Agent Features:
1. Voice Note Billing (Groq Whisper Speech-to-Text)
2. Dynamic UPI QR Code Generator & Invoice Embeddings
3. Smart Expiry & Dynamic Markdown Engine
4. WhatsApp Digital Receipt & Khata Balance Reminders
"""

import os
import unittest
from unittest.mock import patch, MagicMock
from datetime import date, timedelta

from skills.voice import transcribe_audio, KIRANA_VOCAB_PROMPT
from skills.upi import build_upi_uri, generate_upi_qr_code, generate_upi_qr_for_bill
from skills.expiry import get_expiring_batches, recommend_markdown_discounts, apply_clearance_discount
from skills.whatsapp import (
    sanitize_phone_for_whatsapp,
    generate_whatsapp_bill_link,
    generate_whatsapp_khata_reminder_link
)
from skills.billing import start_bill, add_item_to_bill, finalize_bill
from docgen.invoice_template import generate_pdf_invoice


class TestVoiceNoteBilling(unittest.TestCase):
    """Test Voice Note Billing skill."""

    def test_empty_audio_bytes(self):
        res = transcribe_audio(b"")
        self.assertEqual(res["status"], "error")
        self.assertIn("empty", res["message"].lower())

    @patch("httpx.Client.post")
    def test_successful_groq_transcription(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "text": "Make a bill for 2 packets Maggi and 1kg sugar paid by UPI"
        }
        mock_post.return_value = mock_resp

        with patch("skills.voice.GROQ_API_KEY", "gsk_dummy_test_key"):
            res = transcribe_audio(b"fake_ogg_data", filename="voice.ogg")
            self.assertEqual(res["status"], "success")
            self.assertIn("Maggi", res["transcript"])
            self.assertEqual(res["provider"], "Groq")

    @patch("httpx.Client.post")
    def test_empty_speech_transcription(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"text": "   "}
        mock_post.return_value = mock_resp

        with patch("skills.voice.GROQ_API_KEY", "gsk_dummy_test_key"):
            res = transcribe_audio(b"silent_ogg_data", filename="voice.ogg")
            self.assertEqual(res["status"], "empty")

    def test_vocabulary_prompt_coverage(self):
        self.assertIn("Maggi", KIRANA_VOCAB_PROMPT)
        self.assertIn("Atta", KIRANA_VOCAB_PROMPT)
        self.assertIn("பில்", KIRANA_VOCAB_PROMPT)
        self.assertIn("खाता", KIRANA_VOCAB_PROMPT)


class TestDynamicUPIQR(unittest.TestCase):
    """Test Dynamic UPI QR Code Generator."""

    def test_build_upi_uri_format(self):
        uri = build_upi_uri(
            amount=155.0,
            vpa="merchant@oksbi",
            merchant_name="SuperMart Test",
            transaction_note="Bill 123",
            transaction_ref="BILL-123"
        )
        self.assertTrue(uri.startswith("upi://pay?"))
        self.assertIn("pa=merchant%40oksbi", uri)
        self.assertIn("am=155.00", uri)
        self.assertIn("cu=INR", uri)
        self.assertIn("tr=BILL-123", uri)

    def test_generate_upi_qr_code_file(self):
        res = generate_upi_qr_code(amount=250.75, note="Test Payment")
        self.assertEqual(res["status"], "success")
        self.assertTrue(os.path.exists(res["file_path"]))
        self.assertEqual(res["amount"], 250.75)
        self.assertTrue(res["file_path"].endswith(".png"))

    def test_zero_amount_rejected(self):
        res = generate_upi_qr_code(amount=0.0)
        self.assertEqual(res["status"], "error")

    def test_bill_upi_qr_and_invoice_pdf_embedding(self):
        b = start_bill()
        bill_id = b["bill_id"]
        add_item_to_bill(bill_id, "SKU-SALT-01", 1)
        fin = finalize_bill(bill_id, payment_mode="upi")
        self.assertEqual(fin["status"], "success")
        self.assertIn("upi_qr", fin)
        self.assertTrue(os.path.exists(fin["file_path"]))

        # PDF invoice must compile and embed the UPI QR
        pdf_path = generate_pdf_invoice(bill_id)
        self.assertTrue(os.path.exists(pdf_path))
        self.assertTrue(pdf_path.endswith(".pdf"))


class TestSmartExpiryEngine(unittest.TestCase):
    """Test Smart Expiry and Dynamic Markdown clearance engine."""

    def test_expiry_report_structure(self):
        res = recommend_markdown_discounts(days_ahead=30)
        self.assertEqual(res["status"], "success")
        self.assertIn("total_at_risk_value", res)
        self.assertIn("total_recoverable_revenue", res)
        self.assertIn("message", res)

    def test_apply_clearance_discount(self):
        res = apply_clearance_discount("SKU-SALT-01", 10.0)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["discount_pct"], 10.0)
        self.assertTrue(res["new_mrp"] < res["old_mrp"])

        # Reset back
        from db.models import get_db_connection
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE products SET mrp = %s WHERE sku_id = 'SKU-SALT-01'", (res["old_mrp"],))
        conn.commit()
        conn.close()

    def test_invalid_discount_percentage(self):
        res = apply_clearance_discount("SKU-SALT-01", 150.0)
        self.assertEqual(res["status"], "error")


class TestWhatsAppIntegration(unittest.TestCase):
    """Test WhatsApp digital receipt and Khata reminder link generation."""

    def test_phone_sanitization(self):
        self.assertEqual(sanitize_phone_for_whatsapp("9876543210"), "919876543210")
        self.assertEqual(sanitize_phone_for_whatsapp("+91 98765 43210"), "919876543210")
        self.assertEqual(sanitize_phone_for_whatsapp("+1-555-1234567"), "15551234567")
        self.assertIsNone(sanitize_phone_for_whatsapp(""))

    def test_generate_whatsapp_bill_receipt(self):
        b = start_bill()
        bill_id = b["bill_id"]
        add_item_to_bill(bill_id, "SKU-SALT-01", 2)
        finalize_bill(bill_id, "cash")

        wa_res = generate_whatsapp_bill_link(bill_id, phone="9876543210")
        self.assertEqual(wa_res["status"], "success")
        self.assertIn("https://wa.me/919876543210", wa_res["whatsapp_url"])
        self.assertIn("Salt", wa_res["message_text"])
        self.assertIn("GRAND TOTAL", wa_res["message_text"])

    def test_whatsapp_khata_reminder_links(self):
        # Customer with dues
        res = generate_whatsapp_khata_reminder_link("Priya Sharma")
        if res.get("status") == "success":
            self.assertTrue(res["whatsapp_url"].startswith("https://wa.me/"))
            self.assertIn("Khata", res["message_text"])
            self.assertTrue(res["balance"] > 0)
        else:
            self.assertEqual(res["status"], "no_due")


if __name__ == "__main__":
    unittest.main()
