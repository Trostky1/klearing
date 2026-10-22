-- Immutable snapshots let COGS use a cost that was known before the sale,
-- rather than applying a later current Shopify cost to historical orders.
CREATE TABLE IF NOT EXISTS shopify_variant_costs (
  variant_id TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  inventory_item_id TEXT,
  unit_cost_cents INTEGER,
  cost_status TEXT NOT NULL CHECK (cost_status IN (
    'available', 'missing', 'unavailable'
  )),
  detail TEXT,
  PRIMARY KEY (variant_id, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_shopify_variant_costs_lookup
  ON shopify_variant_costs(variant_id, captured_at DESC);

-- A durable, visible explanation for source records that are intentionally
-- not posted. A future successful import clears the corresponding record.
CREATE TABLE IF NOT EXISTS ledger_source_import_skips (
  source_reference TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  reason TEXT NOT NULL,
  detail TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
