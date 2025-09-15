-- SoR de estoque no Postgres: TOTAL por SKU.
-- O RDI levará estas linhas -> Redis HASH por SKU (campo "total").

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'order_status') THEN
    CREATE TYPE order_status AS ENUM ('PENDING','CONFIRMED','CANCELLED');
  END IF;
END$$;

-- Catálogo
CREATE TABLE IF NOT EXISTS skus (
  id   TEXT PRIMARY KEY,
  name TEXT NOT NULL
);

-- Estoque "final" por SKU (SoR)
CREATE TABLE IF NOT EXISTS inventory (
  sku_id TEXT PRIMARY KEY REFERENCES skus(id),
  total  INTEGER NOT NULL CHECK (total >= 0)
);

-- (Opcional) Pedidos – se quiser registrar checkout no PG por aqui
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

-- Seeds mínimos (catálogo + estoque SoR)
INSERT INTO skus (id, name) VALUES
  ('sku-123', 'Hydra Serum 30ml'),
  ('sku-456', 'Hydra Serum 50ml')
ON CONFLICT (id) DO NOTHING;

INSERT INTO inventory (sku_id, total) VALUES
  ('sku-123', 10),
  ('sku-456', 5)
ON CONFLICT (sku_id) DO NOTHING;