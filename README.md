# Demo de Reserva de Inventário Online (Redis + Postgres, com CDC/RDI)

> Reservas lock-free e atômicas usando scripts Lua no Redis.  
> O **Postgres** é o *system of record* (SoR) do estoque (`inventory.total`) e dos pedidos.  
> O **RDI/CDC** replica automaticamente o campo `total` para o Redis (em `inv:{sku}.total`).  
> O app gerencia apenas `reserved` e `hold:*` no Redis.  
> Disponível = `total (PG/RDI)` – `reserved (Redis)`.

---

## Por que isso existe

No e-commerce é crítico garantir que, quando o cliente entra no checkout, haja estoque para atender.  
Locks em banco ou distribuídos são caros e frágeis.  

Este design usa:
- **Redis** → fonte operacional de verdade (quem decide “pode vender agora?”).  
- **Postgres** → fonte financeira/auditável (pedidos, estoque consolidado).  
- **RDI/CDC** → garante que `inventory.total` do PG seja refletido no Redis automaticamente.

---

## Arquitetura de alto nível

```
Cliente ---> /reserve (Lua Redis) -----> Cria hold + reserved++
        \                               \-> Evento em Stream (opcional)
         \-> Pedido no PG --> /commit ----> UPDATE inventory.total (PG)
                                        \-> RDI/CDC replica 'total' → Redis
         \-> /release (Lua Redis) -----> reserved-- + DEL hold
         \-> Reaper -------------------> libera holds expirados
```

- **Reserve**: checa `available = total - reserved`, cria hold com prazo.  
- **Commit**: decrementa `inventory.total` no PG; RDI replica no Redis. Depois, o hold é finalizado em Redis (`reserved -= qty`).  
- **Release/Expire**: devolve estoque (reserved--, hold deletado).  
- **Reaper**: garante que carrinhos abandonados liberem reservas.  
- **Sem loops**: app nunca escreve `total` no Redis; RDI nunca toca `reserved`/`hold:*`.

---

## Como funciona

1. **Seed inicial**  
   - O Redis recebe `inventory.total` do PG via RDI.  
   - `reserved=0` inicializado pelo app.

2. **Reserva** (`/reserve`)  
   - Script Lua atômico valida `available >= qty`, atualiza `reserved`, cria hold com `expiresAt`, indexa no ZSET `holds:exp`.  
   - Evento opcional em Stream.

3. **Commit** (`/commit`)  
   - Serviço lê o hold, tenta `UPDATE inventory.total = total - qty` no PG.  
   - Se sucesso: RDI replica novo `total` → Redis. App roda `COMMIT_LUA` para `reserved--` e apagar hold.  
   - Se falha (PG total insuficiente): app libera hold (`RELEASE_LUA`) e retorna erro 409.

4. **Release manual** (`/release`)  
   - Executa `RELEASE_LUA` → `reserved--` + DEL hold.  
   - Não altera `total` no PG.

5. **Reaper**  
   - Loop periódico varre `holds:exp`, chama `RELEASE_LUA` para holds vencidos.  
   - Garante liberação automática de carrinhos abandonados.

6. **Extend** (`/extend`)  
   - Endpoint opcional para prorrogar prazo de um hold enquanto cliente está ativo.

---

## Segurança e integridade

- **Atomicidade**: cada operação é um script Lua → indivisível.  
- **Sem oversell**: reserva falha se `available < qty`.  
- **Idempotência**: `holdId = cart_id:sku` → retries seguros.  
- **Abandono coberto**: reaper sempre chama `RELEASE_LUA`.  
- **SoR garantido**: `total` só muda no PG; RDI replica para Redis.  
- **Sem loops**: app nunca mexe em `total` do Redis.

---

## Configuração

| Variável | Default | Propósito |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Conexão Redis |
| `PG_DSN` | `postgresql://postgres:postgres@localhost:5432/postgres` | Conexão PG |
| `HOLD_TTL_SECONDS_DEFAULT` | `600` | Prazo padrão do hold |
| `REAPER_INTERVAL_SECS` | `1` | Intervalo de sweep do reaper |
| `ENABLE_EVENTS_STREAM` | `true` | Liga Stream de eventos no Redis |
| `EVENTS_STREAM_NAME` | `inv:events` | Nome do Stream |
| `STRICT_UUID` | `true` | Validação de UUID |

---

## Setup & run

```bash
python -m venv .venv && source .venv/bin/activate
pip install "fastapi>=0.111,<1" "uvicorn[standard]>=0.27,<1" "redis>=5,<6" "psycopg[binary]>=3.1,<4"

export PG_DSN="postgresql://USER:PASS@RDS:5432/DB?sslmode=require"
export REDIS_URL="redis://localhost:6379/0"

uvicorn app_rdi:app --reload
```

---

## Esquema PG (mínimo)

```sql
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE TYPE order_status AS ENUM ('PENDING','CONFIRMED','CANCELLED');

CREATE TABLE skus (
  id   TEXT PRIMARY KEY,
  name TEXT NOT NULL
);

CREATE TABLE inventory (
  sku_id TEXT PRIMARY KEY,
  total  INTEGER NOT NULL CHECK (total >= 0)
);

INSERT INTO skus (id,name) VALUES ('sku-123','Hydra Serum 30ml') ON CONFLICT DO NOTHING;
INSERT INTO inventory (sku_id,total) VALUES ('sku-123',10) ON CONFLICT DO NOTHING;
```

---

## API quickstart (cURLs)

```bash
API="http://127.0.0.1:8000"
```

### Reservar
```bash
curl -s -X POST "$API/reserve" -H 'content-type: application/json' \
  -d '{"sku":"sku-123","qty":2,"cart_id":"cart-abc","ttl_seconds":120}' | jq
```

### Estender hold
```bash
curl -s -X POST "$API/extend" -H 'content-type: application/json' \
  -d '{"cart_id":"cart-abc","sku":"sku-123","add_seconds":60}' | jq
```

### Commit
```bash
curl -s -X POST "$API/commit" -H 'content-type: application/json' \
  -d '{"cart_id":"cart-abc","sku":"sku-123"}' | jq
```

### Release manual
```bash
curl -s -X POST "$API/release" -H 'content-type: application/json' \
  -d '{"cart_id":"cart-abc","sku":"sku-123"}' | jq
```

### Snapshot
```bash
curl -s "$API/snapshot/sku-123" | jq
```

### Eventos
```bash
curl -s "$API/events?limit=10" | jq
```

---

## Por que é seguro agora

- **Redis decide disponibilidade** (`available = total - reserved`).  
- **PG continua SoR** → commit só confirma se conseguiu decrementar `inventory.total`.  
- **RDI garante convergência** entre PG e Redis.  
- **Reaper** evita vazamentos de reserva em abandono.  
- **Sem loops** entre sistemas.  
