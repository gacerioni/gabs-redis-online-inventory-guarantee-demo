#!/usr/bin/env bash
set -euo pipefail

API="${API:-http://127.0.0.1:8000}"
SKU="${SKU:-sku-123}"
CART="${CART:-cart-happy}"
QTY="${QTY:-3}"
TTL="${TTL:-120}"  # seconds

echo "== Snapshot (before)"
curl -s "$API/snapshot/$SKU" | jq

echo "== Reserve $QTY of $SKU for $CART (ttl=${TTL}s)"
curl -s -X POST "$API/reserve" -H 'content-type: application/json' \
  -d "{\"sku\":\"$SKU\",\"qty\":$QTY,\"cart_id\":\"$CART\",\"ttl_seconds\":$TTL}" | jq

echo "== Optional extend by +60s"
curl -s -X POST "$API/extend" -H 'content-type: application/json' \
  -d "{\"cart_id\":\"$CART\",\"sku\":\"$SKU\",\"add_seconds\":60}" | jq

echo "== Commit (PG-first, then Redis hold commit)"
curl -s -X POST "$API/commit" -H 'content-type: application/json' \
  -d "{\"cart_id\":\"$CART\",\"sku\":\"$SKU\"}" | jq

echo "== Snapshot (after)"
curl -s "$API/snapshot/$SKU" | jq

echo "== Recent events"
curl -s "$API/events?limit=15" | jq
