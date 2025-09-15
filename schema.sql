-- ============================================================================
-- Minimal Schema for Online Inventory Reservation Demo
-- Redis handles operational inventory; Postgres is system of record for orders
-- This version seeds SKUs + inventory ONLY (no pending orders).
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'order_status') THEN
    CREATE TYPE order_status AS ENUM ('PENDING','CONFIRMED','CANCELLED');
  END IF;
END$$;

CREATE TABLE IF NOT EXISTS skus (
  id   TEXT PRIMARY KEY,
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory (
  sku_id      TEXT PRIMARY KEY REFERENCES skus(id),
  initial_qty INTEGER NOT NULL CHECK (initial_qty >= 0)
);

CREATE TABLE IF NOT EXISTS orders (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  cart_id    TEXT,
  status     order_status NOT NULL DEFAULT 'PENDING',
  created_at TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_items (
  order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  sku_id   TEXT NOT NULL REFERENCES skus(id),
  qty      INTEGER NOT NULL CHECK (qty > 0),
  PRIMARY KEY(order_id, sku_id)
);

-- ---------------------------------------------------------------------------
-- Seed data (catalog + bootstrap inventory)
-- ---------------------------------------------------------------------------
INSERT INTO skus (id, name) VALUES
  ('sku-123', 'Hydra Serum 30ml'),
  ('sku-456', 'Hydra Serum 50ml'),
  ('sku-789', 'Hydra Mask 100ml')
ON CONFLICT (id) DO NOTHING;

INSERT INTO inventory (sku_id, initial_qty) VALUES
  ('sku-123', 10),
  ('sku-456', 5),
  ('sku-789', 8)
ON CONFLICT (sku_id) DO NOTHING;

-- Done. Use the API to create orders dynamically:
--   1) POST /reserve  (Redis hold)
--   2) POST /order    (creates orders + order_items here)
--   3) POST /confirm or /cancel (commits/releases hold in Redis)
