-- Additional accounts and Plaid-account metadata for safer source routing.
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('1090', 'Transfer Clearing', 'asset',
        (SELECT id FROM accounts WHERE code = '1000'));
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('4190', 'Uncategorized Income', 'revenue', NULL);
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('4200', 'Interest Income', 'revenue', NULL);
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('6090', 'Refunds & Reimbursements', 'expense',
        (SELECT id FROM accounts WHERE code = '6000'));

-- Source-scoped account metadata and explicit Ledger routing. SimpleFIN does
-- not reliably identify asset vs liability, so its mapping stays NULL until a
-- person chooses it. Plaid metadata remains eligible for its existing safe
-- automatic routing.
CREATE TABLE IF NOT EXISTS bank_accounts (
  source TEXT NOT NULL,
  account_id TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  official_name TEXT,
  type TEXT NOT NULL DEFAULT '',
  subtype TEXT NOT NULL DEFAULT '',
  mask TEXT,
  ledger_account_id INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
  synced_at TEXT NOT NULL,
  PRIMARY KEY (source, account_id)
);

CREATE INDEX IF NOT EXISTS idx_bank_accounts_ledger
  ON bank_accounts(source, ledger_account_id);
