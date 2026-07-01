#!/usr/bin/env python3
"""Portal Watchdog -- keeps the PureBrain Portal alive.

Checks every INTERVAL seconds whether the portal is running and healthy.
If not, kills any zombie process and restarts cleanly.

Usage:
    # Run in foreground (for testing):
    python3 portal_watchdog.py

    # Run as background daemon:
    nohup python3 portal_watchdog.py > /dev/null 2>&1 &

    # Run via tmux (recommended):
    tmux new-session -d -s portal-watchdog 'python3 ~/purebrain_portal/portal_watchdog.py'

Environment variables:
    WATCHDOG_INTERVAL   -- seconds between checks (default: 30)
    PORTAL_PORT         -- portal port to health-check (default: 8097)
    PORTAL_LOG          -- where portal stdout/stderr goes (default: /tmp/portal.log)
    WATCHDOG_LOG        -- watchdog's own log (default: /tmp/portal_watchdog.log)
"""
import os
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# ---------- Configuration ----------

PORTAL_DIR = Path(__file__).resolve().parent
PORTAL_SCRIPT = PORTAL_DIR / "portal_server.py"
PORTAL_PORT = int(os.environ.get("PORTAL_PORT", "8097"))
PORTAL_LOG = os.environ.get("PORTAL_LOG", "/tmp/portal.log")
WATCHDOG_LOG = os.environ.get("WATCHDOG_LOG", "/tmp/portal_watchdog.log")
INTERVAL = int(os.environ.get("WATCHDOG_INTERVAL", "30"))


# ---------- Helpers ----------

def _run_cmd(cmd: str) -> str:
    """Run a shell command and return stdout. Returns empty string on error."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=10
        )
        return result.stdout
    except Exception:
        return ""


def _log(msg: str, log_path: str = WATCHDOG_LOG):
    """Append a timestamped line to the watchdog log."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        with open(log_path, "a") as f:
            f.write(line)
    except Exception:
        pass
    # Also print to stdout for tmux visibility
    print(line.rstrip(), flush=True)


# ---------- Core functions ----------

def find_portal_pid() -> int | None:
    """Find the PID of a running portal_server.py process.

    Returns the PID as an integer, or None if not found.
    Filters out grep/pgrep processes and the watchdog itself.
    """
    output = _run_cmd("pgrep -af portal_server.py")
    if not output.strip():
        return None

    my_pid = os.getpid()
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Skip grep/pgrep lines
        if "grep" in line or "pgrep" in line:
            continue
        # Skip the watchdog itself
        if "portal_watchdog" in line:
            continue
        try:
            pid = int(line.split()[0])
            if pid == my_pid:
                continue
            return pid
        except (ValueError, IndexError):
            continue
    return None


def health_check(port: int = PORTAL_PORT, timeout: int = 5) -> bool:
    """Check if the portal is responding to HTTP requests on /health.

    Returns True if the portal responds with HTTP 200 within timeout.
    """
    url = f"http://127.0.0.1:{port}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def kill_portal(pid: int):
    """Kill a portal process, trying SIGTERM first, then SIGKILL.

    Waits up to 5 seconds for graceful shutdown before escalating.
    """
    # Try SIGTERM first
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return  # Already dead
    except PermissionError:
        _log(f"WARN: Permission denied killing PID {pid}")
        return

    # Wait for process to exit
    for _ in range(10):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)  # Check if still alive
        except ProcessLookupError:
            return  # Process exited

    # Escalate to SIGKILL
    try:
        os.kill(pid, signal.SIGKILL)
        _log(f"Sent SIGKILL to PID {pid}")
    except ProcessLookupError:
        return
    except PermissionError:
        _log(f"WARN: Permission denied SIGKILL PID {pid}")


def start_portal():
    """Start the portal server as a background process.

    Redirects stdout/stderr to PORTAL_LOG.
    """
    log_file = open(PORTAL_LOG, "a")
    proc = subprocess.Popen(
        ["python3", str(PORTAL_SCRIPT)],
        stdout=log_file,
        stderr=log_file,
        cwd=str(PORTAL_DIR),
        start_new_session=True,  # Detach from watchdog's process group
    )
    _log(f"Started portal with PID {proc.pid}")
    return proc.pid


def check_and_restart(log_path: str = WATCHDOG_LOG):
    """Core watchdog logic: check portal health and restart if needed.

    Decision tree:
    1. If health check passes -> do nothing (portal is fine)
    2. If health check fails AND no PID -> start portal
    3. If health check fails AND PID exists -> kill zombie, start portal
    """
    pid = find_portal_pid()
    healthy = health_check()

    if healthy:
        # Portal is up and responding -- nothing to do
        return

    if pid is not None:
        # Process exists but is not healthy -- kill it first
        _log(f"RESTART: Portal PID {pid} exists but not responding. Killing.", log_path)
        kill_portal(pid)
        time.sleep(1)  # Brief pause after kill

    # Start a fresh portal
    reason = "no process found" if pid is None else f"killed unresponsive PID {pid}"
    _log(f"RESTART: Starting portal ({reason})", log_path)
    start_portal()


# ---------- Main loop ----------

def main():
    """Run the watchdog loop forever."""
    _log(f"Watchdog starting. Interval={INTERVAL}s, port={PORTAL_PORT}")
    _log(f"Portal script: {PORTAL_SCRIPT}")
    _log(f"Portal log: {PORTAL_LOG}")

    while True:
        try:
            check_and_restart()
        except Exception as e:
            _log(f"ERROR in watchdog check: {e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
