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
        safe_base = os.path.abspath("generated_docs")
        target_path = os.path.abspath(file_path)
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
            with open(file_path, "rb") as doc:
                await update.message.reply_document(
                    document=doc,
                    filename=os.path.basename(file_path),
                    caption=f"Tax Invoice for Bill {bill_id}"
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

async def handle_voice_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle voice notes: transcribe (if Whisper or OpenAI API is configured) and
    route the transcript through the agent as a normal text message.
    Supports VOICE_TRANSCRIBE_API_KEY or standard OPENAI_API_KEY.
    """
    if not update.message or not update.message.voice:
        return

    telegram_id = str(update.effective_user.id) if update.effective_user else "default"
    session = get_user_session(telegram_id)
    if not session:
        await update.message.reply_text("🔐 Authentication required. Send /start to log into your shop.")
        return

    # Auto-detect Whisper provider: GROQ_API_KEY (100% Free), VOICE_TRANSCRIBE_API_KEY, or OPENAI_API_KEY
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    voice_key = os.getenv("VOICE_TRANSCRIBE_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()

    if groq_key:
        api_key = groq_key
        base_url = os.getenv("VOICE_TRANSCRIBE_BASE_URL", "").strip() or "https://api.groq.com/openai/v1"
        model = os.getenv("VOICE_TRANSCRIBE_MODEL", "whisper-large-v3-turbo")
    elif voice_key:
        api_key = voice_key
        base_url = os.getenv("VOICE_TRANSCRIBE_BASE_URL", "").strip() or "https://api.openai.com/v1"
        model = os.getenv("VOICE_TRANSCRIBE_MODEL", "whisper-1")
    elif openai_key:
        api_key = openai_key
        base_url = os.getenv("VOICE_TRANSCRIBE_BASE_URL", "").strip() or "https://api.openai.com/v1"
        model = os.getenv("VOICE_TRANSCRIBE_MODEL", "whisper-1")
    else:
        api_key = ""
        base_url = ""
        model = ""

    if not (api_key and base_url):
        await update.message.reply_text(
            "🎙️ **Voice note received!**\n\n"
            "To enable free voice notes:\n"
            "1. Create a 100% free key at [console.groq.com](https://console.groq.com) (no credit card required).\n"
            "2. Add `GROQ_API_KEY=gsk_...` into your `.env` file.\n\n"
            "For now, please type your message as text! 🛒",
            parse_mode="Markdown"
        )
        return

    waiting = await update.message.reply_text("🎤 Transcribing your voice note...")

    transcript = None
    try:
        import httpx

        voice_file = await context.bot.get_file(update.message.voice.file_id)
        ogg_bytes = await voice_file.download_as_bytearray()

        async def _transcribe():
            async with httpx.AsyncClient(timeout=45) as client:
                files = {"file": ("voice.ogg", bytes(ogg_bytes), "audio/ogg")}
                # Guide Whisper specifically for English, Tamil, and Hindi Kirana supermarket terminology
                data = {
                    "model": model,
                    "prompt": (
                        "Kirana supermarket store operations and billing in English, Tamil (தமிழ், Tanglish), "
                        "and Hindi (हिंदी, Hinglish). Terms: Maggi, Atta, Sugar, Dal, Rice, Salt, Oil, Milk, "
                        "kg, g, litre, ml, packet, piece, MRP, GST, bill, cash, UPI, card, khata, "
                        "ரூபாய், கிலோ, பாக்கெட், பில், அரிசி, சர்க்கரை, பருப்பு, எண்ணெய், பால், "
                        "रुपये, किलो, पैकेट, बिल, आटा, चीनी, दाल, तेल, दूध."
                    )
                }
                resp = await client.post(
                    f"{base_url.rstrip('/')}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    files=files,
                    data=data,
                )
                resp.raise_for_status()
                return resp.json().get("text")

        transcript = await asyncio.wait_for(_transcribe(), timeout=45)
    except asyncio.TimeoutError:
        logger.warning("Voice transcription timed out after 45s.")
        await waiting.edit_text("⏱️ Voice transcription timed out. Please send your request as text.")
        return
    except Exception as e:
        logger.warning(f"Voice transcription failed: {e}")
        await waiting.edit_text("⚠️ Couldn't transcribe the voice note. Please type your request as text.")
        return

    if not transcript or not transcript.strip():
        await waiting.edit_text(
            "⚠️ Voice note was empty or could not be transcribed. Please type your request as text."
        )
        return

    await waiting.edit_text(f"🎧 Transcribed: \"{transcript.strip()}\" — processing...")
    await handle_message(update, context, user_text_override=transcript.strip())


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
        "• `/new` — Reset conversation context (standing preferences persist)\n"
        "• `/invoice <bill_id>` — Download official PDF GST Tax Invoice\n"
        "• `/analysis <period>` — Download PowerPoint (.pptx) operations sales deck\n"
        "• `/help` — Show this interactive command guide\n"
        "• `/logout` — De-authenticate user session\n\n"
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

async def post_init(application):
    """Register interactive slash commands list with Telegram UI popup menu."""
    commands = [
        BotCommand("start", "Start session & fresh context"),
        BotCommand("stock", "View all products & inventory stock"),
        BotCommand("lowstock", "View low stock reorder items"),
        BotCommand("bill", "Create a bill (e.g. /bill 2 sugar, UPI)"),
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
    """Starts a minimal HTTP server in a background thread to satisfy Render Web Service port checks."""
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class HealthCheckHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/health", "/healthz"):
                self.send_response(200)
                self.send_header("Content-type", "text/plain; charset=utf-8")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(b"Bot is healthy!")
            else:
                self.send_response(404)
                self.send_header("Content-type", "text/plain; charset=utf-8")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(b"Not Found")

        def do_POST(self):
            self.send_response(405)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Method Not Allowed")

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
    app.add_handler(CommandHandler("invoice", invoice_command))
    app.add_handler(CommandHandler("analysis", analysis_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_note))
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

