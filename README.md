# Demo de Reserva de Inventário Online (Redis + Postgres, com CDC/RDI)

Este projeto demonstra como implementar **reservas de inventário lock-free e atômicas** usando **scripts Lua no Redis**, com o **Postgres** como *system of record* (SoR) e o **RDI/CDC** garantindo convergência entre os dois mundos.

- **Postgres** mantém o estoque final (`inventory.total`) e os pedidos.  
- **RDI/CDC** replica automaticamente o campo `total` para Redis (`inv:{sku}.total`).  
- **Redis** gerencia apenas `reserved` e `hold:*`, calculando disponibilidade em tempo real:  
  ```
  available = total (PG/RDI) – reserved (Redis)
  ```

---

## Arquitetura

```
Cliente ---> /reserve (Lua Redis) -----> Cria hold + reserved++
        \                               \-> Evento em Stream (opcional)
         \-> Pedido no PG --> /commit ----> UPDATE inventory.total (PG)
                                        \-> RDI/CDC replica 'total' → Redis
         \-> /release (Lua Redis) -----> reserved-- + DEL hold
         \-> Reaper -------------------> libera holds expirados
```

**Componentes principais**:
- **/reserve**: garante atomicamente que há estoque disponível, cria o hold e indexa em ZSET.  
- **/commit**: decrementa o `inventory.total` no PG; o RDI replica para Redis; depois o hold é finalizado.  
- **/release**: devolve reservas manualmente.  
- **Reaper**: varre e libera automaticamente holds expirados (carrinho abandonado).  
- **/extend**: permite estender prazo de um hold se o cliente ainda estiver ativo.  

---

## Por que é seguro

- **Atomicidade**: cada operação é um único script Lua, sem interleaving.  
- **Sem oversell**: reserva falha se `available < qty`.  
- **Idempotência**: cada hold tem ID único (`cart_id:sku`), permitindo retries seguros.  
- **Abandono tratado**: reaper garante liberação de estoque.  
- **PG como SoR**: só o Postgres altera `inventory.total`; Redis nunca fica divergente porque o RDI replica.  
- **Sem loops**: app não toca em `total`; RDI não toca em `reserved`/`hold:*`.  

---

## Setup local (venv + requirements)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

cp .env.example .env   # configure PG_DSN e REDIS_URL
set -a; source .env; set +a

uvicorn app_rdi:app --reload
```

---

## Setup com Docker

Build da imagem:
```bash
docker build -t online-inventory-demo:latest .
```

Run com variáveis de conexão:
```bash
docker run --rm -p 8000:8000 \
  -e PG_DSN="postgresql://USER:PASS@RDS_HOST:5432/DB?sslmode=require" \
  -e REDIS_URL="redis://host.docker.internal:6379/0" \
  -e HOLD_TTL_SECONDS_DEFAULT=600 \
  -e REAPER_INTERVAL_SECS=1 \
  online-inventory-demo:latest
```

Observação: em Mac/Windows use `host.docker.internal` para conectar no Redis local; em Linux use IP do host ou compose.

---

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

---

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

---

## Observabilidade

- **Stream Redis** opcional (`inv:events`) para auditar holds e commits.  
- **`/snapshot/{sku}`** mostra `total/reserved/available` em tempo real.  

---

