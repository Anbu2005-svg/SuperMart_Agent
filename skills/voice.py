"""
Voice Note Billing Skill for SuperMart AI Ops Agent.

Transcribes audio/voice messages (Telegram .ogg/.oga voice notes, mp3, wav, m4a)
using Groq Whisper (whisper-large-v3-turbo) in real-time (~400ms latency)
or OpenAI Whisper as an automatic fallback.
Supports multilingual voice billing in English, Hindi, Tamil, Telugu, and 90+ languages.
"""

import os
import logging
from typing import Dict, Any, Optional
import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
VOICE_TRANSCRIBE_API_KEY = os.getenv("VOICE_TRANSCRIBE_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# Kirana supermarket store vocabulary prompt to prime Whisper for Indian grocery items & regional terms
KIRANA_VOCAB_PROMPT = (
    "Kirana supermarket store operations and billing in English, Tamil (தமிழ், Tanglish), "
    "and Hindi (हिंदी, Hinglish). Terms: Maggi, Atta, Sugar, Dal, Rice, Salt, Oil, Milk, "
    "Aashirvaad, Fortune, Surf Excel, Parle-G, Amul Butter, Red Label Tea, "
    "kg, g, litre, ml, packet, piece, MRP, GST, bill, cash, UPI, card, khata, "
    "ரூபாய், கிலோ, பாக்கெட், பில், அரிசி, சர்க்கரை, பருப்பு, எண்ணெய், பால், "
    "ரமேஷ், சுரேஷ், பிரியா, கடன், "
    "रुपये, किलो, पैकेट, बिल, आटा, चीनी, दाल, तेल, दूध, खाता, उधार."
)


def transcribe_audio(
    audio_bytes: bytes,
    filename: str = "voice.ogg",
    language: Optional[str] = None
) -> Dict[str, Any]:
    """
    Transcribe audio bytes using Groq Whisper (or OpenAI Whisper fallback).
    Supports English, Tamil, Hindi, and 90+ languages with domain-primed vocabulary.
    """
    if not audio_bytes:
        return {"status": "error", "message": "Audio data is empty."}

    # Detect provider: Groq (ultra-fast) -> Voice Key -> OpenAI
    api_key = GROQ_API_KEY or VOICE_TRANSCRIBE_API_KEY or OPENAI_API_KEY
    if not api_key:
        return {
            "status": "error",
            "message": "No Whisper API key configured. Please set GROQ_API_KEY in your .env file."
        }

    is_groq = bool(GROQ_API_KEY)
    endpoint = (
        "https://api.groq.com/openai/v1/audio/transcriptions"
        if is_groq else
        "https://api.openai.com/v1/audio/transcriptions"
    )
    model = (
        os.getenv("VOICE_TRANSCRIBE_MODEL", "whisper-large-v3-turbo")
        if is_groq else
        os.getenv("VOICE_TRANSCRIBE_MODEL", "whisper-1")
    )

    headers = {"Authorization": f"Bearer {api_key}"}
    content_type = "audio/ogg"
    if filename.endswith(".mp3"):
        content_type = "audio/mpeg"
    elif filename.endswith(".wav"):
        content_type = "audio/wav"
    elif filename.endswith(".m4a"):
        content_type = "audio/m4a"

    files = {
        "file": (filename, audio_bytes, content_type)
    }
    data = {
        "model": model,
        "response_format": "json",
        "temperature": 0.0,
        "prompt": KIRANA_VOCAB_PROMPT,
    }
    if language:
        data["language"] = language

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(endpoint, headers=headers, files=files, data=data)
            if resp.status_code == 200:
                result = resp.json()
                transcript = result.get("text", "").strip()
                if not transcript:
                    return {
                        "status": "empty",
                        "transcript": "",
                        "message": "No speech detected in audio."
                    }
                return {
                    "status": "success",
                    "transcript": transcript,
                    "model": model,
                    "provider": "Groq" if is_groq else "OpenAI",
                    "message": f"Transcribed: {transcript}"
                }
            else:
                err_body = resp.text
                logger.error(f"Whisper API error ({resp.status_code}): {err_body}")
                return {
                    "status": "error",
                    "message": f"Transcription failed with status {resp.status_code}: {err_body[:100]}"
                }
    except Exception as e:
        logger.error(f"Audio transcription exception: {e}", exc_info=True)
        return {"status": "error", "message": f"Failed to transcribe audio: {str(e)}"}
