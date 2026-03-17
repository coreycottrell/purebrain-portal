#!/bin/bash
# Graceful portal restart — minimizes downtime
# Starts new process on temp port, verifies health, swaps, kills old

OLD_PID=$(pgrep -f "portal_server.py" | head -1)
PORT=8097

echo "[restart] Old PID: ${OLD_PID:-none}"

if [ -n "$OLD_PID" ]; then
    # Kill old process
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

# Start new process
echo "[restart] Starting new portal..."
nohup python3 /home/jared/purebrain_portal/portal_server.py >> /home/jared/purebrain_portal/portal.log 2>&1 &
NEW_PID=$!
echo "[restart] New PID: $NEW_PID"

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
