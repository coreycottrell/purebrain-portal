"""Lightweight activity logging for the PureBrain Portal.

Logs real AI activity events to a JSONL file. The hub dashboard
reads from this to show a live activity feed.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from portal_config import SCRIPT_DIR, check_auth

ACTIVITY_LOG = SCRIPT_DIR / "activity-log.jsonl"
_MAX_ENTRIES = 200


def log_activity(action: str, detail: str = "", category: str = "system"):
    """Log an AI activity event to the JSONL file.

    Categories: agent, task, session, update, system, chat, file
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "detail": detail,
        "category": category,
    }
    try:
        with open(ACTIVITY_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
        # Trim to last _MAX_ENTRIES
        _trim_log()
    except Exception:
        pass


def _trim_log():
    """Keep only the last _MAX_ENTRIES lines."""
    try:
        if not ACTIVITY_LOG.exists():
            return
        size = ACTIVITY_LOG.stat().st_size
        # Only bother trimming if file is large enough to likely exceed limit
        if size < 20_000:
            return
        with open(ACTIVITY_LOG, "r") as f:
            lines = f.readlines()
        if len(lines) > _MAX_ENTRIES:
            with open(ACTIVITY_LOG, "w") as f:
                f.writelines(lines[-_MAX_ENTRIES:])
    except Exception:
        pass


def _read_recent(n: int = 20) -> list:
    """Read the last n activity entries."""
    if not ACTIVITY_LOG.exists():
        return []
    try:
        with open(ACTIVITY_LOG, "r") as f:
            lines = f.readlines()
        entries = []
        for line in lines[-(n):]:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return list(reversed(entries))  # newest first
    except Exception:
        return []


async def api_activity(request: Request) -> JSONResponse:
    """GET /api/activity — return recent activity entries."""
    if not check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    limit = 20
    try:
        limit = int(request.query_params.get("limit", "20"))
        limit = min(max(limit, 1), 100)
    except (ValueError, TypeError):
        pass
    return JSONResponse({"activity": _read_recent(limit)})
