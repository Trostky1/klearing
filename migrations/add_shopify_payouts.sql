CREATE TABLE IF NOT EXISTS shopify_payouts (
  payout_id TEXT PRIMARY KEY,
  amount_cents INTEGER NOT NULL,
  date TEXT NOT NULL,
  status TEXT NOT NULL,
  currency TEXT,
  fee_amount_cents INTEGER NOT NULL DEFAULT 0,
  gross_amount_cents INTEGER,
  summary_json TEXT NOT NULL,
  synced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shopify_payouts_matching
  ON shopify_payouts(status, date, amount_cents);

INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('6100', 'Payment Processing Fees', 'expense',
        (SELECT id FROM accounts WHERE code = '6000'));
