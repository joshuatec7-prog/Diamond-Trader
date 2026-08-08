#!/usr/bin/env bash
set -e

cd /opt/render/project/src

echo "=== DIAMOND TRADER MARKET LEAD WORKER ==="
echo "BTC-EUR"
echo "duur: 24 uur"
echo "interval: 2 seconden"
echo "orders: NEE"
echo "private API: NEE"

exec python3 market_lead_btc_collector_v1_1.py \
  --duration-hours 24 \
  --sample-seconds 2
