#!/bin/bash
#
# Launch PureBrain Log Server (HTTPS)
#
# Runs the Flask server with SSL/TLS enabled for secure logging
# from purebrain.ai (which requires HTTPS to avoid mixed content).
#
# Usage:
#   ./tools/launch_purebrain_log_server.sh         # Start the server
#   ./tools/launch_purebrain_log_server.sh stop    # Stop the server
#   ./tools/launch_purebrain_log_server.sh status  # Check status
#   ./tools/launch_purebrain_log_server.sh restart # Restart the server
#
# Default port: 8443 (HTTPS)
# Endpoint: https://89.167.19.20:8443/api/log-conversation

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LOG_FILE="$PROJECT_ROOT/logs/purebrain_log_server.log"
PID_FILE="$PROJECT_ROOT/.purebrain_log_server.pid"

# Ensure logs directory exists
mkdir -p "$PROJECT_ROOT/logs"
mkdir -p "$PROJECT_ROOT/config/ssl"

get_pid() {
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if ps -p "$pid" > /dev/null 2>&1; then
            echo "$pid"
            return 0
        fi
    fi
    # Try to find by process name
    local pid=$(pgrep -f "python3.*purebrain_log_server.py" 2>/dev/null | head -1)
    if [ -n "$pid" ]; then
        echo "$pid"
        return 0
    fi
    return 1
}

start_server() {
    if pid=$(get_pid); then
        echo "PureBrain log server already running (PID: $pid)"
        return 0
    fi

    echo "Starting PureBrain Log Server (HTTPS)..."

    cd "$PROJECT_ROOT"

    # Activate virtualenv if it exists
    if [ -f "venv/bin/activate" ]; then
        source venv/bin/activate
    fi

    # Start the server
    nohup python3 tools/purebrain_log_server.py >> "$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"

    sleep 3

    if ps -p "$pid" > /dev/null 2>&1; then
        echo "PureBrain log server started successfully!"
        echo "  PID: $pid"
        echo "  Log: $LOG_FILE"
        echo "  Endpoint: https://89.167.19.20:8443/api/log-conversation"
        echo ""
        echo "To stop: $0 stop"
        echo "To check status: $0 status"
        echo ""
        echo "Test with:"
        echo '  curl -k https://89.167.19.20:8443/api/health'
    else
        echo "ERROR: Server failed to start. Check logs:"
        tail -20 "$LOG_FILE"
        return 1
    fi
}

stop_server() {
    if pid=$(get_pid); then
        echo "Stopping PureBrain log server (PID: $pid)..."
        kill "$pid" 2>/dev/null || true
        sleep 1
        if ps -p "$pid" > /dev/null 2>&1; then
            echo "Force killing..."
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$PID_FILE"
        echo "PureBrain log server stopped."
    else
        echo "PureBrain log server is not running."
    fi
}

status_server() {
    if pid=$(get_pid); then
        echo "PureBrain log server is RUNNING"
        echo "  PID: $pid"
        echo "  Log: $LOG_FILE"
        echo "  Endpoint: https://89.167.19.20:8443/api/log-conversation"
        echo ""
        echo "Recent log entries:"
        tail -10 "$LOG_FILE" 2>/dev/null || echo "  (no logs yet)"
    else
        echo "PureBrain log server is NOT RUNNING"
        echo ""
        echo "To start: $0"
    fi
}

case "${1:-start}" in
    start)
        start_server
        ;;
    stop)
        stop_server
        ;;
    restart)
        stop_server
        sleep 1
        start_server
        ;;
    status)
        status_server
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
