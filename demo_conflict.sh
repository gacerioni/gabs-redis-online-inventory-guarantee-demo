#!/usr/bin/env bash
set -euo pipefail

# Simulate a conflict: another channel reduces PG total before commit.
# Requires psql and PG_DSN environment variable.

API="${API:-http://127.0.0.1:8000}"
PG_DSN="${PG_DSN:?PG_DSN must be set, e.g. export PG_DSN='postgresql://user:pass@host:5432/db?sslmode=require'}"

SKU="${SKU:-sku-123}"
CART="${CART:-cart-conflict}"
QTY="${QTY:-2}"
TTL="${TTL:-120}"

echo "== Reserve $QTY of $SKU for $CART"
curl -s -X POST "$API/reserve" -H 'content-type: application/json' \
  -d "{\"sku\":\"$SKU\",\"qty\":$QTY,\"cart_id\":\"$CART\",\"ttl_seconds\":$TTL}" | jq

echo "== Simulate external consumption in PG (total -= $QTY)"
psql "$PG_DSN" -v ON_ERROR_STOP=1 -c "UPDATE inventory SET total = GREATEST(total - $QTY, 0) WHERE sku_id = '$SKU';"

echo "== Try to commit (should 409 and auto-release hold)"
curl -i -s -X POST "$API/commit" -H 'content-type: application/json' \
  -d "{\"cart_id\":\"$CART\",\"sku\":\"$SKU\"}"

echo
echo "== Snapshot after conflict"
curl -s "$API/snapshot/$SKU" | jq

echo "== Events"
curl -s "$API/events?limit=15" | jq
