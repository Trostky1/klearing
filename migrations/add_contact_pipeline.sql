-- Where a contact sits in the pipeline, as a judgment somebody made.
--
-- This is deliberately its own table rather than a column on contacts,
-- for two reasons. The migration runner gates on table existence, so a
-- migration that only ALTERs an existing table has no condition it can
-- be gated by -- it would either never run or run every time. And
-- contacts is the shared spine behind Invoicing/AR as well as the CRM,
-- so CRM-owned state living beside it keeps the blast radius of a
-- pipeline change inside the CRM.
--
-- A contact with no row here has no stage, and the UI says "Unassigned"
-- rather than defaulting one. An unmade judgment is not the same as a
-- judgment of "lead", and defaulting would put words in the user's
-- mouth about every contact the Shopify backfill ever created.
--
-- The stages themselves are a fixed set, not user-configurable, and they
-- deliberately carry only what the data cannot say for itself. Order
-- count and lifetime revenue are already known exactly -- encoding them
-- again as "active" or "repeat" would create a second answer to a
-- question the ledger already answers, free to drift from it the moment
-- an order lands. What the ledger cannot know is whether this customer
-- matters, so that is what the stage records.
CREATE TABLE IF NOT EXISTS contact_pipeline (
  contact_id INTEGER PRIMARY KEY
    REFERENCES contacts(id) ON DELETE CASCADE,
  stage TEXT NOT NULL CHECK (stage IN (
    'lead', 'customer', 'key_account', 'dormant'
  )),
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contact_pipeline_stage
  ON contact_pipeline(stage);
