#!/bin/bash
# Start the PureBrain Portal Server — serves the REACT portal (and only React).
# Legacy HTML frontends were retired 2026-07-08; deploy path is react-portal/dist/.
# Usage: ./start.sh [port]
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${1:-8080}"

# Deploy-path guard: the React build MUST be present — it is the only frontend.
REACT_INDEX="$DIR/react-portal/dist/index.html"
if [[ ! -f "$REACT_INDEX" ]]; then
    echo "[portal] FATAL: React build not found at $REACT_INDEX" >&2
    echo "[portal] The React portal is the only deployed frontend. Ensure react-portal/dist/ is present" >&2
    echo "[portal] (build it with 'cd react-portal && npm install && npm run build', or pull a commit that includes dist/)." >&2
    exit 1
fi

echo "[portal] Starting portal (React frontend) on port $PORT..."
exec python3 "$DIR/portal_server.py"
