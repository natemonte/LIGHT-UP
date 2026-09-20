-- Lights CRM database schema (SQLite)

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Business owner's login (single user for now)
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    owner_phone   TEXT,               -- where lead/alert texts get sent
    created_at    TEXT DEFAULT (datetime('now'))
);

-- The phone numbers you're tracking (e.g. two Quo numbers, one per yard sign design)
CREATE TABLE IF NOT EXISTS phone_numbers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    number      TEXT UNIQUE NOT NULL,   -- e.g. +15551234567
    label       TEXT NOT NULL,          -- e.g. "Yard Sign A - Blue"
    campaign    TEXT,                   -- free text: where this number is deployed
    monthly_cost REAL DEFAULT 0,        -- what this number/sign costs you per month (optional)
    active      INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS customers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name   TEXT NOT NULL,
    last_name    TEXT,
    phone        TEXT,
    email        TEXT,
    address      TEXT,
    notes        TEXT,
    source_number_id INTEGER REFERENCES phone_numbers(id),  -- which yard sign number they called
    lead_source  TEXT,               -- 'missed_call' | 'website' | 'facebook_ad' | 'referral' | 'other'
    created_at   TEXT DEFAULT (datetime('now'))
);

-- A job = one installation/project for a customer. Can spawn quotes/invoices/payments.
CREATE TABLE IF NOT EXISTS jobs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id        INTEGER NOT NULL REFERENCES customers(id),
    title              TEXT NOT NULL,             -- e.g. "Roofline lights - 120ft"
    status             TEXT NOT NULL DEFAULT 'quote',  -- quote | scheduled | in_progress | completed | paid | cancelled
    scheduled_date     TEXT,             -- install date
    completed_date     TEXT,
    takedown_date      TEXT,             -- when lights should come down
    takedown_completed_date TEXT,
    materials_cost     REAL DEFAULT 0,
    labor_cost         REAL DEFAULT 0,
    price_quoted       REAL DEFAULT 0,
    amount_paid        REAL DEFAULT 0,
    payment_method     TEXT,           -- 'card' | 'ach' | 'cash' | 'check'
    next_service_date  TEXT,           -- for renewal tracking (e.g. next year's install date)
    next_estimated_price REAL,
    stripe_checkout_id TEXT,
    notes              TEXT,
    public_token       TEXT UNIQUE,     -- lets a customer view/pay a quote without logging in
    created_at         TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER NOT NULL REFERENCES jobs(id),
    amount       REAL NOT NULL,
    method       TEXT NOT NULL,        -- 'card' | 'ach' | 'cash' | 'check'
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | paid | failed
    stripe_ref   TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);

-- Every call/text event coming in from Quo, tagged by which number it hit
CREATE TABLE IF NOT EXISTS call_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    phone_number_id  INTEGER REFERENCES phone_numbers(id),
    from_number      TEXT,
    event_type       TEXT NOT NULL,   -- 'missed_call' | 'answered_call' | 'incoming_text'
    auto_reply_sent  INTEGER DEFAULT 0,
    owner_notified   INTEGER DEFAULT 0,
    customer_id      INTEGER REFERENCES customers(id),
    raw_payload      TEXT,
    created_at       TEXT DEFAULT (datetime('now'))
);

-- Generic lead intake log (website form, Facebook ad, missed call, etc.) that triggers owner notification
CREATE TABLE IF NOT EXISTS leads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT,
    phone        TEXT,
    email        TEXT,
    source       TEXT NOT NULL,   -- 'missed_call' | 'website' | 'facebook_ad' | 'referral' | 'other'
    source_number_id INTEGER REFERENCES phone_numbers(id),
    message      TEXT,
    notified     INTEGER DEFAULT 0,
    customer_id  INTEGER REFERENCES customers(id),
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_customer ON jobs(customer_id);
CREATE INDEX IF NOT EXISTS idx_call_events_number ON call_events(phone_number_id);
CREATE INDEX IF NOT EXISTS idx_customers_source_number ON customers(source_number_id);
