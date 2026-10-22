CREATE TABLE IF NOT EXISTS kl_transactions (
  id TEXT PRIMARY KEY,
  wave_id TEXT UNIQUE,
  date TEXT NOT NULL,
  description TEXT,
  merchant TEXT,
  amount_cents INTEGER NOT NULL,
  currency TEXT DEFAULT 'USD',
  raw_wave_category TEXT,
  ai_category TEXT,
  ai_confidence REAL,
  ai_reasoning TEXT,
  user_approved INTEGER DEFAULT 0,
  user_override_category TEXT,
  synced_at TEXT
);

CREATE TABLE IF NOT EXISTS kl_tax_settings (
  id INTEGER PRIMARY KEY DEFAULT 1,
  filing_status TEXT DEFAULT 'single',
  state TEXT DEFAULT 'IA',
  estimated_other_income INTEGER DEFAULT 0,
  tax_year INTEGER DEFAULT 2026,
  business_type TEXT DEFAULT 'sole_prop'
);

CREATE TABLE IF NOT EXISTS kl_units_config (
  id INTEGER PRIMARY KEY DEFAULT 1,
  source TEXT DEFAULT 'shopify',
  manual_sp_cents INTEGER,
  manual_q INTEGER,
  manual_vc_cents INTEGER,
  target_income_cents INTEGER DEFAULT 0,
  date_range_days INTEGER DEFAULT 30
);
