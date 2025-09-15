#!/usr/bin/env bash
set -euo pipefail

API="${API:-http://127.0.0.1:8000}"
SKU="${SKU:-sku-123}"
CART="${CART:-cart-cancel}"
QTY="${QTY:-3}"
TTL="${TTL:-90}"

echo "== Reserve $QTY of $SKU for $CART (ttl=${TTL}s)"
curl -s -X POST "$API/reserve" -H 'content-type: application/json' \
  -d "{\"sku\":\"$SKU\",\"qty\":$QTY,\"cart_id\":\"$CART\",\"ttl_seconds\":$TTL}" | jq

sleep 5

echo "== Release (cancel flow)"
curl -s -X POST "$API/release" -H 'content-type: application/json' \
  -d "{\"cart_id\":\"$CART\",\"sku\":\"$SKU\"}" | jq

echo "== Snapshot"
curl -s "$API/snapshot/$SKU" | jq

echo "== Events"
curl -s "$API/events?limit=15" | jq
