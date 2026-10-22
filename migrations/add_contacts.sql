-- A real party record. Until now nothing in Klearing modelled "a person or
-- company we do business with": orders carry denormalized customer_name /
-- customer_email strings, invoice_reminders carries a different pair of
-- strings, bank transactions carry only a free-text merchant name, and
-- vendor_memory is a category-learning string map rather than an entity.
-- Nothing joins to anything.
--
-- contacts is the shared spine for Invoicing/AR (who an invoice bills) and
-- the CRM/pipeline (the contact list itself). It is deliberately seeded from
-- shopify_customers rather than re-collected, since that sync already runs
-- and already carries a stable per-person id.
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  display_name TEXT NOT NULL,
  email TEXT,
  phone TEXT,
  -- Stable external ids, nullable because a contact can be created by hand
  -- long before it is ever linked to a store or payment record. UNIQUE so a
  -- re-run of the Shopify backfill updates rather than duplicates.
  shopify_customer_id TEXT UNIQUE,
  stripe_customer_id TEXT UNIQUE,
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);
CREATE INDEX IF NOT EXISTS idx_contacts_display_name ON contacts(display_name);

-- The strings a contact is known by somewhere else in the system: a Shopify
-- display name, a bank memo's merchant text, a payout descriptor. Plaid gives
-- no counterparty id at all, so matching a deposit back to a contact is
-- necessarily string work -- this table is where that accumulated knowledge
-- lives instead of being re-guessed per query.
--
-- alias_text is stored lowercased and trimmed by the writer so lookups never
-- have to apply a function to the column (which would defeat the index).
CREATE TABLE IF NOT EXISTS contact_aliases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  alias_text TEXT NOT NULL,
  source TEXT NOT NULL,           -- shopify | manual | bank_match
  created_at TEXT NOT NULL,
  UNIQUE (contact_id, alias_text)
);

CREATE INDEX IF NOT EXISTS idx_contact_aliases_text
  ON contact_aliases(alias_text);
