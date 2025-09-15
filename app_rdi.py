# app_rdi.py
"""
Reservation Service (Redis + PG via RDI)
- PG is SoR of stock (field TOTAL).
- RDI/CDC writes TOTAL into Redis hash inv:{sku} (field 'total').
- App manages only 'reserved' and 'hold:*' in Redis.
- Available = total - reserved.
- No loops: app NEVER writes 'total'; RDI NEVER touches 'reserved' or 'hold:*'.
- Abandoned cart: lease + reaper (ZSET) releases holds safely (no leaked 'reserved').

Run (recommended):
  uvicorn app_rdi:app --reload

Or:
  python app_rdi.py
"""

import os, time, json, threading
from uuid import UUID
from typing import Dict, List, Optional, Tuple

import redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import dict_row

# --------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------
def env_str(name, default): return os.getenv(name, default)
def env_int(name, default):
    try: return int(os.getenv(name, str(default)))
    except: return default
def env_bool(name, default):
    v = os.getenv(name)
    if v is None: return default
    return v.lower() in ("1","true","t","yes","y","on")

REDIS_URL = env_str("REDIS_URL", "redis://default@rediscloud:13000")
PG_DSN    = env_str("PG_DSN",    "postgresql://postgres:postgres@gabs-rdi-cloud.gsgsagsgasg.us-east-1.rds.amazonaws.com:5432/rdi_demo")

def pg_connect(autocommit=False):
    return psycopg.connect(PG_DSN, row_factory=dict_row, autocommit=autocommit)

# Lease (cart hold) defaults
HOLD_TTL_SECONDS_DEFAULT = env_int("HOLD_TTL_SECONDS_DEFAULT", 600)  # "deadline" for hold, not auto-EXPIRE
REAPER_INTERVAL_SECS     = env_int("REAPER_INTERVAL_SECS", 1)       # how often to sweep expired holds
REAPER_BATCH_LIMIT       = env_int("REAPER_BATCH_LIMIT", 200)       # max holds per sweep

# Observability via Stream do Redis
ENABLE_EVENTS_STREAM = env_bool("ENABLE_EVENTS_STREAM", True)
EVENTS_STREAM_NAME   = env_str("EVENTS_STREAM_NAME", "inv:events")

# UUID strict in APIs (if you add order_id later)
STRICT_UUID = env_bool("STRICT_UUID", True)

# Redis key names/prefixes
INV_PREFIX   = env_str("INV_PREFIX", "inventory:sku_id:")  # Hash per SKU: fields { total, reserved }
HOLD_PREFIX  = env_str("HOLD_PREFIX", "hold:")             # Hold by "{cart_id}:{sku}"
HOLDS_ZSET   = env_str("HOLDS_ZSET", "holds:exp")          # ZSET of holds by expiry (score = expiresAt ms)

# --------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

def INV_KEY(sku: str) -> str:   return f"{INV_PREFIX}{sku}"
def HOLD_KEY(hold: str) -> str: return f"{HOLD_PREFIX}{hold}"
def now_ms() -> int:            return int(time.time() * 1000)
def as_uuid_text(s: str) -> str:
    return s if not STRICT_UUID else str(UUID(s))

STREAM_KEY = EVENTS_STREAM_NAME if ENABLE_EVENTS_STREAM else ""

# --------------------------------------------------------------------
# Lua scripts (ATOMIC)
#   - total comes from RDI (PG) at inv:{sku}.total
#   - reserved is managed here
#   - available = total - reserved
#   - holds are leases: store expiresAt + index in ZSET; DO NOT EXPIRE the hold key
# --------------------------------------------------------------------

# Use Redis TIME inside Lua to avoid clock skew
RESERVE_LUA = r.register_script("""
-- KEYS: inv:{sku}, hold:{holdId}, stream?, zset holds:exp
-- ARGV: holdId, sku, qty, ttlSec
local invKey  = KEYS[1]
local holdKey = KEYS[2]
local stream  = KEYS[3]
local zsetKey = KEYS[4]
local holdId  = ARGV[1]
local sku     = ARGV[2]
local qty     = tonumber(ARGV[3])
local ttlSec  = tonumber(ARGV[4])

-- Idempotency: if already held, OK
if redis.call('EXISTS', holdKey) == 1 then
  local exp = redis.call('HGET', holdKey, 'expiresAt')
  return cjson.encode({ok=true, reason='already_held', expiresAt=exp})
end

-- Read counters
local vals = redis.call('HMGET', invKey, 'total', 'reserved')
local total    = tonumber(vals[1]) or 0
local reserved = tonumber(vals[2]) or 0
local available = total - reserved
if available < qty then
  return cjson.encode({ok=false, reason='insufficient', available=available, total=total, reserved=reserved})
end

-- Time / expiry
local t = redis.call('TIME')  -- {sec, usec}
local now_ms = (t[1] * 1000) + math.floor(t[2] / 1000)
local exp_ms = now_ms + (ttlSec * 1000)

-- Apply reservation
redis.call('HINCRBY', invKey, 'reserved', qty)
redis.call('HSET', holdKey,
  'holdId', holdId,
  'sku', sku,
  'qty', qty,
  'createdAt', tostring(now_ms),
  'expiresAt', tostring(exp_ms)
)
redis.call('ZADD', zsetKey, exp_ms, holdId)

-- Events
if stream and #stream > 0 then
  redis.call('XADD', stream, '*',
    'type','hold_created','holdId',holdId,'sku',sku,'qty',tostring(qty),'at',tostring(now_ms),'expiresAt',tostring(exp_ms))
end

return cjson.encode({ok=true, reason='ok', expiresAt=tostring(exp_ms)})
""")

COMMIT_LUA = r.register_script("""
-- KEYS: inv:{sku}, hold:{holdId}, stream?, zset holds:exp
-- ARGV: holdId, sku
local invKey  = KEYS[1]
local holdKey = KEYS[2]
local stream  = KEYS[3]
local zsetKey = KEYS[4]
local holdId  = ARGV[1]
local sku     = ARGV[2]

if redis.call('EXISTS', holdKey) == 0 then
  return cjson.encode({ok=false, reason='no_hold'})
end

local qty = tonumber(redis.call('HGET', holdKey, 'qty')) or 0
redis.call('HINCRBY', invKey, 'reserved', -qty)
redis.call('DEL', holdKey)
redis.call('ZREM', zsetKey, holdId)

if stream and #stream > 0 then
  local t = redis.call('TIME')
  local now_ms = (t[1]*1000) + math.floor(t[2]/1000)
  redis.call('XADD', stream, '*',
    'type','hold_committed','holdId',holdId,'sku',sku,'qty',tostring(qty),'at',tostring(now_ms))
end

return cjson.encode({ok=true, qty=qty})
""")

RELEASE_LUA = r.register_script("""
-- KEYS: inv:{sku}, hold:{holdId}, stream?, zset holds:exp
-- ARGV: holdId, sku
local invKey  = KEYS[1]
local holdKey = KEYS[2]
local stream  = KEYS[3]
local zsetKey = KEYS[4]
local holdId  = ARGV[1]
local sku     = ARGV[2]

if redis.call('EXISTS', holdKey) == 0 then
  -- idempotent: ensure index is gone
  redis.call('ZREM', zsetKey, holdId)
  return cjson.encode({ok=false, reason='no_hold'})
end

local qty = tonumber(redis.call('HGET', holdKey, 'qty')) or 0
redis.call('HINCRBY', invKey, 'reserved', -qty)
redis.call('DEL', holdKey)
redis.call('ZREM', zsetKey, holdId)

if stream and #stream > 0 then
  local t = redis.call('TIME')
  local now_ms = (t[1]*1000) + math.floor(t[2]/1000)
  redis.call('XADD', stream, '*',
    'type','hold_released','holdId',holdId,'sku',sku,'qty',tostring(qty),'at',tostring(now_ms))
end

return cjson.encode({ok=true, qty=qty})
""")

EXTEND_LUA = r.register_script("""
-- KEYS: hold:{holdId}, zset holds:exp
-- ARGV: holdId, addTtlSec
local holdKey = KEYS[1]
local zsetKey = KEYS[2]
local holdId  = ARGV[1]
local addSec  = tonumber(ARGV[2])

if redis.call('EXISTS', holdKey) == 0 then
  return cjson.encode({ok=false, reason='no_hold'})
end

local t = redis.call('TIME')
local now_ms = (t[1]*1000) + math.floor(t[2]/1000)
local cur = tonumber(redis.call('HGET', holdKey, 'expiresAt')) or now_ms
local newExp = math.max(cur, now_ms) + (addSec*1000)
redis.call('HSET', holdKey, 'expiresAt', tostring(newExp))
redis.call('ZADD', zsetKey, newExp, holdId)

return cjson.encode({ok=true, expiresAt=tostring(newExp)})
""")

# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def ensure_reserved_field(sku: str):
    """Ensure 'reserved' exists so HMGET doesn't return null → cast error."""
    if not r.hexists(INV_KEY(sku), "reserved"):
        r.hset(INV_KEY(sku), mapping={"reserved": 0})

def snapshot(sku: str) -> Dict[str, int]:
    h = r.hgetall(INV_KEY(sku))
    total    = int(h.get("total", 0))
    reserved = int(h.get("reserved", 0))
    return {"total": total, "reserved": reserved, "available": total - reserved}

def parse_sku_from_hold_id(hold_id: str) -> Optional[str]:
    # If your cart_id can contain ':', store 'sku' in hold hash instead of parsing.
    if ":" in hold_id:
        return hold_id.split(":", 1)[1]
    h = r.hgetall(HOLD_KEY(hold_id))
    return h.get("sku")

def get_hold_qty(sku: str, cart_id: str) -> int:
    hold_id = f"{cart_id}:{sku}"
    qty_s = r.hget(HOLD_KEY(hold_id), "qty")
    if qty_s is None: return 0
    try: return int(qty_s)
    except: return 0

def decrement_total_pg(sku: str, qty: int) -> bool:
    """Atomically subtract qty from inventory.total iff total >= qty."""
    with pg_connect() as conn, conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute(
            "UPDATE inventory SET total = total - %s "
            "WHERE sku_id = %s AND total >= %s "
            "RETURNING total",
            (qty, sku, qty),
        )
        row = cur.fetchone()
        if not row:
            cur.execute("ROLLBACK")
            return False
        cur.execute("COMMIT")
        return True

def reserve(sku: str, qty: int, cart_id: str, ttl: Optional[int]) -> Tuple[bool, str, str, Dict[str,int], Optional[int]]:
    ensure_reserved_field(sku)
    hold_id = f"{cart_id}:{sku}"
    ttl_sec = ttl if (ttl and ttl > 0) else HOLD_TTL_SECONDS_DEFAULT
    res = RESERVE_LUA(
        keys=[INV_KEY(sku), HOLD_KEY(hold_id), STREAM_KEY, HOLDS_ZSET],
        args=[hold_id, sku, str(qty), str(ttl_sec)],
    )
    payload = json.loads(res)
    return bool(payload.get("ok")), payload.get("reason",""), hold_id, snapshot(sku), int(payload.get("expiresAt")) if payload.get("expiresAt") else None

def commit_hold_redis(sku: str, hold_id: str) -> Dict:
    ensure_reserved_field(sku)
    res = COMMIT_LUA(
        keys=[INV_KEY(sku), HOLD_KEY(hold_id), STREAM_KEY, HOLDS_ZSET],
        args=[hold_id, sku],
    )
    out = json.loads(res)
    out["inventory"] = snapshot(sku)
    return out

def release_hold_redis(sku: str, hold_id: str) -> Dict:
    ensure_reserved_field(sku)
    res = RELEASE_LUA(
        keys=[INV_KEY(sku), HOLD_KEY(hold_id), STREAM_KEY, HOLDS_ZSET],
        args=[hold_id, sku],
    )
    out = json.loads(res)
    out["inventory"] = snapshot(sku)
    return out

def extend_hold(cart_id: str, sku: str, add_seconds: int) -> Dict:
    hold_id = f"{cart_id}:{sku}"
    res = EXTEND_LUA(
        keys=[HOLD_KEY(hold_id), HOLDS_ZSET],
        args=[hold_id, str(add_seconds)],
    )
    return json.loads(res)

# ---------------- Reaper (expired holds) ----------------
def sweep_expired_once(limit=REAPER_BATCH_LIMIT):
    now = now_ms()
    due = r.zrangebyscore(HOLDS_ZSET, min='-inf', max=now, start=0, num=limit)
    for hold_id in due:
        sku = parse_sku_from_hold_id(hold_id)
        if not sku:
            # No SKU info; drop index to avoid infinite retry
            r.zrem(HOLDS_ZSET, hold_id)
            continue
        # Release via the same atomic Lua (idempotent).
        RELEASE_LUA(keys=[INV_KEY(sku), HOLD_KEY(hold_id), STREAM_KEY, HOLDS_ZSET],
                    args=[hold_id, sku])
        # Ensure index removed (Lua already does ZREM, but be safe)
        r.zrem(HOLDS_ZSET, hold_id)

stop_evt = threading.Event()
_reaper_thr: Optional[threading.Thread] = None

def reaper_loop():
    while not stop_evt.is_set():
        try:
            sweep_expired_once()
        except Exception:
            pass
        stop_evt.wait(REAPER_INTERVAL_SECS)

# --------------------------------------------------------------------
# API
# --------------------------------------------------------------------
class ReserveIn(BaseModel):
    sku: str
    qty: int
    cart_id: str
    ttl_seconds: Optional[int] = None  # lease deadline (seconds)

class HoldIn(BaseModel):
    cart_id: str
    sku: str

class ExtendIn(BaseModel):
    cart_id: str
    sku: str
    add_seconds: int = 60

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start reaper on startup; stop on shutdown
    global _reaper_thr
    _reaper_thr = threading.Thread(target=reaper_loop, daemon=True)
    _reaper_thr.start()
    try:
        yield
    finally:
        stop_evt.set()
        if _reaper_thr:
            _reaper_thr.join(timeout=2.0)

app = FastAPI(title="Reservation Service (Redis + PG via RDI)", lifespan=lifespan)

@app.get("/snapshot/{sku}")
def api_snapshot(sku: str):
    return {"sku": sku, "counters": snapshot(sku)}

@app.post("/reserve")
def api_reserve(body: ReserveIn):
    ok, reason, hold_id, inv, exp = reserve(body.sku, body.qty, body.cart_id, body.ttl_seconds)
    return {"ok": ok, "reason": reason, "hold_id": hold_id, "expiresAt": exp, "inventory": inv}

@app.post("/commit")
def api_commit(body: HoldIn):
    sku = body.sku
    cart_id = body.cart_id
    hold_id = f"{cart_id}:{sku}"

    # 1) Read hold qty
    qty = get_hold_qty(sku, cart_id)
    if qty <= 0:
        raise HTTPException(404, detail="hold not found or qty invalid")

    # 2) Decrement PG SoR first; RDI will propagate 'total' to Redis
    ok_pg = decrement_total_pg(sku, qty)
    if not ok_pg:
        # Another channel consumed stock in PG; free the hold and signal conflict
        rel = release_hold_redis(sku, hold_id)
        raise HTTPException(
            status_code=409,
            detail={
                "error": "insufficient_total_in_pg",
                "message": "PG total is insufficient at commit time; hold released.",
                "release": rel,
            },
        )

    # 3) Commit hold in Redis (reserved -= qty; DEL hold)
    res = commit_hold_redis(sku, hold_id)
    return {"ok": True, "committed": res}

@app.post("/release")
def api_release(body: HoldIn):
    hold_id = f"{body.cart_id}:{body.sku}"
    return release_hold_redis(body.sku, hold_id)

@app.post("/extend")
def api_extend(body: ExtendIn):
    out = extend_hold(body.cart_id, body.sku, body.add_seconds)
    if not out.get("ok"):
        raise HTTPException(404, detail=out)
    return out

@app.get("/events")
def api_events(limit: int = 20):
    if not ENABLE_EVENTS_STREAM:
        return []
    entries = r.xrevrange(STREAM_KEY, count=limit)
    out = []
    for eid, f in entries:
        data = dict(zip(f[::2], f[1::2])) if isinstance(f, list) else f
        out.append({"id": eid, "data": data})
    return out

if __name__ == "__main__":
    import uvicorn
    # Module-agnostic runner to avoid import-string mismatches
    uvicorn.run(app, host="127.0.0.1", port=8000)