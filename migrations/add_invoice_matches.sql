-- Proposed invoice payments awaiting a human's confirmation.
--
-- Two independent signals detect that an invoice was probably paid: the
-- Stripe poll (a completed payment-link checkout) and the bank cross-match
-- (a Plaid deposit whose exact amount and date line up with an open
-- invoice). Neither posts on its own -- "nothing posts without a click" --
-- so each writes a pending row here, which the Review drawer renders as
-- "Payment detected: apply?".
--
-- This is a separate migration file, not an addition to add_invoicing.sql,
-- because the migration runner is table-gated: an install that already has
-- the invoicing tables would never re-run that file, so a table added to
-- it retroactively would never appear.
CREATE TABLE IF NOT EXISTS invoice_payment_matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  source TEXT NOT NULL,               -- stripe | bank
  -- The external event this proposal came from: stripe:session:<id> or
  -- plaid:transaction:<id>. UNIQUE so the poller and the bank matcher are
  -- each idempotent -- re-running never proposes the same money twice.
  reference TEXT NOT NULL UNIQUE,
  amount_cents INTEGER NOT NULL,      -- gross the customer paid
  fee_cents INTEGER NOT NULL DEFAULT 0,  -- processor fee (Stripe; 0 for bank)
  paid_on TEXT NOT NULL,              -- YYYY-MM-DD
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | applied | dismissed
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_invoice_payment_matches_status
  ON invoice_payment_matches(status);
CREATE INDEX IF NOT EXISTS idx_invoice_payment_matches_invoice
  ON invoice_payment_matches(invoice_id);
