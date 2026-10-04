import pytest
from skills.scheduled_alerts import (
    create_scheduled_alert,
    list_scheduled_alerts,
    toggle_scheduled_alert,
    trigger_due_scheduled_alerts
)
from skills.invoice_ocr import parse_invoice_image, intake_invoice_stock
from skills.i18n import set_bot_language, get_bot_language, get_localized_text, list_supported_languages
from skills.inventory import get_stock


def test_scheduled_alerts_lifecycle():
    # 1. Create scheduled alert
    res = create_scheduled_alert(
        alert_type="low_stock",
        recipient_id="test_admin_chat_123",
        channel="telegram",
        schedule_time="08:30"
    )
    assert res["status"] == "success"
    alert_id = res["alert_id"]

    # 2. List scheduled alerts
    alerts_list = list_scheduled_alerts()
    assert alerts_list["status"] == "success"
    assert any(a["alert_id"] == alert_id for a in alerts_list["alerts"])

    # 3. Toggle alert pause / active
    pause_res = toggle_scheduled_alert(alert_id, is_active=False)
    assert pause_res["status"] == "success"
    assert pause_res["is_active"] is False

    resume_res = toggle_scheduled_alert(alert_id, is_active=True)
    assert resume_res["status"] == "success"
    assert resume_res["is_active"] is True

    # 4. Trigger due alerts
    trig_res = trigger_due_scheduled_alerts(alert_type="low_stock")
    assert trig_res["status"] == "success"
    assert "triggered_count" in trig_res


def test_invoice_ocr_parsing_and_stock_intake():
    # Simulate invoice OCR text
    sample_invoice_text = """
    TAX INVOICE
    Supplier: Modern Wholesale Distributors Ltd
    Invoice No: MWD-9821
    Date: 2026-10-04

    1. Aashirvaad Whole Wheat Atta 5kg    5 packet @ 210.00
    2. Brooke Bond Red Label Tea 250g     10 packet @ 130.00
    """
    
    # 1. Parse invoice text
    parsed = parse_invoice_image(image_bytes_or_path=b"", raw_text_hint=sample_invoice_text)
    assert parsed["status"] == "success"
    assert parsed["invoice_number"] == "MWD-9821"
    assert len(parsed["items"]) >= 1

    # Check stock before
    stock_before = get_stock("Atta")["product"]["quantity"]

    # 2. Intake stock
    intake_res = intake_invoice_stock(
        invoice_number=parsed["invoice_number"],
        vendor_name=parsed["vendor_name"],
        items=parsed["items"],
        record_payable_bill=True
    )
    assert intake_res["status"] == "success"
    assert len(intake_res["received_items"]) >= 1

    # Check stock after
    stock_after = get_stock("Atta")["product"]["quantity"]
    assert stock_after > stock_before


def test_i18n_multilingual_support():
    owner = "telegram_user_999"

    # 1. Supported languages
    langs = list_supported_languages()
    assert "ta" in langs["supported_languages"]
    assert "hi" in langs["supported_languages"]
    assert "en" in langs["supported_languages"]

    # 2. Set language to Tamil
    res_ta = set_bot_language(owner, "ta")
    assert res_ta["status"] == "success"
    assert get_bot_language(owner) == "ta"

    # 3. Localized strings
    tamil_header = get_localized_text("welcome_header", lang_code="ta")
    assert "வணக்கம்" in tamil_header or "வரவேற்கிறோம்" in tamil_header

    hindi_header = get_localized_text("welcome_header", lang_code="hi")
    assert "स्वागत" in hindi_header

    # 4. String interpolation
    reminder = get_localized_text("khata_reminder", lang_code="ta", customer="Murugan", amount="450")
    assert "Murugan" in reminder
    assert "450" in reminder
