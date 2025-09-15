#!/usr/bin/env bash
set -euo pipefail

API="${API:-http://127.0.0.1:8000}"
SKU="${SKU:-sku-123}"
CART="${CART:-cart-abandon}"
QTY="${QTY:-1}"
TTL="${TTL:-3}"  # short lease to trigger reaper

echo "== Reserve $QTY of $SKU for $CART (short ttl=${TTL}s)"
curl -s -X POST "$API/reserve" -H 'content-type: application/json' \
  -d "{\"sku\":\"$SKU\",\"qty\":$QTY,\"cart_id\":\"$CART\",\"ttl_seconds\":$TTL}" | jq

echo "== Snapshot immediately"
curl -s "$API/snapshot/$SKU" | jq

echo "== Wait for reaper to sweep expired hold ..."
sleep $(( TTL + 3 ))

echo "== Snapshot after reaper"
curl -s "$API/snapshot/$SKU" | jq

echo "== Events"
curl -s "$API/events?limit=15" | jq
