CREATE TABLE IF NOT EXISTS products (
    id BIGSERIAL PRIMARY KEY,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    my_price NUMERIC(12, 2) NOT NULL CHECK (my_price >= 0),
    currency TEXT NOT NULL DEFAULT 'TRY',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS competitors (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    store_name TEXT NOT NULL,
    url TEXT NOT NULL,
    parse_mode TEXT NOT NULL DEFAULT 'auto' CHECK (parse_mode IN ('auto', 'jsonld', 'css')),
    css_price_selector TEXT,
    css_stock_selector TEXT,
    last_price NUMERIC(12, 2) CHECK (last_price IS NULL OR last_price >= 0),
    last_in_stock BOOLEAN,
    last_status TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    last_checked_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (product_id, url)
);

CREATE TABLE IF NOT EXISTS price_history (
    id BIGSERIAL PRIMARY KEY,
    competitor_id BIGINT NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
    price NUMERIC(12, 2) NOT NULL CHECK (price >= 0),
    in_stock BOOLEAN,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_price_history_competitor_checked
    ON price_history (competitor_id, checked_at DESC);

CREATE TABLE IF NOT EXISTS alert_rules (
    id BIGSERIAL PRIMARY KEY,
    competitor_id BIGINT NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
    rule_type TEXT NOT NULL CHECK (rule_type IN ('below_price', 'pct_drop', 'stock_change')),
    threshold_value NUMERIC(12, 2),
    cooldown_min INTEGER NOT NULL DEFAULT 60 CHECK (cooldown_min >= 0),
    last_sent_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_alert_rules_active_competitor
    ON alert_rules (competitor_id)
    WHERE is_active;

CREATE TABLE IF NOT EXISTS alert_logs (
    id BIGSERIAL PRIMARY KEY,
    rule_id BIGINT NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    old_val TEXT,
    new_val TEXT,
    status TEXT NOT NULL CHECK (status IN ('sent', 'failed', 'dry_run')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alert_logs_created_at
    ON alert_logs (created_at DESC);

CREATE TABLE IF NOT EXISTS mock_store_items (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    price NUMERIC(12, 2) NOT NULL CHECK (price >= 0),
    currency TEXT NOT NULL DEFAULT 'TRY',
    in_stock BOOLEAN NOT NULL DEFAULT TRUE,
    render_mode TEXT NOT NULL CHECK (render_mode IN ('jsonld', 'css_only', 'broken')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
