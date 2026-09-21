-- PostgreSQL Schema Migration for Render / Supabase / Neon
-- Project: SuperMart AI Ops Agent
-- Idempotent: safe to run against an existing live database.

CREATE TABLE IF NOT EXISTS products (
    sku_id VARCHAR(255) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    category VARCHAR(255) NOT NULL,
    unit VARCHAR(50) NOT NULL,
    is_loose BOOLEAN DEFAULT FALSE,
    cost_price DOUBLE PRECISION NOT NULL,
    mrp DOUBLE PRECISION NOT NULL,
    gst_slab DOUBLE PRECISION NOT NULL DEFAULT 0,
    hsn_code VARCHAR(100),
    quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
    reorder_level DOUBLE PRECISION NOT NULL DEFAULT 10
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    khata_balance DOUBLE PRECISION DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bills (
    bill_id VARCHAR(255) PRIMARY KEY,
    status VARCHAR(50) NOT NULL DEFAULT 'draft',
    customer_id INTEGER NULL REFERENCES customers(customer_id) ON DELETE SET NULL,
    payment_mode VARCHAR(50),
    payment_ref VARCHAR(255),
    subtotal DOUBLE PRECISION DEFAULT 0,
    cgst DOUBLE PRECISION DEFAULT 0,
    sgst DOUBLE PRECISION DEFAULT 0,
    total DOUBLE PRECISION DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    finalized_at TIMESTAMP WITH TIME ZONE NULL
);

CREATE TABLE IF NOT EXISTS bill_items (
    id SERIAL PRIMARY KEY,
    bill_id VARCHAR(255) NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
    sku_id VARCHAR(255) NOT NULL REFERENCES products(sku_id) ON DELETE CASCADE,
    qty DOUBLE PRECISION NOT NULL,
    unit_price DOUBLE PRECISION NOT NULL,
    gst_slab DOUBLE PRECISION NOT NULL DEFAULT 0,
    line_total DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS khata_transactions (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE,
    type VARCHAR(50) NOT NULL,
    amount DOUBLE PRECISION NOT NULL,
    bill_id VARCHAR(255) NULL REFERENCES bills(bill_id) ON DELETE SET NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS preferences (
    owner_id VARCHAR(255) NOT NULL,
    key VARCHAR(255) NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (owner_id, key)
);

CREATE TABLE IF NOT EXISTS idempotency_log (
    update_id VARCHAR(255) PRIMARY KEY,
    processed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shops (
    shop_id SERIAL PRIMARY KEY,
    shop_name VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    shop_address TEXT NULL,
    shop_gstin VARCHAR(100) NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_sessions (
    telegram_id VARCHAR(255) PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shops(shop_id) ON DELETE CASCADE,
    authenticated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS authenticated_users (
    telegram_id VARCHAR(255) PRIMARY KEY,
    phone_number VARCHAR(100) NULL,
    authenticated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    event_type VARCHAR(100) NOT NULL,
    entity_type VARCHAR(100),
    entity_id VARCHAR(255),
    details TEXT,
    old_value DOUBLE PRECISION,
    new_value DOUBLE PRECISION,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ══════════════════════════════════════════════════════════════════
-- IDEMPOTENT COLUMN MIGRATIONS (safe on live databases)
-- ══════════════════════════════════════════════════════════════════

-- products: loose-item pricing + soft delete + timestamps
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='products' AND column_name='base_unit') THEN
        ALTER TABLE products ADD COLUMN base_unit VARCHAR(50) NOT NULL DEFAULT 'piece';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='products' AND column_name='conversion_factor') THEN
        ALTER TABLE products ADD COLUMN conversion_factor DOUBLE PRECISION NOT NULL DEFAULT 1.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='products' AND column_name='price_per_base_unit') THEN
        ALTER TABLE products ADD COLUMN price_per_base_unit DOUBLE PRECISION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='products' AND column_name='is_active') THEN
        ALTER TABLE products ADD COLUMN is_active BOOLEAN DEFAULT TRUE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='products' AND column_name='created_at') THEN
        ALTER TABLE products ADD COLUMN created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='products' AND column_name='updated_at') THEN
        ALTER TABLE products ADD COLUMN updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
    END IF;
    -- Backfill base_unit from unit for loose rows (one-time, idempotent)
    UPDATE products SET base_unit = unit WHERE is_loose = TRUE AND base_unit = 'piece' AND unit IN ('kg','g','litre','ml');
END $$;

-- customers: credit limit + contact info + timestamps
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='customers' AND column_name='credit_limit') THEN
        ALTER TABLE customers ADD COLUMN credit_limit DOUBLE PRECISION DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='customers' AND column_name='phone') THEN
        ALTER TABLE customers ADD COLUMN phone VARCHAR(50);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='customers' AND column_name='address') THEN
        ALTER TABLE customers ADD COLUMN address TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='customers' AND column_name='created_at') THEN
        ALTER TABLE customers ADD COLUMN created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='customers' AND column_name='updated_at') THEN
        ALTER TABLE customers ADD COLUMN updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
    END IF;
END $$;

-- shops: place_of_supply for GST invoices
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='shops' AND column_name='place_of_supply') THEN
        ALTER TABLE shops ADD COLUMN place_of_supply VARCHAR(100);
    END IF;
END $$;

-- bills: sequential invoice number + place of supply snapshot
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='bills' AND column_name='invoice_number') THEN
        ALTER TABLE bills ADD COLUMN invoice_number INTEGER;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='bills' AND column_name='place_of_supply') THEN
        ALTER TABLE bills ADD COLUMN place_of_supply VARCHAR(100);
    END IF;
END $$;

-- idempotency_log: context so retried updates return the cached reply, not a bare ack
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='idempotency_log' AND column_name='reply_text') THEN
        ALTER TABLE idempotency_log ADD COLUMN reply_text TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='idempotency_log' AND column_name='file_paths') THEN
        ALTER TABLE idempotency_log ADD COLUMN file_paths TEXT;
    END IF;
END $$;

-- ══════════════════════════════════════════════════════════════════
-- NEW TABLES: batch / expiry tracking with FEFO (stretch goal)
-- ══════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS stock_batches (
    batch_id SERIAL PRIMARY KEY,
    sku_id VARCHAR(255) NOT NULL REFERENCES products(sku_id) ON DELETE CASCADE,
    batch_code VARCHAR(100) NOT NULL,
    qty_received DOUBLE PRECISION NOT NULL,
    qty_remaining DOUBLE PRECISION NOT NULL,
    cost_price DOUBLE PRECISION NOT NULL,
    expiry_date DATE NULL,
    received_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (sku_id, batch_code)
);

CREATE INDEX IF NOT EXISTS idx_batches_sku ON stock_batches(sku_id);
CREATE INDEX IF NOT EXISTS idx_batches_expiry ON stock_batches(expiry_date);

-- ══════════════════════════════════════════════════════════════════
-- PERFORMANCE INDEXES
-- ══════════════════════════════════════════════════════════════════

CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_bills_finalized_at ON bills(finalized_at);
CREATE INDEX IF NOT EXISTS idx_bills_status ON bills(status);
CREATE INDEX IF NOT EXISTS idx_bill_items_sku ON bill_items(sku_id);
CREATE INDEX IF NOT EXISTS idx_bill_items_bill ON bill_items(bill_id);
CREATE INDEX IF NOT EXISTS idx_khata_customer ON khata_transactions(customer_id);
CREATE INDEX IF NOT EXISTS idx_products_active ON products(is_active);

-- ══════════════════════════════════════════════════════════════════
-- BACKFILL: sequential invoice numbers for existing finalized bills
-- ══════════════════════════════════════════════════════════════════

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='bills' AND column_name='invoice_number') THEN
        WITH ordered AS (
            SELECT bill_id, ROW_NUMBER() OVER (ORDER BY COALESCE(finalized_at, created_at)) AS rn
            FROM bills
            WHERE invoice_number IS NULL
        )
        UPDATE bills b SET invoice_number = ordered.rn
        FROM ordered WHERE b.bill_id = ordered.bill_id;
    END IF;
END $$;

-- ══════════════════════════════════════════════════════════════════
-- SEQUENTIAL INVOICE GENERATOR & PERSISTENT RATE LIMITING
-- ══════════════════════════════════════════════════════════════════

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_sequences WHERE sequencename = 'invoice_number_seq') THEN
        CREATE SEQUENCE invoice_number_seq START WITH 1;
    END IF;
    -- Synchronize sequence with current max invoice number so it never produces duplicates
    PERFORM setval('invoice_number_seq', COALESCE((SELECT MAX(invoice_number) FROM bills), 0) + 1, false);
END $$;

CREATE TABLE IF NOT EXISTS user_rate_limits (
    telegram_id VARCHAR(255) PRIMARY KEY,
    tokens DOUBLE PRECISION NOT NULL,
    last_updated DOUBLE PRECISION NOT NULL
);

