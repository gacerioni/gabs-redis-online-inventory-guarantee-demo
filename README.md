# Demo de Reserva de Inventário Online (Redis + Postgres, com CDC/RDI)

Reservas lock-free e atômicas usando scripts Lua no Redis.  
O Postgres é o *system of record* (SoR) do estoque (`inventory.total`) e dos pedidos.  
O RDI/CDC replica automaticamente o campo `total` para o Redis (em `inv:{sku}.total`).  
O app gerencia apenas `reserved` e `hold:*` no Redis.  
Disponível = `total (PG/RDI)` – `reserved (Redis)`.

## Como funciona (resumo)
1) `/reserve`: script Lua valida `available = total - reserved`, cria hold com `expiresAt` e indexa no ZSET.  
2) `/commit`: primeiro `UPDATE inventory.total` no PG; RDI replica `total` para Redis; então Lua faz `reserved--` e apaga o hold.  
3) `/release`: Lua devolve `reserved--` e apaga hold.  
4) Reaper: varre holds vencidos e chama release automático.  
Sem loops: app nunca escreve `total` em Redis; RDI nunca toca `reserved`/`hold:*`.

## Requisitos e instalação (requirements.txt)
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env  # edite credenciais
set -a; source .env; set +a
uvicorn app_rdi:app --reload
```

## Variáveis (.env)
Veja `.env.example`.
- PG_DSN, REDIS_URL são obrigatórias.

## Rodando com Docker
Crie o container:
```bash
docker build -t online-inventory-demo:latest .
```

Execute com suas variáveis de conexão:
```bash
docker run --rm -p 8000:8000 \
  -e PG_DSN="postgresql://USER:PASS@RDS_HOST:5432/DB?sslmode=require" \
  -e REDIS_URL="redis://host.docker.internal:6379/0" \
  -e HOLD_TTL_SECONDS_DEFAULT=600 \
  -e REAPER_INTERVAL_SECS=1 \
  online-inventory-demo:latest
```

Observação: em Macs/Windows use `host.docker.internal` para falar com o Redis local; em Linux, mapear rede ou passar o IP do host.

## Esquema PG mínimo
```sql
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'order_status') THEN
    CREATE TYPE order_status AS ENUM ('PENDING','CONFIRMED','CANCELLED');
  END IF;
END$$;

CREATE TABLE IF NOT EXISTS skus (
  id   TEXT PRIMARY KEY,
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory (
  sku_id TEXT PRIMARY KEY REFERENCES skus(id),
  total  INTEGER NOT NULL CHECK (total >= 0)
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

INSERT INTO skus (id, name) VALUES
  ('sku-123', 'Hydra Serum 30ml'),
  ('sku-456', 'Hydra Serum 50ml')
ON CONFLICT (id) DO NOTHING;

INSERT INTO inventory (sku_id, total) VALUES
  ('sku-123', 10),
  ('sku-456', 5)
ON CONFLICT (sku_id) DO NOTHING;
```

## Testes rápidos (cURL)
```bash
API="http://127.0.0.1:8000"

curl -s -X POST "$API/reserve" -H 'content-type: application/json' \
  -d '{"sku":"sku-123","qty":2,"cart_id":"cart-abc","ttl_seconds":120}' | jq

curl -s -X POST "$API/commit" -H 'content-type: application/json' \
  -d '{"cart_id":"cart-abc","sku":"sku-123"}' | jq

curl -s "$API/snapshot/sku-123" | jq
curl -s "$API/events?limit=10" | jq
```

## Observabilidade
- Stream Redis opcional (`inv:events`) para auditar holds e commits.  
- `snapshot/{sku}` mostra `total/reserved/available`.  

## Por que é seguro
- Scripts Lua atômicos → sem interleaving.  
- Idempotência por `holdId = cart_id:sku`.  
- Reaper garante liberação em abandono de carrinho.  
- Commit PG-first + RDI garante convergência PG↔Redis.
