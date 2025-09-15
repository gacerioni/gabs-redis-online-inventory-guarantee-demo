#!/usr/bin/env bash
set -euo pipefail

API="${API:-http://127.0.0.1:8000}"
SKU="${SKU:-sku-123}"
TTL="${TTL:-60}"

echo "== Snapshot (start)"
curl -s "$API/snapshot/$SKU" | jq

echo "== Reserve from multiple carts in parallel"
for i in {1..5}; do
  CART="cart-$i"
  QTY=$(( (RANDOM % 2) + 1 )) # 1 or 2
  echo " -> $CART reserves $QTY"
  curl -s -X POST "$API/reserve" -H 'content-type: application/json' \
    -d "{\"sku\":\"$SKU\",\"qty\":$QTY,\"cart_id\":\"$CART\",\"ttl_seconds\":$TTL}" | jq -c . &
done
wait

echo "== Snapshot after reserves"
curl -s "$API/snapshot/$SKU" | jq

echo "== Commit a couple, release others"
for i in 1 3; do
  CART="cart-$i"
  echo " -> commit $CART"
  curl -s -X POST "$API/commit" -H 'content-type: application/json' \
    -d "{\"cart_id\":\"$CART\",\"sku\":\"$SKU\"}" | jq -c . || true
done

for i in 2 4 5; do
  CART="cart-$i"
  echo " -> release $CART"
  curl -s -X POST "$API/release" -H 'content-type: application/json' \
    -d "{\"cart_id\":\"$CART\",\"sku\":\"$SKU\"}" | jq -c . || true
done

echo "== Snapshot (end)"
curl -s "$API/snapshot/$SKU" | jq

#echo "== Events"
#curl -s "$API/events?limit=30" | jq
