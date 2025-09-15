#!/bin/bash
# demo.sh - Example curl commands for Online Inventory Reservation Demo
# Usage:
#   chmod +x demo.sh
#   ./demo.sh
#
# Requires: curl, jq

API="http://127.0.0.1:8000"

echo "==> Seeding inventory"
curl -s -X POST "$API/seed" | jq

echo "==> Checking sku-123 inventory"
curl -s "$API/inventory/sku-123" | jq

echo "==> Reserve 2 units of sku-123 for cart-abc"
curl -s -X POST "$API/reserve" \
  -H 'content-type: application/json' \
  -d '{"sku":"sku-123","qty":2,"cart_id":"cart-abc","ttl_seconds":120}' | jq

echo "==> Create order for cart-abc"
ORDER_ID=$(curl -s -X POST "$API/order" \
  -H 'content-type: application/json' \
  -d '{"cart_id":"cart-abc","items":[{"sku_id":"sku-123","qty":2}]}' | jq -r .order_id)
echo "ORDER_ID=$ORDER_ID"

echo "==> Confirm the order (commit hold)"
curl -s -X POST "$API/confirm" \
  -H 'content-type: application/json' \
  -d "{\"order_id\":\"$ORDER_ID\"}" | jq

echo "==> Check inventory after commit"
curl -s "$API/inventory/sku-123" | jq

echo "==> Reserve 3 units of sku-456 for cart-cancel"
curl -s -X POST "$API/reserve" \
  -H 'content-type: application/json' \
  -d '{"sku":"sku-456","qty":3,"cart_id":"cart-cancel","ttl_seconds":90}' | jq

echo "==> Create order for cart-cancel"
ORDER_ID2=$(curl -s -X POST "$API/order" \
  -H 'content-type: application/json' \
  -d '{"cart_id":"cart-cancel","items":[{"sku_id":"sku-456","qty":3}]}' | jq -r .order_id)
echo "ORDER_ID2=$ORDER_ID2"

echo "==> Cancel the order (release hold)"
curl -s -X POST "$API/cancel" \
  -H 'content-type: application/json' \
  -d "{\"order_id\":\"$ORDER_ID2\"}" | jq

echo "==> Check inventory after cancel"
curl -s "$API/inventory/sku-456" | jq

echo "==> Recent events"
curl -s "$API/events?limit=10" | jq
