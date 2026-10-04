"""
Multi-Language Bot Interface & Localization Skill (i18n) for SuperMart AI Ops Agent.

Supports English ('en'), Tamil ('ta'), and Hindi ('hi').
Persists per-user language preference in the PostgreSQL database.
"""

from typing import Dict, Any, Optional
from skills.preferences import set_preference, get_preference

SUPPORTED_LANGUAGES = {
    "en": "English",
    "ta": "தமிழ் (Tamil)",
    "hi": "हिन्दी (Hindi)"
}

TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "welcome_header": {
        "en": "🛒 *Welcome to SuperMart AI Ops Agent!*",
        "ta": "🛒 *சூப்பர் மார்ட் AI உதவியாளருக்கு வரவேற்கிறோம்!*",
        "hi": "🛒 *सुपरमार्ट एआई ऑप्स एजेंट में आपका स्वागत है!*"
    },
    "receipt_title": {
        "en": "🧾 *OFFICIAL CASH RECEIPT*",
        "ta": "🧾 *அதிகாரப்பூர்வ ரசீது*",
        "hi": "🧾 *आधिकारिक नकद रसीद*"
    },
    "total": {
        "en": "🏷️ TOTAL",
        "ta": "🏷️ மொத்தம்",
        "hi": "🏷️ कुल योग"
    },
    "subtotal": {
        "en": "💰 Subtotal",
        "ta": "💰 கூட்டல் தொகை",
        "hi": "💰 उप-योग"
    },
    "payment_mode": {
        "en": "💳 Payment Mode",
        "ta": "💳 கட்டண முறை",
        "hi": "💳 भुगतान का प्रकार"
    },
    "thank_you": {
        "en": "🙏 Thank you for shopping with us! Visit again.",
        "ta": "🙏 எங்களிடம் பொருட்கள் வாங்கியதற்கு நன்றி! மீண்டும் வருக.",
        "hi": "🙏 हमारे साथ खरीदारी करने के लिए धन्यवाद! फिर पधारें।"
    },
    "low_stock_warning": {
        "en": "⚠️ Low Stock Warning: Replenishment needed!",
        "ta": "⚠️ குறைந்த இருப்பு எச்சரிக்கை: புதிய சரக்கு தேவைப்படுகிறது!",
        "hi": "⚠️ कम स्टॉक चेतावनी: पुनः ऑर्डर करने की आवश्यकता है!"
    },
    "khata_reminder": {
        "en": "Dear {customer}, your outstanding credit balance is ₹{amount}. Kindly settle at your earliest convenience.",
        "ta": "அன்புள்ள {customer}, தங்களின் நிலுவைத் தொகை ₹{amount}. தயவுசெய்து விரைவில் செலுத்தவும்.",
        "hi": "नमस्ते {customer}, आपकी बकाया खाता राशि ₹{amount} है। कृपया जल्द से जल्द भुगतान करें।"
    }
}


def set_bot_language(owner_id: str, lang_code: str) -> Dict[str, Any]:
    """
    Set preferred UI language for a user ('en', 'ta', or 'hi').
    Saves preference to PostgreSQL database.
    """
    code = lang_code.strip().lower()
    if code not in SUPPORTED_LANGUAGES:
        valid_opts = ", ".join([f"'{k}' ({v})" for k, v in SUPPORTED_LANGUAGES.items()])
        return {
            "status": "error",
            "message": f"Unsupported language '{lang_code}'. Supported options: {valid_opts}"
        }

    set_preference(owner_id=owner_id, key="language", value=code)
    lang_name = SUPPORTED_LANGUAGES[code]
    return {
        "status": "success",
        "message": f"🌐 Interface language updated to **{lang_name}**.",
        "language_code": code,
        "language_name": lang_name
    }


def get_bot_language(owner_id: str) -> str:
    """Get active language preference for user (defaults to 'en')."""
    pref = get_preference(owner_id=owner_id, key="language")
    val = pref.get("value")
    return val if val in SUPPORTED_LANGUAGES else "en"


def get_localized_text(key: str, lang_code: str = "en", **kwargs) -> str:
    """Retrieve translated phrase by key with string interpolation."""
    lang = lang_code if lang_code in SUPPORTED_LANGUAGES else "en"
    phrase = TRANSLATIONS.get(key, {}).get(lang, TRANSLATIONS.get(key, {}).get("en", key))
    if kwargs:
        try:
            return phrase.format(**kwargs)
        except Exception:
            return phrase
    return phrase


def list_supported_languages() -> Dict[str, Any]:
    """List all available languages."""
    return {
        "status": "success",
        "supported_languages": SUPPORTED_LANGUAGES
    }
