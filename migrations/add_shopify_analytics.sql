-- Historical Shopify analytics, imported from Shopify Admin CSV exports.
--
-- DELIBERATELY NOT PART OF THE LEDGER. This is aggregated reporting data
-- for trend display only. It has no foreign key into journal_entries or
-- accounts, nothing reads it when posting, and nothing reconciles it
-- against profit_and_loss() — the two describe different scopes
-- (aggregate/historical vs transactional/ongoing) and cross-checking them
-- would invent a disagreement rather than find one.
--
-- The ledger starts fresh going forward; this exists so the dashboard can
-- still show what happened before that line.

CREATE TABLE IF NOT EXISTS shopify_analytics_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  metric_type TEXT NOT NULL CHECK (metric_type IN (
    'total_sales', 'sales_by_product', 'order_count'
  )),
  period TEXT NOT NULL CHECK (period IN ('day', 'week', 'month')),
  period_start_date TEXT NOT NULL,
  -- Integer cents for money metrics, a plain count for order_count. Which
  -- one applies is determined by metric_type, not stored per row.
  value INTEGER NOT NULL,
  -- Only populated for product-level breakdowns.
  product_ref TEXT,
  product_title TEXT,
  source TEXT NOT NULL CHECK (source IN ('csv_export', 'bulk_api')),
  imported_at TEXT NOT NULL
);

-- Natural key: re-importing the same export overwrites rather than
-- duplicating, so a user who exports an overlapping date range twice does
-- not double their historical totals. COALESCE because product_ref is
-- NULL on store-wide metrics and NULLs do not compare equal.
CREATE UNIQUE INDEX IF NOT EXISTS idx_shopify_analytics_natural
  ON shopify_analytics_history(
    metric_type, period, period_start_date, COALESCE(product_ref, '')
  );

CREATE INDEX IF NOT EXISTS idx_shopify_analytics_trend
  ON shopify_analytics_history(metric_type, period, period_start_date);
