# Demo de Reserva de Inventário Online (Redis + Postgres, sem CDC)

> Reservas lock-free e atômicas usando scripts Lua no Redis; o Postgres permanece como *system of record* (SoR) de pedidos e estoque final. Este README explica a arquitetura, setup, como o demo funciona, comandos de teste com `curl`, e como o Lua garante segurança e integridade.

---

## Por que isso existe

Em e-commerce é crítico garantir que, quando um comprador entra no checkout, realmente exista estoque disponível. Usar locks em banco ou locks distribuídos é lento e frágil.  
Este demo usa **Redis** como fonte operacional da verdade (controle em tempo real do “pode vender agora?”) com **scripts Lua atômicos** (sem locks distribuídos) e o **Postgres** como fonte financeira/auditável (pedidos, relatórios).

- **Redis**: contadores ultra-rápidos e reservas de curta duração (**holds**) com prazos de expiração.
- **Postgres**: pedidos e itens; Outbox ou CDC podem ser adicionados em produção para maior resiliência.

---

## Arquitetura de alto nível

```
Cliente ---> /reserve (Lua Redis) ------> Cria hold + incrementa reserved
         \                               \-> Evento em Stream (opcional)
          \-> Pedido no PG --> /commit ou /release (Lua Redis)
```

- **Reserve**: move unidades `available → reserved`, cria `hold:{cartId}:{sku}` com prazo de expiração.
- **Commit**: remove o hold e decrementa `reserved` (estoque efetivamente consumido).
- **Release/Expire**: devolve unidades `reserved → available`, deleta hold.
- **Sem locks distribuídos**: cada passo é um único script Lua no Redis.

### Modelo de autoridade

- **Verdade operacional (disponibilidade)**: **Redis** (`reserved`, `holds` e cálculo de `available`).
- **Verdade financeira (final)**: **Postgres** (`orders`, `inventory.total`).

> Opcional: um Redis Stream (`inv:events`) registra `hold_created`, `hold_committed`, `hold_released` para auditoria.

---

## Como o demo funciona

1. **Seed inicial**
   - No startup, o Redis recebe `inventory.total` do Postgres (via RDI/CDC).
   - Campo `reserved` é inicializado como 0.

2. **Fluxo de reserva**
   - Cliente chama `/reserve {sku, qty, cart_id}`.
   - Script Lua no Redis:
     - Valida `available >= qty`.
     - Atualiza `reserved` atômico.
     - Cria `hold:{cart_id}:{sku}` com `expiresAt`.
     - Indexa hold num ZSET (`holds:exp`) para reaper.
     - Emite evento opcional.

3. **Commit/Release**
   - **Commit**: serviço primeiro decrementa `inventory.total` no PG; depois chama Lua para ajustar `reserved` e remover hold.
   - **Release**: chama Lua para devolver reserva (`reserved -= qty`) e apagar hold.

4. **Reaper de abandonos**
   - Um loop periódico varre `holds:exp` e chama `RELEASE_LUA` para cada hold vencida.
   - Isso garante que abandonos de carrinho liberam estoque corretamente.

5. **(Opcional) Outbox ou CDC**
   - Não necessário para o demo.  
   - Produção: Outbox (simples) ou CDC (mais infra, permite replay) podem ser usados para robustez.

---

## Segurança e integridade: como o Lua garante

### Atomicidade
Cada operação (reserve/commit/release) é um único script Lua → executa de ponta a ponta sem interleaving. Elimina condições de corrida.

### Invariantes
- **Sem oversell**: reserva falha se `available < qty`.  
- **Consistência**: reserve move sempre `available→reserved`; release devolve; commit só reduz `reserved`.  
- **Abandono tratado**: holds têm `expiresAt` + reaper para auto-liberar.

### Idempotência
- **HoldId = cart_id:sku**.  
- Repetir o mesmo reserve/commit/release é seguro:
  - Reserve detecta hold já existente.  
  - Commit/Release podem ser repetidos sem quebrar contadores.

### Durabilidade & HA
- **AOF** habilitado no Redis (`everysec` ou `always`).  
- **Replicação/failover** (Sentinel, Redis Enterprise, ElastiCache Multi-AZ).  
- **Cluster/sharding** para escalar.  

### Recuperação
- Redis reinicia e recarrega AOF.  
- Em desastre, é possível **replay de pedidos do PG** (via CDC/outbox) aplicando os mesmos scripts idempotentes.

---

## Configuração

Variáveis de ambiente ou constantes no topo do `app_rdi.py`.

| Variável | Default | Propósito |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Conexão Redis |
| `PG_DSN` | `postgresql://postgres:postgres@localhost:5432/postgres` | Conexão PG |
| `HOLD_TTL_SECONDS_DEFAULT` | `600` | Prazo padrão do hold (segundos) |
| `ENABLE_EVENTS_STREAM` | `true` | Ativa logging em Stream Redis |
| `EVENTS_STREAM_NAME` | `inv:events` | Nome do stream |
| `REAPER_INTERVAL_SECS` | `1` | Intervalo do reaper |
| `STRICT_UUID` | `true` | Validação de UUID em rotas (se usado) |

---

## Setup & run

```bash
python -m venv .venv && source .venv/bin/activate
pip install "fastapi>=0.111,<1" "uvicorn[standard]>=0.27,<1" "redis>=5,<6" "psycopg[binary]>=3.1,<4"

# env
export PG_DSN="postgresql://USER:PASS@RDS_HOST:5432/DB?sslmode=require"
export REDIS_URL="redis://localhost:6379/0"

# run
uvicorn app_rdi:app --reload
```

### Esquema PG (mínimo)
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

### Eventos recentes
```bash
curl -s "$API/events?limit=10" | jq
```

---

## Choices de design (por que é seguro)

- **Lua atômico**: elimina race conditions.  
- **Idempotência**: `holdId` garante retries seguros.  
- **Lease+Reaper**: abandonos de carrinho sempre liberam estoque.  
- **PG-first commit**: SoR nunca é bypassado; Redis é sincronizado via RDI.  
- **Separation of concerns**: Redis → “pode vender agora”, PG → verdade final.  
