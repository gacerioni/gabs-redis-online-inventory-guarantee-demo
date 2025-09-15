# Online Inventory Reservation Demo (Redis + Postgres, No CDC)

> Lock-free, atomic reservations using Redis Lua scripts; Postgres remains the system of record for orders. Optional CDC notes included. This README explains architecture, setup, how the demo works, curl commands, and how Lua ensures safety and integrity.

---

## Why this exists

E-commerce flows need to guarantee that when a buyer enters checkout there’s actually stock to fulfill. Traditional DB row locking or distributed locks are slow and fragile. This demo uses **Redis** as the *operational* source of truth for “can we sell now?” using **atomic Lua scripts** (no distributed locks), and **Postgres** as the *financial/audit* system of record (orders, reporting).

- **Redis**: ultra-fast counters and short-lived **holds** (reservations) with TTLs.
- **Postgres**: orders and order items; optional outbox/CDC is discussed for hardening.

---

## High-level architecture

```
Client  --->  Reserve (Redis Lua) --------->  Hold (TTL) + counters
          \                                \  Stream event (optional)
           \-> Order in PG  ---> Confirm/Cancel ---> Commit/Release (Redis Lua)
```

- **Reserve**: Moves units `available → reserved`, creates `hold:{cartId}:{sku}` with TTL.
- **Confirm**: Removes the hold, decrements `reserved` (stock permanently consumed).
- **Cancel/Expire**: Returns units `reserved → available`, deletes hold.
- **No distributed locks**: Each step is a single atomic script (Lua) in Redis.

### Authority model

- **Operational truth (availability)**: **Redis** counters (`available`, `reserved`) and per-hold keys.
- **Financial truth**: **Postgres** orders. Reporting/audit is built from PG.

> Optional: A Redis Stream (`inv:events`) logs `hold_created`, `hold_committed`, and `hold_released` for observability.

---

## How the demo works

1. **Startup seeding**
   - On app startup, it attempts to **seed Redis** from Postgres’ `inventory` table:
     - For each `sku_id`, set `inv:{sku}` → `{available=initial_qty, reserved=0}`.
   - If DB seed fails or is disabled, a **fallback seed** is used (configurable).

2. **Reservation flow (no CDC)**
   - Client calls `/reserve` with `{sku, qty, cart_id}`.
   - A single **Lua script** in Redis:
     - Validates `available >= qty`.
     - Updates counters atomically.
     - Creates `hold:{cart_id}:{sku}` with TTL.
     - Emits an event to a stream (optional).

3. **Order creation**
   - Client calls `/order` to write `orders` + `order_items` in Postgres (SoR).

4. **Confirm/Cancel**
   - On **confirm**, the app calls **`commit_hold`** in Redis for each item.
   - On **cancel**, the app calls **`release_hold`** in Redis for each item.
   - Both commit/release are atomic Lua scripts and are **idempotent**.

5. **(Optional) CDC or Outbox**
   - **Not required** for the demo. In production you can:
     - Use **Outbox** (PG tx writes both order & event row, worker delivers commit/release), or
     - **CDC** (listen to order status transitions and deliver commit/release).
   - Avoid mirroring Redis counters back into PG to prevent feedback loops.

---

## Safety and integrity: how Lua guarantees correctness

### Atomicity
Each reservation/commit/release is a **single Lua script** executed by Redis. Redis guarantees the script runs **to completion without interleaving** with other commands. This removes race conditions and eliminates the need for distributed locks.

### Invariants enforced
- **No oversell**: Reserve fails if `available < qty`. Scripts never let `available` go negative.
- **Consistent counters**: Reserve always moves units `available → reserved`; release moves them back; commit reduces only `reserved`.
- **TTL-based holds**: Abandoned carts auto-expire; a background sweeper isn’t required for correctness (TTL handles it).

### Idempotency
- **Hold identity** = `cart_id:sku`. Re-issuing the same reserve/commit/release is safe:
  - Reserve: if `hold:{cart_id}:{sku}` already exists, it returns success without double-reserve.
  - Commit/Release: against the same hold can be retried and won’t break counters.

### Durability & HA (platform settings)
- **AOF** (Append Only File) with `everysec` (or `always` for stricter RPO).
- **Replicas + automatic failover** (Redis Enterprise, ElastiCache Multi-AZ, or Sentinel).
- **Sharding**: Redis Cluster—keys `inv:{sku}` and `hold:{cart:sku}` shard naturally.

### Recovery story
- If Redis crashes and restarts, AOF restores keys.
- If you want deterministic rebuilds, you can **replay Postgres orders** (optionally via CDC) into the same idempotent scripts.

---

## Configuration

Edit environment variables or the constants at the top of `app.py`.

| Variable | Default | Purpose |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URI |
| `PG_DSN` | `postgresql://postgres:postgres@localhost:5432/postgres` | Postgres DSN |
| `HOLD_TTL_SECONDS_DEFAULT` | `600` | Default reservation TTL (seconds) |
| `ENABLE_EVENTS_STREAM` | `true` | Toggle Redis Stream event logging |
| `EVENTS_STREAM_NAME` | `inv:events` | Stream name for events |
| `SEED_FROM_DB_ON_STARTUP` | `true` | Seed Redis from PG `inventory` table at startup |
| `FALLBACK_SKU_123`, `FALLBACK_SKU_456` | `10`, `5` | Fallback quantities if DB seed disabled/unavailable |
| `STRICT_UUID` | `true` | Validate/normalize UUIDs at API boundary |

---

## Setup & run

```bash
python -m venv .venv && source .venv/bin/activate
pip install "fastapi>=0.111,<1" "uvicorn[standard]>=0.27,<1" "redis>=5,<6" "psycopg[binary]>=3.1,<4"

# env
export PG_DSN="postgresql://USER:PASS@RDS_HOST:5432/DB?sslmode=require"
export REDIS_URL="redis://localhost:6379/0"

# run
uvicorn app:app --reload
```

### Postgres schema (minimal)
Use the schema from your project (skus, inventory, orders, order_items). Ensure `gen_random_uuid()` is available via `CREATE EXTENSION IF NOT EXISTS "pgcrypto";`.

---

## API quickstart (curl)

Set base URL:
```bash
API="http://127.0.0.1:8000"
```

### 1) Seed inventory
```bash
curl -s -X POST "$API/seed" | jq
```

### 2) Check a SKU
```bash
curl -s "$API/inventory/sku-123" | jq
```

### 3) Reserve (add to cart / start checkout)
```bash
curl -s -X POST "$API/reserve" \
  -H 'content-type: application/json' \
  -d '{"sku":"sku-123","qty":2,"cart_id":"cart-abc","ttl_seconds":120}' | jq
```

### 4) Create order in Postgres (system of record)
```bash
ORDER_ID=$(curl -s -X POST "$API/order" \
  -H 'content-type: application/json' \
  -d '{"cart_id":"cart-abc","items":[{"sku_id":"sku-123","qty":2}]}' | jq -r .order_id)
echo "ORDER_ID=$ORDER_ID"
```

### 5a) Confirm (commit hold → stock is consumed permanently)
```bash
curl -s -X POST "$API/confirm" \
  -H 'content-type: application/json' \
  -d "{\"order_id\":\"$ORDER_ID\"}" | jq
```

### 5b) Cancel (release hold → stock returns to available)
```bash
curl -s -X POST "$API/cancel" \
  -H 'content-type: application/json' \
  -d "{\"order_id\":\"$ORDER_ID\"}" | jq
```

### 6) Inspect events (if enabled)
```bash
curl -s "$API/events?limit=10" | jq
```

---

## Optional: CDC vs Outbox (production hardening)

- **Outbox (simpler ops)**: In the same PG transaction that marks an order `CONFIRMED` or `CANCELLED`, insert an `outbox` row. A tiny worker reads PENDING rows and calls `commit_hold`/`release_hold`. Works with at-least-once delivery because Redis scripts are idempotent.
- **CDC (more infra, strong repair)**: Listen only to **order status** changes (not inventory counters) and deliver side-effects. Avoid feedback loops by never mirroring Redis counters back to PG. CDC can rebuild Redis from PG after outages.

> The demo intentionally **does not require** Outbox/CDC; it’s app-driven. Add one when you want guaranteed delivery across failures.

---

## Troubleshooting

- **500 on `/confirm`**: Ensure the order exists and has items; UUID must be valid. The app casts with `::uuid` and can validate with `STRICT_UUID=true`.
- **`reserve` returns `insufficient`**: Check `available` via `/inventory/{sku}`. Increase seed in PG or fallback seed, then `/seed` again.
- **No events**: Set `ENABLE_EVENTS_STREAM=true` (default). Check stream: `GET /events`.
- **Redis/PG connectivity**: Verify `REDIS_URL` and `PG_DSN`. For RDS, use `?sslmode=require`.

---

## Key design choices (why this is safe)

- **Single-script atomics**: eliminate races and oversell without locks.
- **Idempotency everywhere**: `holdId = cart:sku` + idempotent commit/release.
- **TTL on holds**: abandons auto-clean; no leaks.
- **Separation of concerns**: Redis = real-time availability; Postgres = financial SoR.
- **Easy recovery**: AOF for Redis, and re-playable order status from PG if needed.
