-- Inventory acquisition tracking.
--
-- The missing half of COGS. shopify_variant_costs already records what a
-- unit cost at a point in time; nothing recorded that units were ever
-- acquired, so COGS had nothing to credit Inventory against and
-- _sync_shopify_cogs refused to post at all (see
-- SkipReason.INVENTORY_ACQUISITION_NOT_TRACKED). These events supply the
-- debit side and make stock-on-hand derivable rather than assumed.
--
-- Keyed on Shopify variant_id, matching shopify_variant_costs, rather
-- than introducing a third product representation alongside
-- shopify_products.variants_json.

CREATE TABLE IF NOT EXISTS inventory_quantity_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  variant_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN (
    'acquisition', 'sale', 'adjustment'
  )),
  -- Signed: acquisitions positive, sales negative, adjustments either.
  quantity INTEGER NOT NULL CHECK (quantity <> 0),
  -- Required on acquisitions (it is the FIFO layer cost) and meaningless
  -- elsewhere; the CHECK enforces that rather than trusting callers.
  unit_cost_cents INTEGER CHECK (unit_cost_cents IS NULL OR unit_cost_cents >= 0),
  effective_date TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('shopify_sync', 'manual')),
  -- Idempotency, same contract as ledger_source_imports: an event
  -- carrying a source reference can only be recorded once. Manual
  -- entries pass NULL, and SQLite treats NULLs as distinct in a UNIQUE
  -- index, so repeated manual corrections stay possible.
  source_reference TEXT,
  note TEXT,
  created_at TEXT NOT NULL,
  CHECK (event_type <> 'acquisition' OR unit_cost_cents IS NOT NULL),
  CHECK (event_type <> 'acquisition' OR quantity > 0),
  CHECK (event_type <> 'sale' OR quantity < 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_events_source
  ON inventory_quantity_events(source_reference)
  WHERE source_reference IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_inventory_events_variant
  ON inventory_quantity_events(variant_id, effective_date, id);
