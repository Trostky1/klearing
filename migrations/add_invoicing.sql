-- Native invoicing and accounts receivable.
--
-- Klearing owns the invoice: it authors, numbers, renders, sends and tracks
-- it, and posts it to this ledger. Stripe's only role is minting a hosted
-- payment link, so no card data ever reaches this app. Wave is deliberately
-- untouched -- see docs/superpowers/specs/2026-07-25-ledger-ui-design.md for
-- the "separate now, converge later" decision these tables preserve.

-- Five accounts invoicing needs that the ledger did not have.
--
-- 1150 and 4900 are contra accounts: 1150 normally carries a credit balance
-- despite being typed 'asset', and 4900 a debit balance despite being typed
-- 'revenue'. That is correct rather than a mistake -- ledger.balance_sheet()
-- and profit_and_loss() use signed math (debit - credit for assets,
-- credit - debit for revenue), so a contra balance naturally subtracts from
-- its group with no special-casing. The accounts table's type CHECK has no
-- 'contra' option, and adding one would mean touching every report.
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('2200', 'Sales Tax Payable', 'liability', NULL);
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('2300', 'Customer Deposits', 'liability', NULL);
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('1150', 'Allowance for Doubtful Accounts', 'asset',
        (SELECT id FROM accounts WHERE code = '1100'));
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('4900', 'Sales Returns & Allowances', 'revenue', NULL);
INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id)
VALUES ('6110', 'Bad Debt Expense', 'expense',
        (SELECT id FROM accounts WHERE code = '6000'));

CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  -- NULL while a draft. A number is allocated at send time, not at
  -- creation, so discarding a draft cannot burn one and leave a gap in a
  -- sequence that auditors expect to be gapless.
  invoice_number TEXT UNIQUE,
  contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE RESTRICT,
  issue_date TEXT NOT NULL,                 -- YYYY-MM-DD
  due_date TEXT NOT NULL,                   -- YYYY-MM-DD
  terms TEXT NOT NULL DEFAULT '',           -- "Net 30" etc, display only
  notes TEXT NOT NULL DEFAULT '',
  currency TEXT NOT NULL DEFAULT 'USD',

  -- Lifecycle stamps. There is deliberately no status column: status is
  -- derived from these plus the open balance (see invoice_ledger.status).
  -- A stored status is a second source of truth that drifts from the
  -- ledger within a week.
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  sent_at TEXT,
  voided_at TEXT,
  written_off_at TEXT,
  write_off_method TEXT,                    -- allowance | direct

  -- The accounting basis in force when this invoice posted. Captured per
  -- invoice rather than read live, so flipping the Settings toggle later
  -- cannot retroactively reinterpret entries that already exist.
  posted_basis TEXT,

  payment_link_url TEXT,
  stripe_payment_link_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_invoices_contact ON invoices(contact_id);
CREATE INDEX IF NOT EXISTS idx_invoices_due ON invoices(due_date);
CREATE INDEX IF NOT EXISTS idx_invoices_sent ON invoices(sent_at);

CREATE TABLE IF NOT EXISTS invoice_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  position INTEGER NOT NULL DEFAULT 0,
  description TEXT NOT NULL DEFAULT '',
  -- Quantity in thousandths so "2.5 hours" stays exact integer maths, the
  -- same discipline the rest of the money path uses (no floats, ever).
  quantity_milli INTEGER NOT NULL DEFAULT 1000,
  unit_price_cents INTEGER NOT NULL DEFAULT 0,
  -- Basis points: 875 = 8.75%. Per line, because a single invoice can mix
  -- taxable goods and non-taxable labour.
  tax_rate_bp INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_invoice_lines_invoice
  ON invoice_lines(invoice_id, position);

CREATE TABLE IF NOT EXISTS invoice_payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE RESTRICT,
  paid_on TEXT NOT NULL,                    -- YYYY-MM-DD
  -- Gross amount applied against AR. The processor fee is recorded
  -- separately and expensed, never netted off revenue -- netting
  -- understates both revenue and expenses and will not tie to a 1099-K.
  amount_cents INTEGER NOT NULL,
  fee_cents INTEGER NOT NULL DEFAULT 0,
  method TEXT NOT NULL DEFAULT 'manual',    -- manual | stripe | bank_match
  reference TEXT,                           -- stripe payment id / plaid txn id
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_invoice_payments_invoice
  ON invoice_payments(invoice_id);

CREATE TABLE IF NOT EXISTS invoice_credit_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE RESTRICT,
  issued_on TEXT NOT NULL,                  -- YYYY-MM-DD
  amount_cents INTEGER NOT NULL,            -- gross reduction, tax included
  tax_cents INTEGER NOT NULL DEFAULT 0,     -- the tax portion, reversed to 2200
  reason TEXT NOT NULL DEFAULT '',
  -- 0 = credit note against an unpaid balance (credits AR)
  -- 1 = cash refund of money already banked (credits Cash)
  -- The distinction changes which account is credited, so it is stored
  -- rather than inferred from whether the invoice happens to be paid.
  is_refund INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_invoice_credit_notes_invoice
  ON invoice_credit_notes(invoice_id);

-- Money taken before there is an invoice to apply it to. It is a liability
-- until earned, not revenue -- the single most common small-business
-- bookkeeping error this table exists to prevent.
CREATE TABLE IF NOT EXISTS customer_deposits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE RESTRICT,
  received_on TEXT NOT NULL,                -- YYYY-MM-DD
  amount_cents INTEGER NOT NULL,
  applied_invoice_id INTEGER REFERENCES invoices(id) ON DELETE RESTRICT,
  applied_on TEXT,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_customer_deposits_contact
  ON customer_deposits(contact_id);
