#!/bin/bash
# Start the PureBrain Portal Server
# Usage: ./start.sh [port]
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${1:-8080}"

echo "[portal] Starting portal on port $PORT..."
exec python3 "$DIR/portal_server.py"
