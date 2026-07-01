#!/bin/bash
# Graceful portal restart — minimizes downtime
# Detects supervisor (tmux → systemd → nohup fallback) to avoid orphaning.

DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=8097

# Detect configured port from .env
if [[ -f "${DIR}/.env" ]]; then
    ENV_PORT=$(grep -m1 -oP '^PORT\s*=\s*\K[0-9]+' "${DIR}/.env" 2>/dev/null || true)
    [[ -n "$ENV_PORT" ]] && PORT="$ENV_PORT"
fi

OLD_PID=$(pgrep -f "python3.*portal_server.py" | head -1)
echo "[restart] Old PID: ${OLD_PID:-none}"

_restart_done=false

# Priority 1: tmux session — restart inside tmux so it stays supervised
if tmux has-session -t portal-server 2>/dev/null; then
    echo "[restart] Detected tmux session 'portal-server' — restarting inside tmux"
    tmux send-keys -t portal-server C-c '' 2>/dev/null || true
    sleep 2
    tmux send-keys -t portal-server "cd ${DIR} && python3 portal_server.py" Enter
    _restart_done=true

# Priority 2: systemd service — use systemctl
elif systemctl is-active --quiet portal-server 2>/dev/null; then
    echo "[restart] Detected systemd service 'portal-server' — restarting via systemctl"
    systemctl restart portal-server
    _restart_done=true
fi

# Priority 3: Fallback — kill + nohup (original behavior)
if ! $_restart_done; then
    if [ -n "$OLD_PID" ]; then
        kill "$OLD_PID" 2>/dev/null

        # Wait up to 5 seconds for clean exit
        for i in $(seq 1 10); do
            if ! kill -0 "$OLD_PID" 2>/dev/null; then
                echo "[restart] Old process exited cleanly"
                break
            fi
            sleep 0.5
        done

        # Force kill if still alive
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "[restart] Force killing old process"
            kill -9 "$OLD_PID" 2>/dev/null
            sleep 1
        fi
    fi

    echo "[restart] Starting new portal (nohup fallback)..."
    nohup python3 "$DIR/portal_server.py" >> /tmp/portal.log 2>&1 &
    NEW_PID=$!
    echo "[restart] New PID: $NEW_PID"
fi

# Wait for health check to pass (up to 15 seconds)
for i in $(seq 1 30); do
    if curl -s -o /dev/null -w "%{http_code}" "http://localhost:${PORT}/health" 2>/dev/null | grep -q "200"; then
        echo "[restart] Portal healthy on port $PORT — restart complete"
        exit 0
    fi
    sleep 0.5
done

echo "[restart] WARNING: Portal didn't respond to health check within 15s"
echo "[restart] Check: ps aux | grep portal_server"
