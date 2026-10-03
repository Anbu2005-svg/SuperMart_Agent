import os
import sys
import hashlib
import asyncio
import logging
from typing import Dict, Any, List, Optional
from dotenv import load_dotenv

# Fix Windows console Unicode encoding for emoji support
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf_8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Suppress HTTP client request logs to prevent Telegram bot token exposure in URLs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# In-memory guard: prevent the same Telegram update_id from being processed
# more than once in a single bot process (handles Telegram's retry/duplicate sends)
_PROCESSING_UPDATES: set = set()

# ── Persistent Token-Bucket Rate Limiter: 20 msgs / 60s ──
_RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "20"))
_RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))
_USER_RATE_BUCKETS: Dict[str, List[float]] = {}

def is_rate_limited(telegram_id: str) -> bool:
    """
    Persistent token-bucket rate limit per Telegram user to prevent API-cost abuse.
    Persists state in PostgreSQL across bot restarts and multiple worker instances,
    with an automatic in-memory fallback if the database is busy or unreachable.
    """
    import time as _time
    now = _time.time()
    max_tokens = float(_RATE_LIMIT_MAX)
    refill_rate = max_tokens / float(_RATE_LIMIT_WINDOW)

    try:
        from db.models import get_db_connection, immediate_transaction
        conn = get_db_connection()
        try:
            with immediate_transaction(conn):
                cur = conn.cursor()
                cur.execute(
                    "SELECT tokens, last_updated FROM user_rate_limits WHERE telegram_id = %s FOR UPDATE",
                    (str(telegram_id),)
                )
                row = cur.fetchone()
                if row:
                    elapsed = max(0.0, now - float(row["last_updated"]))
                    tokens = min(max_tokens, float(row["tokens"]) + elapsed * refill_rate)
                else:
                    tokens = max_tokens

                if tokens < 1.0:
                    cur.close()
                    return True

                tokens -= 1.0
                cur.execute("""
                    INSERT INTO user_rate_limits (telegram_id, tokens, last_updated)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (telegram_id)
                    DO UPDATE SET tokens = EXCLUDED.tokens, last_updated = EXCLUDED.last_updated
                """, (str(telegram_id), tokens, now))
                cur.close()
                return False
        finally:
            conn.close()
    except Exception:
        # Fallback to local in-memory token bucket if database is busy or unmigrated
        bucket = _USER_RATE_BUCKETS.setdefault(telegram_id, [])
        while bucket and bucket[0] <= now - _RATE_LIMIT_WINDOW:
            bucket.pop(0)
        if len(bucket) >= _RATE_LIMIT_MAX:
            return True
        bucket.append(now)
        return False

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
from db.models import init_db
from agent.control_loop import run_agent_turn, clear_conversation
from skills.documents import generate_invoice_pdf, generate_analysis_deck
from skills.inventory import get_product_count, populate_default_inventory
from skills.auth import (
    is_user_authenticated,
    register_shop,
    login_shop,
    get_user_session,
    logout_user_session
)

# User login/signup state machine: {telegram_id: {"step": "choice"|"signup_name"|"signup_pwd"|"signup_meta"|"login_name"|"login_pwd", "data": {}}}
USER_AUTH_STATE: Dict[str, Dict[str, Any]] = {}

def is_safe_generated_file(file_path: str) -> bool:
    """Validate that the file exists and is strictly located inside the generated_docs directory to prevent path traversal."""
    if not file_path or not isinstance(file_path, str):
        return False
    try:
        safe_base = os.path.realpath(os.path.abspath("generated_docs"))
        target_path = os.path.realpath(os.path.abspath(file_path))
        return os.path.commonpath([safe_base, target_path]) == safe_base and os.path.isfile(target_path)
    except Exception:
        return False

def get_auth_choice_keyboard():
    """Returns New Shop (Sign Up) vs Existing Shop (Log In) inline choice keyboard buttons."""
    keyboard = [
        [KeyboardButton(text="🆕 New Shop (Sign Up)")],
        [KeyboardButton(text="🔑 Existing Shop (Log In)")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

def get_empty_inventory_keyboard():
    """Returns inline keyboard asking user if they want to load default problem statement stocks."""
    keyboard = [
        [InlineKeyboardButton("📦 Add Default Problem Statement Stocks", callback_data="seed_default_stocks")],
        [InlineKeyboardButton("➕ Skip & Add Custom Stocks", callback_data="skip_default_stocks")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_barcode_action_keyboard(barcode: str, sku_id: str):
    """Returns inline buttons for product actions upon barcode scan."""
    keyboard = [
        [
            InlineKeyboardButton("🛒 Add to Bill", callback_data=f"bc_bill:{barcode}"),
            InlineKeyboardButton("📦 Restock +10", callback_data=f"bc_restock:{sku_id}")
        ],
        [
            InlineKeyboardButton("🔍 View Stock & Batches", callback_data=f"bc_view:{sku_id}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command — starts fresh conversation context for user."""
    telegram_id = str(update.effective_user.id) if update.effective_user else "default"
    chat_id = update.effective_chat.id
    clear_conversation(chat_id)
    USER_AUTH_STATE.pop(telegram_id, None)
    
    session = get_user_session(telegram_id)
    if not session:
        auth_msg = (
            "🏬 *Welcome to Supermarket Ops Agent!*\n\n"
            "To get started, please select whether you want to register a **New Shop** or log into an **Existing Shop**."
        )
        await update.message.reply_text(auth_msg, parse_mode="Markdown", reply_markup=get_auth_choice_keyboard())
        return

    welcome_text = (
        f"🛒 *Welcome back to {session['shop_name']}!*\n\n"
        f"📍 Address: {session['shop_address'] or 'Not specified'}\n"
        f"📑 GSTIN: {session['shop_gstin'] or 'Not specified'}\n\n"
        "You can manage your supermarket using natural language or slash commands:\n\n"
        "• `/stock` — View all products & inventory stock\n"
        "• `/lowstock` — View low stock reorder items\n"
        "• `/bill` — Create a bill (e.g. `/bill 2 sugar, 4 Maggi, UPI`)\n"
        "• `/khata` — View customer credit balances\n"
        "• `/summary` — View today's sales & revenue summary\n"
        "• `/invoice <bill_id>` — Download PDF Tax Invoice\n"
        "• `/analysis` — Download PowerPoint Sales Deck\n"
        "• `/logout` — Log out of this shop session"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())

    # Check if inventory is empty
    if get_product_count() == 0:
        empty_msg = (
            "⚠️ **Your Shop Inventory is currently empty (0 products)!**\n\n"
            "Would you like to auto-populate the **Default Problem Statement Stock Items** (10 essentials: Maggi, Wheat Atta, Sugar, Oil, Milk, Rice, Salt, Soap, Butter, Tea)?"
        )
        await update.message.reply_text(empty_msg, parse_mode="Markdown", reply_markup=get_empty_inventory_keyboard())

async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /logout command."""
    telegram_id = str(update.effective_user.id) if update.effective_user else "default"
    logout_user_session(telegram_id)
    USER_AUTH_STATE.pop(telegram_id, None)
    await update.message.reply_text("🔒 Logged out successfully. Send /start anytime to log into another shop session.", reply_markup=ReplyKeyboardRemove())

async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /new command — resets conversation memory while preserving database preferences."""
    telegram_id = str(update.effective_user.id) if update.effective_user else "default"
    if not is_user_authenticated(telegram_id):
        await update.message.reply_text("🔐 Authentication required. Send /start to log into your shop.", parse_mode="Markdown")
        return
        
    chat_id = update.effective_chat.id
    clear_conversation(chat_id)
    await update.message.reply_text("🔄 Conversation history cleared! Active draft bills and inventory data remain saved in the database.")

async def invoice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /invoice <bill_id> command."""
    telegram_id = str(update.effective_user.id) if update.effective_user else "default"
    if not is_user_authenticated(telegram_id):
        await update.message.reply_text("🔐 Authentication required. Send /start to log into your shop.", parse_mode="Markdown")
        return

    if not context.args:
        await update.message.reply_text("Please specify a Bill ID. Example: `/invoice BILL-12345678`", parse_mode="Markdown")
        return
        
    bill_id = context.args[0].strip()
    await update.message.reply_text(f"📄 Generating PDF Invoice for Bill `{bill_id}`...", parse_mode="Markdown")
    
    res = generate_invoice_pdf(bill_id)
    if res.get("status") == "success" and "file_path" in res:
        file_path = res["file_path"]
        if is_safe_generated_file(file_path):
            buttons = []
            try:
                from skills.whatsapp import generate_whatsapp_bill_link
                wa_res = generate_whatsapp_bill_link(bill_id)
                if wa_res.get("status") == "success" and wa_res.get("whatsapp_url"):
                    buttons.append([InlineKeyboardButton("💬 Share on WhatsApp", url=wa_res["whatsapp_url"])])
            except Exception:
                pass

            reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
            with open(file_path, "rb") as doc:
                await update.message.reply_document(
                    document=doc,
                    filename=os.path.basename(file_path),
                    caption=f"Tax Invoice for Bill {bill_id} (with embedded UPI QR Code)",
                    reply_markup=reply_markup
                )
            return
    await update.message.reply_text(f"❌ Failed to generate PDF invoice: {res.get('message', 'Unknown error')}")

async def analysis_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /analysis command."""
    telegram_id = str(update.effective_user.id) if update.effective_user else "default"
    if not is_user_authenticated(telegram_id):
        await update.message.reply_text("🔐 Authentication required. Send /start to log into your shop.", parse_mode="Markdown")
        return

    period = " ".join(context.args) if context.args else "Today"
    await update.message.reply_text(f"📊 Generating PowerPoint Operations & Sales Deck for period '{period}'...", parse_mode="Markdown")
    
    res = generate_analysis_deck(period)
    if res.get("status") == "success" and "file_path" in res:
        file_path = res["file_path"]
        if is_safe_generated_file(file_path):
            with open(file_path, "rb") as doc:
                await update.message.reply_document(
                    document=doc,
                    filename=os.path.basename(file_path),
                    caption=f"Supermarket Operations & Sales Analysis Deck ({period})"
                )
            return
    await update.message.reply_text(f"❌ Failed to generate analysis deck: {res.get('message', 'Unknown error')}")

async def upi_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /upi <bill_id or amount> command — generates dynamic UPI QR code with GPay/PhonePe/Paytm link."""
    telegram_id = str(update.effective_user.id) if update.effective_user else "default"
    session = get_user_session(telegram_id)
    if not session:
        await update.message.reply_text("🔐 Authentication required. Send /start to log into your shop.", parse_mode="Markdown")
        return

    if not context.args:
        await update.message.reply_text(
            "📱 **Dynamic UPI QR Code Generator**\n\n"
            "• **Generate for a bill:** `/upi <bill_id>` (e.g. `/upi BILL-804AE9F8`)\n"
            "• **Generate for an amount:** `/upi <amount>` (e.g. `/upi 250` or `/upi 49.50`)\n\n"
            "⚡ Generates instant GPay, PhonePe, Paytm, and BHIM scannable payment QR code!",
            parse_mode="Markdown"
        )
        return

    arg = context.args[0].strip()
    from skills.upi import generate_upi_qr_for_bill, generate_upi_qr_code

    if arg.upper().startswith("BILL-"):
        res = generate_upi_qr_for_bill(arg)
    else:
        try:
            amt = float(arg.replace("₹", "").replace(",", ""))
            note = " ".join(context.args[1:]) if len(context.args) > 1 else "Supermarket Payment"
            res = generate_upi_qr_code(amount=amt, note=note)
        except ValueError:
            res = generate_upi_qr_for_bill(arg)

    if res.get("status") == "success" and "file_path" in res:
        qr_file = res["file_path"]
        if os.path.exists(qr_file):
            caption = (
                f"📱 **Dynamic UPI Payment QR**\n\n"
                f"• **Amount:** ₹{res['amount']:.2f}\n"
                f"• **Payee VPA:** `{res['vpa']}`\n"
                f"• **Merchant:** {res['merchant_name']}\n\n"
                f"⚡ Scan with Google Pay, PhonePe, Paytm, or BHIM to pay instantly!"
            )
            with open(qr_file, "rb") as photo:
                await update.message.reply_photo(
                    photo=photo,
                    caption=caption,
                    parse_mode="Markdown"
                )
            return

    await update.message.reply_text(f"❌ Failed to generate UPI QR: {res.get('message', 'Unknown error')}")

async def expiry_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /expiry [days] command — smart expiry alerts & dynamic clearance markdowns."""
    telegram_id = str(update.effective_user.id) if update.effective_user else "default"
    session = get_user_session(telegram_id)
    if not session:
        await update.message.reply_text("🔐 Authentication required. Send /start to log into your shop.", parse_mode="Markdown")
        return

    days_ahead = 15
    if context.args:
        try:
            days_ahead = max(1, min(90, int(context.args[0].strip())))
        except ValueError:
            pass

    from skills.expiry import recommend_markdown_discounts
    res = recommend_markdown_discounts(days_ahead=days_ahead)

    buttons = []
    for item in res.get("recommendations", [])[:5]:
        if item["days_left"] > 0:
            btn_text = f"⚡ Apply {item['discount_pct']:.0f}% Off ({item['product_name'][:14]})"
            cb_data = f"md_disc:{item['sku_id']}:{int(item['discount_pct'])}"
            buttons.append([InlineKeyboardButton(btn_text, callback_data=cb_data)])

    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
    await update.message.reply_text(res.get("message", "No expiry data."), parse_mode="Markdown", reply_markup=reply_markup)

async def handle_voice_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle voice notes and audio clips: transcribe using Groq Whisper AI in real-time
    and route the transcript through the agent for multilingual voice note billing.
    Supports English, Tamil, Hindi, Hinglish, Tanglish.
    """
    if not update.message or not (update.message.voice or update.message.audio):
        return

    telegram_id = str(update.effective_user.id) if update.effective_user else "default"
    session = get_user_session(telegram_id)
    if not session:
        await update.message.reply_text("🔐 Authentication required. Send /start to log into your shop.", parse_mode="Markdown")
        return

    # Check for Groq or Whisper API key
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    voice_key = os.getenv("VOICE_TRANSCRIBE_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()

    if not (groq_key or voice_key or openai_key):
        await update.message.reply_text(
            "🎙️ **Voice note received!**\n\n"
            "To enable free voice billing:\n"
            "1. Create a 100% free key at [console.groq.com](https://console.groq.com) (no credit card required).\n"
            "2. Add `GROQ_API_KEY=gsk_...` into your `.env` file.\n\n"
            "For now, please type your message as text! 🛒",
            parse_mode="Markdown"
        )
        return

    waiting = await update.message.reply_text("🎤 Transcribing your voice message (Groq Whisper AI)...")

    try:
        audio_target = update.message.voice or update.message.audio
        is_voice = bool(update.message.voice)
        filename = "voice.ogg" if is_voice else getattr(audio_target, "file_name", "voice.mp3")

        voice_file = await context.bot.get_file(audio_target.file_id)
        raw_bytes = await voice_file.download_as_bytearray()

        from skills.voice import transcribe_audio
        res = await asyncio.to_thread(transcribe_audio, bytes(raw_bytes), filename)

        if res.get("status") == "success" and res.get("transcript"):
            transcript = res["transcript"]
            await waiting.edit_text(f"🎧 Transcribed ({res.get('provider', 'Groq')}): \"{transcript}\" — processing...")
            await handle_message(update, context, user_text_override=transcript)
            return
        elif res.get("status") == "empty":
            await waiting.edit_text("⚠️ Voice message was empty or no speech was detected. Please try speaking again or send text.")
            return
        else:
            await waiting.edit_text(f"⚠️ Voice transcription error: {res.get('message', 'Please type your request as text.')}")
            return
    except asyncio.TimeoutError:
        logger.warning("Voice transcription timed out.")
        await waiting.edit_text("⏱️ Voice transcription timed out. Please send your request as text.")
        return
    except Exception as e:
        logger.warning(f"Voice handling exception: {e}")
        await waiting.edit_text("⚠️ Couldn't transcribe the voice note. Please type your request as text.")
        return


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text_override: Optional[str] = None):
    """Handle regular text messages and multi-step shop authentication state machine."""
    if not update.message:
        return

    telegram_id = str(update.effective_user.id) if update.effective_user else "default"
    user_text = user_text_override or (update.message.text or "").strip()
    if not user_text:
        return

    # Check if user has an active shop session
    session = get_user_session(telegram_id)
    
    # State machine for unauthenticated users (Login / Sign Up flow)
    if not session:
        state = USER_AUTH_STATE.get(telegram_id, {}).get("step")
        
        if not state or user_text in ["🆕 New Shop (Sign Up)", "🔑 Existing Shop (Log In)"]:
            if user_text == "🆕 New Shop (Sign Up)":
                USER_AUTH_STATE[telegram_id] = {"step": "signup_name", "data": {}}
                await update.message.reply_text("📝 *New Shop Registration*\n\nPlease enter your **Shop Name** (Mandatory):", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
                return
            elif user_text == "🔑 Existing Shop (Log In)":
                USER_AUTH_STATE[telegram_id] = {"step": "login_name", "data": {}}
                await update.message.reply_text("🔑 *Shop Login*\n\nPlease enter your **Shop Name**:", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
                return
            else:
                await update.message.reply_text("🏬 Please select an option below:", parse_mode="Markdown", reply_markup=get_auth_choice_keyboard())
                return

        # Handle Registration Steps
        if state == "signup_name":
            USER_AUTH_STATE[telegram_id]["data"]["shop_name"] = user_text
            USER_AUTH_STATE[telegram_id]["step"] = "signup_pwd"
            await update.message.reply_text(f"🔐 Setting up **{user_text}**.\n\nPlease enter a **Password** for this shop (Mandatory):", parse_mode="Markdown")
            return

        elif state == "signup_pwd":
            try:
                await update.message.delete()
            except Exception:
                pass
            USER_AUTH_STATE[telegram_id]["data"]["password"] = user_text
            USER_AUTH_STATE[telegram_id]["step"] = "signup_meta"
            await update.message.reply_text(
                "📍 *Optional Shop Details*\n\n"
                "Enter **Shop Address & GSTIN** separated by comma (or reply `skip` to complete registration):\n"
                "Example: `123 Main St Chennai, 33AABCU9603R1ZM`",
                parse_mode="Markdown"
            )
            return

        elif state == "signup_meta":
            shop_name = USER_AUTH_STATE[telegram_id]["data"]["shop_name"]
            password = USER_AUTH_STATE[telegram_id]["data"]["password"]
            
            shop_address = None
            shop_gstin = None
            if user_text.lower() != "skip":
                parts = [p.strip() for p in user_text.split(",") if p.strip()]
                if len(parts) >= 1:
                    shop_address = parts[0]
                if len(parts) >= 2:
                    shop_gstin = parts[1]
                    # Validate GSTIN format (15 chars: 2-digit state + 10-char PAN + entity + checksum)
                    from agent.harness import validate_gstin
                    if not validate_gstin(shop_gstin):
                        await update.message.reply_text(
                            "⚠️ That GSTIN doesn't look valid. A GSTIN has 15 characters like `33AABCU9603R1ZM`.\n\n"
                            "Please re-enter **Address, GSTIN** (or reply `skip` to finish without GSTIN).",
                            parse_mode="Markdown"
                        )
                        return

            reg_res = register_shop(shop_name=shop_name, password=password, shop_address=shop_address, shop_gstin=shop_gstin)
            if reg_res.get("status") == "success":
                login_res = login_shop(telegram_id=telegram_id, shop_name=shop_name, password=password)
                USER_AUTH_STATE.pop(telegram_id, None)
                welcome_new_shop = (
                    f"🎉 **Registration Successful!** Shop **{shop_name}** created!\n\n"
                    "📦 **Inventory Setup Option**:\n"
                    "Would you like to auto-populate the **Default Problem Statement Stock Items** (10 essentials: Maggi, Wheat Atta, Sugar, Oil, Milk, Rice, Salt, Soap, Butter, Tea) to get started immediately, or add your own custom stocks?"
                )
                await update.message.reply_text(welcome_new_shop, parse_mode="Markdown", reply_markup=get_empty_inventory_keyboard())
            else:
                await update.message.reply_text(f"❌ {reg_res.get('message')}\n\nPlease try again by clicking /start.")
                USER_AUTH_STATE.pop(telegram_id, None)
            return

        # Handle Login Steps
        elif state == "login_name":
            USER_AUTH_STATE[telegram_id]["data"]["shop_name"] = user_text
            USER_AUTH_STATE[telegram_id]["step"] = "login_pwd"
            await update.message.reply_text(f"🔑 Enter Password for shop **{user_text}**:", parse_mode="Markdown")
            return

        elif state == "login_pwd":
            try:
                await update.message.delete()
            except Exception:
                pass
            shop_name = USER_AUTH_STATE[telegram_id]["data"]["shop_name"]
            password = user_text
            login_res = login_shop(telegram_id=telegram_id, shop_name=shop_name, password=password)
            USER_AUTH_STATE.pop(telegram_id, None)
            
            if login_res.get("status") == "success":
                if get_product_count() == 0:
                    empty_msg = (
                        f"✅ **Login Successful!** Connected to **{shop_name}**.\n\n"
                        "⚠️ **Your Shop Inventory is currently empty (0 products)!**\n\n"
                        "Would you like to auto-populate the **Default Problem Statement Stock Items** (10 essentials: Maggi, Wheat Atta, Sugar, Oil, Milk, Rice, Salt, Soap, Butter, Tea)?"
                    )
                    await update.message.reply_text(empty_msg, parse_mode="Markdown", reply_markup=get_empty_inventory_keyboard())
                else:
                    await update.message.reply_text(
                        f"✅ **Login Successful!** Connected to **{shop_name}**.\n\nYou now have full access to this shop's database. Type `/stock` or ask any query!",
                        parse_mode="Markdown"
                    )
            else:
                await update.message.reply_text(f"❌ {login_res.get('message')}\n\nPlease try logging in again with /start.")
            return

    chat_id = update.effective_chat.id
    owner_id = telegram_id
    update_id = str(update.update_id)

    # ⚡ Per-user rate limiting — refuse bursts that would burn LLM tokens
    if is_rate_limited(telegram_id):
        await update.message.reply_text(
            f"⏳ You're sending messages too quickly. Please wait a moment (limit: {_RATE_LIMIT_MAX} messages / {_RATE_LIMIT_WINDOW}s)."
        )
        return

    # ⚡ In-memory dedup: silently drop if this update_id is already being processed
    # (handles Telegram retries / duplicate deliveries within the same process)
    if update_id in _PROCESSING_UPDATES:
        logger.info(f"Duplicate update_id {update_id} already in-flight — silently dropped.")
        return
    _PROCESSING_UPDATES.add(update_id)

    try:
        # ⚡ 1. Send immediate typing status to Telegram chat header
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass

        # ⚡ 2. Send instant "thinking" placeholder message — user sees feedback in chat instantly
        thinking_phrases = [
            "🤔 *Agent is thinking...*",
            "⚙️ *Processing your request...*",
            "🔍 *Looking up your supermarket data...*",
        ]
        import hashlib as _hs
        phrase_idx = int(_hs.md5(user_text.encode()).hexdigest(), 16) % len(thinking_phrases)
        thinking_msg = await update.message.reply_text(
            thinking_phrases[phrase_idx], parse_mode="Markdown"
        )

        # 💬 Keep sending typing action in background so Telegram shows "typing..." in chat header
        async def keep_typing():
            try:
                while True:
                    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
                    await asyncio.sleep(4)
            except asyncio.CancelledError:
                pass

        typing_task = asyncio.create_task(keep_typing())

        try:
            reply_text, generated_files = await asyncio.to_thread(
                run_agent_turn,
                user_message=user_text,
                chat_id=chat_id,
                owner_id=owner_id,
                update_id=update_id
            )
        except Exception as err:
            logger.error(f"Error during agent turn for chat {chat_id}: {err}", exc_info=True)
            reply_text = "⚠️ An unexpected error occurred while processing your request. Please try again in a moment."
            generated_files = []
        finally:
            typing_task.cancel()

        # ✅ Edit the "thinking" placeholder with the actual response
        try:
            await thinking_msg.edit_text(reply_text, parse_mode="Markdown")
        except Exception:
            try:
                await thinking_msg.edit_text(reply_text)
            except Exception:
                # If edit fails (e.g. message too old), send as new message
                try:
                    await update.message.reply_text(reply_text, parse_mode="Markdown")
                except Exception:
                    await update.message.reply_text(reply_text)

        # Send generated document files safely (confined strictly to generated_docs/)
        for file_path in generated_files:
            if is_safe_generated_file(file_path):
                with open(file_path, "rb") as doc:
                    await update.message.reply_document(
                        document=doc,
                        filename=os.path.basename(file_path),
                        caption=f"Generated File: {os.path.basename(file_path)}"
                    )
    finally:
        # Always release the in-flight guard — even if an error occurred
        _PROCESSING_UPDATES.discard(update_id)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command — displays command menu and quick start guide."""
    help_text = (
        "🤖 *Supermarket Ops Agent — Available Commands*\n\n"
        "• `/start` — Start bot session & verify mobile contact\n"
        "• `/barcode <number>` — Scan photo or enter barcode\n"
        "• `/stock` — View inventory products, quantities & MRPs\n"
        "• `/lowstock` — View low stock items at or below reorder level\n"
        "• `/bill <items>` — Create draft bill (e.g. `/bill 2 sugar, 4 maggi, UPI`)\n"
        "• `/upi <bill_id | amount>` — Dynamic UPI QR code (GPay/PhonePe/Paytm/BHIM)\n"
        "• `/expiry [days]` — Smart expiry tracking & clearance markdown engine\n"
        "• `/khata [customer]` — View customer credit ledgers & WhatsApp reminders\n"
        "• `/summary` — View today's sales & revenue breakdown\n"
        "• `/invoice <bill_id>` — Download official PDF GST Tax Invoice with embedded UPI QR\n"
        "• `/analysis <period>` — Download PowerPoint (.pptx) operations sales deck\n"
        "• `/new` — Reset conversation context (standing preferences persist)\n"
        "• `/logout` — De-authenticate user session\n"
        "• `/help` — Show this interactive command guide\n\n"
        "🎙️ *Voice Note Billing:* Send a Telegram voice note in **Tamil, Hindi, or English** (e.g. _'2 packet Maggi bill pannunga'_ or _'1kg sugar Ramesh khata me dalo'_)\n\n"
        "💬 *You can also ask anything in plain text:* e.g. \"Show stock\", \"Start a bill\", \"Charge khata ₹500 to Ravi\", \"Show today's sales summary\""
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks for populating default problem statement stocks with auth check."""
    query = update.callback_query
    await query.answer()

    telegram_id = str(update.effective_user.id) if update.effective_user else "default"
    if not is_user_authenticated(telegram_id):
        await query.edit_message_text("🔐 Authentication required. Please send /start to log in first.")
        return

    if query.data == "seed_default_stocks":
        res = populate_default_inventory()
        if res.get("status") == "success":
            msg = (
                "✅ **Default Problem Statement Stocks Populated!**\n\n"
                "📦 **Problem Statement SKUs Loaded:**\n"
                "• `[SKU-ATTA-5K]` Aashirvaad Whole Wheat Atta 5kg — MRP ₹245 (Stock: 30)\n"
                "• `[SKU-SALT-01]` Tata Iodized Salt 1kg — MRP ₹28 (Stock: 50)\n"
                "• `[SKU-BUTTER-100]` Amul Pasteurised Butter 100g — MRP ₹62 (Stock: 20)\n"
                "• `[SKU-OIL-1L]` Fortune Sunlite Sunflower Oil 1L — MRP ₹155 (Stock: 40)\n"
                "• `[SKU-MAGGI-70]` Maggi 2-Minute Instant Noodles 70g — MRP ₹14 (Stock: 100)\n"
                "• `[SKU-PARLEG-80]` Parle-G Gold Biscuits 80g — MRP ₹10 (Stock: 80)\n"
                "• `[SKU-SURF-1K]` Surf Excel Detergent Powder 1kg — MRP ₹140 (Stock: 25)\n"
                "• `[SKU-MILK-1L]` Amul Taaza Toned Milk 1L — MRP ₹56 (Stock: 25)\n"
                "• `[SKU-SUGAR-1K]` Refined White Sugar 1kg (Loose) — MRP ₹48 (Stock: 60)\n"
                "• `[SKU-RICE-1K]` Basmati Rice 1kg (Loose) — MRP ₹80 (Stock: 50)\n"
                "• `[SKU-DAL-1K]` Toor Dal 1kg (Loose) — MRP ₹135 (Stock: 40)\n"
                "• `[SKU-TEA-250]` Brooke Bond Red Label Tea 250g — MRP ₹140 (Stock: 15)\n\n"
                "🛒 Your shop is ready! Type `/stock` or `/bill` to start."
            )
            await query.edit_message_text(msg, parse_mode="Markdown")
        else:
            await query.edit_message_text(f"❌ {res.get('message')}")
    elif query.data == "skip_default_stocks":
        await query.edit_message_text(
            "👍 **Got it! Starting with clean inventory.**\n\n"
            "You can add products anytime by asking the agent, e.g.:\n"
            "`Add product Milk 1L, MRP 60, Cost 50, Stock 20` or type `/stock`!",
            parse_mode="Markdown"
        )
    elif query.data.startswith("bc_bill:"):
        bc = query.data.split(":", 1)[1]
        from skills.barcode import lookup_product_by_barcode
        from skills.billing import start_bill, add_item_to_bill
        lk = lookup_product_by_barcode(bc)
        if lk.get("status") == "success":
            p = lk["product"]
            b = start_bill()
            add_res = add_item_to_bill(b["bill_id"], p["sku_id"], 1)
            if add_res.get("status") == "success":
                await query.edit_message_text(
                    f"🛒 **Added 1x {p['name']} to draft bill `{b['bill_id']}`!**\n\n"
                    f"Subtotal: ₹{p['mrp']:.2f}\n"
                    f"Type `/bill` to view or finalize payment.",
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text(f"⚠️ Could not add to bill: {add_res.get('message')}")
        else:
            await query.edit_message_text(f"❌ Product not found for barcode `{bc}`")
    elif query.data.startswith("bc_restock:"):
        sku = query.data.split(":", 1)[1]
        from skills.inventory import receive_stock
        res = receive_stock(sku_id=sku, qty=10)
        if res.get("status") == "success":
            await query.edit_message_text(f"📦 **Restocked +10 units!** New stock: {res['product']['quantity']} {res['product']['unit']}", parse_mode="Markdown")
        else:
            await query.edit_message_text(f"❌ Failed to restock: {res.get('message')}")
    elif query.data.startswith("bc_view:"):
        sku = query.data.split(":", 1)[1]
        from skills.inventory import get_stock
        st = get_stock(sku)
        if st.get("status") == "success":
            p = st["product"]
            await query.edit_message_text(
                f"📊 **Product Details:**\n\n"
                f"• **Name:** {p['name']}\n"
                f"• **SKU:** `{p['sku_id']}`\n"
                f"• **Stock:** {p['quantity']} {p['unit']}\n"
                f"• **Reorder Level:** {p['reorder_level']}\n"
                f"• **Cost:** ₹{p['cost_price']} | **MRP:** ₹{p['mrp']}",
                parse_mode="Markdown"
            )
    elif query.data.startswith("md_disc:"):
        parts = query.data.split(":", 2)
        sku = parts[1]
        disc_pct = float(parts[2])
        from skills.expiry import apply_clearance_discount
        res = apply_clearance_discount(sku, disc_pct)
        if res.get("status") == "success":
            await query.edit_message_text(res["message"], parse_mode="Markdown")
        else:
            await query.edit_message_text(f"❌ {res.get('message', 'Failed to apply clearance discount')}")

async def stock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stock command."""
    if get_product_count() == 0:
        empty_msg = (
            "📦 *Shop Inventory is Empty (0 products)*\n\n"
            "Would you like to auto-populate the **Default Problem Statement Stock Items** (10 essentials: Maggi, Wheat Atta, Sugar, Oil, Milk, Rice, Salt, Soap, Butter, Tea)?"
        )
        await update.message.reply_text(empty_msg, parse_mode="Markdown", reply_markup=get_empty_inventory_keyboard())
        return

    await handle_message(update, context, user_text_override="Show all products in stock with prices and quantities")

async def lowstock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /lowstock command."""
    await handle_message(update, context, user_text_override="List all low stock items at or below reorder level")

async def bill_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /bill command."""
    args = " ".join(context.args) if context.args else ""
    user_text = f"make a bill: {args}" if args else "Start a new draft bill"
    await handle_message(update, context, user_text_override=user_text)

async def khata_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /khata command."""
    args = " ".join(context.args) if context.args else ""
    user_text = f"Khata query for {args}" if args else "List all customer khata credit balances"
    await handle_message(update, context, user_text_override=user_text)

async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /summary command."""
    await handle_message(update, context, user_text_override="Show today's sales summary and total revenue breakdown")

async def barcode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /barcode <number> command for handheld scanners or manual code entry."""
    telegram_id = str(update.effective_user.id) if update.effective_user else "default"
    session = get_user_session(telegram_id)
    if not session:
        await update.message.reply_text("🔐 Authentication required. Send /start to log into your shop.", parse_mode="Markdown")
        return

    if not context.args:
        await update.message.reply_text(
            "📸 **Barcode & QR Scanner**\n\n"
            "• **Send a photo:** Snap a picture of any product barcode/QR code and send it directly into this chat.\n"
            "• **Type barcode:** `/barcode <number>` (e.g. `/barcode 8901030383458`)\n"
            "• **Handheld scanner:** Aim and scan with your USB/Bluetooth barcode gun into the chat!",
            parse_mode="Markdown"
        )
        return

    barcode_str = context.args[0].strip()
    from skills.barcode import lookup_product_by_barcode
    lookup = lookup_product_by_barcode(barcode_str)

    if lookup.get("status") == "success":
        prod = lookup["product"]
        caption = (
            f"✅ **Barcode Scanned:** `{barcode_str}`\n\n"
            f"🏷️ **Product:** {prod['name']}\n"
            f"• **SKU:** `{prod['sku_id']}`\n"
            f"• **Price (MRP):** ₹{prod['mrp']:.2f} (GST: {prod['gst_slab']}%)\n"
            f"• **Current Stock:** {prod['quantity']} {prod['unit']}"
        )
        if prod.get("earliest_batch") and prod["earliest_batch"].get("expiry_date"):
            caption += f"\n• **Nearest Expiry:** {prod['earliest_batch']['expiry_date']} (Batch: {prod['earliest_batch']['batch_code']})"

        await update.message.reply_text(
            caption,
            parse_mode="Markdown",
            reply_markup=get_barcode_action_keyboard(barcode_str, prod["sku_id"])
        )
    elif lookup.get("status") == "global_recognized":
        gp = lookup["global_product"]
        caption = (
            f"🌍 **Global Product Recognized!**\n\n"
            f"🏷️ **Product:** {gp['name']}\n"
            f"• **Brand:** {gp['brand']}\n"
            f"• **Category:** {gp['category']}\n"
            f"• **Origin:** {gp['origin_country']}\n"
            f"• **Barcode:** `{barcode_str}`\n\n"
            "💡 *This product is verified in the global product registry, but not yet in your local supermarket inventory.*\n\n"
            "To add it to your shop, ask the agent:\n"
            f"`Add {gp['name']} to inventory with price <MRP> and stock <qty>`"
        )
        await update.message.reply_text(caption, parse_mode="Markdown")
    else:
        origin = lookup.get("origin_country", "International")
        await update.message.reply_text(
            f"📦 **Unregistered Barcode:** `{barcode_str}`\n"
            f"🌐 **GS1 Origin:** {origin}\n\n"
            "This barcode is not mapped to any product in your supermarket.\n\n"
            "To link it to an existing product, ask the agent:\n"
            f"`Link barcode {barcode_str} to <product name>`\n"
            "Or register it as a new product:\n"
            f"`Add product <name> with barcode {barcode_str} price <MRP>`",
            parse_mode="Markdown"
        )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming product photo or barcode image upload."""
    if not update.message or not update.message.photo:
        return

    telegram_id = str(update.effective_user.id) if update.effective_user else "default"
    session = get_user_session(telegram_id)
    if not session:
        await update.message.reply_text("🔐 Authentication required. Send /start to log into your shop.", parse_mode="Markdown")
        return

    status_msg = await update.message.reply_text("🔍 Scanning barcode from image...", parse_mode="Markdown")

    try:
        photo = update.message.photo[-1]
        photo_file = await context.bot.get_file(photo.file_id)
        photo_bytes = await photo_file.download_as_bytearray()

        from skills.barcode import scan_barcode_from_image, lookup_product_by_barcode
        scan_res = scan_barcode_from_image(bytes(photo_bytes))

        if scan_res.get("status") != "success" or not scan_res.get("primary_barcode"):
            await status_msg.edit_text(
                "🔍 **No Barcode Detected**\n\n"
                "Could not detect a clear barcode in this photo.\n\n"
                "💡 *Tips for better scanning:*\n"
                "• Hold the camera steady and flat directly over the barcode\n"
                "• Ensure good lighting without glare or shadow\n"
                "• Or manually enter the number using `/barcode <digits>`",
                parse_mode="Markdown"
            )
            return

        barcode_str = scan_res["primary_barcode"]
        lookup = lookup_product_by_barcode(barcode_str)

        if lookup.get("status") == "success":
            prod = lookup["product"]
            caption = (
                f"✅ **Barcode Scanned:** `{barcode_str}`\n\n"
                f"🏷️ **Product:** {prod['name']}\n"
                f"• **SKU:** `{prod['sku_id']}`\n"
                f"• **Price (MRP):** ₹{prod['mrp']:.2f} (GST: {prod['gst_slab']}%)\n"
                f"• **Current Stock:** {prod['quantity']} {prod['unit']}"
            )
            if prod.get("earliest_batch") and prod["earliest_batch"].get("expiry_date"):
                caption += f"\n• **Nearest Expiry:** {prod['earliest_batch']['expiry_date']} (Batch: {prod['earliest_batch']['batch_code']})"

            await status_msg.edit_text(
                caption,
                parse_mode="Markdown",
                reply_markup=get_barcode_action_keyboard(barcode_str, prod["sku_id"])
            )
        elif lookup.get("status") == "global_recognized":
            gp = lookup["global_product"]
            caption = (
                f"🌍 **Global Product Recognized!**\n\n"
                f"🏷️ **Product:** {gp['name']}\n"
                f"• **Brand:** {gp['brand']}\n"
                f"• **Category:** {gp['category']}\n"
                f"• **Origin:** {gp['origin_country']}\n"
                f"• **Barcode:** `{barcode_str}`\n\n"
                "💡 *This product is verified in the global registry (Open Food Facts) but not in your local shop inventory yet.*\n\n"
                "To add it to your shop, tell the agent:\n"
                f"`Add {gp['name']} to inventory with price <MRP> and stock <qty>`"
            )
            await status_msg.edit_text(caption, parse_mode="Markdown")
        else:
            origin = lookup.get("origin_country", "International")
            await status_msg.edit_text(
                f"📦 **Unregistered Barcode Scanned:** `{barcode_str}`\n"
                f"🌐 **GS1 Origin:** {origin}\n\n"
                "This barcode is not mapped to any product in your supermarket.\n\n"
                "To link it to an existing product, ask the agent:\n"
                f"`Link barcode {barcode_str} to <product name>`\n"
                "Or register it as a new product:\n"
                f"`Add product <name> with barcode {barcode_str} price <MRP>`",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Error handling barcode photo: {e}", exc_info=True)
        await status_msg.edit_text("⚠️ An error occurred while scanning the image. Please try again.")

async def post_init(application):
    """Register interactive slash commands list with Telegram UI popup menu."""
    commands = [
        BotCommand("start", "Start session & fresh context"),
        BotCommand("barcode", "Scan or enter barcode (e.g. /barcode 8901030383458)"),
        BotCommand("stock", "View all products & inventory stock"),
        BotCommand("lowstock", "View low stock reorder items"),
        BotCommand("bill", "Create a bill (e.g. /bill 2 sugar, UPI)"),
        BotCommand("upi", "Dynamic UPI QR code (e.g. /upi 250)"),
        BotCommand("expiry", "Smart expiry alerts & clearance discounts"),
        BotCommand("khata", "View customer credit balances"),
        BotCommand("summary", "View today's sales & revenue summary"),
        BotCommand("invoice", "Download PDF GST Tax Invoice"),
        BotCommand("analysis", "Download PowerPoint Sales Deck"),
        BotCommand("new", "Reset chat history fresh"),
        BotCommand("help", "Show interactive commands guide"),
        BotCommand("logout", "Logout & clear session")
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Successfully pushed comprehensive bot commands menu to Telegram API.")

def start_health_check_server():
    """Starts a hardened HTTP server in a background thread to satisfy Render Web Service port checks."""
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from skills.security import get_security_headers, check_http_rate_limit

    class HealthCheckHandler(BaseHTTPRequestHandler):
        server_version = "SuperMartOps/2026"
        sys_version = ""

        def _send_headers(self, status_code: int, content_type: str = "text/plain; charset=utf-8"):
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            for header, value in get_security_headers().items():
                self.send_header(header, value)
            self.end_headers()

        def do_OPTIONS(self):
            """Handle CORS pre-flight checks cleanly."""
            self._send_headers(204)

        def do_HEAD(self):
            """Handle HEAD requests."""
            client_ip = self.client_address[0] if self.client_address else "127.0.0.1"
            if not check_http_rate_limit(client_ip):
                self._send_headers(429)
                return
            if self.path in ("/", "/health", "/healthz"):
                self._send_headers(200)
            else:
                self._send_headers(404)

        def do_GET(self):
            client_ip = self.client_address[0] if self.client_address else "127.0.0.1"
            if not check_http_rate_limit(client_ip):
                self._send_headers(429)
                self.wfile.write(b"429 Too Many Requests - Rate limit exceeded\n")
                return

            if self.path in ("/", "/health", "/healthz"):
                self._send_headers(200)
                self.wfile.write(b"SuperMarket Ops Agent is healthy!\n")
            else:
                self._send_headers(404)
                self.wfile.write(b"404 Not Found\n")

        def do_POST(self):
            self._send_headers(405)
            self.wfile.write(b"405 Method Not Allowed\n")

        def do_PUT(self):
            self.do_POST()

        def do_DELETE(self):
            self.do_POST()

        def log_message(self, format, *args):
            return  # Suppress HTTP server access logs

    port = int(os.getenv("PORT", "8080"))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(f"🌐 Health check HTTP server listening on port {port}")
    except Exception as e:
        print(f"⚠️ Could not start health check server on port {port}: {e}")


def start_keep_alive_pinger():
    """Periodically pings the Render Web Service URL to prevent free tier 15-minute spin-down."""
    import threading
    import time
    import urllib.request

    enable_keep_alive = os.getenv("ENABLE_KEEP_ALIVE", "false").lower().strip() in ("true", "1", "yes")
    url = os.getenv("KEEP_ALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if not enable_keep_alive or not url:
        return

    def ping_loop():
        while True:
            time.sleep(600)  # Ping every 10 minutes (before 15-min spin-down)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "RenderKeepAlive/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    logger.info(f"Keep-alive ping to {url} status: {resp.status}")
            except Exception as e:
                logger.warning(f"Keep-alive ping failed: {e}")

    thread = threading.Thread(target=ping_loop, daemon=True)
    thread.start()
    print(f"🔄 Self-pinging keep-alive enabled for {url} (every 10 mins).")


def start_daily_morning_logout_scheduler():
    """
    Background daemon thread that triggers an automatic logout of all accounts
    every morning between 4:00 AM and 5:00 AM Indian Standard Time (IST).
    Default reset time: 4:30 AM IST (configurable via DAILY_LOGOUT_HOUR_IST / DAILY_LOGOUT_MINUTE_IST).
    """
    import threading
    import time
    from datetime import datetime, timezone, timedelta
    from skills.auth import logout_all_sessions

    IST = timezone(timedelta(hours=5, minutes=30), name="IST")
    reset_hour = int(os.getenv("DAILY_LOGOUT_HOUR_IST", "4"))
    reset_minute = int(os.getenv("DAILY_LOGOUT_MINUTE_IST", "30"))

    def scheduler_loop():
        last_run_date = None
        while True:
            try:
                now_ist = datetime.now(IST)
                current_date = now_ist.date()
                if now_ist.hour == reset_hour and now_ist.minute >= reset_minute and last_run_date != current_date:
                    logger.info("🌅 Executing daily morning logout of all sessions (IST %02d:%02d)...", now_ist.hour, now_ist.minute)
                    count = logout_all_sessions()
                    USER_AUTH_STATE.clear()
                    last_run_date = current_date
                    logger.info("✅ Morning reset complete: %d active session(s) logged out for the new day.", count)
            except Exception as e:
                logger.error("Error in daily morning logout scheduler: %s", e)
            time.sleep(30)

    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()
    print(f"🌅 Daily morning logout scheduler active (every morning at {reset_hour:02d}:{reset_minute:02d} AM IST).")


def start_weekly_deck_scheduler():
    """
    Scheduled weekly analysis deck auto-send (stretch goal).
    Every Sunday ~8:00 PM IST, generates the weekly analysis PPTX and pushes it
    to every shop owner with an active session (or all authenticated users).
    Enable with WEEKLY_DECK_ENABLED=true. Delegates generation to the agent's
    own document tools (never screenshots).
    """
    import threading
    import time as _time
    from datetime import datetime, timezone, timedelta
    from skills.documents import generate_analysis_deck

    if os.getenv("WEEKLY_DECK_ENABLED", "false").lower().strip() not in ("true", "1", "yes"):
        return

    IST = timezone(timedelta(hours=5, minutes=30), name="IST")
    # Day 6 = Sunday (Python weekday(): Monday=0)
    target_dow = int(os.getenv("WEEKLY_DECK_DOW", "6"))
    target_hour = int(os.getenv("WEEKLY_DECK_HOUR_IST", "20"))

    def scheduler_loop():
        last_sent_week = None
        while True:
            try:
                now_ist = datetime.now(IST)
                iso_year, iso_week, _ = now_ist.isocalendar()
                week_key = f"{iso_year}-W{iso_week}"
                if (now_ist.weekday() == target_dow and now_ist.hour == target_hour
                        and last_sent_week != week_key):
                    logger.info("📊 Generating scheduled weekly analysis decks...")
                    last_sent_week = week_key

                    result = generate_analysis_deck("This Week", days=7)
                    if result.get("status") != "success":
                        continue
                    file_path = result["file_path"]
                    if not is_safe_generated_file(file_path):
                        continue

                    # Push to all authenticated Telegram users
                    try:
                        from db.models import get_db_connection
                        conn = get_db_connection()
                        try:
                            cur = conn.cursor()
                            cur.execute("SELECT telegram_id FROM user_sessions")
                            users = cur.fetchall()
                            cur.close()
                        finally:
                            conn.close()

                        from telegram import Bot
                        token = os.getenv("TELEGRAM_BOT_TOKEN")
                        if token:
                            bot = Bot(token)
                            import asyncio as _a
                            loop = _a.new_event_loop()

                            def send_all():
                                for u in users:
                                    try:
                                        with open(file_path, "rb") as doc:
                                            loop.run_until_complete(bot.send_document(
                                                chat_id=u["telegram_id"],
                                                document=doc,
                                                filename=os.path.basename(file_path),
                                                caption="📊 Your scheduled weekly sales & operations analysis deck is ready!"
                                            ))
                                    except Exception as e_user:
                                        logger.warning(f"Weekly deck send failed for {u['telegram_id']}: {e_user}")
                            send_all()
                            loop.close()
                    except Exception as e:
                        logger.warning(f"Weekly deck distribution failed: {e}")
            except Exception as e:
                logger.error(f"Error in weekly deck scheduler: {e}")
            _time.sleep(300)  # check every 5 minutes

    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()
    print(f"📊 Weekly analysis deck scheduler active (every Sunday {target_hour:02d}:00 IST).")


# ── Proactive Smart Notifications Scheduler ──────────────────────────
def start_proactive_notifications_scheduler():
    """Start APScheduler for proactive Telegram alerts: expiry, low-stock, daily summary."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        import pytz
    except ImportError:
        logger.warning("⚠️ APScheduler not installed — proactive notifications disabled. Run: pip install APScheduler")
        return

    ist = pytz.timezone("Asia/Kolkata")
    scheduler = BackgroundScheduler(timezone=ist)

    def _get_all_active_chat_ids():
        """Get all authenticated Telegram user IDs to send alerts to."""
        try:
            from db.models import get_db_connection
            conn = get_db_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT telegram_id FROM user_sessions")
                rows = cur.fetchall()
                cur.close()
                return [r["telegram_id"] for r in rows]
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Could not fetch chat IDs for notifications: {e}")
            return []

    def _send_notification(message: str):
        """Send a message to all authenticated users."""
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            return
        chat_ids = _get_all_active_chat_ids()
        if not chat_ids:
            return
        import asyncio
        from telegram import Bot
        bot = Bot(token=token)

        async def _send_all():
            for cid in chat_ids:
                try:
                    await bot.send_message(chat_id=cid, text=message, parse_mode="Markdown")
                except Exception as e:
                    logger.warning(f"Notification send failed for {cid}: {e}")

        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_send_all())
            loop.close()
        except Exception as e:
            logger.warning(f"Notification dispatch error: {e}")

    def job_expiry_alerts():
        """9 AM IST — Check expiring stock and alert."""
        try:
            from skills.notifications import check_expiring_stock
            result = check_expiring_stock(days_ahead=7)
            if result.get("count", 0) > 0:
                _send_notification(result["message"])
                logger.info(f"📦 Sent {result['count']} expiry alert(s)")
        except Exception as e:
            logger.error(f"Expiry alert job error: {e}")

    def job_low_stock_check():
        """Every 4 hours — Check critically low stock."""
        try:
            from skills.notifications import check_critical_low_stock
            result = check_critical_low_stock()
            if result.get("count", 0) > 0:
                _send_notification(result["message"])
                logger.info(f"📉 Sent {result['count']} low-stock alert(s)")
        except Exception as e:
            logger.error(f"Low stock alert job error: {e}")

    def job_daily_closeout():
        """9 PM IST — Send daily closeout summary."""
        try:
            from skills.notifications import generate_daily_closeout_message
            result = generate_daily_closeout_message()
            if result.get("status") == "success":
                _send_notification(result["message"])
                logger.info(f"📊 Sent daily closeout summary (₹{result.get('revenue', 0)})")
        except Exception as e:
            logger.error(f"Daily closeout job error: {e}")

    # Schedule jobs
    scheduler.add_job(job_expiry_alerts, CronTrigger(hour=9, minute=0), id="expiry_alerts")
    scheduler.add_job(job_low_stock_check, CronTrigger(hour="*/4", minute=30), id="low_stock_check")
    scheduler.add_job(job_daily_closeout, CronTrigger(hour=21, minute=0), id="daily_closeout")

    scheduler.start()
    print("🔔 Proactive notifications scheduler started:")
    print("   ⏰ 9:00 AM IST  → Expiry alerts (items expiring within 7 days)")
    print("   ⏰ Every 4 hours → Low-stock warnings")
    print("   ⏰ 9:00 PM IST  → Daily closeout summary")


def main():
    """Main application entry point."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or token == "your_telegram_bot_token_here":
        print("ERROR: Please set a valid TELEGRAM_BOT_TOKEN in your .env file!")
        return

    # Initialize PostgreSQL database schema if not exists
    init_db()

    # Start self-pinging keep-alive loop if RENDER_EXTERNAL_URL is configured
    start_keep_alive_pinger()

    # Start daily morning logout scheduler (between 4:00 AM and 5:00 AM IST)
    start_daily_morning_logout_scheduler()

    # Start scheduled weekly analysis deck auto-sender (optional, WEEKLY_DECK_ENABLED=true)
    start_weekly_deck_scheduler()

    # Start proactive notification scheduler (expiry alerts, low-stock, daily summary)
    start_proactive_notifications_scheduler()

    app = ApplicationBuilder().token(token).post_init(post_init).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("logout", logout_command))
    app.add_handler(CommandHandler("new", new_command))
    app.add_handler(CommandHandler("reset", new_command))
    app.add_handler(CommandHandler("clear", new_command))
    app.add_handler(CommandHandler("stock", stock_command))
    app.add_handler(CommandHandler("lowstock", lowstock_command))
    app.add_handler(CommandHandler("bill", bill_command))
    app.add_handler(CommandHandler("khata", khata_command))
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(CommandHandler("barcode", barcode_command))
    app.add_handler(CommandHandler("invoice", invoice_command))
    app.add_handler(CommandHandler("analysis", analysis_command))
    app.add_handler(CommandHandler("upi", upi_command))
    app.add_handler(CommandHandler("expiry", expiry_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_note))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.CONTACT | (filters.TEXT & ~filters.COMMAND), handle_message))

    print(f"🤖 Supermarket Ops Agent Telegram Bot is running...")
    use_webhook = os.getenv("USE_WEBHOOK", "false").lower().strip() in ("true", "1", "yes")
    render_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_URL") or "https://supermart-agent.onrender.com"

    try:
        if use_webhook and render_url:
            port = int(os.getenv("PORT", "8080"))
            webhook_url = f"{render_url.rstrip('/')}/telegram"
            # Secure webhook token to verify updates genuinely originate from Telegram API
            webhook_secret = os.getenv("WEBHOOK_SECRET_TOKEN") or hashlib.sha256(f"secret_{token}".encode()).hexdigest()[:32]
            print(f"🌐 Starting Telegram Webhook mode on port {port} at {webhook_url} (secret_token verification enabled)...")
            print(f"⚡ Render will sleep when idle and automatically wake up whenever a Telegram user sends a message!")
            app.run_webhook(
                listen="0.0.0.0",
                port=port,
                url_path="telegram",
                webhook_url=webhook_url,
                secret_token=webhook_secret,
                drop_pending_updates=False
            )
        else:
            # Start health check server & self-pinger for polling mode
            start_health_check_server()
            start_keep_alive_pinger()
            app.run_polling(drop_pending_updates=True)
    except Exception as e:
        if "Conflict" in str(e) or "terminated by other" in str(e):
            print("\n⚠️ CONFLICT WARNING: Another instance of bot.py is already running on this Bot Token!")
            print("Telegram allows only 1 active bot process at a time. Please close other terminals or processes running bot.py.")
        else:
            raise e

if __name__ == "__main__":
    main()

