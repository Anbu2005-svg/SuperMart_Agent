"""Central Security Hardening & Protection Module for SuperMart Ops Agent.

Covers:
  - API Key Masking & Leak Prevention
  - Environment Variable Validation
  - Admin Access Control & Role Checking
  - Input Sanitization & Control-Character Stripping
  - XSS & ReportLab XML Tag Escaping
  - HTTP Security Headers & CORS Policies
  - HTTP Endpoint Rate Limiting (Anti-DoS)
  - Password Strength & Complexity Validation
"""

import os
import re
import time
import logging
from xml.sax.saxutils import escape as _sax_escape
from typing import Dict, Any, Tuple, Optional, List

logger = logging.getLogger(__name__)

# In-memory IP tracking for HTTP health-check / webhook rate limiting
# {client_ip: [timestamp1, timestamp2, ...]}
_HTTP_IP_RATE_BUCKETS: Dict[str, List[float]] = {}
MAX_HTTP_IPS_TRACKED = 2000


def mask_secret(secret: Optional[str], visible_start: int = 4, visible_end: int = 4) -> str:
    """Safely mask API keys or passwords for logging, e.g. '8799...sipw'."""
    if not secret or not isinstance(secret, str):
        return "<not set>"
    secret = secret.strip()
    if len(secret) <= (visible_start + visible_end):
        return "***"
    return f"{secret[:visible_start]}...{secret[-visible_end:]}"


def validate_environment() -> Dict[str, Any]:
    """Validate all required and security-critical environment variables on startup.
    Returns status and diagnostic details without exposing secrets.
    """
    issues = []
    warnings = []

    # 1. Telegram Bot Token
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token == "your_telegram_bot_token_here":
        issues.append("TELEGRAM_BOT_TOKEN is missing or set to placeholder string!")
    elif not re.match(r"^\d{8,11}:[A-Za-z0-9_-]{30,40}$", token):
        warnings.append("TELEGRAM_BOT_TOKEN format looks atypical for Telegram Bot API.")

    # 2. Database URL
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url or "password@host" in db_url:
        issues.append("DATABASE_URL is missing or using default unconfigured placeholder!")
    elif not (db_url.startswith("postgresql://") or db_url.startswith("postgres://") or db_url.startswith("sqlite://")):
        warnings.append("DATABASE_URL should begin with 'postgresql://' or 'sqlite://'.")

    # 3. LLM Configuration
    llm_keys = [
        os.getenv(f"LLM_API_KEY_{i}", "").strip()
        for i in range(1, 5)
        if os.getenv(f"LLM_API_KEY_{i}", "").strip()
    ]
    ollama_key = os.getenv("OLLAMA_API_KEY", "").strip()
    if not llm_keys and not ollama_key:
        warnings.append("No LLM API keys found (LLM_API_KEY_1 or OLLAMA_API_KEY). Bot will not be able to answer natural language queries.")

    # 4. Port Configuration
    port_str = os.getenv("PORT", "8080").strip()
    try:
        port = int(port_str)
        if not (1 <= port <= 65535):
            issues.append(f"PORT {port} is out of valid TCP range (1-65535).")
    except ValueError:
        issues.append(f"PORT '{port_str}' is not a valid integer.")

    is_valid = len(issues) == 0
    return {
        "valid": is_valid,
        "issues": issues,
        "warnings": warnings,
        "telegram_token_configured": bool(token and token != "your_telegram_bot_token_here"),
        "database_configured": bool(db_url and "password@host" not in db_url),
        "llm_keys_count": len(llm_keys) + (1 if ollama_key else 0)
    }


def is_admin_user(telegram_id: str) -> bool:
    """Check if a given Telegram user ID is an authorized Administrator.
    Configured via ADMIN_TELEGRAM_IDS in .env (comma-separated list).
    If ADMIN_TELEGRAM_IDS is not configured, authenticated shop users are permitted by default.
    """
    if not telegram_id or not isinstance(telegram_id, str):
        return False
        
    admin_env = os.getenv("ADMIN_TELEGRAM_IDS", "").strip()
    if not admin_env:
        # Default policy: allow authenticated shop users if no explicit admin whitelist is set
        return True
        
    admins = {tid.strip() for tid in admin_env.split(",") if tid.strip()}
    return str(telegram_id).strip() in admins


def sanitize_input(value: Any, max_length: int = 255) -> str:
    """Sanitize user input:
    - Strips leading/trailing whitespace
    - Removes dangerous ASCII control characters (\x00 to \x1f except tab/newline)
    - Enforces maximum character length
    """
    if value is None:
        return ""
    text = str(value).strip()
    # Strip null bytes and non-printable control codes
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    if len(text) > max_length:
        text = text[:max_length]
    return text


def escape_xml_text(value: Any) -> str:
    """Safely escape text for ReportLab Paragraph XML rendering to prevent syntax corruption."""
    if value is None:
        return ""
    return _sax_escape(str(value), {'"': "&quot;", "'": "&apos;"})


def validate_password_strength(password: str) -> Tuple[bool, str]:
    """Validate password strength for shop accounts:
    - Minimum 6 characters (maximum 128)
    - Must not be purely whitespace
    """
    if not password or not isinstance(password, str):
        return False, "Password cannot be empty!"
    pwd = password.strip()
    if len(pwd) < 6:
        return False, "Password must be at least 6 characters long for security!"
    if len(pwd) > 128:
        return False, "Password cannot exceed 128 characters!"
    return True, "Password is valid."


def validate_safe_workspace_path(
    file_path: Optional[str],
    default_dir: str = "generated_docs",
    allowed_dirs: Optional[List[str]] = None,
    allow_create_dir: bool = True
) -> Tuple[bool, str, Optional[str]]:
    """
    Validate that file_path is strictly confined to allowed workspace directories.
    Prevents path traversal (e.g. '../', absolute paths pointing outside project).
    Returns (is_valid, resolved_absolute_path, error_message).
    """
    if not file_path or not isinstance(file_path, str) or not file_path.strip():
        return False, "", "File path cannot be empty."

    raw_path = file_path.strip()
    if "\x00" in raw_path or ".." in raw_path:
        return False, "", "Path traversal attempts ('..') or null bytes are strictly forbidden."

    if allowed_dirs is None:
        allowed_dirs = ["generated_docs", "data"]

    cwd = os.path.realpath(os.getcwd())
    norm = os.path.normpath(raw_path)

    # Disallow absolute drives or root paths outside the workspace
    if os.path.isabs(norm):
        target_abs = os.path.realpath(norm)
    else:
        parts = norm.split(os.sep)
        if parts[0] not in allowed_dirs:
            norm = os.path.join(default_dir, norm)
        target_abs = os.path.realpath(os.path.join(cwd, norm))

    # Verify containment in allowed directories or system temp (handling cross-drive comparisons on Windows)
    import tempfile
    is_safe = False
    try:
        target_drive = os.path.splitdrive(target_abs)[0].lower()
        sys_temp = os.path.realpath(tempfile.gettempdir())
        if os.path.splitdrive(sys_temp)[0].lower() == target_drive:
            if os.path.commonpath([sys_temp, target_abs]) == sys_temp:
                is_safe = True

        if not is_safe:
            for ad in allowed_dirs:
                safe_base = os.path.realpath(os.path.join(cwd, ad))
                safe_drive = os.path.splitdrive(safe_base)[0].lower()
                if target_drive != safe_drive:
                    continue
                if os.path.commonpath([safe_base, target_abs]) == safe_base:
                    is_safe = True
                    break
    except (ValueError, Exception):
        is_safe = False

    if not is_safe:
        return False, "", f"File path must reside strictly within allowed directories: {allowed_dirs}"

    # Disallow sensitive or system files
    basename = os.path.basename(target_abs)
    if basename.startswith(".") or basename in (".env", ".env.example", "bot.py"):
        return False, "", "Access to hidden or protected system files is forbidden."

    if allow_create_dir:
        os.makedirs(os.path.dirname(target_abs), exist_ok=True)

    return True, target_abs, None


def get_security_headers() -> Dict[str, str]:
    """Standard HTTP security headers for webhooks & health check endpoints."""
    return {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
        "X-XSS-Protection": "1; mode=block",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "interest-cohort=()",
        "Access-Control-Allow-Origin": os.getenv("CORS_ALLOWED_ORIGIN", ""),
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def check_http_rate_limit(client_ip: str, max_reqs: int = 60, window_sec: int = 60) -> bool:
    """In-memory rate limiter for HTTP health check endpoints (anti-DoS).
    Returns True if request is ALLOWED, False if RATE LIMITED.
    """
    now = time.time()
    if len(_HTTP_IP_RATE_BUCKETS) >= MAX_HTTP_IPS_TRACKED:
        # Prune expired IP records
        expired_ips = [
            ip for ip, timestamps in _HTTP_IP_RATE_BUCKETS.items()
            if not timestamps or timestamps[-1] <= now - window_sec
        ]
        for ip in expired_ips:
            _HTTP_IP_RATE_BUCKETS.pop(ip, None)

    timestamps = _HTTP_IP_RATE_BUCKETS.setdefault(client_ip, [])
    # Filter out entries older than window
    cutoff = now - window_sec
    while timestamps and timestamps[0] <= cutoff:
        timestamps.pop(0)

    if len(timestamps) >= max_reqs:
        return False

    timestamps.append(now)
    return True
