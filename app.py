# app.py
"""
Lock-free Online Inventory (FastAPI demo, no CDC)
- Atomic reserve/commit/release in Redis via Lua
- Holds keyed by (cart_id:sku) with TTL
- App-driven commit/release around DB writes
- Minimal REST endpoints to simulate a shop flow

Run:
  export PG_DSN="postgresql://user:pass@rds-host:5432/db?sslmode=require"
  export REDIS_URL="redis://localhost:6379/0"
  uvicorn app:app --reload
"""

import os
import time
import json
from uuid import UUID
from typing import Dict, List, Tuple, Optional

import redis
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException
from fastapi import Response
from pydantic import BaseModel
from contextlib import asynccontextmanager

# ============================================================================
# CONFIG PANEL (tweak here or via environment variables)
# ============================================================================
def env_str(name: str, default: str) -> str:
    return os.getenv(name, default)

def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "t", "yes", "y", "on")

# Connections
REDIS_URL = env_str("REDIS_URL", "redis://localhost:6379/0")
PG_DSN    = env_str("PG_DSN",    "postgresql://postgres:Secret_42@gabs-rdi-cloud.c7glrswbovia.us-east-1.rds.amazonaws.com:5432/rdi_demo")

# Behavior
HOLD_TTL_SECONDS_DEFAULT = env_int("HOLD_TTL_SECONDS_DEFAULT", 600)   # default hold TTL
ENABLE_EVENTS_STREAM     = env_bool("ENABLE_EVENTS_STREAM", True)     # toggle Redis Stream logging
EVENTS_STREAM_NAME       = env_str("EVENTS_STREAM_NAME", "inv:events")

# Startup seeding
SEED_FROM_DB_ON_STARTUP  = env_bool("SEED_FROM_DB_ON_STARTUP", True)
# If DB seed fails or disabled, fallback seed (sku -> qty)
FALLBACK_SEED            = {
    "sku-123": env_int("FALLBACK_SKU_123", 10),
    "sku-456": env_int("FALLBACK_SKU_456", 5),
}

# Safety
STRICT_UUID              = env_bool("STRICT_UUID", True)  # validate/normalize UUIDs at API boundary
# ============================================================================

# Redis connection (decode_responses=True for str values)
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

def pg_connect():
    # autocommit for simple demo; wrap in explicit tx if needed
    return psycopg.connect(PG_DSN, row_factory=dict_row, autocommit=True)

# Redis key helpers
def INV_KEY(sku: str) -> str:        # SKU inventory hash (fields: available, reserved)
    return f"inv:{sku}"

def HOLD_KEY(hold_id: str) -> str:   # per-hold hash (fields: holdId, sku, qty, createdAt)
    return f"hold:{hold_id}"

def now_ms() -> str:
    return str(int(time.time() * 1000))

def as_uuid_text(s: str) -> str:
    if not STRICT_UUID:
        return s
    return str(UUID(s))  # raises if invalid -> 422 via FastAPI handler

# ----------------------------------------------------------------------------
# Lua scripts (atomic)
# ----------------------------------------------------------------------------
STREAM_KEY = EVENTS_STREAM_NAME if ENABLE_EVENTS_STREAM else ""

RESERVE_LUA = r.register_script("""
-- Atomic RESERVE for a SKU
-- KEYS[1] = inv:{sku}
-- KEYS[2] = hold:{holdId}
-- KEYS[3] = events stream (optional)
-- ARGV[1] = holdId
-- ARGV[2] = sku
-- ARGV[3] = qty
-- ARGV[4] = ttlSeconds
-- ARGV[5] = nowMillis
local invKey   = KEYS[1]
local holdKey  = KEYS[2]
local stream   = KEYS[3]
local holdId   = ARGV[1]
local sku      = ARGV[2]
local qty      = tonumber(ARGV[3])
local ttl      = tonumber(ARGV[4])
local now      = ARGV[5]

-- (A) Idempotency: if the hold exists, treat as success
if redis.call('EXISTS', holdKey) == 1 then
  return cjson.encode({ok=true, reason='already_held'})
end

-- (B) Read counters
local vals = redis.call('HMGET', invKey, 'available', 'reserved')
local available = tonumber(vals[1]) or 0
local reserved  = tonumber(vals[2]) or 0

-- (C) Check stock
if available < qty then
  return cjson.encode({ok=false, reason='insufficient', available=available})
end

-- (D) Apply counters
redis.call('HINCRBY', invKey, 'available', -qty)
redis.call('HINCRBY', invKey, 'reserved',   qty)

-- (E) Record hold
redis.call('HSET', holdKey, 'holdId', holdId, 'sku', sku, 'qty', qty, 'createdAt', now)
redis.call('EXPIRE', holdKey, ttl)

-- (F) Event (optional)
if stream and #stream > 0 then
  redis.call('XADD', stream, '*',
    'type', 'hold_created',
    'holdId', holdId,
    'sku', sku,
    'qty', tostring(qty),
    'at', now
  )
end

return cjson.encode({ok=true, reason='ok'})
""")

COMMIT_LUA = r.register_script("""
-- Commit a hold -> decrease reserved, delete the hold
-- KEYS[1] = inv:{sku}
-- KEYS[2] = hold:{holdId}
-- KEYS[3] = events stream
-- ARGV[1] = holdId
-- ARGV[2] = sku
-- ARGV[3] = nowMillis
local invKey   = KEYS[1]
local holdKey  = KEYS[2]
local stream   = KEYS[3]
local holdId   = ARGV[1]
local sku      = ARGV[2]
local now      = ARGV[3]

if redis.call('EXISTS', holdKey) == 0 then
  return cjson.encode({ok=false, reason='no_hold'})
end

local qty = tonumber(redis.call('HGET', holdKey, 'qty')) or 0

redis.call('HINCRBY', invKey, 'reserved', -qty)
redis.call('DEL', holdKey)

if stream and #stream > 0 then
  redis.call('XADD', stream, '*',
    'type', 'hold_committed',
    'holdId', holdId,
    'sku', sku,
    'qty', tostring(qty),
    'at', now
  )
end

return cjson.encode({ok=true, qty=qty})
""")

RELEASE_LUA = r.register_script("""
-- Release a hold -> increase available, decrease reserved, delete the hold
-- KEYS[1] = inv:{sku}
-- KEYS[2] = hold:{holdId}
-- KEYS[3] = events stream
-- ARGV[1] = holdId
-- ARGV[2] = sku
-- ARGV[3] = nowMillis
local invKey   = KEYS[1]
local holdKey  = KEYS[2]
local stream   = KEYS[3]
local holdId   = ARGV[1]
local sku      = ARGV[2]
local now      = ARGV[3]

if redis.call('EXISTS', holdKey) == 0 then
  return cjson.encode({ok=false, reason='no_hold'})
end

local qty = tonumber(redis.call('HGET', holdKey, 'qty')) or 0

redis.call('HINCRBY', invKey, 'available',  qty)
redis.call('HINCRBY', invKey, 'reserved',  -qty)
redis.call('DEL', holdKey)

if stream and #stream > 0 then
  redis.call('XADD', stream, '*',
    'type', 'hold_released',
    'holdId', holdId,
    'sku', sku,
    'qty', tostring(qty),
    'at', now
  )
end

return cjson.encode({ok=true, qty=qty})
""")

# ----------------------------------------------------------------------------
# Inventory helpers
# ----------------------------------------------------------------------------
def seed_inventory_from_db() -> Dict[str, Dict[str, int]]:
    """Copy Postgres inventory.initial_qty into Redis counters."""
    with pg_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT sku_id, initial_qty FROM inventory")
        rows = cur.fetchall()

    pipe = r.pipeline()
    for row in rows:
        inv_key = INV_KEY(row["sku_id"])
        pipe.hset(inv_key, mapping={"available": row["initial_qty"], "reserved": 0})
    pipe.execute()

    return {row["sku_id"]: get_inv(row["sku_id"]) for row in rows}

def seed_inventory_fallback() -> Dict[str, Dict[str, int]]:
    """Fallback canned seed if DB seeding is not desired/available."""
    pipe = r.pipeline()
    for sku, qty in FALLBACK_SEED.items():
        pipe.hset(INV_KEY(sku), mapping={"available": qty, "reserved": 0})
    pipe.execute()
    return {sku: get_inv(sku) for sku in FALLBACK_SEED}

def get_inv(sku: str) -> Dict[str, int]:
    h = r.hgetall(INV_KEY(sku))
    return {"available": int(h.get("available", 0)), "reserved": int(h.get("reserved", 0))}

def reserve(sku: str, qty: int, cart_id: str, ttl_seconds: Optional[int]) -> Tuple[bool, str, str]:
    ttl = ttl_seconds if ttl_seconds and ttl_seconds > 0 else HOLD_TTL_SECONDS_DEFAULT
    hold_id = f"{cart_id}:{sku}"  # idempotent
    res = RESERVE_LUA(
        keys=[INV_KEY(sku), HOLD_KEY(hold_id), STREAM_KEY],
        args=[hold_id, sku, str(qty), str(ttl), now_ms()],
    )
    payload = json.loads(res)
    return bool(payload.get("ok")), payload.get("reason", ""), hold_id

def commit_hold(sku: str, hold_id: str) -> Dict:
    res = COMMIT_LUA(
        keys=[INV_KEY(sku), HOLD_KEY(hold_id), STREAM_KEY],
        args=[hold_id, sku, now_ms()],
    )
    return json.loads(res)

def release_hold(sku: str, hold_id: str) -> Dict:
    res = RELEASE_LUA(
        keys=[INV_KEY(sku), HOLD_KEY(hold_id), STREAM_KEY],
        args=[hold_id, sku, now_ms()],
    )
    return json.loads(res)

# ----------------------------------------------------------------------------
# DB helpers (orders)
# ----------------------------------------------------------------------------
def create_order(cart_id: str, items: List[Tuple[str, int]]) -> str:
    """Creates a PENDING order + items; returns order_id (uuid text)."""
    with pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO orders (cart_id, status) VALUES (%s, 'PENDING') RETURNING id",
            (cart_id,),
        )
        order_id = str(cur.fetchone()["id"])
        for sku_id, qty in items:
            cur.execute(
                "INSERT INTO order_items (order_id, sku_id, qty) VALUES (%s::uuid, %s, %s)",
                (order_id, sku_id, qty),
            )
        return order_id

def set_order_status(order_id: str, status: str):
    with pg_connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE orders SET status = %s WHERE id = %s::uuid", (status, order_id))

def get_order_items(order_id: str) -> List[Dict]:
    with pg_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT sku_id, qty FROM order_items WHERE order_id = %s::uuid", (order_id,))
        return cur.fetchall()

def get_order_cart_id(order_id: str) -> Optional[str]:
    with pg_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT cart_id FROM orders WHERE id = %s::uuid", (order_id,))
        row = cur.fetchone()
        return row["cart_id"] if row else None

# ----------------------------------------------------------------------------
# API models
# ----------------------------------------------------------------------------
class ReserveIn(BaseModel):
    sku: str
    qty: int
    cart_id: str
    ttl_seconds: Optional[int] = None  # falls back to HOLD_TTL_SECONDS_DEFAULT

class OrderIn(BaseModel):
    cart_id: str
    items: List[Dict]  # [{sku_id, qty}]

class OrderIdIn(BaseModel):
    order_id: str

# ----------------------------------------------------------------------------
# FastAPI app (using lifespan instead of deprecated on_event)
# ----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    try:
        seeded = seed_inventory_from_db() if SEED_FROM_DB_ON_STARTUP else seed_inventory_fallback()
        print("[startup] Seeded Redis:", seeded)
    except Exception as e:
        print("[startup] DB seed failed; using fallback:", e)
        seeded = seed_inventory_fallback()
        print("[startup] Fallback seed:", seeded)
    yield
    # Shutdown (no-op)

app = FastAPI(title="Online Inventory Demo (Redis + Postgres, No CDC)", lifespan=lifespan)

# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.post("/seed")
def seed():
    """Rerun DB -> Redis inventory seeding (or fallback on error)."""
    try:
        return {"source": "db", "seeded": seed_inventory_from_db()}
    except Exception as e:
        return {"source": "fallback", "seeded": seed_inventory_fallback(), "note": str(e)}

@app.get("/inventory/{sku}")
def inventory(sku: str):
    return {"sku": sku, "counters": get_inv(sku)}

@app.get("/events")
def events(limit: int = 20):
    if not ENABLE_EVENTS_STREAM:
        return []
    entries = r.xrevrange(STREAM_KEY, count=limit)
    out = []
    for eid, fields in entries:
        if isinstance(fields, list):
            data = dict(zip(fields[::2], fields[1::2]))
        else:
            data = fields
        out.append({"id": eid, "data": data})
    return out

@app.post("/reserve")
def api_reserve(body: ReserveIn):
    ok, reason, hold_id = reserve(body.sku, body.qty, body.cart_id, body.ttl_seconds)
    return {"ok": ok, "reason": reason, "hold_id": hold_id, "inventory": get_inv(body.sku)}

@app.post("/order")
def api_order(body: OrderIn):
    items = [(it["sku_id"], it["qty"]) for it in body.items]
    order_id = create_order(body.cart_id, items)
    return {"order_id": order_id, "status": "PENDING"}

@app.post("/confirm")
def api_confirm(body: OrderIdIn):
    oid = as_uuid_text(body.order_id)
    items = get_order_items(oid)
    if not items:
        raise HTTPException(404, "order not found or has no items")
    set_order_status(oid, "CONFIRMED")
    cart_id = get_order_cart_id(oid)
    if not cart_id:
        raise HTTPException(404, "order not found")
    results = []
    for it in items:
        sku = it["sku_id"]
        hold_id = f"{cart_id}:{sku}"
        results.append({"sku": sku, "hold": hold_id, "commit": commit_hold(sku, hold_id)})
    return {"order_id": oid, "status": "CONFIRMED", "results": results}

@app.post("/cancel")
def api_cancel(body: OrderIdIn):
    oid = as_uuid_text(body.order_id)
    items = get_order_items(oid)
    if not items:
        raise HTTPException(404, "order not found or has no items")
    set_order_status(oid, "CANCELLED")
    cart_id = get_order_cart_id(oid)
    if not cart_id:
        raise HTTPException(404, "order not found")
    results = []
    for it in items:
        sku = it["sku_id"]
        hold_id = f"{cart_id}:{sku}"
        results.append({"sku": sku, "hold": hold_id, "release": release_hold(sku, hold_id)})
    return {"order_id": oid, "status": "CANCELLED", "results": results}

# Local dev entrypoint
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)