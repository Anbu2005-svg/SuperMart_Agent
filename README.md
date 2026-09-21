# SuperMart AI Ops Agent 🛒🤖

> **Supermarket Operations AI Agent for Telegram**  
> An intelligent, autonomous Telegram AI Operations Agent for Indian Kirana Supermarkets built with **100% Free & Open-Source Tools**, Prisma Cloud PostgreSQL database, Groq Whisper multilingual voice recognition, dual LLM key failover, FEFO batch inventory, and ReportLab / Matplotlib document generators.

---

## 📌 Project & Repository Details
* **GitHub Repository:** [https://github.com/Anbu2005-svg/SuperMart_Agent](https://github.com/Anbu2005-svg/SuperMart_Agent)
* **Telegram Bot:** [@SuperMart_Ops_Agent_bot](https://t.me/SuperMart_Ops_Agent_bot)
* **Live Deployment:** **Deployed Live on Render** 🚀 ([Render Web Service](https://render.com))
* **Project Demo Video:** [Watch Full Demo Video on Google Drive 🎥](https://drive.google.com/file/d/1pdQ5xrjZ3hvjJYbu41JMtpRS_JPcJHjR/view?usp=sharing)
* **Author / Contributor:** `Anbu2005-svg`
* **Core Tech Stack:** Python 3.9+, Telegram Bot API (`python-telegram-bot`), Groq Whisper API (`whisper-large-v3`), Ollama Cloud API (`nemotron-3-super`), Prisma Cloud PostgreSQL (`psycopg2-binary`), ReportLab (PDF Invoices), python-pptx & Matplotlib (PPTX Decks), pytest.

---

## 🌟 Key New Features & Capabilities

### 1. 🎙️ Multilingual Voice Note Support (English, Tamil & Hindi)
* **Seamless Voice Commands:** Shop owners can record audio/voice notes in Telegram instead of typing out complex orders.
* **Ultrafast Transcription (~0.4s):** Powered by Groq's `whisper-large-v3` engine.
* **Trilingual Kirana Biasing:** Specifically prompted to handle Indian regional languages and colloquial speech:
  * **English:** Standard Kirana terms (*"Add 2 packets of Maggi, bill to Suresh on UPI"*).
  * **Tamil (தமிழ் / Tanglish):** (*"இரண்டு பாக்கெட் மேகி, ஒரு கிலோ சர்க்கரை பில் போடுங்க"*, *"2 packet Maggi bill podunga"*).
  * **Hindi (हिंदी / Hinglish):** (*"दो पैकेट मैगी और एक किलो चीनी का बिल बनाओ"*, *"2 packet Maggi aur 1kg chini ka bill bana do"*).
* **Language-Preserving AI Responses:** The agent automatically replies in the user's spoken language while executing the underlying database tools directly.

### 2. 📦 FEFO (First Expiring, First Out) Batch Inventory
* **Smart Batch Tracking:** Products track batch IDs, arrival dates, and expiry dates.
* **Auto-Deduction by Expiry:** Billing automatically decrements items from the earliest-expiring batch first, minimizing spoilage and stock waste.
* **Near-Expiry Alerts:** Proactive warnings for items approaching expiry within 30 days.
* **Loose vs. Packaged Units:** Accurate decimal accounting for loose goods (kg, litre) vs discrete packaged units (packets, bottles).

### 3. 💳 Customer Khata Credit Guards & Friendly Reminders
* **Hard Credit Limits:** Enforces customer-specific credit caps (`credit_limit`). Attempting to add credit to a ledger exceeding the limit automatically refuses the charge.
* **One-Click Payment Reminders:** Automatically drafts friendly Rupee-formatted payment reminder messages for pending balances above customizable thresholds.
* **Repayment Recording:** Real-time ledger settlement with timestamped audit trail records.

### 4. 📈 Sales Velocity Analytics & Smart Reordering
* **Data-Driven Reordering:** Calculates daily burn rates (`avg_daily_velocity`) over 7–90 day rolling windows.
* **Days of Cover Metrics:** Identifies items whose remaining stock will deplete before the cover horizon, preventing stockouts during high sales volume periods.

### 5. 🛡️ Advanced Security & Hardening
* **PBKDF2-HMAC-SHA256 Password Hashing:** 100,000 rounds with per-user cryptographically random 16-byte salts.
* **Brute-Force Lockout:** Automatically throttles accounts for 5 minutes after 5 consecutive failed login attempts.
* **Persistent Rate Limiting:** Token-bucket rate limiter persisted in PostgreSQL across server restarts and multiple worker instances.
* **Path-Traversal Defense:** Canonical path verification (`is_safe_generated_file`) ensuring no unauthorized directory access.
* **Daily Morning Auto-Logout:** Automatic logout daemon clears sessions daily between 4:00 AM – 5:00 AM IST (at 04:30 AM IST cutoff).

---

## 📱 Complete Telegram Bot Command Menu

| Command | Description | Example Usage |
|---|---|---|
| `/start` | Start bot session, view welcome card, register or log in | `/start` |
| `/stock` | List full inventory catalog with SKUs, MRP, GST slabs, and stock levels | `/stock` |
| `/lowstock` | List items at or below reorder level requiring immediate restock | `/lowstock` |
| `/bill <items>` | Create & finalize a multi-item bill with GST & stock decrement | `/bill 2 sugar, 4 maggi, UPI` |
| `/khata` | View customer credit ledger & outstanding balance details | `/khata` |
| `/summary` | View daily sales revenue, GST collected, and payment breakdown | `/summary` |
| `/invoice <bill_id>` | Download official PDF GST Tax Invoice for a finalized bill | `/invoice BILL-7C9A41E2` |
| `/analysis [period]` | Download 4-slide executive PowerPoint (.pptx) sales & ops deck | `/analysis Today` |
| `/new` / `/reset` / `/clear` | Clear in-memory chat session (preserves database & preferences) | `/new` |
| `/help` | Display interactive command menu and usage guide | `/help` |
| `/logout` | Log out of current shop session | `/logout` |

---

## 🏗️ Agent Design & Architecture

```
Telegram Voice Note / Text Message (update_id)
       │
       ▼
 1. Check Idempotency Cache ──(If duplicate)──► Return Cached Reply
       │
       ▼
 2. Transcribe Audio (Groq Whisper: English / Tamil / Hindi)
       │
       ▼
 3. Load Active Shop Session & Standing Preferences from Prisma Cloud PostgreSQL
       │
       ▼
 4. Build Dynamic Grounded System Prompt
       │
       ▼
 5. Function-Calling Loop (Ollama Cloud / Groq with key failover):
    ├── Execute Target Tool (/skills: inventory, billing, khata, analytics, docgen)
    ├── Atomic Transactions (FOR UPDATE row-level locks, FEFO deduction)
    └── Pass JSON Results back to Agent
       │
       ▼
 6. Context Compression & Delivery:
    ├── Deliver response text in user's language (EN / TA / HI)
    └── Send generated documents (ReportLab PDF / python-pptx PPTX)
```

---

## 🧪 Comprehensive Automated Test Suite (84 Tests - 100% Passed)

The entire application is covered by **84 automated tests** running against Prisma Cloud PostgreSQL:

```bash
pytest tests/ -v
```

### Test Suite Summary:
* **[tests/test_auth.py](file:///f:/Anbu%20Final%20Year%20Project/SuperMarket%20ops%20Agent/tests/test_auth.py)** (6 tests): PBKDF2 hashing, multi-tenant session isolation, daily morning auto-logout.
* **[tests/test_billing_edge_cases.py](file:///f:/Anbu%20Final%20Year%20Project/SuperMarket%20ops%20Agent/tests/test_billing_edge_cases.py)** (10 tests): Multi-item bills, credit limit rejection, discount caps.
* **[tests/test_gst_calc.py](file:///f:/Anbu%20Final%20Year%20Project/SuperMarket%20ops%20Agent/tests/test_gst_calc.py)** (9 tests): Deterministic CGST/SGST/IGST math, rounding accuracy.
* **[tests/test_inventory_edge_cases.py](file:///f:/Anbu%20Final%20Year%20Project/SuperMarket%20ops%20Agent/tests/test_inventory_edge_cases.py)** (12 tests): FEFO expiry batch ordering, loose goods decimal quantities, low-stock warnings.
* **[tests/test_ollama_cloud_config.py](file:///f:/Anbu%20Final%20Year%20Project/SuperMarket%20ops%20Agent/tests/test_ollama_cloud_config.py)** (3 tests): LLM provider failover and cloud connectivity.
* **[tests/test_oversell.py](file:///f:/Anbu%20Final%20Year%20Project/SuperMarket%20ops%20Agent/tests/test_oversell.py)** (6 tests): Concurrent multi-cashier locking and oversell prevention.
* **[tests/test_docgen_and_harness.py](file:///f:/Anbu%20Final%20Year%20Project/SuperMarket%20ops%20Agent/tests/test_docgen_and_harness.py)** (6 tests): ReportLab PDF invoices, PPTX decks, zero-sales chart rendering.
* **[tests/test_idempotency.py](file:///f:/Anbu%20Final%20Year%20Project/SuperMarket%20ops%20Agent/tests/test_idempotency.py)** (3 tests): Atomic Telegram `update_id` claim and cached replay.
* **[tests/test_security_hardening.py](file:///f:/Anbu%20Final%20Year%20Project/SuperMarket%20ops%20Agent/tests/test_security_hardening.py)** (10 tests): Persistent token-bucket rate limiter, brute-force lockouts, GSTIN regex validation, path traversal defense.
* **[tests/test_audit_trail.py](file:///f:/Anbu%20Final%20Year%20Project/SuperMarket%20ops%20Agent/tests/test_audit_trail.py)** (5 tests): Immutable financial, khata, and stock audit logging.
* **[tests/test_agent_flow.py](file:///f:/Anbu%20Final%20Year%20Project/SuperMarket%20ops%20Agent/tests/test_agent_flow.py)** (9 tests): System prompt rules, tool dispatch table, khata reminders, Indian currency words.
* **[tests/test_analytics_and_concurrency.py](file:///f:/Anbu%20Final%20Year%20Project/SuperMarket%20ops%20Agent/tests/test_analytics_and_concurrency.py)** (5 tests): Sales velocity, days of cover, reorder suggestion calculation.

**Result: 84 / 84 Passed (100% Success Rate) ✅**

---

## 🚀 Quickstart & Deployment Guide

### 1. Local Setup
```bash
# Clone repository
git clone https://github.com/Anbu2005-svg/SuperMart_Agent.git
cd SuperMart_Agent

# Create and activate virtual environment
python -m venv venv
.\venv\Scripts\activate   # Windows (or source venv/bin/activate on Linux/macOS)

# Install dependencies
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env
# Fill in TELEGRAM_BOT_TOKEN, DATABASE_URL, GROQ_API_KEY, OLLAMA_CLOUD_API_KEY

# Run test suite
pytest tests/ -v

# Launch Telegram bot locally
python bot.py
```

### 2. 🌐 Render Cloud Web Service Deployment
1. Connect your repository `Anbu2005-svg/SuperMart_Agent` on [Render.com](https://render.com).
2. Configure settings:
   * **Runtime**: Python 3
   * **Build Command**: `pip install -r requirements.txt`
   * **Start Command**: `python bot.py`
3. Add Environment Variables:
   * `TELEGRAM_BOT_TOKEN`: Telegram bot token from @BotFather
   * `DATABASE_URL`: Prisma Cloud / PostgreSQL connection string
   * `GROQ_API_KEY`: Groq API key for Whisper voice processing
   * `LLM_API_KEY_1`: Primary LLM API key
   * `PORT`: `8080` (binds automatically to built-in health check server)
4. Deploy and enjoy 24/7 automated Kirana operations!
