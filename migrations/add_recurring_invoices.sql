-- Recurring invoices: a template that says what to bill and how often,
-- and one instance row per cycle that became due.
--
-- The split matters. A template is an editable intention that changes
-- over time; an instance is a historical fact about a date that arrived.
-- Folding them together would mean editing next month's price silently
-- rewrote what last month said it would bill.
--
-- Nothing here posts. An instance is a queued decision, and only the
-- user approving it in Review causes a real invoice to be created and
-- sent through invoice_ledger.send_invoice -- the same call a manual
-- invoice makes, so the ledger cannot tell the two apart.
CREATE TABLE IF NOT EXISTS recurring_invoice_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE RESTRICT,
  name TEXT NOT NULL DEFAULT '',
  -- weekly and monthly are the two people actually ask for; custom is
  -- every N days. Deliberately no "every 2nd Tuesday" and no quarterly
  -- preset: monthly and custom already express what those mean, and each
  -- extra pattern is another set of date-math edge cases to get wrong.
  recurrence TEXT NOT NULL CHECK (recurrence IN ('weekly', 'monthly', 'custom')),
  -- Days, for custom only. NULL otherwise so an unused value cannot be
  -- mistaken for a meaningful one.
  interval_days INTEGER,
  -- The day-of-month a monthly template is anchored to, captured when it
  -- is created. Kept separately from next_due_date because clamping is
  -- lossy: a template anchored on the 31st lands on the 28th in February,
  -- and without the anchor the following March would inherit the 28th and
  -- the schedule would walk backwards permanently after one short month.
  anchor_day INTEGER,
  next_due_date TEXT NOT NULL,
  -- Paused rather than deleted is the reversible option, matching the
  -- app's wider "deny is recoverable" stance.
  active INTEGER NOT NULL DEFAULT 1,
  terms TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  -- Days between an instance's issue date and its due date, applied when
  -- the real invoice is created.
  due_in_days INTEGER NOT NULL DEFAULT 30,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recurring_templates_due
  ON recurring_invoice_templates(active, next_due_date);

-- Mirrors invoice_lines exactly, including the integer disciplines:
-- quantity in thousandths, price in cents, tax in basis points. A
-- template line is copied into a real invoice line verbatim, so any
-- divergence in representation here would be a rounding bug waiting for
-- the first approval.
CREATE TABLE IF NOT EXISTS recurring_invoice_template_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  template_id INTEGER NOT NULL
    REFERENCES recurring_invoice_templates(id) ON DELETE CASCADE,
  position INTEGER NOT NULL DEFAULT 0,
  description TEXT NOT NULL DEFAULT '',
  quantity_milli INTEGER NOT NULL DEFAULT 1000,
  unit_price_cents INTEGER NOT NULL DEFAULT 0,
  tax_rate_bp INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_recurring_template_lines
  ON recurring_invoice_template_lines(template_id, position);

-- One row per cycle that came due. invoice_id is filled in only once the
-- user approves, so a pending row provably has no accounting behind it.
--
-- UNIQUE (template_id, due_date) IS the idempotency guard, and it is a
-- constraint rather than a check-then-insert because the two callers --
-- app launch and the daily 8am job -- can fire seconds apart. A guard
-- that reads before writing would let both pass the read before either
-- writes; this one cannot be raced.
CREATE TABLE IF NOT EXISTS recurring_invoice_instances (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  template_id INTEGER NOT NULL
    REFERENCES recurring_invoice_templates(id) ON DELETE CASCADE,
  due_date TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending_review' CHECK (status IN (
    'pending_review', 'approved_sent', 'skipped'
  )),
  invoice_id INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
  -- Why a cycle could not be generated into a valid invoice, when that
  -- happens. Recorded rather than guessed around: a template whose lines
  -- total zero must not silently produce a zero invoice.
  skip_reason TEXT NOT NULL DEFAULT '',
  decided_at TEXT,
  UNIQUE (template_id, due_date)
);

CREATE INDEX IF NOT EXISTS idx_recurring_instances_status
  ON recurring_invoice_instances(status, due_date);
