#!/bin/bash
set -e

PROJECT_DIR="/opt/mqttPython"
PYTHON_BIN="$PROJECT_DIR/venv/bin/python"

# On attend que le réseau soit prêt
for _ in {1..60}; do
  IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
  [ -n "$IP" ] && break
  sleep 1
done

[ -z "$IP" ] && IP="No IP"

"$PYTHON_BIN" "$PROJECT_DIR/ip/lcd_write.py" "IP address:" "$IP"
