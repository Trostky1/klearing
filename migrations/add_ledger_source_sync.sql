-- Persist normalized bank data for local matching and ledger workflows.
-- Signs follow the BankAdapter contract: negative is money out, positive is in.
-- SimpleFIN transaction IDs are unique only within an account, so the source
-- and account are part of the durable identity.
CREATE TABLE IF NOT EXISTS bank_transactions (
  source TEXT NOT NULL,
  transaction_id TEXT NOT NULL,
  account_id TEXT NOT NULL DEFAULT '',
  date TEXT NOT NULL,
  authorized_date TEXT,
  amount_cents INTEGER NOT NULL,
  merchant_name TEXT,
  name TEXT,
  pending INTEGER NOT NULL DEFAULT 0,
  category_primary TEXT,
  category_detailed TEXT,
  removed INTEGER NOT NULL DEFAULT 0,
  synced_at TEXT NOT NULL,
  PRIMARY KEY (source, account_id, transaction_id)
);

CREATE INDEX IF NOT EXISTS idx_bank_transactions_importable
  ON bank_transactions(source, pending, removed, date);

-- One source event can be posted only once. Keeping this guard separate from
-- journal_entries permits future source events to create multiple entries
-- (for example, a revenue and COGS pair) without weakening idempotency.
CREATE TABLE IF NOT EXISTS ledger_source_imports (
  source_reference TEXT PRIMARY KEY,
  journal_entry_id INTEGER NOT NULL UNIQUE
    REFERENCES journal_entries(id) ON DELETE RESTRICT,
  imported_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_source_imports_entry
  ON ledger_source_imports(journal_entry_id);

-- The Shopify clearing account avoids treating both a paid Shopify order and
-- its later bank payout as separate revenue/cash events.
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('1015', 'Shopify Clearing', 'asset',
        (SELECT id FROM accounts WHERE code = '1000'));

INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('6080', 'Uncategorized Expense', 'expense',
        (SELECT id FROM accounts WHERE code = '6000'));
