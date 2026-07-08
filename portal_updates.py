"""Portal Update & Module Health system for the PureBrain Portal.

Extracted from portal_server.py for modularity.
Contains: update check/apply, release update runner, module health,
module backup/restore, git operations, migration guidance.
"""
import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from starlette.requests import Request
from starlette.responses import JSONResponse

from portal_config import (
    SCRIPT_DIR, PORTAL_VERSION, RELEASE_NOTES_FILE,
    PORTAL_EXECUTOR as _PORTAL_EXECUTOR,
    check_auth, sanitize_error as _sanitize_error,
)

# ---------------------------------------------------------------------------
# Portal Update Mechanism (ADR-003)
# ---------------------------------------------------------------------------

# Persisted status file — survives portal restarts
_LAST_UPDATE_STATUS_FILE = SCRIPT_DIR / "last-update-status.json"

def _load_last_update() -> dict | None:
    """Load the last update result from disk (if any)."""
    try:
        if _LAST_UPDATE_STATUS_FILE.exists():
            return json.loads(_LAST_UPDATE_STATUS_FILE.read_text())
    except Exception:
        pass
    return None

# In-memory state for update tracking
_update_state: dict = {
    "status": "idle",        # idle | in_progress | success | failed
    "job_id": None,
    "step": None,
    "steps_completed": [],
    "steps_remaining": [],
    "started_at": None,
    "completed_at": None,
    "error": None,
    "previous_sha": None,
    "new_sha": None,
    "new_version": None,
    "rolled_back_to": None,
    "step_failed": None,
    "tests_passed": None,
    "message": None,
    "last_update": _load_last_update(),
}

_update_lock: asyncio.Lock | None = None


async def _get_update_lock() -> asyncio.Lock:
    """Lazily create the asyncio.Lock (must be inside an async context)."""
    global _update_lock
    if _update_lock is None:
        _update_lock = asyncio.Lock()
    return _update_lock


# ─── MODIFICATION DETECTION & HEALTH CHECK ─────────────────────────────
_MIGRATION_GUIDANCE = {
    "react-portal/dist/index.html": "UI changes belong in the React source (react-portal/src/**), then `npm run build` -- the HTML portal is retired",
    "portal_server.py": "Endpoints should be in custom/routes.py, config in custom/config.json -- see portal-mod-protocol skill",
    "static/commands-shortcuts.js": "Quick Fire customizations should be in custom/quickfire.json -- see portal-mod-protocol skill",
}
_MIGRATION_GUIDANCE_DEFAULT = "Move to the custom/ overlay system -- see skills/core/portal-mod-protocol/SKILL.md"


def _check_tracked_modifications() -> dict:
    """Detect modifications to tracked files via git status.

    Runs synchronously (intended for startup).  Returns a dict with
    has_tracked_modifications, modified_files, migration_guidance, etc.
    """
    result = {"has_tracked_modifications": False, "modified_files": [], "migration_guidance": {},
              "skill_path": "skills/core/portal-mod-protocol/SKILL.md", "update_safe": True}
    try:
        proc = subprocess.run(
            ["git", "-C", str(SCRIPT_DIR), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return result  # Not a git repo or git error -- nothing to report
        if not proc.stdout.strip():
            return result  # Clean working tree

        tracked_changes = []
        for line in proc.stdout.strip().split("\n"):
            if not line.strip():
                continue
            # Skip untracked files (lines starting with ??)
            if line.startswith("??"):
                continue
            # Extract filename (strip XY status prefix + space)
            # Format: "XY filename" or "XY filename -> newname"
            fname = line[3:].strip()
            if " -> " in fname:
                fname = fname.split(" -> ")[-1]
            tracked_changes.append(fname)

        if tracked_changes:
            result["has_tracked_modifications"] = True
            result["modified_files"] = tracked_changes
            result["update_safe"] = False
            for f in tracked_changes:
                result["migration_guidance"][f] = _MIGRATION_GUIDANCE.get(f, _MIGRATION_GUIDANCE_DEFAULT)

            # Print warnings to portal log
            print("[portal-health] WARNING: Tracked file modifications detected!")
            print("[portal-health] The following tracked files have local changes that WILL BE LOST on next update:")
            for f in tracked_changes:
                print(f"[portal-health]   M {f}")
            print("[portal-health] ")
            print("[portal-health] These modifications should be migrated to the custom/ overlay system:")
            print("[portal-health]   - UI changes (HTML/CSS/JS) -> custom/panels/*.html")
            print("[portal-health]   - API endpoints -> custom/routes.py")
            print("[portal-health]   - Config values -> custom/config.json")
            print("[portal-health]   - Startup logic -> custom/startup.py")
            print("[portal-health] ")
            print("[portal-health] See skills/core/portal-mod-protocol/SKILL.md for migration instructions.")
            print("[portal-health] Run GET /api/health/mods to see details.")
    except Exception as e:
        print(f"[portal-health] WARNING: tracked modification check failed: {e}")

    return result


async def api_health_mods(request: Request) -> JSONResponse:
    """GET /api/health/mods -- Report tracked file modifications and migration guidance."""
    if not check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    # Run the check in an executor to avoid blocking the event loop
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_PORTAL_EXECUTOR, _check_tracked_modifications)
    return JSONResponse(result)

# ─── END MODIFICATION DETECTION ─────────────────────────────────────────


# ─── MODULE BACKUP / RESTORE / REBUILD ─────────────────────────────────────
PORTAL_MODULES = {
    "js": [
        {"file": "static/js/core/auth.js", "name": "auth.js", "type": "core"},
        {"file": "static/js/core/panel-manager.js", "name": "panel-manager.js", "type": "core"},
        {"file": "static/js/features/dock.js", "name": "dock.js", "type": "feature"},
        {"file": "static/js/features/chat.js", "name": "chat.js", "type": "feature"},
        {"file": "static/js/features/inbox.js", "name": "inbox.js", "type": "feature"},
        {"file": "static/js/features/cc-chat.js", "name": "cc-chat.js", "type": "feature"},
        {"file": "static/js/features/terminal.js", "name": "terminal.js", "type": "feature"},
        {"file": "static/js/features/agents.js", "name": "agents.js", "type": "feature"},
        {"file": "static/js/features/files.js", "name": "files.js", "type": "feature"},
        {"file": "static/js/features/tasks.js", "name": "tasks.js", "type": "feature"},
        {"file": "static/js/features/hub.js", "name": "hub.js", "type": "feature"},
        {"file": "static/js/features/settings.js", "name": "settings.js", "type": "feature"},
        {"file": "static/js/features/deployments.js", "name": "deployments.js", "type": "feature"},
    ],
    "css": [
        {"file": "static/css/base.css", "name": "base.css", "type": "css"},
        {"file": "static/css/components.css", "name": "components.css", "type": "css"},
        {"file": "static/css/panels.css", "name": "panels.css", "type": "css"},
        {"file": "static/css/dock.css", "name": "dock.css", "type": "css"},
        {"file": "static/css/themes.css", "name": "themes.css", "type": "css"},
    ],
}

MODULE_BACKUP_DIR = SCRIPT_DIR / ".module-backup"


def _all_modules():
    """Flat list of all module defs."""
    return PORTAL_MODULES["js"] + PORTAL_MODULES["css"]


def _module_by_name(name: str):
    """Find a module def by its short name (e.g. 'auth.js')."""
    for m in _all_modules():
        if m["name"] == name:
            return m
    return None


def _file_md5(path: Path) -> str:
    """Compute MD5 hex digest for a file."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_module_health() -> dict:
    """Check health of all modules against backup checksums."""
    manifest_path = MODULE_BACKUP_DIR / "manifest.json"
    manifest_checksums = {}
    if manifest_path.exists():
        try:
            manifest_checksums = {
                m["file"]: m["checksum"]
                for m in json.loads(manifest_path.read_text()).get("modules", [])
            }
        except Exception:
            pass

    modules = []
    ok = missing = corrupted = no_backup = 0
    for m in _all_modules():
        fpath = SCRIPT_DIR / m["file"]
        entry = {"name": m["name"], "file": m["file"], "type": m["type"]}
        if not fpath.exists():
            entry["status"] = "missing"
            entry["size"] = 0
            entry["checksum"] = ""
            missing += 1
        else:
            entry["size"] = fpath.stat().st_size
            entry["checksum"] = _file_md5(fpath)
            expected = manifest_checksums.get(m["file"])
            if expected is None:
                entry["status"] = "no_backup"
                no_backup += 1
            elif entry["checksum"] == expected:
                entry["status"] = "ok"
                ok += 1
            else:
                entry["status"] = "corrupted"
                corrupted += 1
        modules.append(entry)

    return {
        "modules": modules,
        "summary": {
            "total": len(modules),
            "ok": ok,
            "missing": missing,
            "corrupted": corrupted,
            "no_backup": no_backup,
        },
    }


def _backup_modules() -> dict:
    """Copy all module files to backup dir and write manifest."""
    MODULE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backed_up = 0
    manifest_modules = []
    for m in _all_modules():
        src = SCRIPT_DIR / m["file"]
        if not src.exists():
            continue
        dst = MODULE_BACKUP_DIR / m["file"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
        checksum = _file_md5(src)
        manifest_modules.append({
            "file": m["file"],
            "name": m["name"],
            "type": m["type"],
            "checksum": checksum,
            "size": src.stat().st_size,
        })
        backed_up += 1

    manifest = {
        "modules": manifest_modules,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (MODULE_BACKUP_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return {"backed_up": backed_up, "timestamp": manifest["created_at"]}


def _restore_module(name: str) -> dict:
    """Restore a single module from backup. Returns status dict."""
    m = _module_by_name(name)
    if m is None:
        return {"error": "unknown_module", "name": name}
    backup_path = MODULE_BACKUP_DIR / m["file"]
    if not backup_path.exists():
        return {"error": "no_backup", "name": name}
    dst = SCRIPT_DIR / m["file"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(backup_path), str(dst))
    # Verify
    manifest_path = MODULE_BACKUP_DIR / "manifest.json"
    expected_checksum = None
    if manifest_path.exists():
        try:
            for entry in json.loads(manifest_path.read_text()).get("modules", []):
                if entry["file"] == m["file"]:
                    expected_checksum = entry["checksum"]
                    break
        except Exception:
            pass
    actual_checksum = _file_md5(dst)
    if expected_checksum and actual_checksum != expected_checksum:
        return {"error": "verify_failed", "name": name}
    return {"status": "restored", "name": name, "checksum": actual_checksum}


def _restore_all_modules() -> dict:
    """Restore all modules from backup."""
    restored = 0
    failed = 0
    details = []
    for m in _all_modules():
        result = _restore_module(m["name"])
        if result.get("status") == "restored":
            restored += 1
        else:
            failed += 1
        details.append(result)
    return {"restored": restored, "failed": failed, "details": details}


async def api_mods_health(request: Request) -> JSONResponse:
    """GET /api/mods/health — per-module health check."""
    if not check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_PORTAL_EXECUTOR, _check_module_health)
    return JSONResponse(result)


async def api_mods_backup(request: Request) -> JSONResponse:
    """POST /api/mods/backup — backup all modules to .module-backup/."""
    if not check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_PORTAL_EXECUTOR, _backup_modules)
    return JSONResponse(result)


async def api_mods_restore_single(request: Request) -> JSONResponse:
    """POST /api/mods/restore/{module_name} — restore one module from backup."""
    if not check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    module_name = request.path_params.get("module_name", "")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_PORTAL_EXECUTOR, _restore_module, module_name)
    if "error" in result:
        status = 404 if result["error"] in ("unknown_module", "no_backup") else 500
        return JSONResponse(result, status_code=status)
    return JSONResponse(result)


async def api_mods_restore_all(request: Request) -> JSONResponse:
    """POST /api/mods/restore-all — rebuild all modules from backup."""
    if not check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_PORTAL_EXECUTOR, _restore_all_modules)
    return JSONResponse(result)


# ─── END MODULE BACKUP / RESTORE / REBUILD ─────────────────────────────────


async def _git_cmd(args: list, timeout: int = 15) -> tuple:
    """Run a git command in the portal directory. Returns (returncode, stdout)."""
    cmd = ["git", "-C", str(SCRIPT_DIR)] + args
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(
                _PORTAL_EXECUTOR,
                lambda: subprocess.run(cmd, timeout=timeout, capture_output=True, text=True)
            ),
            timeout=timeout + 2
        )
        return (result.returncode, result.stdout.strip())
    except (asyncio.TimeoutError, Exception) as e:
        return (-1, str(e))


# ---------------------------------------------------------------------------
# Release server configuration (replaces git-based updates)
# ---------------------------------------------------------------------------
RELEASE_SERVER_URL = os.getenv("RELEASE_SERVER_URL", "https://cc.purebrain.ai")

# Built-in shared key for portal release downloads.
# Every portal ships with this key — no per-CIV configuration needed.
# This only grants read access to releases (code we distribute to all CIVs anyway).
# Always used for downloads; .env token is ignored to prevent misconfiguration.
PORTAL_UPDATE_TOKEN = "Hq-Of6ktPmQ-xDsJ4SjqbgGFZGIUR1oEushkZsghODY"


async def _get_current_version() -> str:
    """Return the current portal version. PORTAL_VERSION is the single source of truth."""
    return PORTAL_VERSION


async def api_update_check(request: Request) -> JSONResponse:
    """GET /api/update/check -- Check for updates from the release server."""
    if not check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, 401)

    now_iso = datetime.now(timezone.utc).isoformat()
    current_version = await _get_current_version()

    if not PORTAL_UPDATE_TOKEN:
        return JSONResponse({
            "status": "error",
            "error": "No PORTAL_UPDATE_TOKEN configured. Set it in .env to enable updates.",
            "checked_at": now_iso,
        })

    # Fetch remote version from the release server
    version_url = f"{RELEASE_SERVER_URL}/api/releases/portal/version"
    try:
        loop = asyncio.get_event_loop()
        req = urllib.request.Request(
            version_url,
            headers={"X-Portal-Token": PORTAL_UPDATE_TOKEN, "User-Agent": f"PureBrain-Portal/{PORTAL_VERSION}"},
        )
        resp_data = await asyncio.wait_for(
            loop.run_in_executor(
                _PORTAL_EXECUTOR,
                lambda: urllib.request.urlopen(req, timeout=15).read().decode(),
            ),
            timeout=20,
        )
        remote_info = json.loads(resp_data)
    except urllib.error.HTTPError as e:
        _sanitize_error(e, "update check")
        code = e.code
        if code in (401, 403):
            hint = "Authentication failed — check PORTAL_UPDATE_TOKEN in .env"
        elif code == 404:
            hint = "Release endpoint not found — check RELEASE_SERVER_URL in .env"
        else:
            hint = f"Release server returned HTTP {code}"
        return JSONResponse({
            "status": "error",
            "error": hint,
            "checked_at": now_iso,
        })
    except urllib.error.URLError as e:
        _sanitize_error(e, "update check")
        reason = str(e.reason) if hasattr(e, "reason") else ""
        if "SSL" in reason or "CERTIFICATE" in reason.upper():
            hint = "SSL certificate error connecting to release server"
        elif "Name or service not known" in reason or "getaddrinfo" in reason:
            hint = "Cannot resolve release server hostname — check DNS/network"
        elif "Connection refused" in reason:
            hint = "Release server refused connection — server may be down"
        else:
            hint = "Cannot reach release server — check network connectivity"
        return JSONResponse({
            "status": "error",
            "error": hint,
            "checked_at": now_iso,
        })
    except (asyncio.TimeoutError, TimeoutError):
        _sanitize_error(TimeoutError("timeout"), "update check")
        return JSONResponse({
            "status": "error",
            "error": "Release server did not respond in time — try again later",
            "checked_at": now_iso,
        })
    except json.JSONDecodeError as e:
        _sanitize_error(e, "update check")
        return JSONResponse({
            "status": "error",
            "error": "Release server returned invalid response — server may be misconfigured",
            "checked_at": now_iso,
        })
    except Exception as e:
        _sanitize_error(e, "update check")
        return JSONResponse({
            "status": "error",
            "error": f"Failed to check release server: {type(e).__name__}",
            "checked_at": now_iso,
        })

    remote_version = remote_info.get("version", "")
    remote_sha256 = remote_info.get("sha256", "")
    remote_size = remote_info.get("size_bytes", 0)

    if not remote_version:
        return JSONResponse({
            "status": "error",
            "error": "Release server returned no version info",
            "checked_at": now_iso,
        })

    if current_version == remote_version:
        return JSONResponse({
            "status": "up_to_date",
            "current_version": current_version,
            "checked_at": now_iso,
        })

    return JSONResponse({
        "status": "available",
        "current_version": current_version,
        "remote_version": remote_version,
        "remote_sha256": remote_sha256,
        "remote_size_bytes": remote_size,
        "checked_at": now_iso,
    })


async def api_update_apply(request: Request) -> JSONResponse:
    """POST /api/update/apply -- Download and apply update from release server."""
    if not check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, 401)

    if not PORTAL_UPDATE_TOKEN:
        return JSONResponse({"status": "error", "error": "No PORTAL_UPDATE_TOKEN configured"})

    # Guard 1: 30-minute throttle — prevent rapid-fire updates after 3+ in a window
    force = request.query_params.get("force", "").lower() in ("true", "1", "yes")
    if not force:
        try:
            if _LAST_UPDATE_STATUS_FILE.exists():
                last = json.loads(_LAST_UPDATE_STATUS_FILE.read_text())
                if last.get("status") == "success" and last.get("completed_at"):
                    completed = datetime.fromisoformat(last["completed_at"])
                    elapsed = datetime.now(timezone.utc) - completed
                    update_count = last.get("update_count_in_window", 1)
                    window_start = last.get("window_start")

                    # Reset window if older than 30 min
                    if window_start:
                        try:
                            ws = datetime.fromisoformat(window_start)
                            if (datetime.now(timezone.utc) - ws) > timedelta(minutes=30):
                                update_count = 0  # Window expired, reset counter
                        except Exception:
                            pass

                    if update_count >= 3 and elapsed < timedelta(minutes=30):
                        hours_ago = elapsed.total_seconds() / 3600
                        next_allowed = completed + timedelta(minutes=30)
                        return JSONResponse({
                            "status": "throttled",
                            "message": f"Already updated {update_count} times in the last 30 minutes. Throttled to prevent rapid-fire updates.",
                            "last_update_at": last["completed_at"],
                            "next_check_after": next_allowed.isoformat(),
                            "update_count": update_count,
                        })
        except Exception:
            pass  # If we can't read the file, allow the update

    lock = await _get_update_lock()

    # Guard 2: If lock is already held, another update is running
    if lock.locked():
        return JSONResponse({"status": "error", "error": "Update already in progress"})

    # Guard 3: Check state flag (belt-and-suspenders with the lock)
    if _update_state["status"] == "in_progress":
        return JSONResponse({"status": "error", "error": "Update already in progress"})

    # Acquire the lock before mutating state -- background task will release it
    await lock.acquire()

    job_id = f"update-{datetime.now():%Y%m%d-%H%M%S}"

    # Reset state for new update
    _update_state.update({
        "status": "in_progress",
        "job_id": job_id,
        "step": "starting",
        "steps_completed": [],
        "steps_remaining": ["download", "verify_checksum", "backup",
                            "extract", "restore_preserved", "pip_install",
                            "update_version", "restart"],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "error": None,
        "previous_version": None,
        "new_version": None,
        "step_failed": None,
        "message": None,
    })

    # Launch background task (lock is held; _run_release_update releases it in finally)
    asyncio.create_task(_run_release_update(job_id, lock))

    return JSONResponse({
        "status": "started",
        "job_id": job_id,
        "message": "Downloading update... Poll /api/update/status for progress.",
    })


async def api_update_apply_force(request: Request) -> JSONResponse:
    """POST /api/update/apply-force -- (Legacy) Redirects to normal apply since release-server updates are always clean."""
    return await api_update_apply(request)


def _update_step(step_name: str):
    """Mark a step as current and move it from remaining to completed."""
    _update_state["step"] = step_name
    if step_name in _update_state["steps_remaining"]:
        _update_state["steps_remaining"].remove(step_name)
    if step_name not in _update_state["steps_completed"]:
        _update_state["steps_completed"].append(step_name)


# Update log lives alongside portal code (should be .gitignored via logs/)
_UPDATE_LOG_DIR = SCRIPT_DIR / "logs"
_UPDATE_LOG_FILE = _UPDATE_LOG_DIR / "update.log"

def _log_update(message: str):
    """Append a message to the update log file."""
    try:
        _UPDATE_LOG_DIR.mkdir(exist_ok=True)
        with open(_UPDATE_LOG_FILE, "a") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] {message}\n")
    except Exception:
        pass


def _select_restart_method(tmux_present: bool, systemd_svc, watchdog_name) -> str:
    """Pure decision helper: choose the PRIMARY restart method.

    Precedence: systemd > watchdog > execv.

    - systemd_svc truthy  -> "systemd:<svc>"   (supervisor owns lifecycle;
                                                 execv would fight the supervisor)
    - elif watchdog_name truthy -> "watchdog:<name>"  (process manager owns it)
    - else (tmux present OR nothing) -> "execv"

    KEY CHANGE: tmux being present no longer selects a "tmux" dispatch. Under
    tmux, os.execv replaces the process in-place inside the same pane -- no C-c
    send-keys dance, no blind/unverified relaunch, no port double-bind. The
    ``tmux_present`` argument is accepted (detection is still performed by the
    caller) but never selects a dedicated tmux path.
    """
    if systemd_svc:
        return f"systemd:{systemd_svc}"
    if watchdog_name:
        return f"watchdog:{watchdog_name}"
    return "execv"


# Files to preserve during tarball extraction (never overwritten)
_PRESERVED_FILES = [
    ".env", ".portal-token", "portal-chat.jsonl", "user-settings.json",
    "agents.db", "referrals.db", "clients.db", "portal_data.db", "boop_config.json",
    "scheduled_tasks.json",
    "portal_owner.json", "kanban_tasks.json", "todo_tasks.json",
    "hub_tasks.json", ".gdrive-tokens.json", "reaction-sentiment.jsonl",
    "telegram_config.json", "bookmarks.json", "investor_config.json",
    "installed-skills.json", "activity-log.jsonl", "last-update-status.json",
]

# Directories to preserve (never overwritten)
# Directories to backup before update and restore after (small, critical user data)
_PRESERVED_DIRS = [
    "memories", ".claude", "custom", "logs", "skills/local", "constitution",
]

# Directories to NEVER overwrite during extraction (too large or self-referential to backup)
# Protected by tarball exclusion in deploy-release.sh; this is defense-in-depth.
_SKIP_EXTRACT_DIRS = [
    "backups", ".module-backup", "portal_uploads",
]


async def _run_release_update(job_id: str, lock: asyncio.Lock):
    """Download and apply a release server update as a background task.

    The caller must hold ``lock`` before calling; this function releases it
    in its ``finally`` block so that a new update can be triggered after
    completion (success or failure).
    """
    previous_version = None
    backup_dir = None
    try:
        previous_version = await _get_current_version()
        _update_state["previous_version"] = previous_version

        # Step 1: DOWNLOAD tarball from release server
        _update_step("download")
        _log_update(f"[{job_id}] Step 1: Downloading release tarball...")
        download_url = f"{RELEASE_SERVER_URL}/api/releases/portal/latest"
        loop = asyncio.get_event_loop()

        tmp_tarball = SCRIPT_DIR / f".update-{job_id}.tar.gz"
        expected_sha256 = None

        try:
            # First get version info for SHA256
            version_url = f"{RELEASE_SERVER_URL}/api/releases/portal/version"
            ver_req = urllib.request.Request(
                version_url,
                headers={"X-Portal-Token": PORTAL_UPDATE_TOKEN, "User-Agent": f"PureBrain-Portal/{PORTAL_VERSION}"},
            )
            ver_data = await asyncio.wait_for(
                loop.run_in_executor(
                    _PORTAL_EXECUTOR,
                    lambda: json.loads(urllib.request.urlopen(ver_req, timeout=15).read().decode()),
                ),
                timeout=20,
            )
            expected_sha256 = ver_data.get("sha256", "")
            remote_version = ver_data.get("version", "unknown")

            # Download the tarball
            dl_req = urllib.request.Request(
                download_url,
                headers={"X-Portal-Token": PORTAL_UPDATE_TOKEN, "User-Agent": f"PureBrain-Portal/{PORTAL_VERSION}"},
            )

            def _download_tarball():
                resp = urllib.request.urlopen(dl_req, timeout=120)
                with open(tmp_tarball, "wb") as f:
                    shutil.copyfileobj(resp, f)

            await asyncio.wait_for(
                loop.run_in_executor(_PORTAL_EXECUTOR, _download_tarball),
                timeout=180,
            )
            _log_update(f"[{job_id}] Downloaded tarball to {tmp_tarball} ({tmp_tarball.stat().st_size} bytes)")
        except Exception as e:
            raise RuntimeError(f"Download failed: {e}")

        # Step 2: VERIFY SHA256 checksum
        _update_step("verify_checksum")
        _log_update(f"[{job_id}] Step 2: Verifying SHA256 checksum...")
        if expected_sha256:
            sha256_hash = hashlib.sha256()
            with open(tmp_tarball, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    sha256_hash.update(chunk)
            actual_sha256 = sha256_hash.hexdigest()
            if actual_sha256 != expected_sha256:
                raise RuntimeError(
                    f"SHA256 mismatch: expected {expected_sha256[:16]}..., got {actual_sha256[:16]}..."
                )
            _log_update(f"[{job_id}] SHA256 verified: {actual_sha256[:16]}...")
        else:
            _log_update(f"[{job_id}] WARNING: No SHA256 from server, skipping checksum verification")

        # Step 3: BACKUP preserved files
        _update_step("backup")
        _log_update(f"[{job_id}] Step 3: Backing up preserved files...")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = SCRIPT_DIR / "backups" / "pre-update" / timestamp
        backup_dir.mkdir(parents=True, exist_ok=True)

        backed_up = []
        for pf in _PRESERVED_FILES:
            src = SCRIPT_DIR / pf
            if src.exists():
                dest = backup_dir / pf
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dest))
                backed_up.append(pf)

        for pd in _PRESERVED_DIRS:
            src = SCRIPT_DIR / pd
            if src.exists() and src.is_dir():
                dest = backup_dir / pd
                shutil.copytree(str(src), str(dest), dirs_exist_ok=True)
                backed_up.append(f"{pd}/")

        _log_update(f"[{job_id}] Backed up {len(backed_up)} items to {backup_dir}")

        # Step 4: EXTRACT tarball over portal directory
        _update_step("extract")
        _log_update(f"[{job_id}] Step 4: Extracting tarball...")

        def _extract_tarball():
            with tarfile.open(str(tmp_tarball), "r:gz") as tar:
                safe_members = []
                skip_prefixes = tuple(
                    d.rstrip("/") + "/" for d in _PRESERVED_DIRS + _SKIP_EXTRACT_DIRS
                )
                skip_files = set(_PRESERVED_FILES)
                skipped = []
                for member in tar.getmembers():
                    # Security: prevent path traversal and symlink attacks
                    if member.name.startswith("/") or ".." in member.name:
                        raise RuntimeError(f"Unsafe path in tarball: {member.name}")
                    if member.issym() or member.islnk():
                        continue  # Skip symlinks for safety
                    # Strip leading ./ for comparison (NOT lstrip — that strips character sets)
                    clean = member.name[2:] if member.name.startswith("./") else member.name
                    # Skip preserved files and directories
                    if clean in skip_files:
                        skipped.append(clean)
                        continue
                    if any(clean.startswith(p) for p in skip_prefixes):
                        skipped.append(clean)
                        continue
                    safe_members.append(member)

                _log_update(f"[{job_id}] Tarball: {len(safe_members)} to extract, {len(skipped)} preserved/skipped")

                # Backup code files before pre-delete so we can rollback if extraction fails
                code_backup_dir = backup_dir / "_code_rollback"
                code_backup_dir.mkdir(parents=True, exist_ok=True)
                for member in safe_members:
                    if member.isfile():
                        clean = member.name[2:] if member.name.startswith("./") else member.name
                        src = SCRIPT_DIR / clean
                        if src.exists() and src.is_file():
                            dest = code_backup_dir / clean
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(str(src), str(dest))

                # Pre-delete ALL files that will be extracted (not just critical ones).
                # This guarantees clean extraction — tarfile.extractall() can silently
                # fail to overwrite files on some OS/filesystem combinations.
                pre_deleted = 0
                for member in safe_members:
                    if member.isfile():
                        target = SCRIPT_DIR / member.name
                        if target.exists():
                            target.unlink()
                            pre_deleted += 1
                _log_update(f"[{job_id}] Pre-deleted {pre_deleted} existing files before extraction")

                tar.extractall(
                    path=str(SCRIPT_DIR),
                    members=safe_members,
                    filter="fully_trusted",
                )

                # Post-extraction verification: ensure critical files were written.
                # Frontend critical file is the React bundle (portal-pb-styled.html
                # retired from the deploy path 2026-07-08 — React is the only frontend).
                _critical = ["react-portal/dist/index.html", "portal_server.py",
                             "portal_config.py", "portal_updates.py"]
                missing = [cf for cf in _critical if not (SCRIPT_DIR / cf).exists()]
                if missing:
                    raise RuntimeError(
                        f"Extraction failed: critical files missing after extract: {missing}"
                    )

        await asyncio.wait_for(
            loop.run_in_executor(_PORTAL_EXECUTOR, _extract_tarball),
            timeout=60,
        )
        _log_update(f"[{job_id}] Tarball extracted to {SCRIPT_DIR}")

        # Step 5: RESTORE preserved files that may have been overwritten
        _update_step("restore_preserved")
        _log_update(f"[{job_id}] Step 5: Restoring preserved files...")
        for pf in _PRESERVED_FILES:
            backup_src = backup_dir / pf
            dest = SCRIPT_DIR / pf
            if backup_src.exists():
                shutil.copy2(str(backup_src), str(dest))

        for pd in _PRESERVED_DIRS:
            backup_src = backup_dir / pd
            dest = SCRIPT_DIR / pd
            if backup_src.exists() and backup_src.is_dir():
                shutil.copytree(str(backup_src), str(dest), dirs_exist_ok=True)

        _log_update(f"[{job_id}] Preserved files restored")

        # Step 5.5: PIP INSTALL dependencies
        _update_step("pip_install")
        _log_update(f"[{job_id}] Step 5.5: Installing dependencies from requirements.txt...")
        req_file = SCRIPT_DIR / "requirements.txt"
        if req_file.exists():
            def _run_pip():
                pip_result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r",
                     str(req_file), "--quiet", "--break-system-packages"],
                    capture_output=True, text=True, timeout=120,
                )
                if pip_result.returncode != 0:
                    # Retry without --break-system-packages (older pip)
                    pip_result = subprocess.run(
                        [sys.executable, "-m", "pip", "install", "-r",
                         str(req_file), "--quiet"],
                        capture_output=True, text=True, timeout=120,
                    )
                return pip_result

            try:
                pip_res = await asyncio.wait_for(
                    loop.run_in_executor(_PORTAL_EXECUTOR, _run_pip),
                    timeout=180,
                )
            except FileNotFoundError:
                _log_update(f"[{job_id}] pip not available, skipping dependency install")
                pip_res = None

            if pip_res is not None:
                _log_update(f"[{job_id}] pip install: exit={pip_res.returncode}")
                if pip_res.returncode != 0:
                    _log_update(f"[{job_id}] pip install FAILED: {pip_res.stderr[:500]}")
                    raise RuntimeError(f"pip install failed (exit {pip_res.returncode})")
        else:
            _log_update(f"[{job_id}] No requirements.txt found, skipping pip install")

        # Step 6: UPDATE VERSION in release_notes.json
        _update_step("update_version")
        _log_update(f"[{job_id}] Step 6: Updating version info...")
        try:
            rn_data = json.loads(RELEASE_NOTES_FILE.read_text())
        except Exception:
            rn_data = {"current_version": previous_version, "releases": []}
        rn_data["current_version"] = remote_version
        RELEASE_NOTES_FILE.write_text(json.dumps(rn_data, indent=2))
        _update_state["new_version"] = remote_version

        # Step 7: RESTART with supervisor detection (mirrors restart.sh logic)
        _update_step("restart")
        _update_state["status"] = "success"
        _update_state["completed_at"] = datetime.now(timezone.utc).isoformat()
        _update_state["message"] = f"Updated to {remote_version}. Portal will restart momentarily."
        _update_state["last_update"] = {
            "job_id": job_id,
            "status": "success",
            "version": remote_version,
            "completed_at": _update_state["completed_at"],
        }
        # Persist to disk so status survives the restart
        try:
            # Read existing counter state to increment window counter
            try:
                existing = json.loads(_LAST_UPDATE_STATUS_FILE.read_text()) if _LAST_UPDATE_STATUS_FILE.exists() else {}
                window_start = existing.get("window_start")
                count = existing.get("update_count_in_window", 0)

                # If window expired (>30 min), reset
                if window_start:
                    try:
                        ws = datetime.fromisoformat(window_start)
                        if (datetime.now(timezone.utc) - ws) > timedelta(minutes=30):
                            count = 0
                            window_start = None
                    except Exception:
                        count = 0

                if not window_start:
                    window_start = datetime.now(timezone.utc).isoformat()

                count += 1
            except Exception:
                count = 1
                window_start = datetime.now(timezone.utc).isoformat()

            _update_state["last_update"]["update_count_in_window"] = count
            _update_state["last_update"]["window_start"] = window_start
            _LAST_UPDATE_STATUS_FILE.write_text(json.dumps(_update_state["last_update"], indent=2))
        except Exception:
            pass
        _log_update(f"[{job_id}] SUCCESS: Updated from {previous_version} to {remote_version}")
        try:
            from portal_activity import log_activity
            log_activity(f"Portal updated to v{remote_version}", f"from v{previous_version}", "update")
        except Exception:
            pass

        # Clean up temp tarball
        try:
            tmp_tarball.unlink(missing_ok=True)
        except Exception:
            pass

        # Delay 2s so the success status can be polled, then restart
        await asyncio.sleep(2)

        # Detect port from .env for health checks
        _port = "8097"
        try:
            env_path = SCRIPT_DIR / ".env"
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    if line.strip().startswith("PORT") and "=" in line:
                        _port = line.split("=", 1)[1].strip()
                        break
        except Exception:
            pass

        # --- Supervisor detection (detection != dispatch) ---
        # We DETECT tmux/systemd/watchdog, then let _select_restart_method pick
        # the PRIMARY method. Precedence: systemd > watchdog > in-place replace.
        # tmux is detected (and logged) but no longer selects a dedicated
        # dispatch -- under tmux we prefer in-place process replacement (no blind
        # relaunch, no port double-bind). See _select_restart_method.
        tmux_present = False
        systemd_svc = None
        watchdog_name = None

        # Detection: tmux session named 'portal-server' (detection only).
        try:
            tmux_check = subprocess.run(
                ["tmux", "has-session", "-t", "portal-server"],
                capture_output=True, text=True, timeout=5,
            )
            if tmux_check.returncode == 0:
                tmux_present = True
                _log_update(f"[{job_id}] Detected tmux session 'portal-server' (execv preferred)")
        except Exception:
            pass

        # Detection: systemd service.
        for svc in ["portal-server", "portal", "purebrain-portal", "puresurf-portal"]:
            try:
                result = subprocess.run(
                    ["systemctl", "is-active", "--quiet", svc],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    systemd_svc = svc
                    _log_update(f"[{job_id}] Detected systemd service '{svc}'")
                    break
            except Exception:
                pass

        # Detection: process manager parent (supervisord, s6, etc.).
        try:
            ppid = os.getppid()
            ppid_comm = Path(f"/proc/{ppid}/comm")
            ppid_name = ppid_comm.read_text().strip() if ppid_comm.exists() else ""
            if ppid_name in ("systemd", "supervisord", "s6-supervise", "runit", "init"):
                watchdog_name = ppid_name
                _log_update(f"[{job_id}] Process manager detected: {ppid_name} (PID {ppid})")
        except Exception:
            pass

        # Decide the PRIMARY restart method from detected supervisors.
        restart_method = _select_restart_method(tmux_present, systemd_svc, watchdog_name)
        _log_update(f"[{job_id}] Selected restart method: {restart_method}")

        # Execute restart based on selected method.
        if restart_method.startswith("systemd:"):
            svc_name = restart_method.split(":", 1)[1]
            _log_update(f"[{job_id}] Restarting via systemctl restart {svc_name}...")
            try:
                subprocess.run(
                    ["systemctl", "restart", svc_name],
                    capture_output=True, timeout=15,
                )
                await asyncio.sleep(1)
                os._exit(0)
            except Exception as e:
                _log_update(f"[{job_id}] systemctl restart failed: {e}, falling back...")

        elif restart_method and restart_method.startswith("watchdog:"):
            _log_update(f"[{job_id}] Sending SIGTERM for watchdog restart...")
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            # execv — replaces the current process in-place. This is the chosen
            # path for tmux AND bare/Docker environments. Under tmux it replaces
            # the process inside the SAME pane: no C-c send-keys dance, no blind
            # re-run, no second server bound to the port. Works universally:
            # Docker, bare metal, tmux. Industry-standard Python self-restart.
            _log_update(f"[{job_id}] Restarting via os.execv (in-place replace, universal)...")
            try:
                os.execv(sys.executable, [sys.executable, str(SCRIPT_DIR / "portal_server.py")])
            except Exception as e:
                _log_update(f"[{job_id}] os.execv failed: {e}, trying restart.sh...")

            # Fallback: restart.sh (only if os.execv failed). os.execv above
            # always runs BEFORE this Popen -- never spawn a second server while
            # the first still holds the port.
            restart_script = SCRIPT_DIR / "restart.sh"
            if restart_script.exists():
                _log_update(f"[{job_id}] Using restart.sh for restart...")
                try:
                    subprocess.Popen(
                        ["bash", str(restart_script)],
                        start_new_session=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    await asyncio.sleep(1)
                    os._exit(0)
                except Exception as e:
                    _log_update(f"[{job_id}] restart.sh also failed: {e}")

        # Step 8: POST-RESTART HEALTH CHECK
        # (Only reached if we didn't os._exit or os.execv above — i.e. systemd/watchdog restart)
        _log_update(f"[{job_id}] Step 8: Post-restart health check (port {_port})...")
        health_ok = False
        for attempt in range(30):
            try:
                health_resp = urllib.request.urlopen(
                    f"http://localhost:{_port}/health", timeout=2,
                )
                health_data = json.loads(health_resp.read().decode())
                if health_data.get("version") == remote_version:
                    health_ok = True
                    _log_update(f"[{job_id}] Health check PASSED: version={remote_version}")
                    break
                elif health_data.get("status") == "ok":
                    _log_update(f"[{job_id}] Health check: portal up but version={health_data.get('version')} (expected {remote_version}), retrying...")
            except Exception:
                pass
            await asyncio.sleep(1)

        if not health_ok:
            _log_update(f"[{job_id}] WARNING: Post-restart health check FAILED after 30s")
            _log_update(f"[{job_id}] Previous version backup: {backup_dir}")
            _log_update(f"[{job_id}] Manual recovery: cd {SCRIPT_DIR} && python3 portal_server.py")
            _log_update(f"[{job_id}] To rollback: cp -r {backup_dir}/_code_rollback/* {SCRIPT_DIR}/")
            _update_state["message"] = (
                f"Updated to {remote_version} but health check failed after restart. "
                f"Previous version backup at: {backup_dir} — "
                f"Manual recovery: cd {SCRIPT_DIR} && python3 portal_server.py"
            )

    except Exception as e:
        error_msg = str(e)
        failed_step = _update_state.get("step")
        _log_update(f"[{job_id}] FAILED at step '{failed_step}': {error_msg}")

        # Attempt rollback if we have a backup
        rolled_back = False
        if backup_dir and backup_dir.exists():
            _log_update(f"[{job_id}] Attempting rollback from {backup_dir}...")
            try:
                # Restore code files first (portal_server.py, portal_updates.py, etc.)
                code_backup = backup_dir / "_code_rollback"
                if code_backup.exists():
                    for f in code_backup.rglob("*"):
                        if f.is_file():
                            rel = f.relative_to(code_backup)
                            dest = SCRIPT_DIR / rel
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(str(f), str(dest))
                    _log_update(f"[{job_id}] Code files restored from _code_rollback")

                # Restore preserved user files
                for pf in _PRESERVED_FILES:
                    src = backup_dir / pf
                    if src.exists():
                        shutil.copy2(str(src), str(SCRIPT_DIR / pf))
                for pd in _PRESERVED_DIRS:
                    src = backup_dir / pd
                    if src.exists() and src.is_dir():
                        shutil.copytree(str(src), str(SCRIPT_DIR / pd), dirs_exist_ok=True)
                rolled_back = True
                _log_update(f"[{job_id}] Rollback completed — code + user data restored")
            except Exception as rb_err:
                _log_update(f"[{job_id}] CRITICAL: Rollback failed: {rb_err}")

        # Clean up temp tarball on failure
        try:
            tmp_file = SCRIPT_DIR / f".update-{job_id}.tar.gz"
            tmp_file.unlink(missing_ok=True)
        except Exception:
            pass

        rollback_msg = " (user data restored from backup)" if rolled_back else ""
        _update_state.update({
            "status": "failed",
            "step_failed": failed_step,
            "error": error_msg,
            "rolled_back": rolled_back,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "message": f"Update failed at {failed_step}: {error_msg}{rollback_msg}",
            "last_update": {
                "job_id": job_id,
                "status": "failed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        })
        # Persist failure status to disk
        try:
            _LAST_UPDATE_STATUS_FILE.write_text(json.dumps(_update_state["last_update"], indent=2))
        except Exception:
            pass
    finally:
        # Always release the lock so a new update can be triggered
        if lock.locked():
            lock.release()


async def api_update_status(request: Request) -> JSONResponse:
    """GET /api/update/status -- Poll the status of the current/recent update."""
    if not check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, 401)

    status = _update_state["status"]

    if status == "in_progress":
        return JSONResponse({
            "status": "in_progress",
            "job_id": _update_state["job_id"],
            "step": _update_state["step"],
            "steps_completed": _update_state["steps_completed"],
            "steps_remaining": _update_state["steps_remaining"],
            "started_at": _update_state["started_at"],
        })

    if status == "success":
        return JSONResponse({
            "status": "success",
            "job_id": _update_state["job_id"],
            "previous_version": _update_state.get("previous_version"),
            "new_version": _update_state.get("new_version"),
            "message": _update_state["message"],
            "completed_at": _update_state["completed_at"],
        })

    if status == "failed":
        return JSONResponse({
            "status": "failed",
            "job_id": _update_state["job_id"],
            "step_failed": _update_state.get("step_failed"),
            "error": _update_state.get("error"),
            "message": _update_state.get("message"),
            "completed_at": _update_state.get("completed_at"),
        })

    # idle
    return JSONResponse({
        "status": "idle",
        "last_update": _update_state.get("last_update"),
    })

