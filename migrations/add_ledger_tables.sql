CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN (
    'asset', 'liability', 'equity', 'revenue', 'expense'
  )),
  parent_account_id INTEGER REFERENCES accounts(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_accounts_parent
  ON accounts(parent_account_id);

CREATE TABLE IF NOT EXISTS journal_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  memo TEXT NOT NULL,
  created_by TEXT NOT NULL,
  source_reference TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_journal_entries_date
  ON journal_entries(date);

CREATE TABLE IF NOT EXISTS journal_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  journal_entry_id INTEGER NOT NULL
    REFERENCES journal_entries(id) ON DELETE CASCADE,
  account_id INTEGER NOT NULL
    REFERENCES accounts(id) ON DELETE RESTRICT,
  debit_amount INTEGER NOT NULL DEFAULT 0 CHECK (debit_amount >= 0),
  credit_amount INTEGER NOT NULL DEFAULT 0 CHECK (credit_amount >= 0),
  CHECK (
    (debit_amount > 0 AND credit_amount = 0)
    OR (debit_amount = 0 AND credit_amount > 0)
  )
);

CREATE INDEX IF NOT EXISTS idx_journal_lines_entry
  ON journal_lines(journal_entry_id);
CREATE INDEX IF NOT EXISTS idx_journal_lines_account
  ON journal_lines(account_id);

-- Minimal starter Chart of Accounts. Codes make the seed stable and give
-- future integrations a durable reference without coupling them to IDs.
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('1000', 'Cash', 'asset', NULL);
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('1100', 'Accounts Receivable', 'asset', NULL);
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('1200', 'Inventory', 'asset', NULL);
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('2000', 'Accounts Payable', 'liability', NULL);
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('2100', 'Credit Card Payable', 'liability', NULL);
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('3000', 'Owner''s Equity', 'equity', NULL);
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('3100', 'Owner Draws', 'equity', NULL);
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('4000', 'Sales Revenue', 'revenue', NULL);
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('4100', 'Service Revenue', 'revenue', NULL);
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('5000', 'Cost of Goods Sold', 'expense', NULL);
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('6000', 'Operating Expenses', 'expense', NULL);

INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('1010', 'Checking', 'asset',
        (SELECT id FROM accounts WHERE code = '1000'));
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('6010', 'Advertising & Marketing', 'expense',
        (SELECT id FROM accounts WHERE code = '6000'));
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('6020', 'Bank & Payment Fees', 'expense',
        (SELECT id FROM accounts WHERE code = '6000'));
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('6030', 'Software & Subscriptions', 'expense',
        (SELECT id FROM accounts WHERE code = '6000'));
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('6040', 'Office Expense', 'expense',
        (SELECT id FROM accounts WHERE code = '6000'));
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('6050', 'Professional Services', 'expense',
        (SELECT id FROM accounts WHERE code = '6000'));
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('6060', 'Payroll & Contractors', 'expense',
        (SELECT id FROM accounts WHERE code = '6000'));
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('6070', 'Rent & Utilities', 'expense',
        (SELECT id FROM accounts WHERE code = '6000'));
