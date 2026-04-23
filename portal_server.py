#!/usr/bin/env python3
"""PureBrain Portal Server — per-CIV mini server for purebrain.ai
Auth via Bearer token. JSONL-based chat history (same as TG bot).
"""
import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
import httpx
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiosqlite

# ── User tracking module ────────────────────────────────────────────────────
from tracking import (
    ensure_tracking_columns,
    record_login,
    record_activity,
    log_webhook_event,
    process_webhook_event,
    update_next_billing_date,
    get_tracking_stats,
)

from html import escape
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect


# ---------------------------------------------------------------------------
# Thread-pool safety net + fire-and-forget task tracking (prevents exhaustion)
# ---------------------------------------------------------------------------
_PORTAL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=32, thread_name_prefix="portal"
)
# Track background tasks so they don't get GC'd and we can monitor accumulation
_background_tasks: set = set()

def _fire_and_forget(coro):
    """Schedule a coroutine as a tracked background task that auto-cleans."""
    task = asyncio.ensure_future(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
TOKEN_FILE = SCRIPT_DIR / ".portal-token"
PORTAL_HTML = SCRIPT_DIR / "portal.html"
PORTAL_PB_HTML = SCRIPT_DIR / "portal-pb-styled.html"
REACT_DIST = SCRIPT_DIR / "react-portal" / "dist"
START_TIME = time.time()
PORTAL_VERSION = "1.4.1"
RELEASE_NOTES_FILE = SCRIPT_DIR / "release_notes.json"
# Auto-detect CIV_NAME and HUMAN_NAME from identity file — works in any fleet container.
# Falls back to generic defaults if identity file not found (local dev).
_identity_file = Path.home() / ".aiciv-identity.json"
try:
    _identity = json.loads(_identity_file.read_text())
    CIV_NAME = _identity.get("civ_id", "witness")
    HUMAN_NAME = _identity.get("human_name", "User")
except Exception:
    CIV_NAME = "witness"
    HUMAN_NAME = "User"
# Auto-derive Claude project JSONL directory.
# Claude encodes the PROJECT directory (git root) by replacing '/' with '-'.
# We scan ALL project directories for JSONL files to find the active one.
_PROJECTS_DIR = Path.home() / ".claude" / "projects"
# Primary LOG_ROOT: try the most recently modified project directory
LOG_ROOT = _PROJECTS_DIR  # fallback — _get_all_session_log_paths handles the real search
HISTORY_FILE = Path.home() / ".claude" / "history.jsonl"
PORTAL_CHAT_LOG = SCRIPT_DIR / "portal-chat.jsonl"
UPLOADS_DIR = Path.home() / "portal_uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
UPLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
PAYOUT_REQUESTS_FILE = SCRIPT_DIR / "payout-requests.jsonl"
# Paths to Aether log files used for client data import
_AETHER_LOG_ROOT = Path.home() / "projects" / "AI-CIV" / "aether" / "logs"
WEB_CONVERSATIONS_LOG = _AETHER_LOG_ROOT / "purebrain_web_conversations.jsonl"
PAYMENTS_LOG          = _AETHER_LOG_ROOT / "purebrain_payments.jsonl"
PAY_TEST_LOG          = _AETHER_LOG_ROOT / "purebrain_pay_test.jsonl"
PAYOUT_MIN_AMOUNT = 25.0   # minimum payout threshold ($)
PAYOUT_AUTO_APPROVE_LIMIT = 1000.0  # auto-approve payouts up to this amount; above requires manual approval
PAYOUT_COOLDOWN_DAYS = 30  # days between payout requests
REFERRALS_DB = SCRIPT_DIR / "referrals.db"
CLIENTS_DB   = SCRIPT_DIR / "clients.db"
AGENTS_DB    = SCRIPT_DIR / "agents.db"
REFERRAL_CODE_PREFIX = "PB-"
REFERRAL_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no ambiguous chars
REFERRAL_CODE_LENGTH = 4
REFERRAL_COMMISSION_RATE = 0.05  # 5% recurring commission on every payment from referred members

# Allowed directories for file downloads (generic — works in any customer container)
DOWNLOAD_ALLOWED_DIRS = [
    Path.home() / "exports",
    Path.home() / "to-human",
    Path.home() / "purebrain_portal",
    Path.home() / "from-acg",
    Path.home() / "portal_uploads",
]

# OAuth flow state
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
OAUTH_URL_PATTERN = re.compile(r'https://[^\s\x1b\x07\]]*oauth/authorize\?[^\s\x1b\x07\]]+')
_captured_oauth_url = None
_auth_prewarm_task = None  # background prewarm task handle
_auth_flow_running = False  # lock to prevent concurrent auth flows

# Auth flow v2 — screen detection patterns for state machine
AUTH_SCREEN_PATTERNS = {
    'oauth_url': OAUTH_URL_PATTERN,
    'login_menu': re.compile(
        r'Select login method|Use OAuth|How would you like to authenticate',
        re.IGNORECASE,
    ),
    'csat_survey': re.compile(
        r'How is Claude doing\?|rate your experience|satisfaction survey|'
        r'How would you rate|thumbs up|Would you recommend',
        re.IGNORECASE,
    ),
    'update_prompt': re.compile(
        r'Auto-update|update available|Update now\?|new version|'
        r'would you like to update|upgrade available',
        re.IGNORECASE,
    ),
    'trust_folder': re.compile(
        r'Do you trust the authors|trust this (?:project|folder)|'
        r'Trust this project|Do you want to trust',
        re.IGNORECASE,
    ),
    'theme_picker': re.compile(
        r'Choose (?:the |your )?(?:text )?style|'
        r'Select (?:a |your )?theme|'
        r'Dark mode|Light text on dark background|'
        r"Let's get started",
        re.IGNORECASE,
    ),
    'logged_in': re.compile(
        r'Logged in as|Login successful|Successfully authenticated|'
        r'You are now logged in',
        re.IGNORECASE,
    ),
    'shell_prompt': re.compile(
        r'(?:aiciv@|[$#])\s*$',
        re.MULTILINE,
    ),
    'error': re.compile(
        r'(?:Error|ENOENT|crash|fatal|SIGTERM|SIGKILL|panic|'
        r'Cannot connect|Connection refused)',
        re.IGNORECASE,
    ),
}
AUTH_SCREEN_PRIORITY = [
    'oauth_url', 'logged_in', 'csat_survey', 'update_prompt',
    'trust_folder', 'theme_picker', 'login_menu', 'error', 'shell_prompt',
]

if TOKEN_FILE.exists():
    BEARER_TOKEN = TOKEN_FILE.read_text().strip()
else:
    BEARER_TOKEN = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(BEARER_TOKEN)
    TOKEN_FILE.chmod(0o600)
    print(f"[portal] Generated new bearer token (saved to {TOKEN_FILE})")

# ─── Affiliate login rate-limiting (in-memory, resets on restart) ───────────
# { ip_hash: {"count": N, "window_start": epoch_float} }
_AFFILIATE_LOGIN_ATTEMPTS: dict = {}
_LOGIN_MAX_ATTEMPTS = 10          # per window
_LOGIN_WINDOW_SECS  = 900         # 15 minutes

# ─── Affiliate session tokens: { token: {"code": ..., "expires": epoch} } ───
_AFFILIATE_SESSIONS: dict = {}
_SESSION_TTL_SECS = 86400 * 7     # 7 days

# ─── Referral track rate-limiting (prevents click-spam abuse) ────────────────
_TRACK_RATE_LIMITS: dict = {}  # ip_hash -> {"count": N, "window_start": epoch}
_TRACK_MAX_PER_WINDOW = 30    # max clicks per IP per window
_TRACK_WINDOW_SECS = 300      # 5-minute window

# ─── PayPal credentials ───────────────────────────────────────────────────────
PAYPAL_SANDBOX = os.environ.get("PAYPAL_SANDBOX", "true").lower() != "false"
if PAYPAL_SANDBOX:
    PAYPAL_CLIENT_ID     = os.environ.get("PAYPAL_SANDBOX_CLIENT_ID", os.environ.get("PAYPAL_CLIENT_ID", ""))
    PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_SANDBOX_SECRET", os.environ.get("PAYPAL_SECRET", ""))
else:
    PAYPAL_CLIENT_ID     = os.environ.get("PAYPAL_CLIENT_ID", "")
    PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_SECRET", "")


def _run_subprocess_sync(cmd, timeout=5, check=False, capture=False, text=False):
    """Run a subprocess with mandatory timeout. Used by sync callers only."""
    try:
        return subprocess.run(
            cmd, timeout=timeout, check=check,
            capture_output=capture, text=text,
            stderr=subprocess.DEVNULL if not capture else None,
        )
    except subprocess.TimeoutExpired:
        return None
    except subprocess.CalledProcessError:
        return None
    except Exception:
        return None


async def _run_subprocess_async(cmd, timeout=5, check=False):
    """Run a subprocess WITHOUT blocking the asyncio event loop.
    Uses asyncio.create_subprocess_exec — avoids the thread pool entirely."""
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *[str(c) for c in cmd],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=timeout + 2,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if check and proc.returncode != 0:
            print(f"[portal] WARN subprocess error (rc={proc.returncode}): {' '.join(str(c) for c in cmd)} stderr={stderr}")
            return None
        # Return a subprocess.CompletedProcess for API compatibility
        return subprocess.CompletedProcess(
            args=cmd, returncode=proc.returncode, stdout=stdout, stderr=stderr,
        )
    except asyncio.TimeoutError:
        print(f"[portal] WARN _run_subprocess_async timeout: {' '.join(str(c) for c in cmd)}")
        return None
    except Exception as e:
        print(f"[portal] WARN _run_subprocess_async unexpected {type(e).__name__}: {e} cmd={' '.join(str(c) for c in cmd)}")
        return None


async def _run_subprocess_output(cmd, timeout=5):
    """Run subprocess and capture output without blocking the event loop.
    Uses asyncio.create_subprocess_exec — avoids the thread pool entirely."""
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *[str(c) for c in cmd],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=timeout + 2,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode() if proc.returncode == 0 else ""
    except (asyncio.TimeoutError, Exception):
        return ""


# Cached tmux session name — refreshed every 30s to avoid repeated subprocess calls
_tmux_session_cache: tuple = (0.0, "")  # (last_check_time, session_name)
_TMUX_CACHE_TTL = 30.0


def get_tmux_session() -> str:
    """Find the live primary Claude Code session for this container.
    Result is cached for 30s to avoid hammering tmux."""
    global _tmux_session_cache
    now = time.time()
    if now - _tmux_session_cache[0] < _TMUX_CACHE_TTL and _tmux_session_cache[1]:
        return _tmux_session_cache[1]

    def alive(name):
        try:
            subprocess.check_output(["tmux", "has-session", "-t", name],
                                    stderr=subprocess.DEVNULL, timeout=3)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False

    result = None

    # FIRST: Find the currently attached session — mirrors telegram_bridge logic.
    try:
        out = subprocess.check_output(
            ["tmux", "list-sessions", "-F", "#{session_name}:#{session_attached}"],
            stderr=subprocess.DEVNULL, text=True, timeout=3
        )
        for line in out.splitlines():
            parts = line.strip().rsplit(":", 1)
            if len(parts) == 2 and parts[1].strip().isdigit() and int(parts[1].strip()) > 0:
                attached = parts[0].strip()
                if attached:
                    result = attached
                    break
    except Exception:
        pass

    if not result:
        marker = Path.home() / ".current_session"
        if marker.exists():
            name = marker.read_text().strip()
            if name and alive(name):
                result = name

    if not result:
        try:
            out = subprocess.check_output(["tmux", "list-sessions", "-F", "#{session_name}"],
                                          stderr=subprocess.DEVNULL, text=True, timeout=3)
            sessions = out.strip().splitlines()
            for line in sessions:
                if CIV_NAME in line.lower():
                    result = line.strip()
                    break
            if not result and sessions:
                result = sessions[0].strip()
        except Exception:
            pass

    if not result:
        result = f"{CIV_NAME}-primary"

    _tmux_session_cache = (now, result)
    return result


# ---------------------------------------------------------------------------
# Serialized tmux injection queue — prevents race conditions when multiple
# files are uploaded simultaneously (e.g. 6 screenshots at once).
#
# Without this, concurrent api_chat_upload calls fire tmux send-keys in
# parallel. The -l (literal) paste writes interleave in the tmux buffer,
# causing messages to overwrite each other and only SOME files get injected.
#
# Fix: all tmux injections go through this async lock, serialized with a
# 1.5-second inter-injection delay so Claude processes each one cleanly.
# ---------------------------------------------------------------------------
_tmux_inject_lock = None  # type: asyncio.Lock | None


def _get_tmux_inject_lock():
    """Lazy-init the injection lock (must be created inside running event loop)."""
    global _tmux_inject_lock
    if _tmux_inject_lock is None:
        _tmux_inject_lock = asyncio.Lock()
    return _tmux_inject_lock


_DEBOUNCE_WINDOW_S = 2.5  # seconds to wait for more uploads before flushing

_upload_batch: list = []          # list of dicts: {original_name, portal_copy_path, is_image, caption}
_upload_batch_task = None         # asyncio.Task handle for the pending flush


async def _flush_upload_batch():
    """Wait for the debounce window, then inject ONE combined notification."""
    global _upload_batch, _upload_batch_task
    await asyncio.sleep(_DEBOUNCE_WINDOW_S)

    batch = _upload_batch[:]
    _upload_batch = []
    _upload_batch_task = None

    if not batch:
        return

    if len(batch) == 1:
        item = batch[0]
        parts = [f"[Portal Upload from {HUMAN_NAME}] File saved to: {item['portal_copy_path']}"]
        if item["caption"]:
            parts.append(f"INSTRUCTIONS from {HUMAN_NAME}: {item['caption']}")
        if item["is_image"]:
            parts.append(f"[Image: {item['original_name']} — USE Read tool on {item['portal_copy_path']} TO VIEW]")
        notification = " ".join(parts)
    else:
        file_count = len(batch)
        file_names = ", ".join(f["original_name"] for f in batch)
        image_paths = [str(f["portal_copy_path"]) for f in batch if f["is_image"]]
        non_image_paths = [str(f["portal_copy_path"]) for f in batch if not f["is_image"]]

        parts = [f"[Portal Upload from {HUMAN_NAME}] {file_count} files saved: {file_names}"]

        shared_caption = next((f["caption"] for f in batch if f["caption"]), "")
        if shared_caption:
            parts.append(f"INSTRUCTIONS from {HUMAN_NAME}: {shared_caption}")

        if non_image_paths:
            parts.append(f"Files: {', '.join(non_image_paths)}")

        if image_paths:
            parts.append(
                f"Images ({len(image_paths)}): {', '.join(image_paths)}"
                f" — USE Read tool on each path TO VIEW"
            )

        notification = " ".join(parts)

    await _inject_into_tmux_serialized(notification)


def _schedule_upload_batch_item(original_name, portal_copy_path, is_image, caption):
    """Add one upload to the debounce batch and (re)start the flush timer."""
    global _upload_batch, _upload_batch_task

    _upload_batch.append({
        "original_name": original_name,
        "portal_copy_path": portal_copy_path,
        "is_image": is_image,
        "caption": caption,
    })

    if _upload_batch_task is not None and not _upload_batch_task.done():
        _upload_batch_task.cancel()

    _upload_batch_task = asyncio.ensure_future(_flush_upload_batch())


async def _inject_into_tmux_serialized(notification):
    """Inject a notification into the active tmux session, serialized via lock.

    Returns True if injection succeeded, False otherwise.
    Each injection is followed by a 1.5s sleep INSIDE the lock so rapid
    multi-file uploads are spaced out — Claude gets time to read each one
    before the next arrives.

    Uses the same 5x Enter retry pattern as api_chat_send to ensure the
    message executes even when Claude Code is busy with tool calls or
    generation (single Enter is insufficient in that state).
    """
    lock = _get_tmux_inject_lock()
    async with lock:
        session = get_tmux_session()
        try:
            # Leading newline clears any partial input already in the tmux buffer
            await _run_subprocess_async(
                ["tmux", "send-keys", "-t", session, "-l", f"\n{notification}"],
                timeout=5, check=True,
            )
            await _run_subprocess_async(
                ["tmux", "send-keys", "-t", session, "Enter"],
                timeout=5, check=True,
            )
            # 5x Enter retries — ensures Claude processes the message even if
            # busy with tool calls or generation at the moment of injection.
            # Spaced 0.5s apart; runs outside the lock so it does not block
            # the next queued injection.
            async def _retry_enters_upload():
                for _ in range(5):
                    await asyncio.sleep(0.5)
                    await _run_subprocess_async(
                        ["tmux", "send-keys", "-t", session, "Enter"],
                        timeout=3,
                    )
            _fire_and_forget(_retry_enters_upload())
            # Give Claude time to start processing before next injection arrives.
            # 1.5s is enough for Claude to register the message without being overwhelmed.
            await asyncio.sleep(1.5)
            return True
        except Exception:
            return False


def _find_current_session_id():
    """Find the current Claude Code session ID from history.jsonl."""
    try:
        if not HISTORY_FILE.exists():
            return None
        with HISTORY_FILE.open("r") as f:
            f.seek(0, 2)
            length = f.tell()
            window = min(16384, length)
            f.seek(max(0, length - window))
            lines = f.read().splitlines()
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                proj = entry.get("project", "")
                if proj and (CIV_NAME in proj or str(Path.home()) in proj):
                    return entry.get("sessionId")
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return None


_project_jsonl_cache: tuple = (0.0, [])  # (last_scan_time, results)
_PROJECT_JSONL_CACHE_TTL = 30.0  # Re-scan filesystem at most every 30 seconds

def _find_all_project_jsonl():
    """Find all JSONL session files across ALL project directories, sorted by mtime descending.
    Cached for 30s to avoid hammering the filesystem on every WebSocket poll."""
    global _project_jsonl_cache
    now = time.time()
    if now - _project_jsonl_cache[0] < _PROJECT_JSONL_CACHE_TTL and _project_jsonl_cache[1]:
        return _project_jsonl_cache[1]

    all_logs = []
    try:
        if not _PROJECTS_DIR.exists():
            return []
        for proj_dir in _PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            for jf in proj_dir.glob("*.jsonl"):
                try:
                    all_logs.append((jf.stat().st_mtime, jf))
                except OSError:
                    continue
        all_logs.sort(key=lambda x: x[0], reverse=True)
    except Exception:
        pass
    result = [p for _, p in all_logs]
    _project_jsonl_cache = (now, result)
    return result


def _get_all_session_log_paths(max_files=3):
    """Get paths to recent JSONL session logs across ALL project directories, ordered oldest-first.
    Reduced from 10 to 3 files for performance — parsing 10x 50-97MB files every 0.8s was burning 66% CPU."""
    logs = _find_all_project_jsonl()
    return list(reversed(logs[:max_files]))


def _despace(text):
    """Collapse spaced-out text like 'H  e  l  l  o' back to 'Hello'.
    Some older JSONL sessions store text with spaces between every character."""
    if not text or len(text) < 6:
        return text
    # Check if text follows the pattern: char, spaces, char, spaces...
    # Sample first 40 chars to detect the pattern
    sample = text[:40]
    # Pattern: single non-space char followed by 1-2 spaces, repeating
    spaced_chars = 0
    i = 0
    while i < len(sample):
        if i + 1 < len(sample) and sample[i] != " " and sample[i + 1] == " ":
            spaced_chars += 1
            i += 1
            while i < len(sample) and sample[i] == " ":
                i += 1
        else:
            i += 1
    # If >60% of non-space chars are followed by spaces, it's spaced text
    non_space = sum(1 for c in sample if c != " ")
    if non_space > 0 and spaced_chars / non_space > 0.6:
        # Collapse: take every non-space char, but preserve intentional word gaps
        result = []
        i = 0
        while i < len(text):
            if text[i] != " ":
                result.append(text[i])
                i += 1
                # Skip the inter-character spaces (1-2 spaces)
                spaces = 0
                while i < len(text) and text[i] == " ":
                    spaces += 1
                    i += 1
                # 3+ spaces likely means intentional word boundary
                if spaces >= 3:
                    result.append(" ")
            else:
                i += 1
        return "".join(result)
    return text


def _is_real_user_message(text):
    """Check if a user message is a real human message (not system/teammate noise)."""
    if not text or len(text) < 2:
        return False
    # Telegram messages from user - always real
    if "[TELEGRAM" in text:
        return True
    # Portal-sent messages (stored in portal chat log)
    if text.startswith("[PORTAL]"):
        return True
    # Filter out noise
    noise_markers = [
        "<teammate-message", "<system-reminder", "system-reminder",
        "Base directory for this skill", "teammate_id=",
        "<tool_result", "<function_calls", "hook success",
        "Session Ledger", "MEMORY INJECTION", "<task-notification",
        "[Image: source:", "PHOTO saved to:",
        "This session is being continued from a previous",
        "Called the Read tool", "Called the Bash tool",
        "Called the Write tool", "Called the Glob tool",
        "Called the Grep tool", "Result of calling",
        "[from-ACG]",                  # Cross-CIV system messages
        "Context restored",
        "Summary:  ",                  # Agent task summaries
        "` regex", "` sed", "| sed",   # Code snippets leaking as messages
        "re.search(r'", "re.DOTALL",
        "<command-name>", "<command-message>",  # CLI commands
        "<command-args>", "<local-command",
        "local-command-caveat", "local-command-stdout",
        "Compacted (ctrl+o",           # Compaction messages
        "&& [ -x ", "| cut -d",        # Shell code fragments
        "[portal",                     # Portal messages from session JSONL (already in portal-chat.jsonl)
    ]
    for marker in noise_markers:
        if marker in text[:300]:
            return False
    # Skip messages that look like code/config (too many special chars)
    special = sum(1 for c in text[:200] if c in '{}[]|\\`$()#')
    if len(text) < 200 and special > len(text) * 0.15:
        return False
    return True


def _clean_user_text(text):
    """Clean up user message text for display."""
    # Strip Telegram prefix for cleaner display
    if "[TELEGRAM" in text:
        # Format: [TELEGRAM private:NNN from @Username] actual message
        idx = text.find("]")
        if idx > 0:
            return text[idx + 1:].strip()
    # Strip portal injection prefixes (case-insensitive, handles both [portal] and [portal-react])
    # These are added by api_chat_send before tmux injection: "[portal] message" or "[portal-react] message"
    # The session JSONL records the tagged version, so we must strip the prefix for clean display.
    cleaned = re.sub(r'^\[portal(?:-react)?\]\s*', '', text, flags=re.IGNORECASE)
    if cleaned != text:
        return cleaned
    return text


def _is_real_assistant_message(text):
    """Check if an assistant message is substantive (not just tool calls or noise)."""
    if not text or len(text) < 10:
        return False
    stripped = text.strip()
    # Reject short non-alphanumeric noise (pipes, brackets, stray chars)
    if len(stripped) <= 3 and not any(c.isalnum() for c in stripped):
        return False
    return True


_jsonl_cache: dict = {}  # path -> (mtime, messages, fsize, last_parse_time)
_TAIL_BYTES = 500_000   # read last 500KB of large files (reduced from 2MB — stability fix 2026-03-14)
_CACHE_MIN_INTERVAL = 10.0  # Don't re-parse any file more than once per 10 seconds (was 3s — CPU stability fix)

# Cache for portal-chat.jsonl — avoids re-reading 8k-line file on every /api/chat/history request
# Tuple: (mtime: float, fsize: int, messages: list)
_portal_chat_cache: tuple = (0.0, 0, [])

# IDs already written to portal-chat.jsonl — prevents duplicate mirror writes
_portal_log_ids: set = set()

# Active WebSocket connections for pushing thinking blocks
_chat_ws_clients: set = set()

# Hashes of thinking blocks already sent — prevents duplicates across reconnects
_sent_thinking_hashes: set = set()


def _trim_portal_chat_log(max_entries=3000):
    """Trim portal-chat.jsonl to last max_entries, deduplicating by ID.
    Prevents unbounded growth. Called periodically in the background."""
    global _portal_chat_cache
    if not PORTAL_CHAT_LOG.exists():
        return
    try:
        entries = []
        with PORTAL_CHAT_LOG.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if len(entries) <= max_entries:
            return  # No trim needed
        # Sort by timestamp, deduplicate, keep last max_entries
        entries.sort(key=lambda m: float(m.get("timestamp", 0) or 0))
        seen: dict = {}
        for i, e in enumerate(entries):
            seen[e.get("id", str(i))] = e
        trimmed = list(seen.values())[-max_entries:]
        # Atomic write
        import tempfile, os
        tmp = PORTAL_CHAT_LOG.parent / f".portal-chat-trim-{os.getpid()}.jsonl"
        with tmp.open("w") as f:
            for e in trimmed:
                f.write(json.dumps(e) + "\n")
        os.replace(tmp, PORTAL_CHAT_LOG)
        # Invalidate cache so next read picks up trimmed version
        _portal_chat_cache = (0.0, 0, [])
        print(f"[portal] Trimmed portal-chat.jsonl: {len(entries)} → {len(trimmed)} entries")
    except Exception as e:
        print(f"[portal] Trim failed: {e}")


def _init_portal_log_ids():
    """Load IDs already in portal-chat.jsonl so we don't re-mirror them."""
    if not PORTAL_CHAT_LOG.exists():
        return
    try:
        with PORTAL_CHAT_LOG.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    mid = entry.get("id")
                    if mid:
                        _portal_log_ids.add(mid)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass


def _mirror_to_portal_log(msg):
    """Write a discovered session message to portal-chat.jsonl so it survives refreshes."""
    mid = msg.get("id")
    if not mid:
        return
    # Guard: never persist noise-only messages to the log (prevents stale pipe/char glitches)
    msg_text = msg.get("text", "").strip()
    if not msg_text or len(msg_text) < 3:
        return
    if len(msg_text) <= 2 and not any(c.isalnum() for c in msg_text):
        return  # Skip stray pipe/bracket/noise artifacts
    if mid in _portal_log_ids:
        # Already mirrored — skip. Overwriting every time was causing 22s+ history loads
        # by rewriting the entire 3.4MB portal-chat.jsonl hundreds of times per request.
        return
    _portal_log_ids.add(mid)
    try:
        with PORTAL_CHAT_LOG.open("a") as f:
            f.write(json.dumps(msg) + "\n")
    except Exception:
        pass


def _overwrite_portal_log_entry(mid: str, updated_msg: dict) -> None:
    """Atomically rewrite portal-chat.jsonl replacing the entry for mid with updated_msg.
    Uses temp-file + rename for crash safety (Fix 4)."""
    if not PORTAL_CHAT_LOG.exists():
        return
    try:
        lines = []
        with PORTAL_CHAT_LOG.open("r") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    lines.append(line)
                    continue
                try:
                    entry = json.loads(stripped)
                    if entry.get("id") == mid:
                        lines.append(json.dumps(updated_msg) + "\n")
                    else:
                        lines.append(line)
                except json.JSONDecodeError:
                    lines.append(line)
        tmp = PORTAL_CHAT_LOG.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(lines))
        tmp.replace(PORTAL_CHAT_LOG)
    except Exception:
        pass


def _parse_jsonl_messages_from_file(log_path):
    """Parse a single JSONL log into clean chat messages.
    Tail-reads large files and caches by mtime for fast repeated calls."""
    messages = []
    if not log_path or not log_path.exists():
        return messages

    try:
        stat = log_path.stat()
        mtime = stat.st_mtime
        fsize = stat.st_size
        cached = _jsonl_cache.get(str(log_path))
        # Cache key includes BOTH mtime AND file size to catch writes within same second
        if cached and cached[0] == mtime and cached[2] == fsize:
            return cached[1]
        # Rate-limit re-parsing: even if file changed, don't re-parse more often than _CACHE_MIN_INTERVAL
        # This prevents CPU spin on large actively-growing JSONL files (70MB+ during long sessions)
        if cached and len(cached) >= 4 and (time.time() - cached[3]) < _CACHE_MIN_INTERVAL:
            return cached[1]

        # Read only the tail of large files to avoid parsing megabytes each poll
        with log_path.open("rb") as fb:
            if stat.st_size > _TAIL_BYTES:
                fb.seek(-_TAIL_BYTES, 2)
                fb.readline()  # skip partial first line
            raw = fb.read()
        lines_iter = raw.decode("utf-8", errors="replace").splitlines()
    except Exception:
        return messages

    try:
        for line in lines_iter:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg = entry.get("message", {})
                role = msg.get("role", entry.get("type", ""))

                if role not in ("user", "assistant"):
                    continue

                content_blocks = msg.get("content", []) or []
                text_parts = []    # For normal text blocks
                char_parts = []    # For single-character string blocks
                is_char_stream = False
                for block in content_blocks:
                    if isinstance(block, str):
                        # Single char blocks: preserve spaces for word boundaries
                        if len(block) <= 2:  # single chars including '\n'
                            char_parts.append(block)
                            is_char_stream = True
                        else:
                            s = block.strip()
                            if s:
                                text_parts.append(s)
                    elif isinstance(block, dict) and block.get("type") == "text":
                        t = (block.get("text") or "").strip()
                        if t:
                            text_parts.append(t)

                # Build combined text
                if is_char_stream and len(char_parts) > 10:
                    # Join character stream directly (preserves spaces/newlines)
                    combined = "".join(char_parts).strip()
                    # Also append any text blocks
                    if text_parts:
                        combined += "\n\n" + "\n\n".join(text_parts)
                elif text_parts:
                    combined = "\n\n".join(text_parts)
                else:
                    continue

                if not combined or len(combined) < 2:
                    continue

                # Collapse spaced-out text from older sessions
                combined = _despace(combined)

                # Filter based on role
                if role == "user":
                    if not _is_real_user_message(combined):
                        continue
                    combined = _clean_user_text(combined)
                elif role == "assistant":
                    if not _is_real_assistant_message(combined):
                        continue

                ts = entry.get("timestamp")
                if isinstance(ts, (int, float)):
                    ts = ts / 1000  # ms to seconds
                elif isinstance(ts, str):
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        ts = dt.timestamp()
                    except (ValueError, AttributeError):
                        ts = time.time()
                else:
                    ts = time.time()

                messages.append({
                    "role": role,
                    "text": combined,
                    "timestamp": int(ts),
                    "id": entry.get("uuid", f"msg-{log_path.stem[:8]}-{len(messages)}")
                })
    except Exception:
        pass

    _jsonl_cache[str(log_path)] = (mtime, messages, stat.st_size, time.time())
    return messages


def _load_portal_messages():
    """Load messages sent via the portal chat, filtering out noise.
    Uses mtime+size cache to avoid re-reading 8k+ line file on every request (was 75ms/call)."""
    global _portal_chat_cache
    messages = []
    if not PORTAL_CHAT_LOG.exists():
        return messages
    try:
        stat = PORTAL_CHAT_LOG.stat()
        mtime = stat.st_mtime
        fsize = stat.st_size
        cached_mtime, cached_fsize, cached_msgs = _portal_chat_cache
        # Cache hit: file unchanged since last read
        if mtime == cached_mtime and fsize == cached_fsize and cached_msgs:
            return cached_msgs
        # Cache miss: re-read file
        # Use errors='replace' to handle surrogate chars that break UTF-8 serialization
        with PORTAL_CHAT_LOG.open("r", errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    # Filter noise from portal log (stray pipes, single chars, etc.)
                    msg_text = entry.get("text", "").strip()
                    if not msg_text:
                        continue
                    if len(msg_text) <= 2 and not any(c.isalnum() for c in msg_text):
                        continue  # Skip stray pipe/bracket/noise artifacts
                    messages.append(entry)
                except json.JSONDecodeError:
                    continue
        # Update cache
        _portal_chat_cache = (mtime, fsize, messages)
    except Exception:
        pass
    return messages


def _save_portal_message(text, role="user"):
    """Save a message sent via the portal."""
    entry = {
        "role": role,
        "text": text,
        "timestamp": int(time.time()),
        "id": f"portal-{int(time.time() * 1000)}-{secrets.token_hex(4)}",
    }
    try:
        with PORTAL_CHAT_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        _portal_log_ids.add(entry["id"])  # Prevent _mirror_to_portal_log from double-writing
    except Exception:
        pass
    return entry


def _parse_all_messages(last_n=100):
    """Parse messages across all recent session logs + portal log."""
    session_msgs = []
    portal_msgs = []

    # JSONL session logs -- authoritative source for message text (Fix 3)
    # Uses tail-read (last 500KB) + 10s cache — safe even for 138MB files.
    # The CPU killer was /api/context reading the FULL file, not this parser.
    # Fix (2026-03-20): Read top 3 files instead of 1 — subagent JSONL files
    # (BOOP, ST# dispatches, etc.) frequently become more recently modified than
    # the primary conversation JSONL, causing the portal to lose the main chat
    # when max_files=1 picks the subagent file instead of the real session.
    for log_path in _get_all_session_log_paths(max_files=3):
        session_msgs.extend(_parse_jsonl_messages_from_file(log_path))

    # Portal-sent messages
    portal_msgs.extend(_load_portal_messages())

    # Tag by source so dedup can prefer session JSONL over portal-chat.jsonl (Fix 3)
    for m in session_msgs:
        m['_src'] = 'session'
    for m in portal_msgs:
        m['_src'] = 'portal'

    all_messages = session_msgs + portal_msgs

    # Sort by timestamp
    all_messages.sort(key=lambda m: m["timestamp"])

    # Deduplicate by ID -- session JSONL always wins (most complete, authoritative text)
    seen_idx: dict = {}
    for i, m in enumerate(all_messages):
        existing_idx = seen_idx.get(m["id"])
        if existing_idx is None or m['_src'] == 'session':
            seen_idx[m["id"]] = i
    deduped = [all_messages[i] for i in sorted(seen_idx.values())]

    # Secondary dedup: remove portal-log entries whose cleaned text closely matches
    # a session-JSONL entry within a 30s window. This prevents the double-message
    # problem where the same user message appears from both sources (different IDs).
    # Portal log is always subordinate — prefer session JSONL text.
    final: list = []
    session_texts_by_ts: list = []  # list of (ts, text_lower) from session entries
    for m in deduped:
        if m['_src'] == 'session':
            session_texts_by_ts.append((m['timestamp'], (m.get('text') or '').strip().lower()))
            final.append(m)
        else:
            # Portal entry: check if any session entry within 30s has the same text
            m_ts = m['timestamp']
            m_text = (m.get('text') or '').strip().lower()
            is_dup = False
            for s_ts, s_text in session_texts_by_ts:
                if abs(m_ts - s_ts) <= 30 and s_text == m_text:
                    is_dup = True
                    break
            if not is_dup:
                final.append(m)

    # Re-sort after secondary dedup (insertion order is already correct but be safe)
    final.sort(key=lambda m: m['timestamp'])

    return final[-last_n:] if len(final) > last_n else final


def check_auth(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return hmac.compare_digest(auth[7:], BEARER_TOKEN)
    # Allow query param token for WebSocket paths (browsers cannot set headers on WS upgrade)
    # and for /api/chat/uploads/ (inline images in chat rendered via <img src="...?token=">)
    # and for /api/download (browser navigates directly to download URL, cannot set headers)
    path = request.url.path
    if "/ws" in path or "/api/chat/uploads/" in path or "/api/download" in path:
        return hmac.compare_digest(request.query_params.get("token", ""), BEARER_TOKEN)
    return False


# ── User activity tracking (throttled, in-memory gate) ──────────────────────
# We track the portal owner's activity for session counting.
# In-memory cache prevents hitting DB on every single request.
_last_activity_track_time: float = 0.0
_ACTIVITY_TRACK_INTERVAL = 60  # seconds — matches tracking.ACTIVITY_THROTTLE_SECONDS


def _maybe_track_activity() -> None:
    """Fire-and-forget: track portal owner activity (throttled to 1x/min).

    Called on authenticated requests. Uses in-memory gate so we don't
    even open the DB more than once per minute.
    """
    global _last_activity_track_time
    now = time.time()
    if now - _last_activity_track_time < _ACTIVITY_TRACK_INTERVAL:
        return
    _last_activity_track_time = now

    try:
        owner_file = SCRIPT_DIR / "portal_owner.json"
        if owner_file.exists():
            owner = json.loads(owner_file.read_text())
            email = owner.get("human_email", "")
            if email:
                record_activity(str(CLIENTS_DB), email)
    except Exception as e:
        print(f"[tracking] activity track error: {e}")


_login_recorded_this_process: bool = False

def check_auth_and_track(request: Request) -> bool:
    """check_auth + activity tracking + first-request login recording."""
    global _login_recorded_this_process
    authed = check_auth(request)
    if authed:
        # C-1: Record login on the FIRST authenticated request per process lifetime.
        # Since the portal uses a single Bearer token (no per-user sessions),
        # we detect "login" as the first auth'd request after process start.
        if not _login_recorded_this_process:
            _login_recorded_this_process = True
            try:
                owner_file = SCRIPT_DIR / "portal_owner.json"
                if owner_file.exists():
                    owner = json.loads(owner_file.read_text())
                    email = owner.get("human_email", "")
                    if email:
                        record_login(str(CLIENTS_DB), email)
                        print(f"[tracking] recorded login for {email}")
            except Exception as e:
                print(f"[tracking] login record error: {e}")
        _maybe_track_activity()
    return authed


# ── PayPal Webhook Endpoint ─────────────────────────────────────────────────

async def api_webhooks_paypal(request: Request) -> JSONResponse:
    """POST /api/webhooks/paypal -- receive PayPal push notifications.

    No Bearer auth required (PayPal sends these). Basic header validation
    blocks casual spoofing; full signature verification is Phase 2.

    # TODO: Full PayPal webhook signature validation using PayPal API (Phase 2)
    # TODO: Push to Brevo when API key is configured
    """
    # C-3: Basic PayPal header validation -- blocks casual spoofing.
    # Real PayPal webhooks always include these transmission headers.
    transmission_id = request.headers.get("PAYPAL-TRANSMISSION-ID")
    if not transmission_id:
        print("[paypal-webhook] REJECTED: missing PAYPAL-TRANSMISSION-ID header")
        return JSONResponse(
            {"error": "missing PayPal transmission headers"},
            status_code=400,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    event_type = body.get("event_type", "")
    event_id = body.get("id", "")

    if not event_type:
        return JSONResponse({"error": "missing event_type"}, status_code=400)

    db_path = str(CLIENTS_DB)

    # H-5: Idempotency -- skip if this event_id was already processed
    if event_id:
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.execute(
                "SELECT id FROM paypal_webhook_log WHERE event_id = ? AND processed = 1",
                (event_id,),
            )
            already_done = cur.fetchone()
            conn.close()
            if already_done:
                print(f"[paypal-webhook] DUPLICATE event_id={event_id}, skipping")
                return JSONResponse({
                    "status": "duplicate",
                    "event_type": event_type,
                    "processed": False,
                    "detail": "duplicate event",
                })
        except Exception as e:
            print(f"[paypal-webhook] idempotency check error: {e}")
            # Non-fatal -- continue processing

    # Log every event (even unknown types)
    log_id = None
    try:
        log_id = log_webhook_event(db_path, body)
        print(f"[paypal-webhook] Received {event_type} (event_id={event_id}, log_id={log_id})")
    except Exception as e:
        print(f"[paypal-webhook] ERROR logging event: {e}")
        return JSONResponse({"error": "internal error"}, status_code=500)

    # Process known event types
    try:
        result = process_webhook_event(db_path, body)
        print(f"[paypal-webhook] Processed: {result}")

        # H-4: Mark webhook log entry as processed after successful processing
        if result.get("processed") and log_id is not None:
            try:
                conn = sqlite3.connect(db_path)
                conn.execute(
                    "UPDATE paypal_webhook_log SET processed = 1 WHERE id = ?",
                    (log_id,),
                )
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"[paypal-webhook] ERROR marking processed: {e}")
    except Exception as e:
        print(f"[paypal-webhook] ERROR processing event: {e}")
        result = {"processed": False, "event_type": event_type, "detail": str(e)}

    return JSONResponse({
        "status": "received",
        "event_type": event_type,
        "processed": result.get("processed", False),
    })


# ── Tracking Status Endpoint ────────────────────────────────────────────────

async def api_tracking_status(request: Request) -> JSONResponse:
    """GET /api/tracking/status — tracking health dashboard.

    Returns webhook log stats, last events, sync status.
    Bearer auth required.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    db_path = str(CLIENTS_DB)
    result = {"healthy": True, "webhook_log": {}, "tracking_columns": False}

    try:
        import sqlite3 as _sq3
        conn = _sq3.connect(db_path)
        conn.row_factory = _sq3.Row

        # Check tracking columns exist
        cur = conn.execute("PRAGMA table_info(clients)")
        columns = {row[1] for row in cur.fetchall()}
        result["tracking_columns"] = all(
            c in columns for c in ("last_login_at", "login_count", "session_count", "next_billing_date")
        )

        # Webhook log stats
        try:
            cur = conn.execute("SELECT COUNT(*) as total FROM paypal_webhook_log")
            total = cur.fetchone()[0]
            cur = conn.execute(
                "SELECT COUNT(*) as processed FROM paypal_webhook_log WHERE processed = 1"
            )
            processed = cur.fetchone()[0]
            cur = conn.execute(
                "SELECT event_type, received_at FROM paypal_webhook_log "
                "ORDER BY id DESC LIMIT 5"
            )
            recent = [{"event_type": r["event_type"], "received_at": r["received_at"]}
                      for r in cur.fetchall()]
            result["webhook_log"] = {
                "total_events": total,
                "processed_events": processed,
                "recent": recent,
            }
        except Exception:
            result["webhook_log"] = {"error": "paypal_webhook_log table not found"}

        # Clients with tracking data
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM clients WHERE login_count > 0"
            )
            result["clients_with_logins"] = cur.fetchone()[0]
            cur = conn.execute(
                "SELECT COUNT(*) FROM clients WHERE session_count > 0"
            )
            result["clients_with_sessions"] = cur.fetchone()[0]
            cur = conn.execute(
                "SELECT COUNT(*) FROM clients WHERE next_billing_date != ''"
            )
            result["clients_with_renewal_date"] = cur.fetchone()[0]
        except Exception:
            pass

        conn.close()
    except Exception as e:
        result["healthy"] = False
        result["error"] = str(e)

    return JSONResponse(result)


# ---------------------------------------------------------------------------

# ── Favicon ──────────────────────────────────────────────────────────────

async def favicon(request: Request):
    """Serve PureBrain favicon for unified branding across all subdomains."""
    ico = SCRIPT_DIR / "favicon.ico"
    if ico.exists():
        return FileResponse(str(ico), media_type="image/x-icon")
    return Response(status_code=204)

async def favicon_png(request: Request):
    """Serve 32px favicon PNG."""
    png = SCRIPT_DIR / "favicon-32.png"
    if png.exists():
        return FileResponse(str(png), media_type="image/png")
    return Response(status_code=204)

async def apple_touch_icon(request: Request):
    """Serve Apple touch icon."""
    icon = SCRIPT_DIR / "apple-touch-icon.png"
    if icon.exists():
        return FileResponse(str(icon), media_type="image/png")
    return Response(status_code=204)

# Routes
# ---------------------------------------------------------------------------

def _parse_panel_meta(html_content: str) -> dict:
    """Extract panel metadata from HTML comment headers (Flux overlay)."""
    meta = {}
    for line in html_content.split('\n')[:10]:
        m = re.match(r'<!--\s*panel-(\w+):\s*(.+?)\s*-->', line)
        if m:
            meta[m.group(1)] = m.group(2)
    return meta


def _inject_custom_panels(html: str) -> str:
    """Inject custom panels from custom/panels/*.html into the portal HTML (Flux overlay).

    If custom/panels/ does not exist or is empty, returns html unchanged (no-op).
    """
    custom_panels_dir = SCRIPT_DIR / "custom" / "panels"
    if not custom_panels_dir.exists():
        return html

    nav_items = []
    panel_html_parts = []
    mobile_items = []

    for panel_file in sorted(custom_panels_dir.glob("*.html")):
        try:
            panel_content = panel_file.read_text()
        except Exception as _e:
            print(f"[portal-custom] WARNING: could not read panel file {panel_file}: {_e}")
            continue

        meta = _parse_panel_meta(panel_content)
        if not meta.get("id"):
            print(f"[portal-custom] WARNING: panel file {panel_file.name} missing panel-id metadata, skipping")
            continue

        panel_id = escape(meta["id"], quote=True)
        panel_label = escape(meta.get("label", panel_id), quote=True)
        panel_icon = meta.get("icon", "&#x2726;")  # icons are HTML entities, keep as-is
        panel_tooltip = escape(meta.get("tooltip", ""), quote=True)

        nav_items.append(
            f'    <div class="nav-item" data-panel="{panel_id}" '
            f'data-tooltip="{panel_tooltip}">'
            f'<span class="nav-icon">{panel_icon}</span>'
            f'{panel_label}</div>'
        )
        panel_html_parts.append(
            f'  <div class="panel" id="panel-{panel_id}">{panel_content}</div>'
        )
        mobile_items.append(
            f'    <div class="tab-menu-item" data-panel="{panel_id}" '
            f'onclick="selectMobileMenuItem(\'{panel_id}\')">'
            f'<span style="margin-right:10px;">{panel_icon}</span>'
            f'{panel_label}</div>'
        )

        print(f"[portal-custom] Injecting panel: {panel_id} ({panel_label})")

    if not nav_items:
        return html

    markers_found = 0
    markers_expected = 3

    # Inject nav items among other panel nav items (before <!-- /nav-panels --> marker)
    if '<!-- /nav-panels -->' in html:
        nav_inject = '\n'.join(nav_items)
        html = html.replace(
            '    <!-- /nav-panels -->',
            f'{nav_inject}\n    <!-- /nav-panels -->',
            1
        )
        markers_found += 1
    else:
        print("[portal-custom] WARNING: <!-- /nav-panels --> marker not found — custom nav items not injected")

    # Inject panel divs inside .content area, before <!-- /panels --> marker
    if '<!-- /panels -->' in html:
        panels_inject = '\n'.join(panel_html_parts)
        html = html.replace(
            '<!-- /panels -->',
            f'{panels_inject}\n  <!-- /panels -->',
            1
        )
        markers_found += 1
    else:
        print("[portal-custom] WARNING: <!-- /panels --> marker not found — custom panels not injected")

    # Inject mobile menu items inside #mobile-more-menu, before its closing marker
    if '<!-- /mobile-menu-items -->' in html:
        mobile_inject = '\n'.join(mobile_items)
        html = html.replace(
            '    <!-- /mobile-menu-items -->',
            f'{mobile_inject}\n    <!-- /mobile-menu-items -->',
            1
        )
        markers_found += 1
    else:
        print("[portal-custom] WARNING: <!-- /mobile-menu-items --> marker not found — mobile items not injected")

    if markers_found < markers_expected:
        print(f"[portal-custom] WARNING: Only {markers_found}/{markers_expected} injection markers found — some custom panels may not display")

    return html

async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "civ": CIV_NAME, "uptime": int(time.time() - START_TIME)})


async def index(request: Request) -> Response:
    if PORTAL_PB_HTML.exists():
        html = PORTAL_PB_HTML.read_text()
        html = _inject_custom_panels(html)
        resp = Response(html, media_type="text/html")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp
    if PORTAL_HTML.exists():
        return FileResponse(str(PORTAL_HTML), media_type="text/html")
    return Response("<h1>Portal HTML not found</h1>", media_type="text/html", status_code=503)


async def index_pb(request: Request) -> Response:
    """Serve PureBrain-styled portal at /pb path."""
    if not PORTAL_PB_HTML.exists():
        return Response("<h1>PB Portal not found</h1>", media_type="text/html", status_code=503)
    html = PORTAL_PB_HTML.read_text()
    html = _inject_custom_panels(html)
    resp = Response(html, media_type="text/html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


async def index_react(request: Request) -> Response:
    """Serve React portal at /react path."""
    react_index = REACT_DIST / "index.html"
    if react_index.exists():
        return FileResponse(str(react_index), media_type="text/html")
    return Response("<h1>React Portal not found — run npm run build in react-portal/</h1>",
                    media_type="text/html", status_code=503)


async def api_status(request: Request) -> JSONResponse:
    if not check_auth_and_track(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    session = get_tmux_session()
    tmux_alive = False
    r = await _run_subprocess_async(["tmux", "has-session", "-t", session])
    if r is not None and r.returncode == 0:
        tmux_alive = True

    claude_running = False
    out = await _run_subprocess_output(["pgrep", "-f", "claude"])
    if out and out.strip():
        claude_running = True

    tg_running = False
    out = await _run_subprocess_output(["pgrep", "-f", "telegram"])
    if out and out.strip():
        tg_running = True

    ctx_pct = None
    try:
        ctx_file = Path("/tmp/claude_context_used.txt")
        if ctx_file.exists():
            ctx_pct = float(ctx_file.read_text().strip())
    except Exception:
        pass

    return JSONResponse({
        "civ": CIV_NAME, "uptime": int(time.time() - START_TIME),
        "tmux_session": session, "tmux_alive": tmux_alive,
        "claude_running": claude_running, "tg_bot_running": tg_running,
        "ctx_pct": ctx_pct,
        "timestamp": int(time.time()),
        "version": PORTAL_VERSION,
    })


async def api_release_notes(request: Request) -> JSONResponse:
    """Return release notes and current version."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        data = json.loads(RELEASE_NOTES_FILE.read_text())
        data["current_version"] = PORTAL_VERSION
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"current_version": PORTAL_VERSION, "releases": [], "error": str(e)})


async def api_chat_history(request: Request) -> JSONResponse:
    """Return recent chat messages from JSONL session log."""
    if not check_auth_and_track(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    last_n = int(request.query_params.get("last", "100"))
    last_n = min(last_n, 500)

    messages = _parse_all_messages(last_n=last_n)

    # Note: mirroring moved to websocket loop only — doing it here caused 22s+ load times
    # by rewriting portal-chat.jsonl hundreds of times per history request.

    # Sanitize messages to remove surrogate characters that break UTF-8 encoding
    def _sanitize(obj):
        if isinstance(obj, str):
            return obj.encode('utf-8', errors='replace').decode('utf-8')
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]
        return obj

    messages = _sanitize(messages)
    return JSONResponse({"messages": messages, "count": len(messages), "timestamp": int(time.time())})


async def api_chat_send(request: Request) -> JSONResponse:
    """Inject a message into the tmux session. Response comes via /api/chat/stream or history."""
    if not check_auth_and_track(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        message = str(body.get("message", "")).strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    # Save to portal chat log for history
    # Return the saved entry's ID so the client can pre-register it in knownMsgIds,
    # preventing the WS poll-loop echo from rendering the message a second time.
    saved_entry = _save_portal_message(message, role="user")
    msg_id = saved_entry["id"]

    # Tag injection source so tmux pane shows where input came from
    host = request.headers.get("referer", "")
    if "react" in host:
        tagged = f"[portal-react] {message}"
    else:
        tagged = f"[portal] {message}"

    session = get_tmux_session()
    print(f"[portal] DEBUG api_chat_send: session={session} msg_len={len(message)} tagged_len={len(tagged)} referer={request.headers.get('referer','none')[:50]} client={request.client.host if request.client else 'unknown'}")
    try:
        # For long messages, write to a temp file and use load-buffer instead of send-keys -l
        # tmux send-keys -l has issues with special characters and very long strings
        import tempfile
        # Encode with surrogatepass to handle emoji surrogate pairs from browser JS
        clean_tagged = tagged.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='replace')
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, prefix='portal_msg_', encoding='utf-8') as tf:
            tf.write(f"\n{clean_tagged}")
            tf_path = tf.name
        # Use tmux load-buffer + paste-buffer for reliable injection
        r = await _run_subprocess_async(["tmux", "load-buffer", "-b", "portal_paste", tf_path], check=True)
        if r is None:
            print(f"[portal] ERROR load-buffer returned None for {tf_path}")
            # Fallback to send-keys
            r = await _run_subprocess_async(["tmux", "send-keys", "-t", session, "-l", f"\n{tagged}"], check=True, timeout=10)
            if r is None:
                print(f"[portal] ERROR send-keys fallback ALSO failed for session={session}")
                try:
                    os.unlink(tf_path)
                except OSError:
                    pass
                return JSONResponse({"error": f"tmux injection failed for session {session}"}, status_code=500)
        else:
            r2 = await _run_subprocess_async(["tmux", "paste-buffer", "-b", "portal_paste", "-t", session], check=True)
            if r2 is None:
                print(f"[portal] ERROR paste-buffer returned None for session={session}")
        try:
            os.unlink(tf_path)
        except OSError:
            pass
        await _run_subprocess_async(["tmux", "send-keys", "-t", session, "Enter"], check=True)
        # 5x Enter retries (matches Telegram bridge pattern) — ensures Claude
        # processes the message even if busy with tool calls or generation
        async def _retry_enters():
            for _ in range(5):
                await asyncio.sleep(0.5)
                await _run_subprocess_async(["tmux", "send-keys", "-t", session, "Enter"])
        _fire_and_forget(_retry_enters())
        print(f"[portal] DEBUG api_chat_send: SUCCESS msg_id={msg_id}")
        # Return msg_id so the client pre-registers it and WS echo is suppressed
        return JSONResponse({"status": "sent", "timestamp": int(time.time()), "msg_id": msg_id})
    except Exception as e:
        print(f"[portal] ERROR api_chat_send exception: {type(e).__name__}: {e}")
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


async def api_notify(request: Request) -> JSONResponse:
    """Save a system notification to portal chat (role=assistant, no tmux injection)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        message = str(body.get("message", "")).strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    entry = _save_portal_message(message, role="assistant")

    # Push immediately to all connected WS clients — bypasses 0.8s poll delay
    if _chat_ws_clients and entry:
        import asyncio as _asyncio
        _asyncio.create_task(_push_message_to_clients(entry))

    return JSONResponse({"status": "saved", "id": entry["id"], "timestamp": entry["timestamp"]})


async def ws_chat(websocket: WebSocket) -> None:
    """Stream new chat messages via WebSocket. Polls JSONL log for new entries."""
    token = websocket.query_params.get("token", "")
    if token != BEARER_TOKEN:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    _chat_ws_clients.add(websocket)
    seen_texts: dict[str, int] = {}   # id -> len(text) of last sent version
    first_seen: dict[str, float] = {} # id -> time.time() when first noticed (Fix 2)
    stable_counts: dict[str, int] = {}# id -> consecutive polls with same length (Fix 1)
    # Fix 5 (truncation): track IDs where we already sent the final stable version.
    # Prevents re-sending indefinitely once the complete message is delivered.
    stable_sent: set = set()

    # Register initial batch of recent messages as "seen" to avoid re-sending old messages.
    # Only NEW messages (arriving after connect) will be pushed via the poll loop below.
    messages = _parse_all_messages(last_n=200)
    for msg in messages:
        seen_texts[msg["id"]] = len(msg.get("text", ""))
        stable_sent.add(msg["id"])  # existing messages already complete — skip final-send

    try:
        while True:
            messages = _parse_all_messages(last_n=200)
            for msg in messages:
                msg_id = msg["id"]
                msg_len = len(msg.get("text", ""))
                prev_len = seen_texts.get(msg_id, -1)

                # Fix 2: skip brand-new messages on their very first poll (wait ~0.8s)
                if msg_id not in first_seen:
                    first_seen[msg_id] = time.time()
                    continue  # skip first poll cycle for all new messages

                msg_age = time.time() - first_seen[msg_id]

                # Check if text is still growing
                if prev_len >= 0 and msg_len == prev_len:
                    # Fix 1: stable — increment counter
                    stable_counts[msg_id] = stable_counts.get(msg_id, 0) + 1
                else:
                    # Text changed (new or grown) — reset stability counter
                    stable_counts[msg_id] = 0

                # ── Send path ──────────────────────────────────────────────────────
                # Noise guard (shared by all send paths below)
                _ws_text = msg.get("text", "").strip()
                _is_noise = (not _ws_text or len(_ws_text) < 3 or
                             (len(_ws_text) <= 2 and not any(c.isalnum() for c in _ws_text)))

                if _is_noise:
                    continue

                is_stable = stable_counts.get(msg_id, 0) >= 2

                if prev_len < 0 or (msg_len > prev_len + 20 and msg_age > 0.8):
                    # NEW message or text grew significantly — send current version
                    seen_texts[msg_id] = msg_len
                    # Persist to portal log once stable
                    if is_stable and msg_id not in _portal_log_ids:
                        _mirror_to_portal_log(msg)
                    await websocket.send_text(json.dumps(msg))

                elif is_stable and msg_id not in stable_sent:
                    # Fix 5 (truncation root cause):
                    # Message stopped growing. We may have sent a partial version earlier
                    # (when the growth threshold was met but the message wasn't complete).
                    # Re-send the NOW-COMPLETE text so the client can update its bubble
                    # in-place via the knownMsgIds path. This is the definitive final send.
                    # Only fires ONCE per message (stable_sent prevents re-send every poll).
                    stable_sent.add(msg_id)
                    # Persist complete version to portal log
                    if msg_id not in _portal_log_ids:
                        _mirror_to_portal_log(msg)
                    else:
                        # Overwrite any partial version already persisted
                        _overwrite_portal_log_entry(msg_id, msg)
                    # Only re-send if we previously sent a partial version (prev_len >= 0)
                    # and the final text is longer. No-op for brand-new stable messages
                    # that were already sent complete on the first pass.
                    if prev_len >= 0 and msg_len != prev_len:
                        seen_texts[msg_id] = msg_len
                        await websocket.send_text(json.dumps(msg))

                elif is_stable and msg_id not in _portal_log_ids:
                    # Fix 1: message stopped growing — persist now even if below growth threshold
                    _mirror_to_portal_log(msg)

            await asyncio.sleep(1.5)  # Poll interval — increased from 0.8s to reduce CPU (still near-real-time)
            # Server-side keepalive ping every 20s to prevent Cloudflare/client 30s stale detection
            _now = time.time()
            if not hasattr(websocket, '_last_ping'):
                websocket._last_ping = _now
            if _now - websocket._last_ping >= 20:
                try:
                    await websocket.send_text(json.dumps({"type": "ping", "ts": int(_now)}))
                    websocket._last_ping = _now
                except Exception:
                    break
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _chat_ws_clients.discard(websocket)


async def api_chat_upload(request: Request) -> JSONResponse:
    """Accept a file upload, save to UPLOADS_DIR + docs/from-telegram/, log to portal chat, inject tmux notification."""
    if not check_auth_and_track(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        form = await request.form()
        uploaded = form.get("file")
        if not uploaded or not hasattr(uploaded, "read"):
            return JSONResponse({"error": "no file"}, status_code=400)

        caption = str(form.get("caption", "")).strip()

        content = await uploaded.read()
        if len(content) > UPLOAD_MAX_BYTES:
            return JSONResponse({"error": "file too large (max 50 MB)"}, status_code=413)

        original_name = getattr(uploaded, "filename", None) or "upload"
        # Sanitize: keep alphanumerics, dots, dashes, underscores
        safe_name = "".join(c for c in original_name if c.isalnum() or c in "._-") or "upload"
        timestamp_ms = int(time.time() * 1000)
        stored_name = f"{timestamp_ms}_{secrets.token_hex(4)}_{safe_name}"
        dest = UPLOADS_DIR / stored_name
        dest.write_bytes(content)

        # Also save a named copy to portal_uploads/from-portal/ for easy reference
        from_portal_dir = UPLOADS_DIR / "from-portal"
        from_portal_dir.mkdir(parents=True, exist_ok=True)
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        portal_copy_name = f"portal_{timestamp_str}_{safe_name}"
        portal_copy_path = from_portal_dir / portal_copy_name
        portal_copy_path.write_bytes(content)

        # Detect if this is an image
        is_image = safe_name.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp'))

        # Save ONE combined user message to portal chat log (image + caption together)
        # Include stored_name so frontend can render inline image via /api/chat/uploads/
        chat_text = f"[Image: {stored_name}]" if is_image else f"[File: {stored_name}]"
        if caption:
            chat_text += f"\n{caption}"
        user_entry = _save_portal_message(chat_text, role="user")

        # Inject notification into AI's tmux session via debounced batch.
        # Multiple files within _DEBOUNCE_WINDOW_S (2.5s) are combined into
        # ONE tmux notification instead of N separate messages (saves tokens).
        _schedule_upload_batch_item(original_name, str(portal_copy_path), is_image, caption)
        tmux_ok = True  # Assume success for ack message (file IS saved regardless)

        # Auto-acknowledge in portal chat so user sees confirmation immediately
        ack_parts = [f"Received your file: {original_name}"]
        if is_image:
            ack_parts.append("(image — viewing now)")
        if caption:
            ack_parts.append(f'Instructions noted: "{caption}"')
        if tmux_ok:
            ack_parts.append("Processing...")
        else:
            ack_parts.append("(tmux injection failed — will check docs/from-telegram/ manually)")
        ack_text = " ".join(ack_parts)
        ack_entry = _save_portal_message(ack_text, role="assistant")

        return JSONResponse({
            "ok": True,
            "filename": stored_name,
            "original": original_name,
            "path": str(dest),
            "copy_path": str(portal_copy_path),
            "size": len(content),
            "ack": ack_text,
            "user_msg_id": user_entry["id"],
            "ack_msg_id": ack_entry["id"],
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_chat_serve_upload(request: Request) -> Response:
    """Serve an uploaded file. Token auth via query param or Bearer header."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    filename = request.path_params.get("filename", "")
    # Prevent path traversal
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    filepath = UPLOADS_DIR / filename
    if not filepath.exists() or not filepath.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(filepath))


async def api_download(request: Request) -> Response:
    """Serve a file download from whitelisted directories."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    filepath_str = request.query_params.get("path", "")
    if not filepath_str:
        return JSONResponse({"error": "missing 'path' query parameter"}, status_code=400)
    try:
        filepath = Path(filepath_str).resolve()
    except Exception:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    # Security: reject path traversal and check whitelist
    if ".." in filepath_str:
        return JSONResponse({"error": "path traversal not allowed"}, status_code=403)
    allowed = any(
        filepath == d or d in filepath.parents
        for d in DOWNLOAD_ALLOWED_DIRS
    )
    if not allowed:
        return JSONResponse({"error": f"path not in allowed directories"}, status_code=403)
    if not filepath.exists() or not filepath.is_file():
        return JSONResponse({"error": "file not found"}, status_code=404)
    return FileResponse(str(filepath), filename=filepath.name)


async def api_download_list(request: Request) -> JSONResponse:
    """List files in an allowed directory."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    dir_str = request.query_params.get("dir", "")
    if not dir_str:
        # Return list of allowed base directories
        return JSONResponse({
            "dirs": [str(d) for d in DOWNLOAD_ALLOWED_DIRS if d.exists()]
        })
    try:
        dirpath = Path(dir_str).resolve()
    except Exception:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    allowed = any(
        dirpath == d or d in dirpath.parents
        for d in DOWNLOAD_ALLOWED_DIRS
    )
    if not allowed:
        return JSONResponse({"error": "directory not in allowed list"}, status_code=403)
    if not dirpath.exists() or not dirpath.is_dir():
        return JSONResponse({"error": "directory not found"}, status_code=404)
    items = []
    for item in sorted(dirpath.iterdir()):
        items.append({
            "name": item.name,
            "path": str(item),
            "is_dir": item.is_dir(),
            "size": item.stat().st_size if item.is_file() else None,
        })
    return JSONResponse({"dir": str(dirpath), "items": items})


# ---------------------------------------------------------------------------
# WhatsApp Bridge Endpoints
# ---------------------------------------------------------------------------

async def api_deliverable(request: Request) -> JSONResponse:
    """Accept a file deliverable from the AI, copy to uploads, post download link to portal chat."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        src_path_str = body.get("path", "").strip()
        display_name = body.get("name", "").strip()
        caption = body.get("message", "").strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if not src_path_str:
        return JSONResponse({"error": "missing 'path'"}, status_code=400)
    src_path = Path(src_path_str).resolve()
    if not src_path.exists() or not src_path.is_file():
        return JSONResponse({"error": f"file not found: {src_path_str}"}, status_code=404)

    # HIGH-003: Restrict file access to allowed directories only
    if not any(str(src_path).startswith(str(d.resolve())) for d in DOWNLOAD_ALLOWED_DIRS):
        return JSONResponse({"error": "path not in allowed directories"}, status_code=403)

    if not display_name:
        display_name = src_path.name
    safe_name = "".join(c for c in display_name if c.isalnum() or c in "._-") or "deliverable"
    stored_name = f"{int(time.time() * 1000)}_{safe_name}"
    dest = UPLOADS_DIR / stored_name
    dest.write_bytes(src_path.read_bytes())

    serve_url = f"/api/chat/uploads/{stored_name}"
    # Use PORTAL_FILE tag format — rendered by portal HTML as styled download card
    lines = []
    if caption:
        lines.append(caption)
    lines.append(f"[PORTAL_FILE:{stored_name}:{display_name}]")
    entry = _save_portal_message("\n\n".join(lines), role="assistant")

    # Push immediately to all connected WS clients — bypasses 0.8s poll delay
    # so file download cards appear live without requiring a page refresh.
    if _chat_ws_clients and entry:
        import asyncio as _asyncio
        _asyncio.create_task(_push_message_to_clients(entry))

    return JSONResponse({"ok": True, "filename": stored_name, "url": serve_url})


async def api_whatsapp_qr(request: Request) -> Response:
    """Serve the WhatsApp QR code PNG image (written by whatsapp-bridge)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    qr_path = UPLOADS_DIR / "whatsapp-qr.png"
    if not qr_path.exists():
        return JSONResponse({"error": "no_qr", "message": "No QR code available"}, status_code=404)
    return FileResponse(str(qr_path), media_type="image/png")


async def api_whatsapp_status(request: Request) -> JSONResponse:
    """Return WhatsApp connection status (written by whatsapp-bridge)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    status_path = UPLOADS_DIR / "whatsapp-status.json"
    if not status_path.exists():
        return JSONResponse({"status": "unknown", "updated": None})
    try:
        data = json.loads(status_path.read_text())
        return JSONResponse(data)
    except Exception:
        return JSONResponse({"status": "error", "updated": None})


_pane_cache: tuple = (0.0, "")  # (last_check_time, pane_id)
_PANE_CACHE_TTL = 10.0


def _find_primary_pane():
    """Find the tmux pane ID running the primary Claude Code instance.
    Scans ALL windows (-s) and prefers the pane where claude is running.
    Result cached for 10s to avoid subprocess calls on every poll."""
    global _pane_cache
    now = time.time()
    if now - _pane_cache[0] < _PANE_CACHE_TTL and _pane_cache[1]:
        return _pane_cache[1]
    session = get_tmux_session()
    try:
        # List all panes across all windows with their current command
        out = subprocess.check_output(
            ["tmux", "list-panes", "-s", "-t", session,
             "-F", "#{pane_id} #{pane_current_command}"],
            stderr=subprocess.DEVNULL, text=True, timeout=3
        )
        panes = [p.strip() for p in out.splitlines() if p.strip()]
        if not panes:
            _pane_cache = (now, session)
            return session
        # Prefer the pane where claude is actually running
        for entry in panes:
            parts = entry.split(None, 1)
            if len(parts) == 2 and "claude" in parts[1].lower():
                _pane_cache = (now, parts[0])
                return parts[0]
        # Fallback to last pane (most recently created window)
        fallback = panes[-1].split(None, 1)[0]
        _pane_cache = (now, fallback)
        return fallback
    except Exception:
        _pane_cache = (now, session)
        return session


async def _find_primary_pane_async():
    """Async version of _find_primary_pane — use from async functions.
    Scans ALL windows (-s) and prefers the pane where claude is running."""
    global _pane_cache
    now = time.time()
    if now - _pane_cache[0] < _PANE_CACHE_TTL and _pane_cache[1]:
        return _pane_cache[1]
    session = get_tmux_session()
    out = await _run_subprocess_output(
        ["tmux", "list-panes", "-s", "-t", session,
         "-F", "#{pane_id} #{pane_current_command}"], timeout=3
    )
    panes = [p.strip() for p in out.splitlines() if p.strip()] if out else []
    if not panes:
        _pane_cache = (now, session)
        return session
    # Prefer the pane where claude is actually running
    for entry in panes:
        parts = entry.split(None, 1)
        if len(parts) == 2 and "claude" in parts[1].lower():
            _pane_cache = (now, parts[0])
            return parts[0]
    # Fallback to last pane (most recently created window)
    fallback = panes[-1].split(None, 1)[0]
    _pane_cache = (now, fallback)
    return fallback


async def ws_terminal(websocket: WebSocket) -> None:
    """Stream tmux pane content via WebSocket. Read-only."""
    token = websocket.query_params.get("token", "")
    if token != BEARER_TOKEN:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    pane_target = await _find_primary_pane_async()
    last_content = ""

    try:
        while True:
            content = await _run_subprocess_output(
                ["tmux", "capture-pane", "-t", pane_target, "-p"], timeout=3
            )
            content = content.strip() if content else "[tmux session not found]"

            if content != last_content:
                await websocket.send_text(content)
                last_content = content

            await asyncio.sleep(1.0)  # Terminal poll — increased from 0.5s to reduce CPU
    except (WebSocketDisconnect, Exception):
        pass


async def api_context(request: Request) -> JSONResponse:
    """Return real context window usage from the latest Claude session JSONL."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        MAX_TOKENS = 870_000  # 1M window minus ~130k reserved for responses/summaries
        logs = _find_all_project_jsonl()
        if not logs:
            return JSONResponse({"input_tokens": 0, "max_tokens": MAX_TOKENS, "pct": 0})

        latest = logs[0]
        input_tokens = 0
        cache_read = 0
        cache_creation = 0

        # Read LAST usage entry only — tail the file instead of reading all 138MB
        # STABILITY FIX 2026-03-14: reading entire file on every poll was burning 64% CPU
        fsize = latest.stat().st_size
        tail_bytes = min(fsize, 200_000)  # last 200KB is plenty to find latest usage
        with open(latest, 'rb') as f:
            f.seek(max(0, fsize - tail_bytes))
            tail_data = f.read().decode('utf-8', errors='replace')
        for line in tail_data.splitlines():
            try:
                entry = json.loads(line)
                usage = entry.get("usage") or entry.get("message", {}).get("usage")
                if usage and isinstance(usage, dict):
                    t = usage.get("input_tokens", 0)
                    if t:
                        input_tokens = t
                        cache_read = usage.get("cache_read_input_tokens", 0)
                        cache_creation = usage.get("cache_creation_input_tokens", 0)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue

        total = input_tokens + cache_read + cache_creation
        pct = round(min(total / MAX_TOKENS * 100, 100), 1)
        return JSONResponse({
            "input_tokens": input_tokens,
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "total_tokens": total,
            "max_tokens": MAX_TOKENS,
            "pct": pct,
            "session_id": latest.stem,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_resume(request: Request) -> JSONResponse:
    """Launch a new Claude instance resuming the most recent conversation session."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        logs = _find_all_project_jsonl()
        if not logs:
            return JSONResponse({"error": "no sessions found"}, status_code=404)
        session_id = logs[0].stem  # UUID filename without .jsonl
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        tmux_session = f"{CIV_NAME}-primary-{timestamp}"
        project_dir = str(Path.home())
        # Kill any stale {civ}-primary-* sessions so prefix-matching stays unambiguous
        try:
            old = await _run_subprocess_output(
                ["tmux", "list-sessions", "-F", "#{session_name}"], timeout=3
            )
            if old:
                for s in old.splitlines():
                    if s.startswith(f"{CIV_NAME}-primary-"):
                        await _run_subprocess_async(["tmux", "kill-session", "-t", s])
        except Exception:
            pass
        # Write session name so portal can track it
        marker = Path.home() / ".current_session"
        marker.write_text(tmux_session)
        claude_cmd = (
            f"claude --model claude-sonnet-4-6 --dangerously-skip-permissions "
            f"--resume {session_id}"
        )
        # Popen is fire-and-forget so we use run_in_executor to avoid blocking
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(_PORTAL_EXECUTOR, lambda: subprocess.Popen(
            ["tmux", "new-session", "-d", "-s", tmux_session, "-c", project_dir, claude_cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ))
        return JSONResponse({"status": "resuming", "session_id": session_id, "tmux": tmux_session})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_panes(request: Request) -> JSONResponse:
    """Return all tmux panes with their current content."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    session = get_tmux_session()
    try:
        out = await _run_subprocess_output(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_id}\t#{pane_title}\t#{session_name}:#{window_index}.#{pane_index}"],
            timeout=3
        )
        if not out:
            return JSONResponse({"panes": []})
        panes = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t", 2)
            pane_id = parts[0] if len(parts) > 0 else ""
            title = parts[1] if len(parts) > 1 else pane_id
            target = parts[2] if len(parts) > 2 else pane_id
            session_name = session.split(":")[0] if ":" in session else session
            if session_name not in target and session not in target:
                continue
            capture = await _run_subprocess_output(
                ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-30"], timeout=3
            )
            panes.append({"id": pane_id, "title": title or pane_id, "target": target, "content": (capture or "").strip()})
        return JSONResponse({"panes": panes})
    except Exception as e:
        return JSONResponse({"error": str(e), "panes": []})


async def api_inject_pane(request: Request) -> JSONResponse:
    """Inject a command into a specific tmux pane by pane_id."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    pane_id = body.get("pane_id", "").strip()
    message = body.get("message", "").strip()
    if not pane_id or not message:
        return JSONResponse({"error": "pane_id and message required"}, status_code=400)
    try:
        r = await _run_subprocess_async(["tmux", "send-keys", "-t", pane_id, "-l", message], check=True)
        if r is None:
            return JSONResponse({"error": "tmux send-keys timed out"}, status_code=500)
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane_id, "Enter"], check=True)
        return JSONResponse({"status": "sent"})
    except Exception as e:
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


# ---------------------------------------------------------------------------
# BOOP / Skills Endpoints (from ACG — for Settings panel)
# ---------------------------------------------------------------------------
SKILLS_DIR = Path.home() / ".claude" / "skills"
BOOP_CONFIG_FILE = SCRIPT_DIR / "boop_config.json"


async def api_compact_status(request: Request) -> JSONResponse:
    """Check if Claude is currently compacting context (shows in tmux pane)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pane = await _find_primary_pane_async()
    content = await _run_subprocess_output(
        ["tmux", "capture-pane", "-t", pane, "-p", "-S", "-20"], timeout=3
    )
    if content:
        compacting = "Compacting (ctrl+o" in content or "Compacting…" in content
        return JSONResponse({"compacting": compacting})
    return JSONResponse({"compacting": False})


async def api_boop_config(request: Request) -> JSONResponse:
    """GET: read active BOOP config. POST: update active_command and/or cadence_minutes."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.method == "POST":
        try:
            body = await request.json()
            cfg = json.loads(BOOP_CONFIG_FILE.read_text()) if BOOP_CONFIG_FILE.exists() else {}
            g = cfg.setdefault("global", {})
            if "active_command" in body:
                g["active_command"] = str(body["active_command"])
            if "cadence_minutes" in body:
                g["cadence_minutes"] = int(body["cadence_minutes"])
            if "paused" in body:
                g["paused"] = bool(body["paused"])
            BOOP_CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
            return JSONResponse({"ok": True, "active_command": g.get("active_command"),
                                 "cadence_minutes": g.get("cadence_minutes")})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    # GET
    try:
        cfg = json.loads(BOOP_CONFIG_FILE.read_text()) if BOOP_CONFIG_FILE.exists() else {}
        g = cfg.get("global", {})
        return JSONResponse({
            "active_command": g.get("active_command", "/sprint-mode"),
            "cadence_minutes": g.get("cadence_minutes", 30),
            "paused": g.get("paused", False),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_boops_list(request: Request) -> JSONResponse:
    """List available BOOP/skill entries from the skills directory."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    boops = []
    if SKILLS_DIR.exists():
        for entry in sorted(SKILLS_DIR.iterdir()):
            if entry.is_dir():
                skill_file = entry / "SKILL.md"
                if skill_file.exists():
                    boops.append({"name": entry.name, "path": str(skill_file)})
    return JSONResponse({"boops": boops})


async def api_boop_read(request: Request) -> JSONResponse:
    """Read the content of a specific BOOP/skill."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    name = request.path_params.get("name", "")
    if ".." in name or "/" in name:
        return JSONResponse({"error": "invalid name"}, status_code=400)
    skill_file = SKILLS_DIR / name / "SKILL.md"
    if not skill_file.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    content = skill_file.read_text(encoding="utf-8", errors="replace")
    return JSONResponse({"name": name, "content": content})


# BOOP daemon control — session name and script path for toggle/status
BOOP_TMUX_SESSION = "boop-daemon"
BOOP_DAEMON_SCRIPT = Path.home() / "civ" / "tools" / "boop-daemon.sh"


async def api_boop_status(request: Request) -> JSONResponse:
    """Check if the BOOP daemon tmux session is running."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        r = await _run_subprocess_async(["tmux", "has-session", "-t", BOOP_TMUX_SESSION])
        running = r is not None and r.returncode == 0
        pid = None
        if running:
            try:
                out = await _run_subprocess_output(
                    ["tmux", "list-panes", "-t", BOOP_TMUX_SESSION, "-F", "#{pane_pid}"], timeout=3
                )
                if out and out.strip():
                    pid = int(out.strip().split()[0])
            except (ValueError, Exception):
                pass
        return JSONResponse({"active": running, "pid": pid})
    except Exception:
        return JSONResponse({"active": False, "pid": None})


async def api_boop_toggle(request: Request) -> JSONResponse:
    """Toggle the BOOP daemon on/off via tmux session."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        r = await _run_subprocess_async(["tmux", "has-session", "-t", BOOP_TMUX_SESSION])
        currently_running = r is not None and r.returncode == 0

        if currently_running:
            await _run_subprocess_async(["tmux", "kill-session", "-t", BOOP_TMUX_SESSION])
            return JSONResponse({"active": False, "action": "stopped"})
        else:
            if not BOOP_DAEMON_SCRIPT.exists():
                return JSONResponse(
                    {"error": f"boop-daemon.sh not found at {BOOP_DAEMON_SCRIPT}"},
                    status_code=500
                )
            await _run_subprocess_async(
                ["tmux", "new-session", "-d", "-s", BOOP_TMUX_SESSION,
                 f"bash {BOOP_DAEMON_SCRIPT} > /tmp/boop-daemon.log 2>&1"]
            )
            return JSONResponse({"active": True, "action": "started"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Claude OAuth Auth Endpoints
# ---------------------------------------------------------------------------
async def api_claude_auth_status(request: Request) -> JSONResponse:
    """Check if Claude is authenticated (has valid OAuth credentials)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        if not CREDENTIALS_FILE.exists():
            return JSONResponse({"authenticated": False, "account": None, "expires_at": None})
        creds = json.loads(CREDENTIALS_FILE.read_text())
        oauth = creds.get("claudeAiOauth", {})
        if not oauth.get("accessToken"):
            return JSONResponse({"authenticated": False, "account": None, "expires_at": None})
        expires_at = oauth.get("expiresAt", 0)
        now_ms = int(time.time() * 1000)
        # Claude Code refreshes tokens in memory without updating the file.
        # If the tmux session is alive and Claude is running, trust it — the
        # expiresAt in credentials.json is stale, not reality.
        tmux_alive = False
        r = await _run_subprocess_async(["tmux", "has-session", "-t", get_tmux_session()])
        if r is not None and r.returncode == 0:
            tmux_alive = True
        if expires_at and expires_at < now_ms and not tmux_alive:
            return JSONResponse({"authenticated": False, "account": oauth.get("account"),
                                 "expires_at": expires_at})
        return JSONResponse({
            "authenticated": True, "account": oauth.get("account"),
            "expires_at": expires_at, "subscription": oauth.get("subscriptionType"),
        })
    except Exception:
        return JSONResponse({"authenticated": False, "account": None, "expires_at": None})


async def _is_claude_running_async(pane: str) -> bool:
    """Check if Claude Code is the active process in the given tmux pane."""
    try:
        r = await _run_subprocess_async(
            ["tmux", "display-message", "-t", pane, "-p", "#{pane_current_command}"],
            timeout=3
        )
        if r and r.stdout:
            cmd = r.stdout.strip().lower()
            return "claude" in cmd or "node" in cmd
    except Exception:
        pass
    return False


async def _detect_auth_screen(pane: str) -> tuple:
    """Capture tmux pane and detect what's currently displayed.
    Returns (screen_type, raw_content) where screen_type is one of the
    AUTH_SCREEN_PATTERNS keys or 'unknown'/'empty'.
    """
    content = await _run_subprocess_output(
        ["tmux", "capture-pane", "-t", pane, "-p", "-J", "-S", "-300"], timeout=5
    )
    if not content:
        return 'empty', ''
    for name in AUTH_SCREEN_PRIORITY:
        pattern = AUTH_SCREEN_PATTERNS[name]
        match = pattern.search(content)
        if match:
            if name == 'oauth_url':
                url = match.group(0).strip()
                if 'state=' not in url:
                    continue  # Truncated URL, keep looking
            return name, content
    return 'unknown', content


async def _dismiss_auth_blocker(pane: str, screen_type: str) -> bool:
    """Dismiss a blocking dialog. Returns True if action was taken."""
    if screen_type == 'csat_survey':
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Escape"])
        await asyncio.sleep(0.5)
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Escape"])
        return True
    elif screen_type == 'update_prompt':
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Escape"])
        await asyncio.sleep(0.5)
        return True
    elif screen_type == 'trust_folder':
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "-l", "y"])
        await asyncio.sleep(0.2)
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"])
        return True
    return False


async def _kill_claude_process() -> None:
    """Kill any running Claude process AND its descendants in this container.

    Total kill — Corey constitutional rule (2026-04-07): "NO CLAUDE SESSION
    MAY BE RUNNING WHEN THE AUTHENTICATE BUTTON IN THE PUREBRAIN-PORTAL AUTH
    MODAL FIRES." Plain `pkill -f claude` is insufficient because it does
    substring-matching on cmdline and misses orphaned MCP children like
    `node .../playwright-mcp` whose cmdline does not contain "claude". Those
    orphans hold stdio + lockfile handles and confuse the next claude spawn.
    """
    # Round 1: SIGKILL anything matching claude or known MCP children.
    await _run_subprocess_output(
        ["bash", "-c",
         "pkill -9 -f 'claude' 2>/dev/null; "
         "pkill -9 -f 'node.*claude' 2>/dev/null; "
         "pkill -9 -f 'playwright-mcp' 2>/dev/null; "
         "pkill -9 -f '@modelcontextprotocol' 2>/dev/null; "
         "pkill -9 -f 'mcp-server' 2>/dev/null; "
         "true"],
        timeout=5,
    )
    # Verification loop — wait until pgrep -f claude returns nothing,
    # up to ~3s. If anything survives, hammer it again.
    for _ in range(6):
        await asyncio.sleep(0.5)
        out = await _run_subprocess_output(
            ["bash", "-c", "pgrep -f 'claude' 2>/dev/null || true"],
            timeout=3,
        )
        if not (out or "").strip():
            return
        # Survivors — hit them again, harder.
        await _run_subprocess_output(
            ["bash", "-c",
             "pkill -9 -f 'claude' 2>/dev/null; "
             "pkill -9 -f 'playwright-mcp' 2>/dev/null; "
             "true"],
            timeout=3,
        )


async def _run_auth_state_machine(pane: str) -> dict:
    """Run the auth flow state machine. Returns dict with status info.

    States: start -> waiting_for_screen -> (dismiss blockers | select_login) ->
            waiting_for_url -> success/failed

    This is the core v2 auth logic ported from auth-flow-v2.py, adapted for
    async execution inside the portal server.
    """
    global _captured_oauth_url
    max_retries = 3
    retry_count = 0
    claude_start_timeout = 45.0
    url_wait_timeout = 30.0
    poll_interval = 0.5
    log_entries = []

    def log(msg):
        log_entries.append(msg)
        _save_portal_message(f"[auth-v2] {msg}", role="assistant")

    while retry_count <= max_retries:
        # --- Phase 1: Clean slate + Start Claude /login ---
        log(f"Starting auth flow (attempt {retry_count + 1}/{max_retries + 1})")

        # Gracefully interrupt any running Claude process via Ctrl+C twice
        # This handles the case where Claude is active after a page refresh
        try:
            await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "C-c"])
            await asyncio.sleep(0.3)
            await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "C-c"])
            await asyncio.sleep(1.0)

            # Handle any "are you sure?" / exit confirmation prompt by sending "y" + Enter
            await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "-l", "y"])
            await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"])
            await asyncio.sleep(1.0)
        except Exception:
            # Pane may not exist yet on first attempt — that's fine
            pass

        # Kill ALL claude processes (and MCP descendants) for a clean start.
        # Uses _kill_claude_process which now does total kill + verification
        # loop — Corey constitutional rule: zero claude processes may be
        # running before the auth modal launches a fresh /login.
        await _kill_claude_process()
        await asyncio.sleep(0.5)

        # Kill and recreate tmux session for a clean pane
        session_name = get_tmux_session()
        log(f"Recreating tmux session '{session_name}' for clean pane")
        await _run_subprocess_async(
            ["bash", "-c", f"tmux kill-session -t {session_name} 2>/dev/null; true"]
        )
        await asyncio.sleep(0.5)
        home_dir = str(Path.home())
        await _run_subprocess_async(
            ["tmux", "new-session", "-d", "-s", session_name, "-c", home_dir]
        )
        await asyncio.sleep(0.5)

        # Re-find pane after session recreate.
        # CRITICAL: invalidate _pane_cache first — its 10s TTL will otherwise
        # return the stale pane id of the pane we just destroyed via
        # `tmux kill-session`, causing every subsequent `tmux send-keys` to
        # fire into the void and time out at 45s. (Alfred/Tess incident
        # 2026-04-07.)
        global _pane_cache
        _pane_cache = (0.0, "")
        pane = await _find_primary_pane_async()

        # Resize tmux so URLs don't wrap
        await _run_subprocess_async(["tmux", "resize-window", "-t", pane, "-x", "500"])
        await asyncio.sleep(0.3)

        # Always launch fresh — no "already running" branch
        log("Launching 'claude /login' in clean pane")
        launch_cmd = f"cd {home_dir} && claude /login"
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "-l", launch_cmd], check=True)
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"], check=True)

        # --- Phase 2: Wait for screen and handle blockers ---
        phase_start = time.time()
        login_selected = False

        while True:
            await asyncio.sleep(poll_interval)
            elapsed = time.time() - phase_start

            screen_type, screen_content = await _detect_auth_screen(pane)

            if screen_type == 'oauth_url':
                # Goal state — extract URL
                match = OAUTH_URL_PATTERN.search(screen_content)
                if match:
                    url = match.group(0).strip()
                    if 'state=' in url:
                        _captured_oauth_url = url
                        log(f"OAuth URL captured ({len(url)} chars) in {elapsed:.1f}s")
                        return {"started": True, "url": url, "log": log_entries}

            elif screen_type == 'logged_in':
                log("Already logged in — no OAuth URL needed")
                return {"started": True, "already_authenticated": True, "log": log_entries}

            elif screen_type in ('csat_survey', 'update_prompt', 'trust_folder'):
                log(f"Dismissing blocker: {screen_type}")
                await _dismiss_auth_blocker(pane, screen_type)
                await asyncio.sleep(1.0)
                continue

            elif screen_type == 'theme_picker':
                # Claude's first-run theme selector — accept the highlighted
                # default with Enter so we can move on to the login menu.
                log("Theme picker detected — accepting default (Enter)")
                await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"])
                await asyncio.sleep(1.0)
                continue

            elif screen_type == 'shell_prompt' and elapsed > 5.0:
                # We launched `claude /login` but the pane is sitting at a
                # shell prompt — claude crashed, exited, or never started.
                # Don't burn the full 45s timeout; break to retry immediately.
                log("Shell prompt detected after launch — claude not running, retrying")
                break

            elif screen_type == 'login_menu' and not login_selected:
                log("Login menu detected — selecting first option (Enter)")
                await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"])
                login_selected = True
                phase_start = time.time()  # Reset timeout for URL wait phase
                continue

            elif screen_type == 'error':
                log(f"Error detected on screen — will retry")
                break  # Break to retry loop

            # Check timeouts
            timeout = url_wait_timeout if login_selected else claude_start_timeout
            if elapsed > timeout:
                phase_name = "URL wait" if login_selected else "Claude start"
                log(f"Timeout in {phase_name} ({timeout}s)")
                break  # Break to retry loop

        # --- Phase 3: Retry ---
        retry_count += 1
        if retry_count <= max_retries:
            log(f"Killing Claude for clean restart (retry {retry_count}/{max_retries})")
            await _kill_claude_process()
            await asyncio.sleep(2.0)
            # Verify it's dead
            if await _is_claude_running_async(pane):
                await _kill_claude_process()
                await asyncio.sleep(2.0)

    log(f"Auth flow FAILED after {max_retries + 1} attempts")
    return {"started": False, "error": "auth flow failed after retries", "log": log_entries}


async def api_claude_auth_start(request: Request) -> JSONResponse:
    """Start Claude OAuth flow using v2 state machine with screen detection.

    Drives the full auth flow: starts Claude, detects and dismisses blocking
    dialogs (CSAT surveys, update prompts, trust folder), selects login option,
    and polls for OAuth URL. Returns the URL inline when possible.

    This is a longer-running endpoint (up to ~60s) but returns WITH the URL
    when the flow completes successfully.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global _captured_oauth_url, _auth_flow_running
    if _auth_flow_running:
        return JSONResponse({"status": "already_running", "message": "Auth flow already in progress"})
    _auth_flow_running = True
    _captured_oauth_url = None
    try:
        pane = await _find_primary_pane_async()
        _save_portal_message(f"Auth flow v2 started — {get_tmux_session()} (pane {pane})", role="assistant")
        result = await _run_auth_state_machine(pane)
        return JSONResponse(result)
    except Exception as e:
        _save_portal_message(f"Auth flow v2 failed: {e}", role="assistant")
        return JSONResponse({"error": f"auth flow error: {e}"}, status_code=500)
    finally:
        _auth_flow_running = False


async def api_claude_auth_prewarm(request: Request) -> JSONResponse:
    """Pre-warm Claude for faster auth. Called when portal page loads.

    DISABLED: Prewarm causes stacked /login commands on page refresh,
    leading to interactive prompt deadlocks. Auth starts cleanly from
    api_claude_auth_start instead.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"status": "disabled"})


async def api_claude_auth_code(request: Request) -> JSONResponse:
    """Inject the OAuth authorization code into the Claude tmux session."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        code = str(body.get("code", "")).strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not code:
        return JSONResponse({"error": "empty code"}, status_code=400)
    pane = await _find_primary_pane_async()
    _save_portal_message(f"Auth code submitted — injecting into {get_tmux_session()}...", role="assistant")
    try:
        r = await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "-l", code], check=True)
        if r is None:
            return JSONResponse({"error": "tmux send-keys timed out"}, status_code=500)
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        _save_portal_message("Code injected — Claude is authenticating...", role="assistant")
        return JSONResponse({"injected": True})
    except Exception as e:
        _save_portal_message(f"Code injection failed: tmux error — pane={pane}, err={e}", role="assistant")
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


async def api_claude_auth_url(request: Request) -> JSONResponse:
    """Poll for the captured OAuth URL from tmux output."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global _captured_oauth_url
    if _captured_oauth_url:
        return JSONResponse({"url": _captured_oauth_url, "ready": True})
    pane = await _find_primary_pane_async()
    try:
        # -J joins wrapped lines so long URLs aren't truncated at terminal width
        content = await _run_subprocess_output(
            ["tmux", "capture-pane", "-t", pane, "-p", "-J", "-S", "-200"], timeout=5
        )
        if not content:
            return JSONResponse({"url": None, "ready": False})
        match = OAUTH_URL_PATTERN.search(content)
        if match:
            candidate = match.group(0).strip()
            # Validate URL is complete — must contain state= parameter.
            # A truncated URL is worse than no URL (causes "missing state" error on claude.ai).
            if "state=" not in candidate:
                _save_portal_message("OAuth URL found but truncated (missing state=) — retrying capture", role="assistant")
            else:
                _captured_oauth_url = candidate
                _save_portal_message(f"OAuth URL ready ({len(candidate)} chars, state= confirmed)", role="assistant")
                return JSONResponse({"url": _captured_oauth_url, "ready": True})
        # Silently return — no notification on each poll. Only notify when URL is found.
    except Exception as e:
        _save_portal_message(f"tmux capture failed: {e}", role="assistant")
    return JSONResponse({"url": None, "ready": False})


# ---------------------------------------------------------------------------
# Thinking Stream Monitor
# ---------------------------------------------------------------------------

async def _push_thinking_to_clients(text: str, ts: int) -> None:
    """Push a thinking block to all connected WebSocket clients."""
    msg = json.dumps({
        "role": "thinking",
        "text": text,
        "timestamp": ts,
        "id": f"thinking-{hashlib.sha256(text.encode()).hexdigest()[:12]}",
    })
    dead = set()
    for ws in list(_chat_ws_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _chat_ws_clients.discard(ws)


async def _push_message_to_clients(entry: dict) -> None:
    """Push any portal message to all connected WebSocket clients immediately.

    Used by api_deliverable (and api_notify) to bypass the 0.8s poll delay so
    file download cards appear live without a page refresh.
    The WS poll loop deduplicates via seen_texts, so double-delivery is safe.
    """
    payload = json.dumps(entry)
    dead = set()
    for ws in list(_chat_ws_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _chat_ws_clients.discard(ws)


async def _thinking_monitor_loop() -> None:
    """Background task: tail latest JSONL session file and push thinking blocks to portal."""
    last_file: str = ""
    last_pos: int = 0

    while True:
        try:
            # Find the most recently modified JSONL session file across all projects
            logs = _find_all_project_jsonl()
            if not logs:
                await asyncio.sleep(2)
                continue

            current_file = str(logs[0])

            # If we switched to a new file, reset position
            if current_file != last_file:
                last_file = current_file
                last_pos = 0

            # Read new lines from where we left off
            try:
                with open(current_file, "rb") as f:
                    f.seek(0, 2)
                    file_size = f.tell()
                    if file_size < last_pos:
                        # File was truncated/rotated — reset
                        last_pos = 0
                    f.seek(last_pos)
                    new_bytes = f.read()
                    last_pos = f.tell()
            except Exception:
                await asyncio.sleep(2)
                continue

            if not new_bytes:
                await asyncio.sleep(1.5)
                continue

            lines = new_bytes.decode("utf-8", errors="replace").splitlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Only assistant messages
                msg = entry.get("message", {})
                if not msg or msg.get("role") != "assistant":
                    continue

                content_blocks = msg.get("content", [])
                if not isinstance(content_blocks, list):
                    continue

                # Skip sidechain (background agent output)
                if entry.get("isSidechain"):
                    continue

                # Check if this message has tool_use blocks (mid-turn reasoning)
                has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content_blocks)

                # Extract thinking: both type=thinking AND mid-turn text blocks (white-dot lines)
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    # Original: extended thinking blocks
                    if btype == "thinking":
                        text = block.get("thinking", "").strip()
                    # NEW: mid-turn text blocks between tool calls (the white-dot reasoning)
                    elif btype == "text" and has_tool_use:
                        text = block.get("text", "").strip()
                    else:
                        continue
                    if not text:
                        continue

                    # Dedup via hash
                    content_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
                    if content_hash in _sent_thinking_hashes:
                        continue
                    _sent_thinking_hashes.add(content_hash)

                    ts = entry.get("timestamp")
                    if isinstance(ts, str):
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            ts = int(dt.timestamp())
                        except (ValueError, AttributeError):
                            ts = int(time.time())
                    elif isinstance(ts, (int, float)):
                        ts = int(ts / 1000) if ts > 1e10 else int(ts)
                    else:
                        ts = int(time.time())

                    # Push to all connected clients (non-blocking)
                    if _chat_ws_clients:
                        await _push_thinking_to_clients(text, ts)

        except Exception:
            pass

        await asyncio.sleep(0.8)  # Fast poll — thinking must appear in near-real-time


# ---------------------------------------------------------------------------
# Scheduled Tasks — fire messages at future times
# ---------------------------------------------------------------------------
SCHEDULED_TASKS_FILE = SCRIPT_DIR / "scheduled_tasks.json"
_scheduled_tasks: list = []  # list of {"id", "message", "fire_at", "created_at"}


def _load_scheduled_tasks() -> None:
    """Load pending tasks from disk on startup."""
    global _scheduled_tasks
    if SCHEDULED_TASKS_FILE.exists():
        try:
            data = json.loads(SCHEDULED_TASKS_FILE.read_text())
            now = datetime.now(timezone.utc).isoformat()
            # Only load tasks that haven't fired yet
            _scheduled_tasks = [t for t in data if t.get("fire_at", "") > now]
            print(f"[sched] Loaded {len(_scheduled_tasks)} pending tasks")
        except Exception as e:
            print(f"[sched] Error loading tasks: {e}")
            _scheduled_tasks = []


def _save_scheduled_tasks() -> None:
    """Persist pending tasks to disk."""
    try:
        SCHEDULED_TASKS_FILE.write_text(json.dumps(_scheduled_tasks, indent=2))
    except Exception as e:
        print(f"[sched] Error saving tasks: {e}")


async def _scheduled_task_checker() -> None:
    """Background loop: check every 30s if any tasks are due, inject into tmux."""
    while True:
        await asyncio.sleep(30)
        if not _scheduled_tasks:
            continue
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        fired = []
        for task in _scheduled_tasks:
            if task.get("fire_at", "") <= now_iso:
                # Fire this task: inject into tmux
                session = get_tmux_session()
                if session:
                    msg = task["message"]
                    try:
                        # Inject message into tmux — use async subprocess to avoid blocking event loop
                        await _run_subprocess_async(
                            ["tmux", "send-keys", "-t", session, "-l", f"\n{msg}"],
                            timeout=5, check=True,
                        )
                        await _run_subprocess_async(
                            ["tmux", "send-keys", "-t", session, "Enter"],
                            timeout=5,
                        )
                        # Retry enters to ensure processing
                        for _ in range(3):
                            await asyncio.sleep(0.5)
                            await _run_subprocess_async(
                                ["tmux", "send-keys", "-t", session, "Enter"],
                                timeout=5,
                            )
                        print(f"[sched] Fired task: {task.get('id', 'unknown')}")
                    except Exception as e:
                        print(f"[sched] Failed to fire task: {e}")
                fired.append(task)
        if fired:
            for t in fired:
                _scheduled_tasks.remove(t)
                # If recurring, schedule next occurrence
                recur_type = t.get("recur_type")
                if recur_type in ("daily", "weekly"):
                    try:
                        recur_time = t.get("recur_time", "09:00")
                        h, m = (int(x) for x in recur_time.split(":"))
                        next_fire = None
                        if recur_type == "daily":
                            base = now + timedelta(days=1)
                            next_fire = base.replace(hour=h, minute=m, second=0, microsecond=0)
                        elif recur_type == "weekly":
                            recur_days = t.get("recur_days", [])  # e.g. ["Mon", "Wed"]
                            day_map = {"Sun": 0, "Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6}
                            target_nums = [day_map[d] for d in recur_days if d in day_map]
                            for offset in range(1, 8):
                                candidate = now + timedelta(days=offset)
                                candidate = candidate.replace(hour=h, minute=m, second=0, microsecond=0)
                                if candidate.weekday() in [((n - 1) % 7) for n in target_nums]:
                                    # JS weekday (0=Sun) vs Python weekday (0=Mon) — convert
                                    # JS: Sun=0, Mon=1 ... Sat=6
                                    # Python: Mon=0, Tue=1 ... Sun=6
                                    # JS day n → Python day (n - 1) % 7
                                    next_fire = candidate
                                    break
                        if next_fire:
                            new_task = dict(t)
                            new_task["fire_at"] = next_fire.isoformat()
                            new_task["id"] = f"task-{int(now.timestamp())}-recur-{len(_scheduled_tasks)}"
                            _scheduled_tasks.append(new_task)
                            print(f"[sched] Rescheduled {recur_type} task for {next_fire.isoformat()}")
                    except Exception as re:
                        print(f"[sched] Error rescheduling recurring task: {re}")
            _save_scheduled_tasks()


async def api_schedule_task(request) -> JSONResponse:
    """POST /api/schedule-task — schedule a message for future delivery."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    message = body.get("message", "").strip()
    fire_at = body.get("fire_at", "").strip()  # ISO 8601 UTC string

    if not message:
        return JSONResponse({"error": "No message"}, status_code=400)
    if not fire_at:
        return JSONResponse({"error": "No fire_at time"}, status_code=400)

    recur_type = body.get("recur_type", "").strip() or None   # "daily" | "weekly" | None
    recur_time = body.get("recur_time", "").strip() or None   # "HH:MM"
    recur_days = body.get("recur_days") or None               # ["Mon", "Wed"] for weekly

    task_id = f"task-{int(datetime.now().timestamp())}-{len(_scheduled_tasks)}"
    task = {
        "id": task_id,
        "message": message,
        "fire_at": fire_at,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if recur_type:
        task["recur_type"] = recur_type
    if recur_time:
        task["recur_time"] = recur_time
    if recur_days:
        task["recur_days"] = recur_days
    _scheduled_tasks.append(task)
    _save_scheduled_tasks()
    print(f"[sched] Scheduled task {task_id} for {fire_at}" + (f" (recur: {recur_type})" if recur_type else ""))
    return JSONResponse({"ok": True, "task_id": task_id, "fire_at": fire_at, "recur_type": recur_type})


BOOP_STATE_FILE = Path(os.environ.get("CIV_ROOT", str(Path.home() / "projects/AI-CIV/aether"))) / ".claude/scheduled-tasks-state.json"

async def api_boops_list(request) -> JSONResponse:
    """GET /api/boops — list all BOOPs from boop_executor config."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        data = json.loads(BOOP_STATE_FILE.read_text())
        tasks = data.get("tasks", {})
        rules = data.get("boop_rules", {})
        boops = []
        for boop_id, boop in tasks.items():
            boops.append({
                "id": boop_id,
                "description": boop.get("description", ""),
                "frequency": boop.get("frequency", "unknown"),
                "status": boop.get("status", "active"),
                "category": boop.get("category", ""),
                "agent": boop.get("agent", ""),
                "last_run": boop.get("last_run", ""),
                "schedule_slot": boop.get("schedule_slot", ""),
                "override_max_daily": boop.get("override_max_daily", False),
            })
        return JSONResponse({"boops": boops, "rules": rules})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_boop_update(request) -> JSONResponse:
    """PATCH /api/boops/{boop_id} — update a BOOP's frequency, status, or description."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    boop_id = request.path_params.get("boop_id", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    try:
        data = json.loads(BOOP_STATE_FILE.read_text())
        tasks = data.get("tasks", {})
        if boop_id not in tasks:
            return JSONResponse({"error": "BOOP not found"}, status_code=404)
        boop = tasks[boop_id]
        for field in ("frequency", "status", "description", "schedule_slot", "category", "agent"):
            if field in body:
                boop[field] = body[field]
        data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        BOOP_STATE_FILE.write_text(json.dumps(data, indent=2))
        print(f"[boop] Updated BOOP {boop_id}: {list(body.keys())}")
        return JSONResponse({"ok": True, "boop": boop})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# 777 Command Center — AI Coaching Proxy
# ---------------------------------------------------------------------------
_777_SYSTEM_PROMPTS = {
    'reflection': """You are a supportive daily performance coach inside the 777 Command Center — a private personal development tool.

The user has just completed their daily check-in: 20 yes/no questions across areas like mindset, health, focus, relationships, and action-taking. You have access to today's scores and recent history.

Your role:
- Celebrate genuine wins without being sycophantic
- Ask one probing question about a low score area rather than lecturing
- Spot patterns across days when history shows them ("3 days of low fitness scores")
- Suggest ONE specific micro-action for the biggest gap area
- Be direct, warm, and brief — this is a morning check-in, not therapy
- Never give generic advice — always anchor to their actual scores
- Keep responses under 200 words unless they ask for more

Tone: Direct coach, not cheerleader. Tim Ferriss meets Naval Ravikant.""",

    'fear': """You are a Stoic-inspired fear analysis coach inside the 777 Command Center.

The user is doing Tim Ferriss's Fear Setting exercise: defining worst cases, prevention steps, and repair paths for a specific fear.

Your role:
- Challenge whether worst cases are truly as likely/bad as perceived (Stoic reality check)
- Identify gaps in their prevention column that they haven't considered
- Strengthen the repair column — can they recover faster than they think?
- Ask: "What's the real cost of NOT doing this?" if inaction cost is weak
- Identify if this fear is actually a disguised excitement or opportunity
- Be Socratic — ask questions more than make declarations
- Never dismiss a fear as irrational, but help them see it clearly

Tone: Wise Stoic mentor. Calm, direct, thought-provoking.""",

    'goals': """You are a strategic goal advisor inside the 777 Command Center.

The user has a vision statement, yearly goals with progress sliders, and a list of their Top 77 lifetime goals. You have access to their current progress data.

Your role:
- Analyze which goals are falling behind relative to where we are in the year
- Identify if any yearly goals conflict with each other (resource/time competition)
- Suggest the ONE goal that deserves focus this week based on impact + deadline proximity
- Help them think about what "60% through Q1 but 20% on this goal" actually means
- Flag if a goal seems vague or unmeasurable and suggest how to sharpen it
- Keep the vision statement as the north star in your analysis

Tone: Strategic advisor, not cheerleader. Sharp, practical, focused.""",

    'ceo': """You are an executive performance coach inside the 777 Command Center.

The user does a weekly CEO Review: scoring themselves 1-10 across the 7 F's (Family, Career, Fitness, Faith, Finance, Fellowship, Fun), noting wins, lessons, and next-week focuses.

Your role:
- Generate a 3-bullet "CEO Brief" summarizing the week from the scores and notes
- Identify the 1-2 F's with the lowest scores and ask what specifically drove them down
- Spot trend patterns if history is available ("Finance has been below 6 for 4 weeks")
- Suggest ONE 20-minute action this week for the lowest-scored F
- Validate wins genuinely — don't inflate them
- Help them see if their "next week focuses" are actually addressing their weak F's

Tone: Senior executive coach. Calm, analytical, high-trust.""",

    'ritual': """You are a performance ritual optimizer inside the 777 Command Center.

The user has a morning ritual stack with specific activities and durations. You have their completion history and their goals.

Your role:
- Identify which rituals have low completion rates and ask what's making them hard
- Suggest one ritual addition that connects to their stated goals
- Identify if their ritual stack is overcrowded (too many items = completion failure)
- Flag time conflicts or unrealistic time allocations
- Suggest optimal ordering based on energy management principles (high-focus work first)
- Never suggest removing faith/prayer/family rituals unless user asks
- Connect ritual suggestions back to the 7 F's they scored low on

Tone: Practical performance coach. Evidence-based, respectful of personal practices.""",

    'gratitude': """You are a gratitude depth coach inside the 777 Command Center.

The user journals 3 gratitude entries daily plus a "why" elaboration. You have access to their recent entries and patterns.

Your role:
- Reflect themes you notice across their gratitude entries ("You often mention family — that's a core anchor")
- If entries are shallow (one word, generic), ask ONE question to deepen them
- Generate a monthly gratitude summary when they have enough history
- Ask: "What would you lose if this gratitude was gone?" to deepen reflection
- Identify if their gratitude entries are skewing toward one life area (work-heavy, etc.)
- Never be preachy about gratitude practice — they're already doing it

Tone: Thoughtful journal partner. Warm, curious, reflective.""",

    'thinking': """You are a strategic thinking coach inside the 777 Command Center.

The user is working through a structured thinking exercise. The exercise type and their current inputs are provided in the context data.

Your role:
- Analyze their inputs through the specific framework they're using (Eisenhower, SWOT, Pareto, etc.)
- Challenge assumptions — point out what they might be missing
- Ask ONE probing question that could shift their perspective
- Offer ONE actionable insight based on their data
- Keep responses under 250 words — this is a quick coaching nudge, not a lecture
- Reference their specific data points, don't give generic advice
- If their exercise data is sparse, encourage them to add more before the analysis will be truly useful

Tone: Sharp strategic advisor. Direct, practical, Socratic.""",
}

_777_RATE_LIMITS: dict = {}  # ip -> {window_start, count}
_777_RATE_WINDOW = 60  # seconds
_777_RATE_MAX = 20  # requests per minute per IP
_777_MAX_TURNS = 10
_777_MAX_CHARS = 2000


async def api_777_chat(request) -> JSONResponse:
    """POST /api/777/chat — AI coaching proxy for 777 Command Center."""
    # CORS for Vercel-hosted 777
    origin = request.headers.get("origin", "")
    cors_origin = origin if (
        origin == "https://777-command-center.vercel.app"
    ) else "https://777-command-center.vercel.app"
    cors = {
        "Access-Control-Allow-Origin": cors_origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Vary": "Origin",
    }

    # Handle preflight
    if request.method == "OPTIONS":
        return Response("", status_code=204, headers=cors)

    # Rate limit by IP — use client.host (not X-Forwarded-For which can be spoofed)
    ip = request.client.host or "unknown"
    now = time.time()
    entry = _777_RATE_LIMITS.get(ip)
    if not entry or now - entry["window_start"] > _777_RATE_WINDOW:
        _777_RATE_LIMITS[ip] = {"window_start": now, "count": 1}
    else:
        entry["count"] += 1
        if entry["count"] > _777_RATE_MAX:
            return JSONResponse({"error": "Too many requests. Please wait a moment."}, status_code=429, headers=cors)

    # Get API key
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        # Try loading from .env
        env_path = Path(os.environ.get("CIV_ROOT", str(Path.home()))) / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    break
    if not api_key:
        return JSONResponse({"error": "AI service not configured. Add ANTHROPIC_API_KEY to .env"}, status_code=500, headers=cors)

    # Parse body
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400, headers=cors)

    module = body.get("module", "")
    messages = body.get("messages", [])
    context = body.get("context")

    # Validate module
    if module not in _777_SYSTEM_PROMPTS:
        return JSONResponse(
            {"error": f"Invalid module. Must be one of: {', '.join(_777_SYSTEM_PROMPTS.keys())}"},
            status_code=400, headers=cors
        )

    # Validate messages
    if not isinstance(messages, list) or len(messages) == 0:
        return JSONResponse({"error": "messages array required"}, status_code=400, headers=cors)

    # Sanitize messages
    sanitized = []
    for m in messages[-_777_MAX_TURNS:]:
        if not isinstance(m, dict) or "role" not in m or "content" not in m:
            continue
        role = "user" if m["role"] == "user" else "assistant"
        content = str(m["content"])[:_777_MAX_CHARS]
        sanitized.append({"role": role, "content": content})

    if not sanitized or sanitized[0]["role"] != "user":
        return JSONResponse({"error": "First message must be from user"}, status_code=400, headers=cors)

    # Build system prompt
    system_prompt = _777_SYSTEM_PROMPTS[module]
    if context and isinstance(context, dict):
        context_str = json.dumps(context, indent=2)[:3000]
        system_prompt += f"\n\n---\nCURRENT EXERCISE DATA (JSON):\n{context_str}"

    # Call Anthropic API
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 600,
                    "system": system_prompt,
                    "messages": sanitized,
                },
            )
    except Exception as e:
        print(f"[777-chat] Anthropic fetch error: {e}")
        return JSONResponse({"error": "AI service unreachable. Please try again."}, status_code=502, headers=cors)

    if resp.status_code != 200:
        print(f"[777-chat] Anthropic error {resp.status_code}: {resp.text[:200]}")
        status = 429 if resp.status_code == 429 else 502
        msg = "AI rate limit hit. Please wait 30 seconds." if resp.status_code == 429 else "AI service error. Please try again."
        return JSONResponse({"error": msg}, status_code=status, headers=cors)

    try:
        data = resp.json()
    except Exception:
        return JSONResponse({"error": "Invalid response from AI service."}, status_code=502, headers=cors)

    text = ""
    if data.get("content") and len(data["content"]) > 0:
        text = data["content"][0].get("text", "")
    if not text:
        return JSONResponse({"error": "Empty response from AI."}, status_code=502, headers=cors)

    print(f"[777-chat] {module} response for {ip} ({len(text)} chars)")
    return JSONResponse({"reply": text}, headers=cors)


async def api_scheduled_tasks_list(request) -> JSONResponse:
    """GET /api/scheduled-tasks — list pending scheduled tasks."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"tasks": _scheduled_tasks})


async def api_delete_scheduled_task(request) -> JSONResponse:
    """DELETE /api/scheduled-tasks/{task_id} — cancel a pending scheduled task."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    task_id = request.path_params.get("task_id", "")
    global _scheduled_tasks
    before = len(_scheduled_tasks)
    _scheduled_tasks = [t for t in _scheduled_tasks if t.get("id") != task_id]
    if len(_scheduled_tasks) == before:
        return JSONResponse({"ok": False, "error": "Task not found"}, status_code=404)
    _save_scheduled_tasks()
    print(f"[sched] Cancelled task {task_id}")
    return JSONResponse({"ok": True})


async def api_update_scheduled_task(request) -> JSONResponse:
    """PUT /api/scheduled-tasks/{task_id} — update an existing scheduled task."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    task_id = request.path_params.get("task_id", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    # Find task
    task = None
    for t in _scheduled_tasks:
        if t.get("id") == task_id:
            task = t
            break
    if not task:
        return JSONResponse({"ok": False, "error": "Task not found"}, status_code=404)

    # Update fields
    if "message" in body:
        task["message"] = body["message"]
    if "fire_at" in body:
        task["fire_at"] = body["fire_at"]
    if "recur_type" in body:
        task["recur_type"] = body["recur_type"]
    if "recur_time" in body:
        task["recur_time"] = body["recur_time"]
    if "recur_days" in body:
        task["recur_days"] = body["recur_days"]

    _save_scheduled_tasks()
    print(f"[sched] Updated task {task_id}: fire_at={task.get('fire_at')}")
    return JSONResponse({"ok": True, "task": task})


async def api_patch_scheduled_task(request) -> JSONResponse:
    """PATCH /api/scheduled-tasks/{task_id} — partial update: status, subtasks, notes, order, completion_pct."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    task_id = request.path_params.get("task_id", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    task = None
    for t in _scheduled_tasks:
        if t.get("id") == task_id:
            task = t
            break
    if not task:
        return JSONResponse({"ok": False, "error": "Task not found"}, status_code=404)

    # Status: pending | in_progress | completed
    if "status" in body:
        allowed_statuses = {"pending", "in_progress", "completed"}
        new_status = body["status"]
        if new_status in allowed_statuses:
            task["status"] = new_status
            print(f"[sched] Task {task_id} status -> {new_status}")

    # Subtasks: list of {id, text, done}
    if "subtasks" in body:
        subtasks = body["subtasks"]
        if isinstance(subtasks, list):
            task["subtasks"] = subtasks

    # Append a new note {text, ts}
    if "note" in body:
        note_text = str(body["note"]).strip()
        if note_text:
            if "notes" not in task or not isinstance(task["notes"], list):
                task["notes"] = []
            task["notes"].append({
                "text": note_text,
                "ts": datetime.now(timezone.utc).isoformat()
            })

    # Replace all notes
    if "notes" in body:
        notes = body["notes"]
        if isinstance(notes, list):
            task["notes"] = notes

    # Sort order
    if "order" in body:
        try:
            task["order"] = int(body["order"])
        except (TypeError, ValueError):
            pass

    # Completion percentage 0-100
    if "completion_pct" in body:
        try:
            pct = int(body["completion_pct"])
            task["completion_pct"] = max(0, min(100, pct))
        except (TypeError, ValueError):
            pass

    _save_scheduled_tasks()
    return JSONResponse({"ok": True, "task": task})


async def _startup() -> None:
    """Start background tasks on server startup."""
    _check_tracked_modifications()  # Warn about tracked file edits early
    _init_portal_log_ids()
    await _init_referral_db()
    await _init_clients_db()
    await _init_agents_db()
    asyncio.create_task(_thinking_monitor_loop())
    asyncio.create_task(_trim_portal_log_periodically())
    asyncio.create_task(_scheduled_task_checker())
    asyncio.create_task(_auto_import_clients_loop())
    asyncio.create_task(_paypal_subscription_sync_loop())
    _load_scheduled_tasks()
    for _hook in _custom_startup_hooks:  # Flux overlay: custom startup hooks
        await _hook()


async def _auto_import_clients_loop() -> None:
    """Auto-import clients from JSONL logs every 5 minutes so new signups appear without manual refresh."""
    while True:
        try:
            await _run_clients_import()
        except Exception as _e:
            print(f"[clients-auto-import] error: {_e}")
        await asyncio.sleep(300)  # every 5 minutes


async def _paypal_subscription_sync_loop() -> None:
    """Sync PayPal subscription status into clients.db every hour.

    Resolves the gap where subscriptions are recorded in PayPal but the
    payment log lacks payerEmail (so clients show $0 / 'none' in admin dashboard).
    """
    import importlib.util as _ilu
    import sys as _sys
    from pathlib import Path as _Path

    # Wait 30s on startup so DB is fully initialised before first sync
    await asyncio.sleep(30)

    sync_module_path = _Path(__file__).parent / "paypal_sync_subscriptions.py"

    while True:
        try:
            if sync_module_path.exists():
                spec = _ilu.spec_from_file_location("_paypal_sync", str(sync_module_path))
                mod = _ilu.module_from_spec(spec)
                spec.loader.exec_module(mod)
                result = mod.run_sync(dry_run=False)
                updated = result.get("updated", 0)
                if updated:
                    print(f"[paypal-sync-loop] Updated {updated} client(s) with PayPal subscription data")
            else:
                print("[paypal-sync-loop] paypal_sync_subscriptions.py not found — skipping")
        except Exception as _e:
            print(f"[paypal-sync-loop] error: {_e}")
        await asyncio.sleep(3600)  # every hour


async def _run_clients_import() -> dict:
    """Core import logic shared between the auto-loop and the manual API endpoint."""
    # Reuse the same logic as api_admin_clients_import but without HTTP auth
    # We inline a minimal version here to avoid circular dependency with request object
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _Path

    candidates: dict = {}

    def _collect_pay_test(log_path):
        if not log_path.exists():
            return
        with log_path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = _json.loads(line)
                except Exception:
                    continue
                email = (d.get("email") or "").strip().lower()
                if not email or "@" not in email:
                    continue
                order_id = (d.get("orderId") or "").strip()
                if any(order_id.startswith(p) for p in ("SANDBOX-", "E2E-", "test-", "TEST-")):
                    continue
                if "sandbox" in email or "test" in email.split("@")[0]:
                    continue
                ts = d.get("server_timestamp", "")
                rec = candidates.setdefault(email, {
                    "email": email, "name": "", "goes_by": "", "ai_name": "",
                    "company": "", "role": "", "goal": "", "tier": "unknown",
                    "payment_status": "none", "paypal_subscription_id": "",
                    "total_paid": 0.0, "payment_count": 0, "referral_code": "",
                    "first_seen_at": ts, "last_active_at": ts, "onboarded_at": "",
                    "_sources": set(),
                })
                rec["_sources"].add("pay_test")
                if d.get("name"):
                    rec["name"] = d["name"].strip()
                if d.get("aiName"):
                    rec["ai_name"] = d["aiName"].strip()
                if d.get("goesBy"):
                    rec["goes_by"] = d["goesBy"].strip()
                if d.get("company"):
                    rec["company"] = d["company"].strip()
                if d.get("role"):
                    rec["role"] = d["role"].strip()
                if d.get("primaryGoal"):
                    rec["goal"] = d["primaryGoal"].strip()
                if d.get("tier") and d["tier"] not in ("unknown", "test", ""):
                    rec["tier"] = d["tier"].strip()
                if d.get("paypalSubscriptionId"):
                    rec["paypal_subscription_id"] = d["paypalSubscriptionId"].strip()
                if ts and (not rec["first_seen_at"] or ts < rec["first_seen_at"]):
                    rec["first_seen_at"] = ts
                if ts and ts > rec.get("last_active_at", ""):
                    rec["last_active_at"] = ts

    def _collect_payments(log_path):
        if not log_path.exists():
            return
        with log_path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = _json.loads(line)
                except Exception:
                    continue
                email = (d.get("payerEmail") or "").strip().lower()
                if not email or "@" not in email:
                    continue
                order_id = (d.get("orderId") or "").strip()
                if any(order_id.startswith(p) for p in ("SANDBOX-", "E2E-", "test-", "TEST-")):
                    continue
                if "sandbox" in email or "test" in email.split("@")[0]:
                    continue
                ts = d.get("server_timestamp", "")
                tier = (d.get("tier") or "").strip()
                amount = float(d.get("amount") or 0)
                rec = candidates.setdefault(email, {
                    "email": email, "name": "", "goes_by": "", "ai_name": "",
                    "company": "", "role": "", "goal": "", "tier": "unknown",
                    "payment_status": "none", "paypal_subscription_id": "",
                    "total_paid": 0.0, "payment_count": 0, "referral_code": "",
                    "first_seen_at": ts, "last_active_at": ts, "onboarded_at": "",
                    "_sources": set(),
                })
                rec["_sources"].add("payments")
                if d.get("payerName") and not rec["name"]:
                    rec["name"] = d["payerName"].strip()
                if tier and tier not in ("unknown", ""):
                    rec["tier"] = tier
                if amount > 0:
                    rec["total_paid"] = round(rec["total_paid"] + amount, 2)
                    rec["payment_count"] += 1
                if order_id.startswith("I-"):
                    rec["paypal_subscription_id"] = order_id
                    rec["payment_status"] = "subscription_active"
                elif amount > 0:
                    rec["payment_status"] = "paid"
                if ts and (not rec["first_seen_at"] or ts < rec["first_seen_at"]):
                    rec["first_seen_at"] = ts
                if ts and ts > rec.get("last_active_at", ""):
                    rec["last_active_at"] = ts

    _collect_pay_test(PAY_TEST_LOG)
    _collect_payments(PAYMENTS_LOG)

    if not candidates:
        return {"imported": 0, "updated": 0}

    now = _dt.now(_tz.utc).isoformat()
    imported = 0
    updated = 0

    async with _clients_db() as db:
        for email, rec in candidates.items():
            if not rec.get("name"):
                continue
            try:
                cur = await db.execute(
                    "SELECT id, tier, payment_status FROM clients WHERE email = ? COLLATE NOCASE",
                    (email,)
                )
                existing = await cur.fetchone()
                if existing is None:
                    await db.execute(
                        """INSERT INTO clients
                           (name, email, goes_by, ai_name, company, role, goal, tier, status,
                            payment_status, paypal_subscription_id, total_paid, payment_count,
                            referral_code, first_seen_at, last_active_at, onboarded_at,
                            created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            rec["name"], email, rec["goes_by"], rec["ai_name"],
                            rec["company"], rec["role"], rec["goal"],
                            rec["tier"] if rec["tier"] != "unknown" else "Awakened",
                            "active", rec["payment_status"], rec["paypal_subscription_id"],
                            rec["total_paid"], rec["payment_count"], rec["referral_code"],
                            rec["first_seen_at"], rec["last_active_at"], rec["onboarded_at"],
                            now, now,
                        )
                    )
                    imported += 1
                else:
                    # Update payment info and tier if we have better data.
                    # Use MAX logic: never downgrade payment_status that was set by PayPal sync,
                    # and never reduce total_paid below what is already in the DB.
                    # Also write paypal_subscription_id if we now have one and the DB is missing it.
                    await db.execute(
                        """UPDATE clients SET
                           payment_status = CASE
                               WHEN payment_status IN ('subscription_active','subscription_cancelled') THEN payment_status
                               WHEN ? != 'none' THEN ?
                               ELSE payment_status
                           END,
                           paypal_subscription_id = CASE
                               WHEN (paypal_subscription_id = '' OR paypal_subscription_id IS NULL) AND ? != '' THEN ?
                               ELSE paypal_subscription_id
                           END,
                           tier = CASE WHEN ? NOT IN ('unknown','') THEN ? ELSE tier END,
                           total_paid = CASE WHEN ? > total_paid THEN ? ELSE total_paid END,
                           payment_count = CASE WHEN ? > payment_count THEN ? ELSE payment_count END,
                           last_active_at = CASE WHEN ? > last_active_at THEN ? ELSE last_active_at END,
                           updated_at = ?
                           WHERE email = ? COLLATE NOCASE""",
                        (
                            rec["payment_status"], rec["payment_status"],
                            rec["paypal_subscription_id"], rec["paypal_subscription_id"],
                            rec["tier"], rec["tier"],
                            rec["total_paid"], rec["total_paid"],
                            rec["payment_count"], rec["payment_count"],
                            rec["last_active_at"], rec["last_active_at"],
                            now, email,
                        )
                    )
                    updated += 1
            except Exception:
                pass
        await db.commit()

    return {"imported": imported, "updated": updated}


async def _trim_portal_log_periodically() -> None:
    """Trim portal-chat.jsonl to last 3000 messages every 30 minutes to prevent unbounded growth."""
    while True:
        await asyncio.sleep(1800)  # 30 minutes
        try:
            _trim_portal_chat_log(max_entries=3000)
        except Exception as _e:
            print(f"[portal] trim error: {_e}")


# ---------------------------------------------------------------------------
# Referral System — SQLite-backed (replaces dead WP proxy endpoints)
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager

@asynccontextmanager
async def _referral_db():
    """Open referral DB with WAL mode and foreign keys enabled."""
    async with aiosqlite.connect(str(REFERRALS_DB)) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA foreign_keys = ON")
        yield db


async def _init_referral_db() -> None:
    """Create referral tables on startup if they don't exist."""
    async with aiosqlite.connect(str(REFERRALS_DB)) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrers (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name     TEXT NOT NULL DEFAULT '',
                user_email    TEXT NOT NULL UNIQUE COLLATE NOCASE,
                referral_code TEXT NOT NULL UNIQUE COLLATE NOCASE,
                paypal_email  TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL
            )
        """)
        # Migration: add password_hash column to existing DBs without it
        try:
            await db.execute("ALTER TABLE referrers ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass  # column already exists
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id  INTEGER NOT NULL REFERENCES referrers(id),
                referred_email TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
                referred_name  TEXT NOT NULL DEFAULT '',
                status       TEXT NOT NULL DEFAULT 'pending',
                created_at   TEXT NOT NULL,
                completed_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referral_clicks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                referral_code TEXT NOT NULL COLLATE NOCASE,
                ip_hash      TEXT NOT NULL DEFAULT '',
                clicked_at   TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rewards (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL REFERENCES referrers(id),
                referral_id INTEGER REFERENCES referrals(id),
                reward_type TEXT NOT NULL DEFAULT 'cash',
                reward_value REAL NOT NULL DEFAULT 0.0,
                issued_at   TEXT NOT NULL
            )
        """)
        # commission_payments tracks recurring 5% commissions from referred member payments
        await db.execute("""
            CREATE TABLE IF NOT EXISTS commission_payments (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id     INTEGER NOT NULL REFERENCES referrers(id),
                referral_id     INTEGER NOT NULL REFERENCES referrals(id),
                payer_email     TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
                order_id        TEXT NOT NULL DEFAULT '',
                payment_amount  REAL NOT NULL DEFAULT 0.0,
                commission_rate REAL NOT NULL DEFAULT 0.05,
                commission_value REAL NOT NULL DEFAULT 0.0,
                tier            TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL
            )
        """)
        # admin_tokens table for read-only admin viewers
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_tokens (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                token      TEXT NOT NULL UNIQUE,
                email      TEXT NOT NULL DEFAULT '',
                name       TEXT NOT NULL DEFAULT '',
                role       TEXT NOT NULL DEFAULT 'viewer',
                created_at TEXT NOT NULL
            )
        """)
        # ── payout_requests (replaces JSONL file) ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payout_requests (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id   TEXT NOT NULL UNIQUE,
                referral_code TEXT NOT NULL COLLATE NOCASE,
                paypal_email TEXT NOT NULL DEFAULT '',
                amount       REAL NOT NULL DEFAULT 0.0,
                status       TEXT NOT NULL DEFAULT 'pending',
                batch_id     TEXT NOT NULL DEFAULT '',
                notes        TEXT NOT NULL DEFAULT '',
                created_at   TEXT NOT NULL,
                paid_at      TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_payouts_code ON payout_requests(referral_code)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_payouts_status ON payout_requests(status)")

        # ── financial_audit_log (immutable ledger) ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS financial_audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type  TEXT NOT NULL,
                actor       TEXT NOT NULL DEFAULT '',
                referral_code TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
                amount      REAL NOT NULL DEFAULT 0.0,
                details     TEXT NOT NULL DEFAULT '',
                ip_address  TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL
            )
        """)

        # ── Performance indexes for common query patterns ──
        await db.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer_status ON referrals(referrer_id, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referred_email ON referrals(referred_email)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rewards_referrer ON rewards(referrer_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_clicks_code ON referral_clicks(referral_code)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_commissions_referrer ON commission_payments(referrer_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_commissions_order ON commission_payments(order_id)")

        await db.commit()

        # ── One-time migration: import existing JSONL payout requests into SQLite ──
        if PAYOUT_REQUESTS_FILE.exists():
            try:
                with PAYOUT_REQUESTS_FILE.open("r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            await db.execute(
                                """INSERT OR IGNORE INTO payout_requests
                                   (request_id, referral_code, paypal_email, amount, status, batch_id, notes, created_at, paid_at)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (entry.get("request_id", ""), entry.get("referral_code", ""),
                                 entry.get("paypal_email", ""), entry.get("amount", 0.0),
                                 entry.get("status", "pending"), entry.get("batch_id", ""),
                                 entry.get("notes", ""), entry.get("created_at", ""),
                                 entry.get("paid_at"))
                            )
                        except Exception:
                            continue
                await db.commit()
                # Rename JSONL file to .migrated to prevent re-import
                PAYOUT_REQUESTS_FILE.rename(PAYOUT_REQUESTS_FILE.with_suffix(".jsonl.migrated"))
                print("[referral] Migrated payout requests from JSONL to SQLite")
            except Exception as e:
                print(f"[referral] JSONL migration error (non-fatal): {e}")

    print(f"[referral] SQLite DB ready: {REFERRALS_DB}")


async def _log_financial_event(
    event_type: str,
    referral_code: str = "",
    amount: float = 0.0,
    actor: str = "",
    details: str = "",
    ip_address: str = "",
) -> None:
    """Write an immutable audit log entry for financial operations."""
    try:
        async with _referral_db() as db:
            await db.execute(
                """INSERT INTO financial_audit_log
                   (event_type, actor, referral_code, amount, details, ip_address, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (event_type, actor, referral_code, amount, details, ip_address,
                 datetime.now(timezone.utc).isoformat())
            )
            await db.commit()
    except Exception as e:
        print(f"[referral][AUDIT] Failed to log event: {e}")


def _generate_referral_code() -> str:
    """Generate a unique PB-XXXX referral code."""
    chars = REFERRAL_CODE_CHARS
    suffix = "".join(secrets.choice(chars) for _ in range(REFERRAL_CODE_LENGTH))
    return f"{REFERRAL_CODE_PREFIX}{suffix}"


async def _generate_unique_code(db: aiosqlite.Connection) -> str:
    """Keep generating until we find a code not already in DB."""
    for _ in range(50):
        code = _generate_referral_code()
        cur = await db.execute(
            "SELECT id FROM referrers WHERE referral_code = ? COLLATE NOCASE", (code,)
        )
        if await cur.fetchone() is None:
            return code
    raise RuntimeError("Could not generate unique referral code after 50 attempts")


def _referral_link(code: str, base_url: str = "https://purebrain.ai") -> str:
    return f"{base_url}/?ref={code}"


def _affiliate_login_rate_check(ip: str) -> bool:
    """Returns True if this IP is allowed to attempt login (not rate-limited)."""
    now = time.time()
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16]
    entry = _AFFILIATE_LOGIN_ATTEMPTS.get(ip_hash)
    if entry is None:
        _AFFILIATE_LOGIN_ATTEMPTS[ip_hash] = {"count": 1, "window_start": now}
        return True
    if now - entry["window_start"] > _LOGIN_WINDOW_SECS:
        # Window expired — reset
        _AFFILIATE_LOGIN_ATTEMPTS[ip_hash] = {"count": 1, "window_start": now}
        return True
    if entry["count"] >= _LOGIN_MAX_ATTEMPTS:
        return False
    entry["count"] += 1
    return True


def _create_affiliate_session(referral_code: str) -> str:
    """Create and store a session token for an affiliate. Returns the token."""
    token = secrets.token_urlsafe(32)
    _AFFILIATE_SESSIONS[token] = {
        "code": referral_code.upper(),
        "expires": time.time() + _SESSION_TTL_SECS,
    }
    return token


def _verify_affiliate_session(token: str) -> str | None:
    """Verify a session token. Returns the referral_code on success, None otherwise."""
    if not token:
        return None
    entry = _AFFILIATE_SESSIONS.get(token)
    if entry is None:
        return None
    if time.time() > entry["expires"]:
        del _AFFILIATE_SESSIONS[token]
        return None
    return entry["code"]


async def _paypal_get_access_token() -> str | None:
    """Fetch a short-lived PayPal OAuth2 access token."""
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        return None
    base = "https://api-m.sandbox.paypal.com" if PAYPAL_SANDBOX else "https://api-m.paypal.com"
    url  = f"{base}/v1/oauth2/token"
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    credentials = f"{PAYPAL_CLIENT_ID}:{PAYPAL_CLIENT_SECRET}"
    b64 = __import__("base64").b64encode(credentials.encode()).decode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Basic {b64}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            return body.get("access_token")
    except Exception as e:
        print(f"[paypal] Failed to get access token: {e}")
        return None


async def _execute_paypal_payout(paypal_email: str, amount: float, request_id: str, note: str = "") -> dict:
    """Execute a PayPal payout via the Payouts API.

    Returns dict with keys: ok (bool), batch_id (str|None), error (str|None).
    """
    access_token = await _paypal_get_access_token()
    if not access_token:
        return {"ok": False, "batch_id": None, "error": "Could not obtain PayPal access token. Check credentials."}

    base = "https://api-m.sandbox.paypal.com" if PAYPAL_SANDBOX else "https://api-m.paypal.com"
    url  = f"{base}/v1/payments/payouts"

    sender_batch_id = f"pb-payout-{request_id}-{int(time.time())}"
    payload = {
        "sender_batch_header": {
            "sender_batch_id": sender_batch_id,
            "email_subject":   "PureBrain Affiliate Payout",
            "email_message":   note or "Your PureBrain affiliate commission payout has been sent.",
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "amount":         {"value": f"{amount:.2f}", "currency": "USD"},
                "receiver":       paypal_email,
                "note":           note or "PureBrain affiliate commission",
                "sender_item_id": request_id,
            }
        ],
    }
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization":  f"Bearer {access_token}",
            "Content-Type":   "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())
            batch_id = body.get("batch_header", {}).get("payout_batch_id", sender_batch_id)
            return {"ok": True, "batch_id": batch_id, "error": None}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        print(f"[paypal] Payout HTTP error {e.code}: {err_body}")
        return {"ok": False, "batch_id": None, "error": f"PayPal error {e.code}: {err_body[:200]}"}
    except Exception as e:
        print(f"[paypal] Payout exception: {e}")
        return {"ok": False, "batch_id": None, "error": str(e)}


def _hash_affiliate_password(password: str, salt: str = "") -> str:
    """Bcrypt hash of password. Returns bcrypt hash string.
    The `salt` param is ignored (kept for call-site compatibility with old code).
    """
    import bcrypt as _bcrypt
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt(rounds=12)).decode("utf-8")


def _verify_affiliate_password(password: str, stored_hash: str) -> bool:
    """Verify password against stored hash.
    Supports both bcrypt (new, starts with $2b$) and legacy SHA-256 (salt:hash).
    On successful legacy verify, the caller should migrate to bcrypt.
    """
    import bcrypt as _bcrypt
    if not stored_hash:
        return False
    if stored_hash.startswith("$2b$") or stored_hash.startswith("$2a$"):
        # Bcrypt hash
        try:
            return _bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
        except Exception:
            return False
    # Legacy SHA-256 format: salt:hexdigest
    if ":" not in stored_hash:
        return False
    parts = stored_hash.split(":", 1)
    if len(parts) != 2:
        return False
    salt_val, expected_hex = parts
    h = hashlib.sha256(f"{salt_val}:{password}".encode()).hexdigest()
    return hmac.compare_digest(h, expected_hex)


# ── Password reset tokens (in-memory, expire after 1 hour) ──────────────────
_password_reset_tokens: dict = {}  # token -> {"email": str, "expires": float}
_PASSWORD_RESET_EXPIRY = 3600  # 1 hour


def _send_reset_email(to_email: str, reset_url: str) -> bool:
    """Send a password reset email via Gmail SMTP."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    smtp_user = os.environ.get("SMTP_USER", "") or os.environ.get("GMAIL_USERNAME", "")
    smtp_pass = os.environ.get("SMTP_PASS", "") or os.environ.get("GOOGLE_APP_PASSWORD", "")
    if not smtp_user or not smtp_pass:
        print("[portal] WARNING: SMTP_USER/SMTP_PASS (and GMAIL_USERNAME/GOOGLE_APP_PASSWORD) not set — cannot send reset email")
        return False

    msg = MIMEMultipart("alternative")
    msg["From"] = f"PureBrain <{smtp_user}>"
    msg["To"] = to_email
    msg["Subject"] = "Reset Your PureBrain Affiliate Password"

    text = f"Reset your PureBrain affiliate password:\n\n{reset_url}\n\nThis link expires in 1 hour. If you didn't request this, ignore this email."

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="background:#080a12;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:32px;">
<div style="max-width:500px;margin:0 auto;background:#0d1120;border:1px solid #1e2a40;border-radius:12px;padding:40px;">
  <div style="text-align:center;margin-bottom:24px;">
    <span style="font-size:22px;font-weight:700;">
      <span style="color:#2a93c1;">PUREBR</span><span style="color:#f1420b;">AI</span><span style="color:#2a93c1;">N</span>
    </span>
  </div>
  <h2 style="color:#fff;font-size:20px;margin:0 0 16px;">Reset Your Password</h2>
  <p style="color:#9ca3af;font-size:14px;line-height:1.6;margin:0 0 24px;">
    Click the button below to reset your affiliate dashboard password. This link expires in 1 hour.
  </p>
  <div style="text-align:center;margin:32px 0;">
    <a href="{reset_url}" style="display:inline-block;background:linear-gradient(135deg,#2a93c1,#1d6e99);color:#fff;font-size:15px;font-weight:700;text-decoration:none;padding:14px 36px;border-radius:8px;box-shadow:0 4px 16px rgba(42,147,193,0.4);">
      Reset Password
    </a>
  </div>
  <p style="color:#6b7280;font-size:12px;text-align:center;">If you didn't request this, you can safely ignore this email.</p>
</div>
</body></html>"""

    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, to_email, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print(f"[reset-email] SMTP error: {e}")
        return False


async def api_referral_forgot_password(request: Request) -> JSONResponse:
    """POST /api/referral/forgot-password — send a password reset email."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    email = str(body.get("email", "")).strip().lower()
    if not email or "@" not in email:
        return JSONResponse({"error": "valid email required"}, status_code=400)

    # Always return success to prevent email enumeration
    success_msg = {"ok": True, "message": "If that email is registered, a reset link has been sent."}

    async with _referral_db() as db:
        cur = await db.execute(
            "SELECT referral_code FROM referrers WHERE user_email = ? COLLATE NOCASE",
            (email,)
        )
        row = await cur.fetchone()

    if not row:
        return JSONResponse(success_msg)

    # Generate reset token
    token = secrets.token_urlsafe(32)
    _password_reset_tokens[token] = {
        "email": email,
        "expires": time.time() + _PASSWORD_RESET_EXPIRY,
    }

    # Clean up expired tokens
    now = time.time()
    expired = [t for t, v in _password_reset_tokens.items() if v["expires"] < now]
    for t in expired:
        del _password_reset_tokens[t]

    reset_url = f"https://purebrain.ai/refer/?reset={token}"
    sent = _send_reset_email(email, reset_url)
    if not sent:
        print(f"[reset] Failed to send reset email to {email}")

    return JSONResponse(success_msg)


async def api_referral_reset_password(request: Request) -> JSONResponse:
    """POST /api/referral/reset-password — set new password using reset token."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    token = str(body.get("token", "")).strip()
    new_password = str(body.get("password", "")).strip()

    if not token:
        return JSONResponse({"error": "reset token required"}, status_code=400)
    if not new_password or len(new_password) < 6:
        return JSONResponse({"error": "password must be at least 6 characters"}, status_code=400)

    token_data = _password_reset_tokens.get(token)
    if not token_data:
        return JSONResponse({"error": "invalid or expired reset link. Please request a new one."}, status_code=400)

    if time.time() > token_data["expires"]:
        del _password_reset_tokens[token]
        return JSONResponse({"error": "reset link has expired. Please request a new one."}, status_code=400)

    email = token_data["email"]
    pw_hash = _hash_affiliate_password(new_password)

    async with _referral_db() as db:
        await db.execute(
            "UPDATE referrers SET password_hash = ? WHERE user_email = ? COLLATE NOCASE",
            (pw_hash, email)
        )
        await db.commit()

    # Consume the token
    del _password_reset_tokens[token]

    return JSONResponse({"ok": True, "message": "Password updated successfully. You can now log in."})



async def api_referral_register(request: Request) -> JSONResponse:
    """POST /api/referral/register — register as referrer, get unique code back."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    name     = str(body.get("name", "")).strip()
    email    = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", "")).strip()
    paypal_email = str(body.get("paypal_email", "")).strip()

    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return JSONResponse({"error": "invalid email"}, status_code=400)

    # Auto-generate password if not provided (public form doesn't include a password field)
    is_portal_auth = check_auth(request)
    if not password or len(password) < 6:
        password = secrets.token_urlsafe(16)

    pw_hash = _hash_affiliate_password(password)

    async with _referral_db() as db:
        # Check if already registered
        cur = await db.execute(
            "SELECT id, referral_code FROM referrers WHERE user_email = ? COLLATE NOCASE",
            (email,)
        )
        row = await cur.fetchone()
        if row:
            code = row[1]
            return JSONResponse({
                "ok": True,
                "referral_code": code,
                "referral_link": _referral_link(code),
                "existing": True,
                "message": "You are already registered. Here is your existing referral link.",
            })

        code = await _generate_unique_code(db)
        now  = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO referrers (user_name, user_email, referral_code, password_hash, paypal_email, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, email, code, pw_hash, paypal_email, now)
        )
        await db.commit()

    return JSONResponse({
        "ok": True,
        "referral_code": code,
        "referral_link": _referral_link(code),
        "existing": False,
        "message": "Registration successful!",
    })


async def api_referral_login(request: Request) -> JSONResponse:
    """POST /api/referral/login — verify affiliate password, return referral code."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    code     = str(body.get("referral_code", "")).strip().upper()
    email    = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", "")).strip()

    if not password:
        return JSONResponse({"error": "password required"}, status_code=400)
    if not code and not email:
        return JSONResponse({"error": "referral_code or email required"}, status_code=400)

    # Rate-limit by IP (H1 fix — matches /session endpoint)
    client_ip = request.client.host if request.client else "unknown"
    if not _affiliate_login_rate_check(client_ip):
        return JSONResponse({"error": "too many login attempts — try again later"}, status_code=429)

    async with _referral_db() as db:
        db.row_factory = aiosqlite.Row
        if code:
            cur = await db.execute(
                "SELECT referral_code, password_hash FROM referrers WHERE referral_code = ? COLLATE NOCASE",
                (code,)
            )
        else:
            cur = await db.execute(
                "SELECT referral_code, password_hash FROM referrers WHERE user_email = ? COLLATE NOCASE",
                (email,)
            )
        row = await cur.fetchone()

    if row is None:
        return JSONResponse({"error": "referrer not found"}, status_code=404)

    stored_hash = row["password_hash"]
    if not stored_hash:
        # Account created before password system — allow any password and set it now
        pw_hash = _hash_affiliate_password(password)
        async with _referral_db() as db:
            await db.execute(
                "UPDATE referrers SET password_hash = ? WHERE referral_code = ? COLLATE NOCASE",
                (pw_hash, row["referral_code"])
            )
            await db.commit()
        # Log the first-time password claim for security audit (H2 fix)
        print(f"[referral][SECURITY] First password claim for {row['referral_code']} from IP {client_ip}")
        try:
            _send_telegram_notification(
                f"FIRST PASSWORD SET\n"
                f"Code: {row['referral_code']}\n"
                f"IP: {client_ip}\n"
                f"Via: /api/referral/login"
            )
        except Exception:
            pass  # notification is best-effort
    elif not _verify_affiliate_password(password, stored_hash):
        return JSONResponse({"error": "incorrect password"}, status_code=401)
    elif not (stored_hash.startswith("$2b$") or stored_hash.startswith("$2a$")):
        # Auto-migrate legacy SHA-256 hash to bcrypt on successful login
        migrated_hash = _hash_affiliate_password(password)
        async with _referral_db() as db:
            await db.execute(
                "UPDATE referrers SET password_hash = ? WHERE referral_code = ? COLLATE NOCASE",
                (migrated_hash, row["referral_code"])
            )
            await db.commit()

    return JSONResponse({"ok": True, "referral_code": row["referral_code"]})


async def api_referral_session(request: Request) -> JSONResponse:
    """POST /api/referral/session — login and receive a session token for dashboard access.

    Body: { email, password } or { referral_code, password }
    Returns: { ok, session_token, referral_code, expires_in }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    code     = str(body.get("referral_code", "")).strip().upper()
    email    = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", "")).strip()

    if not password:
        return JSONResponse({"error": "password required"}, status_code=400)
    if not code and not email:
        return JSONResponse({"error": "referral_code or email required"}, status_code=400)

    # Rate-limit by IP
    client_ip = request.client.host if request.client else ""
    if not _affiliate_login_rate_check(client_ip):
        return JSONResponse({"error": "too many login attempts. Please wait 15 minutes."}, status_code=429)

    async with _referral_db() as db:
        db.row_factory = aiosqlite.Row
        if code:
            cur = await db.execute(
                "SELECT referral_code, password_hash FROM referrers WHERE referral_code = ? COLLATE NOCASE",
                (code,)
            )
        else:
            cur = await db.execute(
                "SELECT referral_code, password_hash FROM referrers WHERE user_email = ? COLLATE NOCASE",
                (email,)
            )
        row = await cur.fetchone()

    if row is None:
        return JSONResponse({"error": "account not found"}, status_code=404)

    stored_hash = row["password_hash"]
    if not stored_hash:
        # First login — set the password
        pw_hash = _hash_affiliate_password(password)
        async with _referral_db() as db:
            await db.execute(
                "UPDATE referrers SET password_hash = ? WHERE referral_code = ? COLLATE NOCASE",
                (pw_hash, row["referral_code"])
            )
            await db.commit()
        # FIX 3: Log and notify on first-login password claim
        _code_for_log = row["referral_code"]
        print(f"[SECURITY] First-login password claim for affiliate code {_code_for_log} from IP {client_ip}")
        _tg_send = Path(__file__).parent.parent / "projects" / "AI-CIV" / "aether" / "tools" / "tg_send.sh"
        if _tg_send.exists():
            try:
                subprocess.Popen(
                    [str(_tg_send), f"[SECURITY] First-login claim: affiliate {_code_for_log} from IP {client_ip}. Verify this is legitimate."],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except Exception as _e:
                print(f"[SECURITY] TG notify failed: {_e}")
    elif not _verify_affiliate_password(password, stored_hash):
        return JSONResponse({"error": "incorrect password"}, status_code=401)
    elif not (stored_hash.startswith("$2b$") or stored_hash.startswith("$2a$")):
        # Auto-migrate legacy SHA-256 hash to bcrypt on successful login
        migrated_hash = _hash_affiliate_password(password)
        async with _referral_db() as db:
            await db.execute(
                "UPDATE referrers SET password_hash = ? WHERE referral_code = ? COLLATE NOCASE",
                (migrated_hash, row["referral_code"])
            )
            await db.commit()

    referral_code = row["referral_code"]
    session_token = _create_affiliate_session(referral_code)

    return JSONResponse({
        "ok":           True,
        "session_token": session_token,
        "referral_code": referral_code,
        "expires_in":    _SESSION_TTL_SECS,
    })


async def api_referral_dashboard(request: Request) -> JSONResponse:
    """GET /api/referral/dashboard?code=PB-XXXX — referrer stats. Requires session token or portal bearer token."""
    code     = request.query_params.get("code", "").strip().upper()
    email    = request.query_params.get("email", "").strip().lower()
    # Portal owner (admin) can see any dashboard without affiliate password
    portal_authed = check_auth(request)

    if not code and not email:
        return JSONResponse({"error": "missing code or email"}, status_code=400)

    async with _referral_db() as db:
        db.row_factory = aiosqlite.Row
        if code:
            cur = await db.execute(
                "SELECT * FROM referrers WHERE referral_code = ? COLLATE NOCASE", (code,)
            )
        else:
            cur = await db.execute(
                "SELECT * FROM referrers WHERE user_email = ? COLLATE NOCASE", (email,)
            )
        referrer = await cur.fetchone()
        if referrer is None:
            return JSONResponse({"error": "referrer not found"}, status_code=404)

        # Security: dashboard requires either:
        #   1. Portal bearer token (admin)
        #   2. A valid affiliate session token (?session=TOKEN, or X-Affiliate-Session header)
        #   3. Direct password param (?password=...) as fallback for API callers
        if not portal_authed:
            session_token = (
                request.query_params.get("session", "").strip()
                or request.headers.get("x-affiliate-session", "").strip()
            )
            session_code = _verify_affiliate_session(session_token)
            if session_code:
                # Session token valid — verify it belongs to this referrer
                if session_code.upper() != referrer["referral_code"].upper():
                    return JSONResponse({"error": "session token does not match this account"}, status_code=403)
            else:
                # Password-in-URL removed (security: passwords in query params leak to logs/history).
                # Use POST /api/referral/session to get a session token, then pass ?session=TOKEN.
                return JSONResponse({"error": "authentication required — use a session token (?session=TOKEN) or login at purebrain.ai/refer/"}, status_code=401)

        referrer_id   = referrer["id"]
        referral_code = referrer["referral_code"]

        # Referral counts (exclude rejected paypal placeholder ghosts)
        cur = await db.execute(
            """SELECT COUNT(*) FROM referrals WHERE referrer_id = ?
               AND NOT (status = 'rejected' AND referred_email LIKE 'paypal_%@pending')""",
            (referrer_id,)
        )
        total_referrals = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND status = 'completed'",
            (referrer_id,)
        )
        completed = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND status = 'pending'",
            (referrer_id,)
        )
        pending = (await cur.fetchone())[0]

        # Total earnings from rewards table
        cur = await db.execute(
            "SELECT COALESCE(SUM(reward_value), 0) FROM rewards WHERE referrer_id = ?",
            (referrer_id,)
        )
        earnings = float((await cur.fetchone())[0])

        # Click count
        cur = await db.execute(
            "SELECT COUNT(*) FROM referral_clicks WHERE referral_code = ? COLLATE NOCASE",
            (referral_code,)
        )
        total_clicks = (await cur.fetchone())[0]

        # Referral history
        # Referral history with total commission earned per referred member
        # Exclude rejected placeholder entries (paypal_*@pending) — they are
        # unresolved webhook artifacts, not real referrals.
        cur = await db.execute(
            """SELECT r.referred_name, r.referred_email, r.status, r.created_at,
                      COALESCE(SUM(cp.commission_value), 0) AS earnings,
                      COUNT(cp.id) AS payment_count
               FROM referrals r
               LEFT JOIN commission_payments cp ON cp.referral_id = r.id
               WHERE r.referrer_id = ?
                 AND NOT (r.status = 'rejected' AND r.referred_email LIKE 'paypal_%@pending')
               GROUP BY r.id
               ORDER BY r.created_at DESC""",
            (referrer_id,)
        )
        history = [dict(row) async for row in cur]

    reward_tiers = [
        {"label": "Commission Rate", "reward": f"{REFERRAL_COMMISSION_RATE * 100:.0f}% of every payment"},
        {"label": "Frequency", "reward": "Every month, for as long as they are a member"},
        {"label": "Awakened ($149/mo)", "reward": "$7.45/month per referral"},
        {"label": "Partnered ($499/mo)", "reward": "$24.95/month per referral"},
        {"label": "Unified ($999/mo)", "reward": "$49.95/month per referral"},
        {"label": "Enterprise (Custom)", "reward": "5% of custom monthly rate"},
    ]

    return JSONResponse({
        "referral_code": referral_code,
        "referral_link": _referral_link(referral_code),
        "email": referrer["user_email"],
        "name": referrer["user_name"],
        "paypal_email": referrer["paypal_email"],
        "total_referrals": total_referrals,
        "completed": completed,
        "pending": pending,
        "earnings": round(earnings, 2),
        "total_clicks": total_clicks,
        "history": history,
        "reward_tiers": reward_tiers,
        "commission_rate": REFERRAL_COMMISSION_RATE,
        "commission_rate_pct": f"{REFERRAL_COMMISSION_RATE * 100:.0f}%",
        "model": "recurring",
    })


async def api_referral_track(request: Request) -> JSONResponse:
    """POST /api/referral/track — log a referral link click."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    code = str(body.get("referral_code", "")).strip().upper()
    if not code:
        return JSONResponse({"error": "missing referral_code"}, status_code=400)

    # Hash IP for privacy
    client_ip = request.client.host if request.client else ""
    ip_hash = hashlib.sha256(client_ip.encode()).hexdigest()[:16]

    # HIGH-007: Rate limit click tracking to prevent spam/inflation
    now_ts = time.time()
    entry = _TRACK_RATE_LIMITS.get(ip_hash)
    if entry is None:
        _TRACK_RATE_LIMITS[ip_hash] = {"count": 1, "window_start": now_ts}
    elif now_ts - entry["window_start"] > _TRACK_WINDOW_SECS:
        _TRACK_RATE_LIMITS[ip_hash] = {"count": 1, "window_start": now_ts}
    elif entry["count"] >= _TRACK_MAX_PER_WINDOW:
        return JSONResponse({"error": "rate limited"}, status_code=429)
    else:
        entry["count"] += 1

    now = datetime.now(timezone.utc).isoformat()

    async with _referral_db() as db:
        # Verify code exists
        cur = await db.execute(
            "SELECT id FROM referrers WHERE referral_code = ? COLLATE NOCASE", (code,)
        )
        if await cur.fetchone() is None:
            return JSONResponse({"error": "invalid referral code"}, status_code=404)

        await db.execute(
            "INSERT INTO referral_clicks (referral_code, ip_hash, clicked_at) VALUES (?, ?, ?)",
            (code, ip_hash, now)
        )
        await db.commit()

    return JSONResponse({"ok": True})


async def api_referral_complete(request: Request) -> JSONResponse:
    """POST /api/referral/complete — mark a referral as completed and issue reward.

    NOTE: This endpoint is intentionally PUBLIC (no auth required).
    It is called from browser JS on the landing pages immediately after PayPal payment.
    The browser has no bearer token to send. The referral_code itself acts as the
    credential — only existing referrer codes proceed past the lookup step.
    Single-referrer enforcement: any previous completed referral for this email under
    a DIFFERENT referrer is deleted before recording the new one.
    """
    # If a completion secret is configured, require it (C2 security fix)
    complete_secret = os.environ.get("REFERRAL_COMPLETE_SECRET", "")
    if complete_secret:
        provided = request.headers.get("x-referral-secret", "")
        if not hmac.compare_digest(provided, complete_secret):
            return JSONResponse({"error": "unauthorized"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    referral_code  = str(body.get("referral_code", "")).strip().upper()
    referred_email = str(body.get("referred_email", "")).strip().lower()
    referred_name  = str(body.get("referred_name", "")).strip()
    order_id       = str(body.get("order_id", "")).strip()  # PayPal subscription/order ID

    if not referral_code:
        return JSONResponse({"error": "missing referral_code"}, status_code=400)

    # referred_email is optional for subscription payments where PayPal doesn't
    # provide payer email in the onApprove callback. We accept an empty email and
    # use a placeholder derived from order_id so the referral is still recorded.
    # The admin can manually update the email later via PUT /api/admin/referral/update.
    if not referred_email or "@" not in referred_email:
        if order_id:
            # Try to resolve real client info from clients.db by PayPal subscription ID
            resolved = False
            try:
                async with aiosqlite.connect(str(CLIENTS_DB)) as cdb:
                    cdb.row_factory = aiosqlite.Row
                    ccur = await cdb.execute(
                        "SELECT name, email FROM clients WHERE paypal_subscription_id = ? COLLATE NOCASE LIMIT 1",
                        (order_id,)
                    )
                    client_row = await ccur.fetchone()
                    if client_row and client_row["email"]:
                        referred_email = client_row["email"].strip().lower()
                        if not referred_name:
                            referred_name = client_row["name"].strip()
                        resolved = True
                        print(f"[referral] Resolved PayPal order {order_id} -> {referred_name} <{referred_email}>")
            except Exception as e:
                print(f"[referral] Client lookup failed for order {order_id}: {e}")

            if not resolved:
                # Fallback: record with a placeholder email so the row is traceable
                referred_email = f"paypal_{order_id.lower()}@pending"
        else:
            return JSONResponse({"error": "invalid referred_email"}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()

    async with _referral_db() as db:
        cur = await db.execute(
            "SELECT id FROM referrers WHERE referral_code = ? COLLATE NOCASE", (referral_code,)
        )
        row = await cur.fetchone()
        if row is None:
            return JSONResponse({"error": "invalid referral code"}, status_code=404)
        referrer_id = row[0]

        # Single-referrer enforcement: remove any existing completed referral for
        # this email under a DIFFERENT referrer so a client is never double-counted.
        # Skip single-referrer check for placeholder emails (they are unique per order).
        if "@pending" not in referred_email:
            await db.execute(
                """DELETE FROM referrals
                   WHERE referred_email = ? COLLATE NOCASE
                     AND referrer_id != ?""",
                (referred_email, referrer_id)
            )

        # Prevent double-completion for same referred email under this referrer.
        # For placeholder emails (subscription path), always insert a new row since
        # each order_id is unique and the real email is unknown at this point.
        existing = None
        if "@pending" not in referred_email:
            cur = await db.execute(
                """SELECT id, status FROM referrals
                   WHERE referrer_id = ? AND referred_email = ? COLLATE NOCASE""",
                (referrer_id, referred_email)
            )
            existing = await cur.fetchone()

        if existing:
            if existing[1] == "completed":
                await db.commit()
                return JSONResponse({"ok": True, "message": "already completed"})
            # Update existing pending row
            referral_id = existing[0]
            await db.execute(
                "UPDATE referrals SET status='completed', completed_at=?, referred_name=? WHERE id=?",
                (now, referred_name or "", referral_id)
            )
        else:
            cur = await db.execute(
                """INSERT INTO referrals (referrer_id, referred_email, referred_name, status, created_at, completed_at)
                   VALUES (?, ?, ?, 'completed', ?, ?)""",
                (referrer_id, referred_email, referred_name, now, now)
            )
            referral_id = cur.lastrowid

        await db.commit()

    print(f"[referral] complete: {referral_code} → {referred_email}")
    # Referral relationship recorded. Commission (5% recurring) will be issued
    # automatically each time this referred member makes a payment.
    return JSONResponse({"ok": True, "message": "Referral recorded. You will earn 5% of every payment this member makes."})


async def api_referral_record_commission(request: Request) -> JSONResponse:
    """POST /api/referral/commission — record a 5% recurring commission payment.

    Called by purebrain_log_server when a payment is verified.
    Payload: { payer_email, order_id, amount, tier }
    Requires bearer token authentication.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    payer_email = str(body.get("payer_email", "")).strip().lower()
    order_id    = str(body.get("order_id", "")).strip()
    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0.0
    tier = str(body.get("tier", "")).strip()

    if not payer_email or "@" not in payer_email:
        return JSONResponse({"error": "missing valid payer_email"}, status_code=400)
    if not order_id:
        return JSONResponse({"error": "missing order_id"}, status_code=400)
    if amount <= 0:
        return JSONResponse({"ok": True, "skipped": "zero amount, no commission"})

    now = datetime.now(timezone.utc).isoformat()
    commission_value = round(amount * REFERRAL_COMMISSION_RATE, 2)

    async with _referral_db() as db:
        # Find a completed referral where this payer was referred
        cur = await db.execute(
            """SELECT ref.id, ref.referrer_id
               FROM referrals ref
               WHERE ref.referred_email = ? COLLATE NOCASE AND ref.status = 'completed'
               LIMIT 1""",
            (payer_email,)
        )
        row = await cur.fetchone()
        if row is None:
            # This payer was not referred — no commission
            return JSONResponse({"ok": True, "skipped": "payer not in referrals"})

        referral_id  = row[0]
        referrer_id  = row[1]

        # Prevent duplicate commission for same order_id
        cur = await db.execute(
            "SELECT id FROM commission_payments WHERE order_id = ?", (order_id,)
        )
        if await cur.fetchone():
            return JSONResponse({"ok": True, "skipped": "duplicate order_id"})

        # Record commission payment
        await db.execute(
            """INSERT INTO commission_payments
               (referrer_id, referral_id, payer_email, order_id, payment_amount,
                commission_rate, commission_value, tier, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (referrer_id, referral_id, payer_email, order_id, amount,
             REFERRAL_COMMISSION_RATE, commission_value, tier, now)
        )
        # Also insert into rewards table so balance queries stay consistent
        await db.execute(
            """INSERT INTO rewards (referrer_id, referral_id, reward_type, reward_value, issued_at)
               VALUES (?, ?, 'commission', ?, ?)""",
            (referrer_id, referral_id, commission_value, now)
        )
        await db.commit()

        # Fetch referrer info for notification
        cur = await db.execute(
            "SELECT user_name, user_email FROM referrers WHERE id = ?", (referrer_id,)
        )
        referrer_row = await cur.fetchone()
        referrer_name  = referrer_row[0] if referrer_row else "Unknown"
        referrer_email = referrer_row[1] if referrer_row else ""

    print(f"[referral] Commission recorded: ${commission_value:.2f} for referrer {referrer_email} "
          f"(order {order_id}, payer {payer_email}, amount ${amount:.2f})")

    return JSONResponse({
        "ok": True,
        "commission_value": commission_value,
        "referrer_email": referrer_email,
        "referrer_name": referrer_name,
        "payer_email": payer_email,
        "order_id": order_id,
        "payment_amount": amount,
        "tier": tier,
    })


async def api_referral_code_lookup(request: Request) -> JSONResponse:
    """GET /api/referral/code/{email} — get referral code for a registered email."""
    # Require portal bearer token or affiliate session (H4 fix — prevent email enumeration)
    if not check_auth(request):
        session_token = request.query_params.get("session", "") or request.headers.get("x-affiliate-session", "")
        if not session_token or not _verify_affiliate_session(session_token):
            return JSONResponse({"error": "authentication required"}, status_code=401)

    email = request.path_params.get("email", "").strip().lower()
    if not email:
        return JSONResponse({"error": "missing email"}, status_code=400)

    async with _referral_db() as db:
        cur = await db.execute(
            "SELECT referral_code FROM referrers WHERE user_email = ? COLLATE NOCASE", (email,)
        )
        row = await cur.fetchone()

    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    code = row[0]
    return JSONResponse({
        "referral_code": code,
        "referral_link": _referral_link(code),
    })


async def api_referral_paypal_email(request: Request) -> JSONResponse:
    """POST /api/referral/paypal-email — save PayPal email for a referrer. Requires affiliate password."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    email        = str(body.get("email", "")).strip().lower()
    paypal_email = str(body.get("paypal_email", "")).strip().lower()
    password     = str(body.get("password", "")).strip()

    if not email or "@" not in email:
        return JSONResponse({"error": "invalid email"}, status_code=400)
    if not paypal_email or "@" not in paypal_email:
        return JSONResponse({"error": "invalid paypal_email"}, status_code=400)

    # Auth: portal bearer, affiliate session token, or password
    portal_authed = check_auth(request)

    async with _referral_db() as db:
        if not portal_authed:
            session_token = (
                str(body.get("session_token", "")).strip()
                or request.headers.get("x-affiliate-session", "").strip()
            )
            session_code = _verify_affiliate_session(session_token)
            if not session_code:
                # Fallback to password
                cur_pw = await db.execute(
                    "SELECT password_hash FROM referrers WHERE user_email = ? COLLATE NOCASE", (email,)
                )
                row_pw = await cur_pw.fetchone()
                if row_pw is None:
                    return JSONResponse({"error": "referrer not found"}, status_code=404)
                stored_hash = row_pw[0]
                if stored_hash and not _verify_affiliate_password(password, stored_hash):
                    return JSONResponse({"error": "incorrect password or session required"}, status_code=401)

        cur = await db.execute(
            "UPDATE referrers SET paypal_email = ? WHERE user_email = ? COLLATE NOCASE",
            (paypal_email, email)
        )
        await db.commit()
        if cur.rowcount == 0:
            # If the caller is authenticated via portal bearer token but has no
            # referrer row yet (auto-registration hasn't run or raced), create
            # one now so the PayPal save succeeds on first attempt.
            if portal_authed:
                code = await _generate_unique_code(db)
                now  = datetime.now(timezone.utc).isoformat()
                name = email.split("@")[0]
                pw_hash = _hash_affiliate_password(secrets.token_urlsafe(16))
                await db.execute(
                    "INSERT INTO referrers (user_name, user_email, referral_code, password_hash, paypal_email, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (name, email, code, pw_hash, paypal_email, now)
                )
                await db.commit()
            else:
                return JSONResponse({"error": "referrer not found"}, status_code=404)

    return JSONResponse({"ok": True})


async def api_referral_leaderboard(request: Request) -> JSONResponse:
    """GET /api/referral/leaderboard -- top referrers by completed referrals.

    FIX (2026-03-31): Use subqueries instead of double LEFT JOIN to avoid
    cartesian product between referrals and rewards tables.
    """
    limit = min(int(request.query_params.get("limit", "10")), 50)

    async with _referral_db() as db:
        cur = await db.execute(
            """SELECT r.user_name, r.referral_code,
                      COALESCE(ref_counts.completed_count, 0) AS completed_count,
                      COALESCE(rw_totals.total_earned, 0) AS total_earned
               FROM referrers r
               LEFT JOIN (
                   SELECT referrer_id, COUNT(*) AS completed_count
                   FROM referrals
                   WHERE status = 'completed'
                   GROUP BY referrer_id
               ) ref_counts ON ref_counts.referrer_id = r.id
               LEFT JOIN (
                   SELECT referrer_id, SUM(reward_value) AS total_earned
                   FROM rewards
                   GROUP BY referrer_id
               ) rw_totals ON rw_totals.referrer_id = r.id
               ORDER BY completed_count DESC, total_earned DESC
               LIMIT ?""",
            (limit,)
        )
        rows = await cur.fetchall()

    leaders = [
        {
            "name": row[0] or "Anonymous",
            "referral_code": row[1],
            "completed": row[2],
            "total_earned": round(float(row[3]), 2),
        }
        for row in rows
    ]
    return JSONResponse({"leaderboard": leaders})


async def api_portal_owner(request: Request) -> JSONResponse:
    """Return portal owner identity for dynamic referral/share features."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    owner_file = SCRIPT_DIR / "portal_owner.json"
    try:
        owner = json.loads(owner_file.read_text())
        return JSONResponse(owner)
    except Exception:
        return JSONResponse({"name": "Portal User", "email": "", "referral_code": ""})


# ---------------------------------------------------------------------------
# Payout Request API (Phase 3a — Manual Bridge)
# ---------------------------------------------------------------------------

def _send_telegram_notification(message: str) -> bool:
    """Send a Telegram notification via tg_send.sh (searches standard locations)."""
    try:
        candidates = [
            Path.home() / "civ" / "tools" / "tg_send.sh",
            Path.home() / "tools" / "tg_send.sh",
        ]
        for tg_send in candidates:
            if tg_send.exists():
                subprocess.run(
                    ["bash", str(tg_send), message],
                    timeout=15, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL
                )
                return True
    except Exception:
        pass
    return False


def _read_payout_requests_legacy() -> list:
    """(LEGACY) Read all payout requests from JSONL file. Kept for migration only."""
    requests_list = []
    if not PAYOUT_REQUESTS_FILE.exists():
        return requests_list
    try:
        with PAYOUT_REQUESTS_FILE.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    requests_list.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return requests_list


def _write_payout_request_legacy(entry: dict) -> None:
    """(LEGACY) Append a payout request to JSONL file. Kept for migration only."""
    with PAYOUT_REQUESTS_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _update_payout_status_legacy(request_id: str, status: str, batch_id: str = "") -> bool:
    """(LEGACY) Update payout request in JSONL file. Kept for migration only."""
    all_requests = _read_payout_requests_legacy()
    found = False
    for req in all_requests:
        if req.get("request_id") == request_id:
            req["status"] = status
            if batch_id:
                req["batch_id"] = batch_id
            if status in ("completed", "paid"):
                req["paid_at"] = datetime.now(timezone.utc).isoformat()
            found = True
            break
    if not found:
        return False
    try:
        with PAYOUT_REQUESTS_FILE.open("w") as f:
            for req in all_requests:
                f.write(json.dumps(req) + "\n")
    except Exception:
        return False
    return True


# ── New SQLite-backed payout helpers (replace JSONL) ──

async def _read_payout_requests_db() -> list:
    """Read all payout requests from SQLite."""
    async with _referral_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM payout_requests ORDER BY created_at DESC")
        rows = await cur.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            # Synthesize created_at_ts from ISO created_at for backward compat
            try:
                dt = datetime.fromisoformat(d.get("created_at", ""))
                d["created_at_ts"] = dt.timestamp()
            except (ValueError, TypeError):
                d["created_at_ts"] = 0.0
            result.append(d)
        return result


async def _write_payout_request_db(entry: dict) -> None:
    """Insert a payout request into SQLite. Raises IntegrityError on duplicate request_id."""
    async with _referral_db() as db:
        await db.execute(
            """INSERT INTO payout_requests
               (request_id, referral_code, paypal_email, amount, status, batch_id, notes, created_at, paid_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (entry["request_id"], entry["referral_code"], entry.get("paypal_email", ""),
             entry.get("amount", 0.0), entry.get("status", "pending"),
             entry.get("batch_id", ""), entry.get("notes", ""),
             entry.get("created_at", ""), entry.get("paid_at"))
        )
        await db.commit()


async def _update_payout_status_db(request_id: str, status: str, batch_id: str = "", notes: str = "") -> bool:
    """Update payout request status in SQLite. Returns True if row was found and updated."""
    async with _referral_db() as db:
        paid_at = datetime.now(timezone.utc).isoformat() if status in ("completed", "paid") else None
        cur = await db.execute(
            """UPDATE payout_requests
               SET status = ?,
                   batch_id = CASE WHEN ? != '' THEN ? ELSE batch_id END,
                   notes = CASE WHEN ? != '' THEN ? ELSE notes END,
                   paid_at = CASE WHEN ? IS NOT NULL THEN ? ELSE paid_at END
               WHERE request_id = ?""",
            (status, batch_id, batch_id, notes, notes, paid_at, paid_at, request_id)
        )
        await db.commit()
        return cur.rowcount > 0


async def api_referral_payout_request(request: Request) -> JSONResponse:
    """POST /api/referral/payout-request — user requests a payout.

    Requires affiliate session token OR portal bearer token.
    Body: { referral_code, paypal_email, amount, session_token? }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    # Auth: portal bearer OR valid affiliate session
    portal_authed = check_auth(request)
    session_code = None
    if not portal_authed:
        session_token = (
            str(body.get("session_token", "")).strip()
            or request.headers.get("x-affiliate-session", "").strip()
        )
        session_code = _verify_affiliate_session(session_token)
        if not session_code:
            return JSONResponse({"error": "authentication required"}, status_code=401)

    paypal_email = str(body.get("paypal_email", "")).strip().lower()
    referral_code = str(body.get("referral_code", "")).strip()
    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid amount"}, status_code=400)

    if not paypal_email or "@" not in paypal_email or "." not in paypal_email.split("@")[-1]:
        return JSONResponse({"error": "invalid paypal_email"}, status_code=400)

    if not referral_code:
        return JSONResponse({"error": "missing referral_code"}, status_code=400)

    # IDOR fix: affiliate sessions can only request payouts for their own code
    if not portal_authed and session_code and session_code.upper() != referral_code.upper():
        return JSONResponse({"error": "access denied"}, status_code=403)

    if amount < PAYOUT_MIN_AMOUNT:
        return JSONResponse(
            {"error": f"minimum payout is ${PAYOUT_MIN_AMOUNT:.0f}"},
            status_code=400
        )

    existing = await _read_payout_requests_db()
    cooldown_secs = PAYOUT_COOLDOWN_DAYS * 86400
    now_ts = time.time()
    for req in existing:
        if req.get("referral_code") == referral_code and req.get("status") in ("pending", "processing"):
            created_at = req.get("created_at_ts", 0)
            if (now_ts - created_at) < cooldown_secs:
                days_left = int((cooldown_secs - (now_ts - created_at)) / 86400) + 1
                return JSONResponse(
                    {"error": f"payout already requested. Please wait {days_left} more day(s)."},
                    status_code=429
                )

    # Check balance against SQLite rewards table
    actual_earnings = 0.0
    try:
        async with _referral_db() as _db:
            _cur = await _db.execute(
                """SELECT COALESCE(SUM(rw.reward_value), 0)
                   FROM rewards rw
                   JOIN referrers r ON r.id = rw.referrer_id
                   WHERE r.referral_code = ? COLLATE NOCASE""",
                (referral_code,)
            )
            _row = await _cur.fetchone()
            actual_earnings = float(_row[0]) if _row else 0.0
    except Exception as e:
        print(f"[referral] DB error during payout balance check: {e}")
        return JSONResponse(
            {"error": "unable to verify balance — please try again later"},
            status_code=503
        )

    # Subtract already-paid amounts from available balance (C4 fix, now atomic via SQLite)
    try:
        async with _referral_db() as _db:
            _cur = await _db.execute(
                """SELECT COALESCE(SUM(amount), 0) FROM payout_requests
                   WHERE referral_code = ? COLLATE NOCASE
                   AND status IN ('completed', 'paid')""",
                (referral_code,)
            )
            _row = await _cur.fetchone()
            paid_total = float(_row[0]) if _row else 0.0
            actual_earnings -= paid_total
    except Exception as e:
        print(f"[referral] Error reading payout history for balance deduction: {e}")
        return JSONResponse(
            {"error": "unable to verify payout history — please try again later"},
            status_code=503
        )

    if amount > actual_earnings:
        return JSONResponse(
            {"error": f"requested amount ${amount:.2f} exceeds available balance ${actual_earnings:.2f}"},
            status_code=400
        )

    request_id = f"payout-{referral_code}-{int(now_ts)}"
    entry = {
        "request_id": request_id,
        "referral_code": referral_code,
        "paypal_email": paypal_email,
        "amount": round(amount, 2),
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_at_ts": now_ts,
        "paid_at": None,
        "notes": "",
    }
    await _write_payout_request_db(entry)

    # Audit log: payout requested
    client_ip = request.client.host if request.client else "unknown"
    await _log_financial_event(
        event_type="payout_requested",
        referral_code=referral_code,
        amount=round(amount, 2),
        actor=f"affiliate:{referral_code}",
        details=f"paypal={paypal_email}, request_id={request_id}, available_balance=${actual_earnings:.2f}",
        ip_address=client_ip,
    )

    # Auto-approve payouts up to $1,000; larger amounts require manual approval
    if amount <= PAYOUT_AUTO_APPROVE_LIMIT:
        try:
            payout_result = await _execute_paypal_payout(
                paypal_email=paypal_email,
                amount=round(amount, 2),
                request_id=request_id,
                note=f"PureBrain referral payout for {referral_code}",
            )
            if payout_result.get("ok"):
                # Update payout status to completed
                await _update_payout_status_db(request_id, "completed", batch_id=payout_result.get("batch_id", ""))
                # Audit log: auto-payout sent
                await _log_financial_event(
                    event_type="auto_payout_sent",
                    referral_code=referral_code,
                    amount=round(amount, 2),
                    actor=f"affiliate:{referral_code}",
                    details=f"paypal={paypal_email}, batch_id={payout_result.get('batch_id', 'n/a')}, request_id={request_id}",
                    ip_address=client_ip,
                )
                tg_msg = (
                    f"AUTO-PAYOUT SENT\n"
                    f"Referral: {referral_code}\n"
                    f"Amount: ${amount:.2f}\n"
                    f"PayPal: {paypal_email}\n"
                    f"Batch ID: {payout_result.get('batch_id', 'n/a')}\n"
                    f"Request ID: {request_id}"
                )
                _send_telegram_notification(tg_msg)
                return JSONResponse({
                    "ok": True,
                    "request_id": request_id,
                    "message": f"Payout of ${amount:.2f} sent to {paypal_email}!",
                    "amount": round(amount, 2),
                    "paypal_email": paypal_email,
                    "auto_approved": True,
                    "batch_id": payout_result.get("batch_id"),
                })
            else:
                # PayPal failed — fall through to manual
                tg_msg = (
                    f"AUTO-PAYOUT FAILED — NEEDS MANUAL\n"
                    f"Referral: {referral_code}\n"
                    f"Amount: ${amount:.2f}\n"
                    f"PayPal: {paypal_email}\n"
                    f"Error: {payout_result.get('error', 'unknown')}\n"
                    f"Request ID: {request_id}"
                )
                _send_telegram_notification(tg_msg)
        except Exception as e:
            tg_msg = (
                f"AUTO-PAYOUT EXCEPTION — NEEDS MANUAL\n"
                f"Referral: {referral_code}\n"
                f"Amount: ${amount:.2f}\n"
                f"PayPal: {paypal_email}\n"
                f"Error: {str(e)[:200]}\n"
                f"Request ID: {request_id}"
            )
            _send_telegram_notification(tg_msg)
    else:
        # Over $1,000 — require manual approval
        tg_msg = (
            f"PAYOUT REQUEST — MANUAL APPROVAL REQUIRED (>${PAYOUT_AUTO_APPROVE_LIMIT:.0f})\n"
            f"Referral: {referral_code}\n"
            f"Amount: ${amount:.2f}\n"
            f"PayPal: {paypal_email}\n"
            f"Request ID: {request_id}\n"
            f"Earnings on file: ${actual_earnings:.2f}\n"
            f"To approve: POST /api/referral/payout-approve with request_id"
        )
        _send_telegram_notification(tg_msg)

    return JSONResponse({
        "ok": True,
        "request_id": request_id,
        "message": "Payout request submitted. We will process within 2 business days." if amount > PAYOUT_AUTO_APPROVE_LIMIT else "Payout is being processed.",
        "amount": round(amount, 2),
        "paypal_email": paypal_email,
    })


async def api_referral_payout_history(request: Request) -> JSONResponse:
    """GET /api/referral/payout-history?referral_code=XXX&session=TOKEN"""
    portal_authed = check_auth(request)
    session_code = None
    if not portal_authed:
        session_token = (
            request.query_params.get("session", "").strip()
            or request.headers.get("x-affiliate-session", "").strip()
        )
        session_code = _verify_affiliate_session(session_token)
        if not session_code:
            return JSONResponse({"error": "authentication required"}, status_code=401)

    referral_code = request.query_params.get("referral_code", "").strip()
    if not referral_code:
        return JSONResponse({"error": "missing referral_code"}, status_code=400)

    # HIGH-005: IDOR fix — affiliate sessions can only view their own payout history
    if not portal_authed and session_code and session_code.upper() != referral_code.upper():
        return JSONResponse({"error": "access denied"}, status_code=403)

    all_requests = await _read_payout_requests_db()
    user_requests = [r for r in all_requests if r.get("referral_code") == referral_code]
    user_requests.sort(key=lambda r: r.get("created_at_ts", 0), reverse=True)

    cooldown_secs = PAYOUT_COOLDOWN_DAYS * 86400
    now_ts = time.time()
    has_pending = False
    days_until_eligible = 0
    for req in user_requests:
        if req.get("status") in ("pending", "processing"):
            created_at = req.get("created_at_ts", 0)
            elapsed = now_ts - created_at
            if elapsed < cooldown_secs:
                has_pending = True
                days_until_eligible = int((cooldown_secs - elapsed) / 86400) + 1
                break

    return JSONResponse({
        "requests": user_requests,
        "has_pending": has_pending,
        "days_until_eligible": days_until_eligible,
    })


async def api_admin_payout_mark_paid(request: Request) -> JSONResponse:
    """POST /api/admin/payout/mark-paid — admin marks a payout as paid."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    request_id = str(body.get("request_id", "")).strip()
    notes = str(body.get("notes", "")).strip()

    if not request_id:
        return JSONResponse({"error": "missing request_id"}, status_code=400)

    # Read the target request from DB first to get details for notification
    all_requests = await _read_payout_requests_db()
    paid_entry = None
    for req in all_requests:
        if req.get("request_id") == request_id:
            paid_entry = req
            break

    if not paid_entry:
        return JSONResponse({"error": "request_id not found"}, status_code=404)

    ok = await _update_payout_status_db(request_id, "paid", notes=notes)
    if not ok:
        return JSONResponse({"error": "failed to update payout status"}, status_code=500)

    paid_at = datetime.now(timezone.utc).isoformat()

    tg_msg = (
        f"PAYOUT MARKED PAID\n"
        f"Request: {request_id}\n"
        f"Amount: ${paid_entry.get('amount', 0):.2f}\n"
        f"PayPal: {paid_entry.get('paypal_email', '')}"
    )
    _send_telegram_notification(tg_msg)

    return JSONResponse({
        "ok": True,
        "request_id": request_id,
        "status": "paid",
        "paid_at": paid_at,
    })



async def _is_valid_admin_token(token: str) -> bool:
    """Check if token is a valid admin_tokens entry in the DB."""
    if not token:
        return False
    async with _referral_db() as db:
        cur = await db.execute(
            "SELECT id FROM admin_tokens WHERE token = ?", (token,)
        )
        row = await cur.fetchone()
    return row is not None


async def _is_admin_token_readonly(token: str) -> bool:
    """Returns True if the token exists and is a viewer (read-only) role."""
    if not token:
        return True
    async with _referral_db() as db:
        cur = await db.execute(
            "SELECT role FROM admin_tokens WHERE token = ?", (token,)
        )
        row = await cur.fetchone()
    if row is None:
        return True  # unknown token = treat as read-only
    return row[0] != "admin"


async def api_admin_invite(request: Request) -> JSONResponse:
    """POST /api/admin/invite — generate a read-only admin viewer token (main bearer only)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    email = str(body.get("email", "")).strip().lower()
    name  = str(body.get("name", "")).strip()
    if not email or "@" not in email:
        return JSONResponse({"error": "invalid email"}, status_code=400)

    token = secrets.token_urlsafe(32)
    now   = datetime.now(timezone.utc).isoformat()

    async with _referral_db() as db:
        await db.execute(
            "INSERT INTO admin_tokens (token, email, name, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (token, email, name, "viewer", now)
        )
        await db.commit()

    return JSONResponse({
        "ok": True,
        "token": token,
        "email": email,
        "name": name,
        "role": "viewer",
        "dashboard_url": f"https://portal.purebrain.ai/admin/clients?admin_token={token}",
    })


async def api_admin_invites_list(request: Request) -> JSONResponse:
    """GET /api/admin/invites — list all active admin viewer tokens. Main bearer only."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    async with _referral_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, token, email, name, role, created_at FROM admin_tokens ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
        invitees = [dict(r) for r in rows]

    return JSONResponse({"ok": True, "invitees": invitees})


async def api_admin_invite_revoke(request: Request) -> JSONResponse:
    """POST /api/admin/invite/revoke — delete an admin viewer token by id. Main bearer only."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    token_id = body.get("id")
    if not token_id:
        return JSONResponse({"error": "id required"}, status_code=400)

    async with _referral_db() as db:
        cur = await db.execute("SELECT id FROM admin_tokens WHERE id = ?", (token_id,))
        row = await cur.fetchone()
        if not row:
            return JSONResponse({"error": "token not found"}, status_code=404)
        await db.execute("DELETE FROM admin_tokens WHERE id = ?", (token_id,))
        await db.commit()

    return JSONResponse({"ok": True, "id": token_id})


async def api_referral_payout_approve(request: Request) -> JSONResponse:
    """POST /api/referral/payout-approve — approve a pending payout and execute PayPal transfer.

    Portal bearer token required (admin only).
    Body: { request_id, dry_run? }
    On success: marks payout as "completed", fires PayPal payout, notifies via Telegram.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    request_id = str(body.get("request_id", "")).strip()
    dry_run    = bool(body.get("dry_run", False))

    if not request_id:
        return JSONResponse({"error": "missing request_id"}, status_code=400)

    # Read target request from SQLite
    all_requests = await _read_payout_requests_db()
    target = None
    for req in all_requests:
        if req.get("request_id") == request_id:
            target = req
            break

    if not target:
        return JSONResponse({"error": "request_id not found"}, status_code=404)

    if target.get("status") in ("completed", "paid"):
        return JSONResponse({"error": f"payout already {target['status']}", "request_id": request_id}, status_code=409)

    paypal_email = target.get("paypal_email", "")
    amount       = float(target.get("amount", 0))

    if not paypal_email or "@" not in paypal_email:
        return JSONResponse({"error": "no valid PayPal email on this payout request"}, status_code=400)
    if amount <= 0:
        return JSONResponse({"error": "invalid amount on payout request"}, status_code=400)

    if dry_run:
        return JSONResponse({
            "ok":          True,
            "dry_run":     True,
            "request_id":  request_id,
            "paypal_email": paypal_email,
            "amount":      amount,
            "message":     "Dry run — no payment sent.",
        })

    # Execute PayPal payout
    payout_result = await _execute_paypal_payout(
        paypal_email=paypal_email,
        amount=amount,
        request_id=request_id,
        note=f"PureBrain affiliate commission — request {request_id}",
    )

    if payout_result["ok"]:
        batch_id = payout_result.get("batch_id", "")
        notes_text = f"Auto-paid via PayPal Payouts API. Batch: {batch_id}"
        await _update_payout_status_db(request_id, "completed", batch_id=batch_id, notes=notes_text)
    else:
        error_notes = f"PayPal error: {payout_result.get('error', 'unknown')}"
        await _update_payout_status_db(request_id, "failed", notes=error_notes)

    # Audit log: payout approval attempt
    await _log_financial_event(
        event_type="payout_approved" if payout_result["ok"] else "payout_approve_failed",
        referral_code=target.get("referral_code", ""),
        amount=amount,
        actor="admin:bearer",
        details=f"request_id={request_id}, batch_id={payout_result.get('batch_id', 'n/a')}, paypal={paypal_email}",
        ip_address=request.client.host if request.client else "unknown",
    )

    if payout_result["ok"]:
        tg_msg = (
            f"PAYOUT SENT via PayPal\n"
            f"Request: {request_id}\n"
            f"Amount: ${amount:.2f}\n"
            f"PayPal: {paypal_email}\n"
            f"Batch ID: {payout_result.get('batch_id', 'n/a')}"
        )
        _send_telegram_notification(tg_msg)
        return JSONResponse({
            "ok":          True,
            "request_id":  request_id,
            "batch_id":    payout_result.get("batch_id"),
            "amount":      amount,
            "paypal_email": paypal_email,
            "status":      "completed",
            "message":     f"Payout of ${amount:.2f} sent to {paypal_email}.",
        })
    else:
        tg_msg = (
            f"PAYOUT FAILED\n"
            f"Request: {request_id}\n"
            f"Amount: ${amount:.2f}\n"
            f"PayPal: {paypal_email}\n"
            f"Error: {payout_result.get('error', 'unknown')}"
        )
        _send_telegram_notification(tg_msg)
        return JSONResponse({
            "ok":         False,
            "request_id": request_id,
            "error":      payout_result.get("error"),
            "status":     "failed",
        }, status_code=502)


async def api_admin_affiliates(request: Request) -> JSONResponse:
    """GET /api/admin/affiliates — all referrers with full stats (admin or viewer token)."""
    admin_token = (
        request.query_params.get("admin_token", "").strip()
        or request.headers.get("x-admin-token", "").strip()
    )
    is_main_admin = check_auth(request)
    if not is_main_admin:
        if not await _is_valid_admin_token(admin_token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    async with _referral_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM referrers ORDER BY created_at DESC")
        referrers = await cur.fetchall()

        affiliates = []
        for r in referrers:
            rid  = r["id"]
            code = r["referral_code"]

            cur2 = await db.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (rid,)
            )
            total = (await cur2.fetchone())[0]

            cur2 = await db.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND status = \'completed\'", (rid,)
            )
            completed = (await cur2.fetchone())[0]

            cur2 = await db.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND status = \'pending\'", (rid,)
            )
            pending = (await cur2.fetchone())[0]

            cur2 = await db.execute(
                "SELECT COALESCE(SUM(reward_value), 0) FROM rewards WHERE referrer_id = ?", (rid,)
            )
            earnings = float((await cur2.fetchone())[0])

            cur2 = await db.execute(
                "SELECT COUNT(*) FROM referral_clicks WHERE referral_code = ? COLLATE NOCASE", (code,)
            )
            clicks = (await cur2.fetchone())[0]

            cur2 = await db.execute(
                """SELECT ref.id, ref.referred_name, ref.referred_email, ref.status, ref.created_at,
                          COALESCE(SUM(cp.commission_value), 0) AS earnings,
                          COUNT(cp.id) AS payment_count
                   FROM referrals ref
                   LEFT JOIN commission_payments cp ON cp.referral_id = ref.id
                   WHERE ref.referrer_id = ?
                   GROUP BY ref.id
                   ORDER BY ref.created_at DESC""",
                (rid,)
            )
            history = [dict(row) async for row in cur2]

            affiliates.append({
                "id":          rid,
                "name":        r["user_name"],
                "email":       r["user_email"],
                "code":        code,
                "paypal_email": r["paypal_email"],
                "clicks":      clicks,
                "total":       total,
                "completed":   completed,
                "pending":     pending,
                "earnings":    round(earnings, 2),
                "joined":      r["created_at"],
                "history":     history,
            })

    return JSONResponse({"affiliates": affiliates, "count": len(affiliates)})


async def api_admin_payouts(request: Request) -> JSONResponse:
    """GET /api/admin/payouts — all payout requests (admin or viewer token)."""
    admin_token = (
        request.query_params.get("admin_token", "").strip()
        or request.headers.get("x-admin-token", "").strip()
    )
    is_main_admin = check_auth(request)
    if not is_main_admin:
        if not await _is_valid_admin_token(admin_token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    requests_list = await _read_payout_requests_db()
    requests_list.sort(key=lambda r: r.get("created_at_ts", 0), reverse=True)
    return JSONResponse({"requests": requests_list, "count": len(requests_list)})


async def api_admin_affiliate_update(request: Request) -> JSONResponse:
    """PUT /api/admin/affiliate/update — update affiliate name, email, or PayPal email."""
    if request.method == "OPTIONS":
        return Response(status_code=204)
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    referral_code = str(body.get("referral_code", "")).strip()
    if not referral_code:
        return JSONResponse({"error": "referral_code required"}, status_code=400)

    user_name    = body.get("user_name")
    user_email   = body.get("user_email")
    paypal_email = body.get("paypal_email")

    # Validate emails if provided
    def _valid_email(e: str) -> bool:
        return "@" in e and "." in e.split("@")[-1]

    if user_email is not None:
        user_email = str(user_email).strip().lower()
        if user_email and not _valid_email(user_email):
            return JSONResponse({"error": "invalid user_email format"}, status_code=400)

    if paypal_email is not None:
        paypal_email = str(paypal_email).strip().lower()
        if paypal_email and not _valid_email(paypal_email):
            return JSONResponse({"error": "invalid paypal_email format"}, status_code=400)

    fields: list[str] = []
    params: list = []

    if user_name is not None:
        fields.append("user_name = ?")
        params.append(str(user_name).strip())
    if user_email is not None:
        fields.append("user_email = ?")
        params.append(user_email)
    if paypal_email is not None:
        fields.append("paypal_email = ?")
        params.append(paypal_email)

    if not fields:
        return JSONResponse({"error": "no fields to update"}, status_code=400)

    params.append(referral_code)

    async with _referral_db() as db:
        cur = await db.execute(
            "SELECT id FROM referrers WHERE referral_code = ? COLLATE NOCASE", (referral_code,)
        )
        row = await cur.fetchone()
        if not row:
            return JSONResponse({"error": "affiliate not found"}, status_code=404)

        await db.execute(
            f"UPDATE referrers SET {', '.join(fields)} WHERE referral_code = ? COLLATE NOCASE",
            params,
        )
        await db.commit()

    updated_fields = []
    if user_name is not None:
        updated_fields.append("user_name")
    if user_email is not None:
        updated_fields.append("user_email")
    if paypal_email is not None:
        updated_fields.append("paypal_email")

    print(f"[admin] Affiliate updated: {referral_code} — fields: {updated_fields}")
    return JSONResponse({"ok": True, "updated_fields": updated_fields})


async def api_admin_affiliate_delete(request: Request) -> JSONResponse:
    """DELETE /api/admin/affiliate/delete — delete an affiliate and optionally their referral records."""
    if request.method == "OPTIONS":
        return Response(status_code=204)
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    referral_code    = str(body.get("referral_code", "")).strip()
    delete_referrals = bool(body.get("delete_referrals", False))

    if not referral_code:
        return JSONResponse({"error": "referral_code required"}, status_code=400)

    referrals_deleted = 0

    async with _referral_db() as db:
        cur = await db.execute(
            "SELECT id FROM referrers WHERE referral_code = ? COLLATE NOCASE", (referral_code,)
        )
        row = await cur.fetchone()
        if not row:
            return JSONResponse({"error": "affiliate not found"}, status_code=404)

        referrer_id = row[0]

        if delete_referrals:
            # Count referrals first
            cur2 = await db.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (referrer_id,)
            )
            referrals_deleted = (await cur2.fetchone())[0]

            # Delete dependent records
            await db.execute(
                """DELETE FROM commission_payments
                   WHERE referral_id IN (SELECT id FROM referrals WHERE referrer_id = ?)""",
                (referrer_id,),
            )
            await db.execute(
                "DELETE FROM rewards WHERE referrer_id = ?", (referrer_id,)
            )
            await db.execute(
                "DELETE FROM referral_clicks WHERE referral_code = ? COLLATE NOCASE", (referral_code,)
            )
            await db.execute(
                "DELETE FROM referrals WHERE referrer_id = ?", (referrer_id,)
            )

        await db.execute(
            "DELETE FROM referrers WHERE id = ?", (referrer_id,)
        )
        await db.commit()

    print(f"[admin] Affiliate deleted: {referral_code} (referrals_deleted={referrals_deleted})")
    return JSONResponse({
        "ok": True,
        "deleted": referral_code,
        "referrals_deleted": referrals_deleted,
    })


async def api_admin_referral_update(request: Request) -> JSONResponse:
    """PUT /api/admin/referral/update — update a specific referral record."""
    if request.method == "OPTIONS":
        return Response(status_code=204)
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    referral_id = body.get("referral_id")
    if referral_id is None:
        return JSONResponse({"error": "referral_id required"}, status_code=400)

    try:
        referral_id = int(referral_id)
    except (ValueError, TypeError):
        return JSONResponse({"error": "referral_id must be an integer"}, status_code=400)

    referred_email = body.get("referred_email")
    referred_name  = body.get("referred_name")
    status         = body.get("status")

    allowed_statuses = {"pending", "completed", "rejected"}
    if status is not None:
        status = str(status).strip().lower()
        if status not in allowed_statuses:
            return JSONResponse(
                {"error": f"invalid status — must be one of: {', '.join(sorted(allowed_statuses))}"},
                status_code=400,
            )

    fields: list[str] = []
    params: list = []

    if referred_email is not None:
        referred_email = str(referred_email).strip().lower()
        fields.append("referred_email = ?")
        params.append(referred_email)
    if referred_name is not None:
        fields.append("referred_name = ?")
        params.append(str(referred_name).strip())
    if status is not None:
        fields.append("status = ?")
        params.append(status)

    if not fields:
        return JSONResponse({"error": "no fields to update"}, status_code=400)

    params.append(referral_id)

    async with _referral_db() as db:
        cur = await db.execute("SELECT id FROM referrals WHERE id = ?", (referral_id,))
        row = await cur.fetchone()
        if not row:
            return JSONResponse({"error": "referral not found"}, status_code=404)

        await db.execute(
            f"UPDATE referrals SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        await db.commit()

    updated_fields = []
    if referred_email is not None:
        updated_fields.append("referred_email")
    if referred_name is not None:
        updated_fields.append("referred_name")
    if status is not None:
        updated_fields.append("status")

    print(f"[admin] Referral {referral_id} updated — fields: {updated_fields}")

    # Audit log: admin referral update
    await _log_financial_event(
        event_type="admin_referral_update",
        referral_code=str(referral_id),
        details=f"fields={updated_fields}, email={referred_email}, name={referred_name}, status={status}",
        actor="admin:bearer",
        ip_address=request.client.host if request.client else "unknown",
    )

    return JSONResponse({"ok": True, "updated_fields": updated_fields})



async def api_admin_referral_assign(request: Request) -> JSONResponse:
    """POST /api/admin/referral/assign — manually assign an existing client to a referrer (retroactive credit).
    Body: { referral_code: str, client_email: str, client_name?: str }
    Creates or updates a referral record with status=completed.
    """
    if request.method == "OPTIONS":
        return Response(status_code=204)
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    referral_code  = str(body.get("referral_code", "")).strip().upper()
    client_email   = str(body.get("client_email", "")).strip().lower()
    client_name    = str(body.get("client_name", "")).strip()

    if not referral_code:
        return JSONResponse({"error": "referral_code required"}, status_code=400)
    if not client_email or "@" not in client_email:
        return JSONResponse({"error": "invalid client_email"}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()

    async with _referral_db() as db:
        # Verify referrer exists
        cur = await db.execute(
            "SELECT id FROM referrers WHERE referral_code = ? COLLATE NOCASE", (referral_code,)
        )
        row = await cur.fetchone()
        if row is None:
            return JSONResponse({"error": "referral code not found"}, status_code=404)
        referrer_id = row[0]

        # Look up client name from clients db if not provided
        if not client_name:
            async with _clients_db() as cdb:
                ccur = await cdb.execute(
                    "SELECT name FROM clients WHERE email = ? COLLATE NOCASE", (client_email,)
                )
                crow = await ccur.fetchone()
                if crow:
                    client_name = crow[0]

        # Single-referrer enforcement: remove any existing completed referral for
        # this email under a DIFFERENT referrer before assigning to the new one.
        # A client must never be counted under two referrers simultaneously.
        removed_cur = await db.execute(
            """DELETE FROM referrals
               WHERE referred_email = ? COLLATE NOCASE
                 AND referrer_id != ?""",
            (client_email, referrer_id)
        )
        removed_count = removed_cur.rowcount

        # Check for existing referral record under the target referrer
        cur = await db.execute(
            """SELECT id, status FROM referrals
               WHERE referrer_id = ? AND referred_email = ? COLLATE NOCASE""",
            (referrer_id, client_email)
        )
        existing = await cur.fetchone()

        if existing:
            if existing[1] == "completed" and removed_count == 0:
                await db.commit()
                return JSONResponse({"ok": True, "message": "Referral already credited — no change needed.", "action": "noop"})
            await db.execute(
                "UPDATE referrals SET status='completed', completed_at=?, referred_name=? WHERE id=?",
                (now, client_name, existing[0])
            )
            action = "updated"
        else:
            await db.execute(
                """INSERT INTO referrals (referrer_id, referred_email, referred_name, status, created_at, completed_at)
                   VALUES (?, ?, ?, 'completed', ?, ?)""",
                (referrer_id, client_email, client_name, now, now)
            )
            action = "created"

        await db.commit()

    if removed_count > 0:
        print(f"[admin] Single-referrer enforcement: removed {removed_count} prior referral record(s) for {client_email} from other referrers")
    print(f"[admin] Referral assigned: {referral_code} → {client_email} ({action})")

    # Audit log: admin referral assign
    await _log_financial_event(
        event_type="admin_referral_assign",
        referral_code=referral_code,
        details=f"client={client_email}, action={action}, removed_prior={removed_count}",
        actor="admin:bearer",
        ip_address=request.client.host if request.client else "unknown",
    )

    return JSONResponse({"ok": True, "action": action, "removed_prior": removed_count, "message": f"Client {client_email} assigned to referrer {referral_code}."})


async def serve_admin_referrals(request: Request) -> Response:
    """GET /admin/referrals — serve admin dashboard HTML."""
    html_path = SCRIPT_DIR / "admin-referrals.html"
    if html_path.exists():
        return FileResponse(str(html_path), media_type="text/html")
    return Response("<h1>Admin dashboard not found</h1>", media_type="text/html", status_code=503)


# ---------------------------------------------------------------------------
# Client Admin System
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _clients_db():
    """Open clients DB with WAL mode enabled."""
    async with aiosqlite.connect(str(CLIENTS_DB)) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        yield db


async def _init_clients_db() -> None:
    """Create clients table on startup if it doesn't exist."""
    async with aiosqlite.connect(str(CLIENTS_DB)) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                name                  TEXT NOT NULL,
                email                 TEXT NOT NULL UNIQUE COLLATE NOCASE,
                goes_by               TEXT NOT NULL DEFAULT '',
                ai_name               TEXT NOT NULL DEFAULT '',
                company               TEXT NOT NULL DEFAULT '',
                role                  TEXT NOT NULL DEFAULT '',
                goal                  TEXT NOT NULL DEFAULT '',
                tier                  TEXT NOT NULL DEFAULT 'unknown',
                status                TEXT NOT NULL DEFAULT 'active',
                payment_status        TEXT NOT NULL DEFAULT 'none',
                paypal_subscription_id TEXT NOT NULL DEFAULT '',
                total_paid            REAL NOT NULL DEFAULT 0,
                payment_count         INTEGER NOT NULL DEFAULT 0,
                referral_code         TEXT NOT NULL DEFAULT '',
                first_seen_at         TEXT NOT NULL,
                last_active_at        TEXT NOT NULL DEFAULT '',
                onboarded_at          TEXT NOT NULL DEFAULT '',
                notes                 TEXT NOT NULL DEFAULT '',
                magic_link_token      TEXT NOT NULL DEFAULT '',
                created_at            TEXT NOT NULL DEFAULT '',
                updated_at            TEXT NOT NULL DEFAULT '',
                hidden                INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Ensure hidden column exists for older databases
        try:
            await db.execute("ALTER TABLE clients ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # column already exists
        await db.commit()

    # Add tracking columns (login_count, session_count, etc.) + webhook log table
    ensure_tracking_columns(str(CLIENTS_DB))


async def serve_admin_clients(request: Request) -> Response:
    """GET /admin/clients — serve clients admin dashboard HTML."""
    html_path = SCRIPT_DIR / "admin-clients.html"
    if html_path.exists():
        return FileResponse(str(html_path), media_type="text/html")
    return Response("<h1>Client admin dashboard not found</h1>", media_type="text/html", status_code=503)


async def api_admin_clients(request: Request) -> JSONResponse:
    """GET /api/admin/clients — list all clients with stats. Bearer auth or viewer token required."""
    admin_token_param = request.query_params.get("admin_token", "")
    is_viewer = False
    if not check_auth(request):
        # Check viewer token
        if admin_token_param and await _is_valid_admin_token(admin_token_param):
            is_viewer = True
        else:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    show_hidden = request.query_params.get("show_hidden", "0") == "1"

    async with _clients_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM clients ORDER BY first_seen_at DESC")
        rows = await cur.fetchall()
        all_clients = [dict(r) for r in rows]

        # Separate hidden vs visible
        visible_clients = [c for c in all_clients if not c.get("hidden")]
        hidden_clients  = [c for c in all_clients if c.get("hidden")]

        # Stats are always based on visible (non-hidden) clients
        clients = visible_clients
        total   = len(clients)
        active  = sum(1 for c in clients if c.get("status") == "active")
        onboard = sum(1 for c in clients if c.get("status") == "onboarding")
        churned = sum(1 for c in clients if c.get("status") == "churned")
        total_rev = sum(float(c.get("total_paid") or 0) for c in clients)

        # MRR: subscription_active clients, estimate by tier
        tier_prices = {"awakened": 149, "insiders": 74.50, "partnered": 499, "unified": 999, "brainiac": 299}
        mrr = sum(
            tier_prices.get((c.get("tier") or "").lower(), 0)
            for c in clients
            if c.get("payment_status") == "subscription_active"
        )

    stats = {
        "total":         total,
        "active":        active,
        "onboarding":    onboard,
        "churned":       churned,
        "total_revenue": round(total_rev, 2),
        "mrr":           mrr,
        "hidden_count":  len(hidden_clients),
    }

    # Return visible clients by default; if show_hidden, return all
    response_clients = all_clients if show_hidden else visible_clients
    return JSONResponse({"clients": response_clients, "stats": stats})


async def api_public_client_stats(request: Request) -> JSONResponse:
    """GET /api/public/client-stats — lightweight stats for 777 dashboard. CORS enabled for 777.purebrain.ai."""
    origin = request.headers.get("origin", "")
    cors = {
        "Access-Control-Allow-Origin": "https://777.purebrain.ai" if "777.purebrain.ai" in origin else origin,
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Vary": "Origin",
    }
    if request.method == "OPTIONS":
        return Response("", status_code=204, headers=cors)

    async with _clients_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM clients WHERE hidden = 0 OR hidden IS NULL")
        rows = await cur.fetchall()
        clients = [dict(r) for r in rows]
        total = len(clients)
        active = sum(1 for c in clients if c.get("status") == "active")
        tier_prices = {"awakened": 149, "insiders": 74.50, "partnered": 499, "unified": 999, "brainiac": 299}
        mrr = sum(tier_prices.get((c.get("tier") or "").lower(), 0) for c in clients if c.get("payment_status") == "subscription_active")
        tiers = {}
        for c in clients:
            t = (c.get("tier") or "unknown").lower()
            tiers[t] = tiers.get(t, 0) + 1

    return JSONResponse({
        "subscribers": total,
        "active": active,
        "mrr": round(mrr, 2),
        "tiers": tiers,
        "updated": __import__("datetime").datetime.utcnow().isoformat() + "Z"
    }, headers=cors)


async def api_admin_clients_update(request: Request) -> JSONResponse:
    """POST /api/admin/clients/update — update client fields. Bearer auth required."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    client_id = body.get("id")
    if not client_id:
        return JSONResponse({"error": "id required"}, status_code=400)

    # Validate status
    status = str(body.get("status", "")).strip().lower()
    allowed_statuses = {"active", "onboarding", "churned", "trial", ""}
    if status and status not in allowed_statuses:
        return JSONResponse({"error": "invalid status — must be one of: active, onboarding, trial, churned"}, status_code=400)

    # Validate tier
    tier = str(body.get("tier", "")).strip().lower()
    allowed_tiers = {"awakened", "insiders", "partnered", "unified", "brainiac", "unknown", ""}
    if tier and tier not in allowed_tiers:
        return JSONResponse({"error": "invalid tier — must be one of: awakened, insiders, partnered, unified, brainiac, unknown"}, status_code=400)

    # Email uniqueness check (if email is being changed)
    new_email = str(body.get("email", "")).strip().lower()
    if new_email:
        async with _clients_db() as db:
            cur = await db.execute(
                "SELECT id FROM clients WHERE LOWER(email) = ? AND id != ?",
                (new_email, client_id)
            )
            existing = await cur.fetchone()
        if existing:
            return JSONResponse({"error": "email already in use by another client"}, status_code=409)

    # Build dynamic update — only include fields present in body
    now = datetime.now(timezone.utc).isoformat()
    fields = ["updated_at = ?"]
    params: list = [now]

    text_fields = {
        "name":    body.get("name"),
        "goes_by": body.get("goes_by"),
        "email":   new_email if new_email else None,
        "ai_name": body.get("ai_name"),
        "company": body.get("company"),
        "role":    body.get("role"),
        "goal":    body.get("goal"),
        "notes":   body.get("notes"),
    }
    for col, val in text_fields.items():
        if val is not None:
            fields.append(f"{col} = ?")
            params.append(str(val).strip())

    if status:
        fields.append("status = ?")
        params.append(status)
    elif "status" in body:
        # Allow explicit empty to keep existing — skip
        pass

    if tier:
        fields.append("tier = ?")
        params.append(tier)
    elif "tier" in body:
        pass

    params.append(client_id)
    async with _clients_db() as db:
        await db.execute(f"UPDATE clients SET {', '.join(fields)} WHERE id = ?", params)
        await db.commit()

    return JSONResponse({"ok": True, "id": client_id})


async def api_admin_clients_import(request: Request) -> JSONResponse:
    """POST /api/admin/clients/import — scan JSONL logs and upsert client records. Bearer auth required."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    imported = 0
    updated  = 0
    errors   = 0

    # Collect candidate records keyed by email (lowercased)
    # Priority: seed (pay_test) > payments > web_conversations
    candidates: dict[str, dict] = {}

    # --- 1. Parse purebrain_pay_test.jsonl (seed/questionnaire data) ---
    if PAY_TEST_LOG.exists():
        try:
            with PAY_TEST_LOG.open("r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue

                    email = (d.get("email") or "").strip().lower()
                    if not email or "@" not in email:
                        continue

                    # Skip obvious test/sandbox entries
                    order_id = (d.get("orderId") or "").strip()
                    if any(order_id.startswith(p) for p in ("SANDBOX-", "E2E-", "test-", "TEST-")):
                        continue
                    if "sandbox" in email or "test" in email.split("@")[0]:
                        continue

                    ts = d.get("server_timestamp", "")
                    rec = candidates.setdefault(email, {
                        "email": email,
                        "name": "",
                        "goes_by": "",
                        "ai_name": "",
                        "company": "",
                        "role": "",
                        "goal": "",
                        "tier": "unknown",
                        "payment_status": "none",
                        "paypal_subscription_id": "",
                        "total_paid": 0.0,
                        "payment_count": 0,
                        "referral_code": "",
                        "first_seen_at": ts,
                        "last_active_at": ts,
                        "onboarded_at": "",
                        "_sources": set(),
                    })

                    rec["_sources"].add("pay_test")

                    # Update fields if we get richer data
                    if d.get("name"):
                        rec["name"] = d["name"].strip()
                    if d.get("aiName"):
                        rec["ai_name"] = d["aiName"].strip()
                    if d.get("goesBy"):
                        rec["goes_by"] = d["goesBy"].strip()
                    if d.get("company"):
                        rec["company"] = d["company"].strip()
                    if d.get("role"):
                        rec["role"] = d["role"].strip()
                    if d.get("primaryGoal"):
                        rec["goal"] = d["primaryGoal"].strip()
                    if d.get("tier") and d["tier"] not in ("unknown", "test", ""):
                        rec["tier"] = d["tier"].strip()
                    if d.get("paypalSubscriptionId"):
                        rec["paypal_subscription_id"] = d["paypalSubscriptionId"].strip()
                    if d.get("session_uuid") and d.get("event") == "seed:complete":
                        rec["onboarded_at"] = ts

                    # Track earliest / latest timestamps
                    if ts and (not rec["first_seen_at"] or ts < rec["first_seen_at"]):
                        rec["first_seen_at"] = ts
                    if ts and ts > rec.get("last_active_at", ""):
                        rec["last_active_at"] = ts
        except Exception:
            pass

    # --- 2. Parse purebrain_payments.jsonl ---
    if PAYMENTS_LOG.exists():
        try:
            with PAYMENTS_LOG.open("r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue

                    email = (d.get("payerEmail") or "").strip().lower()
                    if not email or "@" not in email:
                        continue

                    order_id = (d.get("orderId") or "").strip()
                    if any(order_id.startswith(p) for p in ("SANDBOX-", "E2E-", "test-", "TEST-")):
                        continue
                    if "sandbox" in email or "test" in email.split("@")[0]:
                        continue

                    ts    = d.get("server_timestamp", "")
                    tier  = (d.get("tier") or "").strip()
                    amount = float(d.get("amount") or 0)

                    rec = candidates.setdefault(email, {
                        "email": email,
                        "name": "",
                        "goes_by": "",
                        "ai_name": "",
                        "company": "",
                        "role": "",
                        "goal": "",
                        "tier": "unknown",
                        "payment_status": "none",
                        "paypal_subscription_id": "",
                        "total_paid": 0.0,
                        "payment_count": 0,
                        "referral_code": "",
                        "first_seen_at": ts,
                        "last_active_at": ts,
                        "onboarded_at": "",
                        "_sources": set(),
                    })

                    rec["_sources"].add("payments")
                    if d.get("payerName") and not rec["name"]:
                        rec["name"] = d["payerName"].strip()
                    if tier and tier not in ("unknown", ""):
                        rec["tier"] = tier
                    if amount > 0:
                        rec["total_paid"] = round(rec["total_paid"] + amount, 2)
                        rec["payment_count"] += 1
                    # Subscription IDs start with I-
                    if order_id.startswith("I-"):
                        rec["paypal_subscription_id"] = order_id
                        rec["payment_status"] = "subscription_active"
                    elif amount > 0:
                        rec["payment_status"] = "paid"

                    if ts and (not rec["first_seen_at"] or ts < rec["first_seen_at"]):
                        rec["first_seen_at"] = ts
                    if ts and ts > rec.get("last_active_at", ""):
                        rec["last_active_at"] = ts
        except Exception:
            pass

    # --- 3. Parse purebrain_web_conversations.jsonl (fill gaps only) ---
    if WEB_CONVERSATIONS_LOG.exists():
        try:
            with WEB_CONVERSATIONS_LOG.open("r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue

                    ai_name  = (d.get("aiName") or "").strip()
                    user_name = (d.get("userName") or "").strip()
                    tier     = (d.get("userTier") or "").strip()
                    ref_code = (d.get("referralCode") or "").strip()
                    ts       = d.get("server_timestamp", "")

                    # Web conversations rarely have emails — skip if no useful data
                    if not ai_name and not user_name:
                        continue
                    if user_name.lower() in ("guest user", "atlas", "guest", ""):
                        continue

                    # Try to match by ai_name to existing candidate
                    matched = None
                    if ai_name:
                        for rec in candidates.values():
                            if rec.get("ai_name", "").lower() == ai_name.lower():
                                matched = rec
                                break

                    if matched:
                        if ref_code and not matched.get("referral_code"):
                            matched["referral_code"] = ref_code
                        if tier and matched.get("tier") in ("unknown", ""):
                            matched["tier"] = tier
                        if ts and ts > matched.get("last_active_at", ""):
                            matched["last_active_at"] = ts
        except Exception:
            pass

    # --- 4. Upsert into clients DB ---
    now = datetime.now(timezone.utc).isoformat()
    async with _clients_db() as db:
        db.row_factory = aiosqlite.Row
        for email, rec in candidates.items():
            # Require at minimum a name or ai_name to insert
            name = rec.get("name") or rec.get("ai_name") or email.split("@")[0]
            if not name:
                continue

            try:
                # Check existing
                cur = await db.execute(
                    "SELECT id, total_paid, payment_count, name, ai_name FROM clients WHERE email = ? COLLATE NOCASE",
                    (email,)
                )
                existing = await cur.fetchone()

                if existing:
                    # Merge: update fields only if they improve the record
                    ex_id    = existing["id"]
                    ex_paid  = float(existing["total_paid"] or 0)
                    ex_count = int(existing["payment_count"] or 0)
                    new_paid  = max(ex_paid,  rec["total_paid"])
                    new_count = max(ex_count, rec["payment_count"])

                    await db.execute("""
                        UPDATE clients SET
                            name = CASE WHEN name = '' OR name IS NULL THEN ? ELSE name END,
                            goes_by = CASE WHEN goes_by = '' OR goes_by IS NULL THEN ? ELSE goes_by END,
                            ai_name = CASE WHEN ai_name = '' OR ai_name IS NULL THEN ? ELSE ai_name END,
                            company = CASE WHEN company = '' OR company IS NULL THEN ? ELSE company END,
                            role = CASE WHEN role = '' OR role IS NULL THEN ? ELSE role END,
                            goal = CASE WHEN goal = '' OR goal IS NULL THEN ? ELSE goal END,
                            tier = CASE WHEN tier = 'unknown' OR tier = '' OR tier IS NULL THEN ? ELSE tier END,
                            payment_status = CASE WHEN payment_status = 'none' OR payment_status IS NULL THEN ? ELSE payment_status END,
                            paypal_subscription_id = CASE WHEN paypal_subscription_id = '' OR paypal_subscription_id IS NULL THEN ? ELSE paypal_subscription_id END,
                            total_paid = ?,
                            payment_count = ?,
                            referral_code = CASE WHEN referral_code = '' OR referral_code IS NULL THEN ? ELSE referral_code END,
                            last_active_at = CASE WHEN last_active_at < ? THEN ? ELSE last_active_at END,
                            onboarded_at = CASE WHEN onboarded_at = '' OR onboarded_at IS NULL THEN ? ELSE onboarded_at END,
                            updated_at = ?
                        WHERE id = ?
                    """, (
                        name,
                        rec["goes_by"],
                        rec["ai_name"],
                        rec["company"],
                        rec["role"],
                        rec["goal"],
                        rec["tier"],
                        rec["payment_status"],
                        rec["paypal_subscription_id"],
                        new_paid,
                        new_count,
                        rec["referral_code"],
                        rec["last_active_at"],
                        rec["last_active_at"],
                        rec["onboarded_at"],
                        now,
                        ex_id,
                    ))
                    updated += 1
                else:
                    await db.execute("""
                        INSERT INTO clients
                            (name, email, goes_by, ai_name, company, role, goal, tier, status,
                             payment_status, paypal_subscription_id, total_paid, payment_count,
                             referral_code, first_seen_at, last_active_at, onboarded_at,
                             created_at, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        name,
                        email,
                        rec["goes_by"],
                        rec["ai_name"],
                        rec["company"],
                        rec["role"],
                        rec["goal"],
                        rec["tier"],
                        "active",
                        rec["payment_status"],
                        rec["paypal_subscription_id"],
                        rec["total_paid"],
                        rec["payment_count"],
                        rec["referral_code"],
                        rec["first_seen_at"] or now,
                        rec["last_active_at"] or now,
                        rec["onboarded_at"],
                        now,
                        now,
                    ))
                    imported += 1
            except Exception:
                errors += 1
                continue

        await db.commit()

    return JSONResponse({"ok": True, "imported": imported, "updated": updated, "errors": errors})


async def api_admin_clients_hide(request: Request) -> JSONResponse:
    """POST /api/admin/clients/hide — soft-delete a client (hide from default view). Bearer auth required."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    client_id = body.get("id")
    if not client_id:
        return JSONResponse({"error": "id required"}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()
    async with _clients_db() as db:
        cur = await db.execute("SELECT id, name FROM clients WHERE id = ?", (client_id,))
        row = await cur.fetchone()
        if not row:
            return JSONResponse({"error": "client not found"}, status_code=404)
        await db.execute("UPDATE clients SET hidden = 1, updated_at = ? WHERE id = ?", (now, client_id))
        await db.commit()

    print(f"[admin] Client {client_id} hidden (soft-delete)")
    return JSONResponse({"ok": True, "id": client_id})


async def api_admin_clients_restore(request: Request) -> JSONResponse:
    """POST /api/admin/clients/restore — restore a hidden client back to the default view. Bearer auth required."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    client_id = body.get("id")
    if not client_id:
        return JSONResponse({"error": "id required"}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()
    async with _clients_db() as db:
        cur = await db.execute("SELECT id FROM clients WHERE id = ?", (client_id,))
        row = await cur.fetchone()
        if not row:
            return JSONResponse({"error": "client not found"}, status_code=404)
        await db.execute("UPDATE clients SET hidden = 0, updated_at = ? WHERE id = ?", (now, client_id))
        await db.commit()

    print(f"[admin] Client {client_id} restored from hidden")
    return JSONResponse({"ok": True, "id": client_id})


async def serve_affiliate_portal(request: Request) -> Response:
    """GET /affiliate — redirect to canonical /refer/ page on purebrain.ai."""
    code = request.query_params.get("code", "").strip()
    redirect_url = "https://purebrain.ai/refer/"
    if code:
        redirect_url += f"?code={code}"
    from starlette.responses import RedirectResponse
    return RedirectResponse(url=redirect_url, status_code=301)


# ---------------------------------------------------------------------------
# Emoji Reaction Sentiment Engine
# ---------------------------------------------------------------------------

EMOJI_SENTIMENT_MAP = {
    "\U0001F44D": {"label": "positive",   "weight": 1,  "name": "thumbs-up"},
    "\U0001F44E": {"label": "negative",   "weight": -1, "name": "thumbs-down"},
    "\U0001F680": {"label": "excited",    "weight": 2,  "name": "rocket"},
    "\U0001F4B0": {"label": "high-value", "weight": 2,  "name": "money-bag"},
    "\U0001F525": {"label": "fire",       "weight": 2,  "name": "fire"},
    "\u2705":     {"label": "approved",   "weight": 1,  "name": "check-mark"},
    "\U0001F4A5": {"label": "impactful",  "weight": 2,  "name": "explosion"},
    "\U0001F92F": {"label": "mind-blown", "weight": 3,  "name": "mind-blown"},
    "\U0001F4AA": {"label": "empowering", "weight": 1,  "name": "muscle"},
    "\U0001F3AF": {"label": "on-target",  "weight": 2,  "name": "bullseye"},
    "\U0001F48E": {"label": "premium",    "weight": 2,  "name": "gem"},
    "\u2764\uFE0F": {"label": "love",     "weight": 5,  "name": "heart"},
    "\U0001F622": {"label": "disappointed", "weight": -1, "name": "sad-face"},
    "\U0001F610": {"label": "meh",          "weight": 0,  "name": "neutral-face"},
    "\U0001F60D": {"label": "heart-eyes",   "weight": 10, "name": "heart-eyes"},
}

REACTION_LOG = Path.home() / "purebrain_portal" / "reaction-sentiment.jsonl"


async def api_reaction(request: Request) -> JSONResponse:
    """Log emoji reaction as sentiment data point."""
    # MED-005: Require auth to prevent unauthenticated sentiment manipulation
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    msg_id = body.get("msg_id", "")
    emoji = body.get("emoji", "")
    action = body.get("action", "add")
    msg_preview = body.get("msg_preview", "")[:200]
    msg_role = body.get("msg_role", "unknown")

    if not msg_id or not emoji:
        return JSONResponse({"error": "msg_id and emoji required"}, status_code=400)

    sentiment = EMOJI_SENTIMENT_MAP.get(emoji, {"label": "unknown", "weight": 0, "name": emoji})

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "msg_id": msg_id,
        "emoji": emoji,
        "emoji_name": sentiment["name"],
        "sentiment": sentiment["label"],
        "weight": sentiment["weight"] if action == "add" else -sentiment["weight"],
        "action": action,
        "msg_role": msg_role,
        "msg_preview": msg_preview,
    }

    try:
        with open(REACTION_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

    return JSONResponse({"ok": True, "sentiment": sentiment["label"]})


async def api_reaction_summary(request: Request) -> JSONResponse:
    """Aggregate sentiment summary from all reactions."""
    if not REACTION_LOG.exists():
        return JSONResponse({"total_reactions": 0, "sentiment_breakdown": {}, "top_emojis": []})

    sentiment_counts: dict = {}
    emoji_counts: dict = {}
    total = 0
    net_score = 0

    try:
        with open(REACTION_LOG) as f:
            for line in f:
                try:
                    e = json.loads(line.strip())
                    if e.get("action") == "add":
                        total += 1
                        s = e.get("sentiment", "unknown")
                        sentiment_counts[s] = sentiment_counts.get(s, 0) + 1
                        en = e.get("emoji_name", "?")
                        emoji_counts[en] = emoji_counts.get(en, 0) + 1
                        net_score += e.get("weight", 0)
                    elif e.get("action") == "remove":
                        total = max(0, total - 1)
                        s = e.get("sentiment", "unknown")
                        sentiment_counts[s] = max(0, sentiment_counts.get(s, 0) - 1)
                        en = e.get("emoji_name", "?")
                        emoji_counts[en] = max(0, emoji_counts.get(en, 0) - 1)
                        net_score += e.get("weight", 0)
                except (json.JSONDecodeError, KeyError):
                    continue
    except Exception:
        pass

    if total == 0:
        loose_sentiment = "neutral"
    elif net_score >= 10:
        loose_sentiment = "very positive"
    elif net_score >= 3:
        loose_sentiment = "positive"
    elif net_score >= 0:
        loose_sentiment = "slightly positive"
    elif net_score >= -3:
        loose_sentiment = "slightly negative"
    else:
        loose_sentiment = "negative"

    sentiment_counts = {k: v for k, v in sentiment_counts.items() if v > 0}
    emoji_counts = {k: v for k, v in emoji_counts.items() if v > 0}
    top_emojis = sorted(emoji_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    return JSONResponse({
        "total_reactions": total,
        "net_score": net_score,
        "loose_sentiment": loose_sentiment,
        "sentiment_breakdown": sentiment_counts,
        "top_emojis": [{"emoji": e, "count": c} for e, c in top_emojis],
    })


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# User settings (synced across devices via server)
# ---------------------------------------------------------------------------
SETTINGS_FILE = SCRIPT_DIR / "user-settings.json"

def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
    except Exception:
        return {}

def _save_settings(data: dict):
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))

async def api_user_settings(request: Request) -> JSONResponse:
    """GET returns saved settings, POST/PUT merges new settings."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.method == "GET":
        return JSONResponse(_load_settings())
    # POST/PUT — merge incoming keys
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    settings = _load_settings()
    settings.update(body)
    _save_settings(settings)
    return JSONResponse({"ok": True, "settings": settings})

# ---------------------------------------------------------------------------
# Bookmarks API (server-side persistence, syncs across devices)
# ---------------------------------------------------------------------------
BOOKMARKS_FILE = SCRIPT_DIR / "bookmarks.json"

def _load_bookmarks() -> list:
    try:
        return json.loads(BOOKMARKS_FILE.read_text()) if BOOKMARKS_FILE.exists() else []
    except Exception:
        return []

def _save_bookmarks(data: list):
    BOOKMARKS_FILE.write_text(json.dumps(data, indent=2))

async def api_bookmarks(request: Request) -> JSONResponse:
    """GET returns saved bookmarks array, POST saves bookmarks array (server wins)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.method == "GET":
        return JSONResponse(_load_bookmarks())
    # POST — replace bookmarks with the full array sent by client
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(body, list):
        return JSONResponse({"error": "expected array"}, status_code=400)
    _save_bookmarks(body)
    return JSONResponse({"ok": True, "count": len(body)})

# ---------------------------------------------------------------------------
# Agents, Commands & Shortcuts API
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager as _asynccontextmanager_agents

@_asynccontextmanager_agents
async def _agents_db():
    """Open agents DB with WAL mode."""
    async with aiosqlite.connect(str(AGENTS_DB)) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        yield db

async def _init_agents_db() -> None:
    """Create agents table and seed with Aether's roster on first run."""
    async with aiosqlite.connect(str(AGENTS_DB)) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                id            TEXT PRIMARY KEY,
                user_id       TEXT NOT NULL DEFAULT 'default',
                name          TEXT NOT NULL,
                description   TEXT NOT NULL DEFAULT '',
                type          TEXT NOT NULL DEFAULT 'specialist',
                status        TEXT NOT NULL DEFAULT 'idle',
                capabilities  TEXT NOT NULL DEFAULT '[]',
                department    TEXT NOT NULL DEFAULT 'Other',
                is_lead       INTEGER NOT NULL DEFAULT 0,
                last_active   TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL DEFAULT ''
            )
        """)
        # Migrate: add current_task and last_completed columns if they don't exist yet
        for _col, _coldef in [("current_task", "TEXT NOT NULL DEFAULT ''"),
                               ("last_completed", "TEXT NOT NULL DEFAULT ''")]:
            try:
                await db.execute(f"ALTER TABLE agents ADD COLUMN {_col} {_coldef}")
            except Exception:
                pass  # column already exists
        await db.commit()

        # Seed Aether's roster if empty
        cur = await db.execute("SELECT COUNT(*) FROM agents")
        row = await cur.fetchone()
        if row and row[0] == 0:
            await _seed_aether_agents(db)
            await db.commit()
    print(f"[agents] SQLite DB ready: {AGENTS_DB}")


async def _seed_aether_agents(db) -> None:
    """Seed the agents table with Aether's full roster from .claude/agents/ manifests."""
    import yaml as _yaml_mod
    import json as _j
    now = datetime.utcnow().isoformat()

    dept_map = {
        "cto": ("AI & Strategy", True),
        "the-conductor": ("Meta & Governance", True),
        "full-stack-developer": ("Development", False),
        "devops-engineer": ("Development", False),
        "security-engineer-tech": ("Development", False),
        "security-auditor": ("Development", False),
        "qa-engineer": ("Development", False),
        "refactoring-specialist": ("Development", False),
        "performance-optimizer": ("Development", False),
        "test-architect": ("Development", False),
        "api-architect": ("Development", False),
        "ai-ml-engineer": ("Development", False),
        "data-engineer": ("Development", False),
        "data-scientist": ("Development", False),
        "3d-design-specialist": ("Design & UX", False),
        "ui-ux-designer": ("Design & UX", False),
        "feature-designer": ("Design & UX", False),
        "blogger": ("Communications", False),
        "content-specialist": ("Communications", False),
        "bsky-manager": ("Communications", False),
        "linkedin-researcher": ("Communications", False),
        "linkedin-writer": ("Communications", False),
        "linkedin-specialist": ("Communications", False),
        "social-media-specialist": ("Communications", False),
        "marketing-strategist": ("Marketing", False),
        "marketing-automation-specialist": ("Marketing", True),
        "marketing-team": ("Marketing", False),
        "client-marketing": ("Marketing", False),
        "sales-specialist": ("Sales", True),
        "strategy-specialist": ("AI & Strategy", False),
        "pattern-detector": ("Meta & Governance", False),
        "agent-architect": ("Meta & Governance", False),
        "task-decomposer": ("Meta & Governance", False),
        "result-synthesizer": ("Meta & Governance", False),
        "conflict-resolver": ("Meta & Governance", False),
        "health-auditor": ("Meta & Governance", False),
        "integration-auditor": ("Meta & Governance", False),
        "capability-curator": ("Meta & Governance", False),
        "genealogist": ("Meta & Governance", False),
        "ai-psychologist": ("Meta & Governance", False),
        "human-liaison": ("Communications", True),
        "collective-liaison": ("Communications", False),
        "cross-civ-integrator": ("Communications", False),
        "tg-bridge": ("Infrastructure", False),
        "web-researcher": ("Research", False),
        "code-archaeologist": ("Research", False),
        "doc-synthesizer": ("Research", False),
        "claim-verifier": ("Research", False),
        "claude-code-expert": ("Infrastructure", False),
        "naming-consultant": ("AI & Strategy", False),
        "trading-strategist": ("AI & Strategy", False),
        "dept-pure-technology": ("Operations", True),
        "dept-systems-technology": ("Development", False),
        "dept-marketing-advertising": ("Marketing", False),
        "dept-pure-marketing-group": ("Marketing", False),
        "dept-sales-distribution": ("Sales", False),
        "dept-product-development": ("Operations", False),
        "dept-operations-planning": ("Operations", False),
        "dept-pure-research": ("Research", False),
        "dept-accounting-finance": ("Operations", False),
        "dept-human-resources": ("Operations", False),
        "dept-legal-compliance": ("Legal", False),
        "dept-board-advisors": ("Operations", False),
        "dept-commercial-business": ("Operations", False),
        "dept-corporate-org": ("Operations", False),
        "dept-external-share": ("Communications", False),
        "dept-internal-share": ("Communications", False),
        "dept-investor-relations": ("Operations", False),
        "dept-it-support": ("Infrastructure", False),
        "dept-karma": ("Operations", False),
        "dept-pure-capital": ("Operations", False),
        "dept-pure-digital-assets": ("Operations", False),
        "dept-pure-infrastructure": ("Infrastructure", False),
        "dept-pure-love": ("Operations", False),
        "law-generalist": ("Legal", False),
        "florida-bar-specialist": ("Legal", False),
        "browser-vision-tester": ("Development", False),
    }

    type_map = {
        "Development": "specialist",
        "AI & Strategy": "orchestration",
        "Meta & Governance": "governance",
        "Operations": "pipeline",
        "Communications": "specialist",
        "Marketing": "specialist",
        "Sales": "specialist",
        "Research": "specialist",
        "Infrastructure": "core",
        "Legal": "specialist",
        "Design & UX": "specialist",
        "Other": "specialist",
    }

    agents_dir = Path.home() / "projects" / "AI-CIV" / "aether" / ".claude" / "agents"
    if not agents_dir.exists():
        print("[agents] agents dir not found, skipping seed")
        return

    for md_file in sorted(agents_dir.glob("*.md")):
        agent_id = md_file.stem
        try:
            raw = md_file.read_text(encoding="utf-8", errors="replace")
            description = ""
            if raw.startswith("---"):
                end = raw.find("---", 3)
                if end > 0:
                    fm_text = raw[3:end].strip()
                    try:
                        fm = _yaml_mod.safe_load(fm_text)
                        if isinstance(fm, dict):
                            desc_val = fm.get("description", "")
                            if isinstance(desc_val, str):
                                description = desc_val.strip("|").strip()
                    except Exception:
                        pass
        except Exception:
            description = ""

        dept_info = dept_map.get(agent_id, ("Other", False))
        dept = dept_info[0]
        is_lead = 1 if dept_info[1] else 0
        agent_type = type_map.get(dept, "specialist")

        name = agent_id.replace("-", " ").replace("_", " ").title()
        name = name.replace("Dept ", "Dept: ").replace("Ai ", "AI ")

        caps = []
        desc_lower = description.lower()
        if any(k in desc_lower for k in ["python", "backend", "api", "server"]):
            caps.append("Backend")
        if any(k in desc_lower for k in ["frontend", "ui", "css", "html", "react"]):
            caps.append("Frontend")
        if any(k in desc_lower for k in ["security", "auth", "threat", "vulnerability"]):
            caps.append("Security")
        if any(k in desc_lower for k in ["test", "qa", "quality"]):
            caps.append("QA")
        if any(k in desc_lower for k in ["content", "blog", "linkedin", "social", "writing"]):
            caps.append("Content")
        if any(k in desc_lower for k in ["research", "web", "analysis", "synthesis"]):
            caps.append("Research")
        if any(k in desc_lower for k in ["architect", "design", "pattern", "strategy"]):
            caps.append("Strategy")
        if any(k in desc_lower for k in ["data", "analytics", "ml", "ai"]):
            caps.append("Data/ML")
        if any(k in desc_lower for k in ["devops", "infra", "deploy", "docker"]):
            caps.append("DevOps")
        if any(k in desc_lower for k in ["legal", "compliance", "contract"]):
            caps.append("Legal")
        if not caps:
            caps.append("General")

        await db.execute(
            """INSERT OR IGNORE INTO agents
               (id, user_id, name, description, type, status, capabilities, department, is_lead, last_active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent_id,
                CIV_NAME,
                name,
                description[:500] if description else "",
                agent_type,
                "idle",
                _j.dumps(caps),
                dept,
                is_lead,
                now,
                now,
            )
        )

    print(f"[agents] Seeded Aether agent roster from {agents_dir}")


async def api_agents_get_one(request: Request) -> JSONResponse:
    """GET /api/agents/{id} — return full details for a single agent."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    agent_id = request.path_params.get("id", "").strip()
    if not agent_id:
        return JSONResponse({"error": "agent id required"}, status_code=400)

    import json as _j
    async with _agents_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
        row = await cur.fetchone()

    if row is None:
        return JSONResponse({"error": "agent not found"}, status_code=404)

    agent = dict(row)
    try:
        agent["capabilities"] = _j.loads(agent.get("capabilities", "[]"))
    except Exception:
        agent["capabilities"] = []

    # Normalise / rename fields for consistent REST shape
    return JSONResponse({
        "id":          agent.get("id"),
        "name":        agent.get("name"),
        "department":  agent.get("department"),
        "role":        agent.get("type"),          # 'type' maps to 'role' in REST shape
        "description": agent.get("description"),
        "skills":      agent.get("capabilities"),  # 'capabilities' maps to 'skills'
        "status":      agent.get("status"),
        "is_lead":     bool(agent.get("is_lead")),
        "last_active": agent.get("last_active"),
        "created_at":  agent.get("created_at"),
    })


async def api_agents_update_status(request: Request) -> JSONResponse:
    """POST /api/agents/status — update a single agent's live status.

    Body (JSON):
        { "agent": "<agent-id>", "status": "active|idle|working|offline",
          "task": "<description>"  [optional, cleared when idle]  }
    Accepts bearer token OR localhost-only requests (hook scripts).
    """
    import json as _j
    # HIGH-006: Restrict to authenticated users or localhost callers
    if not check_auth(request):
        client_ip = request.client.host if request.client else ""
        if client_ip not in ("127.0.0.1", "::1", "localhost"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    agent_id = (body.get("agent") or body.get("id") or "").strip()
    status   = (body.get("status") or "idle").strip().lower()
    task     = (body.get("task") or "").strip()

    if not agent_id:
        return JSONResponse({"error": "agent field required"}, status_code=400)
    if status not in ("active", "idle", "working", "offline"):
        return JSONResponse({"error": "status must be active|idle|working|offline"}, status_code=400)

    now = datetime.utcnow().isoformat()

    async with _agents_db() as db:
        # Ensure columns exist (graceful on older DBs)
        for _col, _cdef in [("current_task", "TEXT NOT NULL DEFAULT ''"),
                             ("last_completed", "TEXT NOT NULL DEFAULT ''")]:
            try:
                await db.execute(f"ALTER TABLE agents ADD COLUMN {_col} {_cdef}")
            except Exception:
                pass

        # Check agent exists (insert placeholder if unknown so hooks always succeed)
        cur = await db.execute("SELECT id FROM agents WHERE id = ?", (agent_id,))
        row = await cur.fetchone()
        if row is None:
            name = agent_id.replace("-", " ").replace("_", " ").title()
            await db.execute(
                """INSERT OR IGNORE INTO agents
                   (id, user_id, name, status, current_task, last_completed, created_at, last_active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (agent_id, CIV_NAME, name, status, task, "", now, now),
            )
        else:
            if status == "idle":
                # When going idle, clear task and record last_completed timestamp
                await db.execute(
                    """UPDATE agents SET status=?, current_task='', last_completed=?, last_active=? WHERE id=?""",
                    (status, now, now, agent_id),
                )
            else:
                await db.execute(
                    """UPDATE agents SET status=?, current_task=?, last_active=? WHERE id=?""",
                    (status, task, now, agent_id),
                )
        await db.commit()

    return JSONResponse({"ok": True, "agent": agent_id, "status": status, "updated": now})


async def api_agents_list(request: Request) -> JSONResponse:
    """GET /api/agents — list agents (supports search/filter params)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    type_filter   = request.query_params.get("type", "").strip().lower()
    status_filter = request.query_params.get("status", "").strip().lower()
    search_term   = request.query_params.get("search", "").strip().lower()

    import json as _j
    async with _agents_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM agents ORDER BY department, is_lead DESC, name")
        rows = await cur.fetchall()

    agents = []
    for r in rows:
        d = dict(r)
        try:
            d["capabilities"] = _j.loads(d.get("capabilities", "[]"))
        except Exception:
            d["capabilities"] = []

        if type_filter and d.get("type", "") != type_filter:
            continue
        if status_filter and d.get("status", "") != status_filter:
            continue
        if search_term:
            haystack = (d.get("name","") + " " + d.get("description","") + " " + d.get("department","")).lower()
            if search_term not in haystack:
                continue
        agents.append(d)

    return JSONResponse({"agents": agents, "total": len(agents)})


async def api_agents_stats(request: Request) -> JSONResponse:
    """GET /api/agents/stats — agent count statistics."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    async with _agents_db() as db:
        cur = await db.execute("SELECT COUNT(*) FROM agents")
        total = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM agents WHERE status = 'active'")
        active = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM agents WHERE status = 'working'")
        working = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM agents WHERE status = 'idle'")
        idle = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM agents WHERE status = 'offline'")
        offline = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(DISTINCT department) FROM agents")
        depts = (await cur.fetchone())[0]

    return JSONResponse({
        "total": total, "active": active, "working": working,
        "idle": idle, "offline": offline, "departments": depts,
    })


async def api_agents_orgchart(request: Request) -> JSONResponse:
    """GET /api/agents/orgchart — department-grouped org chart."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    import json as _j

    async with _agents_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM agents ORDER BY department, is_lead DESC, name")
        rows = await cur.fetchall()

    agents_data = []
    for r in rows:
        d = dict(r)
        try:
            d["capabilities"] = _j.loads(d.get("capabilities", "[]"))
        except Exception:
            d["capabilities"] = []
        agents_data.append(d)

    dept_order = [
        # Core leadership
        "Pure Technology",
        # Technology & Product
        "Systems & Technology",
        "Product Development",
        # Revenue & Growth
        "Sales & Distribution",
        "Marketing & Advertising",
        "Pure Marketing Group",
        "Commercial & Business Development",
        # Operations & Corporate
        "Operations & Planning",
        "Corporate & Organizational",
        "Human Resources",
        # Finance & Capital
        "Accounting & Finance",
        "Pure Capital",
        "Investor Relations",
        # Research & Knowledge
        "Pure Research",
        "PT Internal Share",
        "PT External Share",
        # Legal & Compliance
        "Legal & Compliance",
        # Infrastructure & IT
        "IT Support",
        "Pure Infrastructure",
        # Specialty units
        "Pure Digital Assets",
        "Pure Love",
        "Board of Advisors",
        "Karma",
        # Catch-all
        "Other",
    ]
    dept_groups: dict = {}
    for a in agents_data:
        dept = a.get("department", "Other")
        if dept not in dept_groups:
            dept_groups[dept] = {"lead": None, "members": []}
        if a.get("is_lead"):
            dept_groups[dept]["lead"] = a
        else:
            dept_groups[dept]["members"].append(a)

    departments = []
    seen: set = set()
    for dept_name in dept_order:
        if dept_name in dept_groups:
            g = dept_groups[dept_name]
            total_in_dept = (1 if g["lead"] else 0) + len(g["members"])
            departments.append({"name": dept_name, "count": total_in_dept, "lead": g["lead"], "members": g["members"]})
            seen.add(dept_name)
    for dept_name, g in dept_groups.items():
        if dept_name not in seen:
            total_in_dept = (1 if g["lead"] else 0) + len(g["members"])
            departments.append({"name": dept_name, "count": total_in_dept, "lead": g["lead"], "members": g["members"]})

    return JSONResponse({"departments": departments, "total": len(agents_data)})


async def api_commands(request: Request) -> JSONResponse:
    """GET /api/commands — server-specific command reference for current deployment."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    import socket as _socket
    try:
        hostname = _socket.gethostname()
    except Exception:
        hostname = "unknown"

    home = str(Path.home())
    civ_root = str(Path.home() / "projects" / "AI-CIV" / "aether")
    portal_dir = str(SCRIPT_DIR)
    tools_dir = str(Path.home() / "projects" / "AI-CIV" / "aether" / "tools")
    logs_dir = str(Path.home() / "projects" / "AI-CIV" / "aether" / "logs")

    try:
        tmux_session = get_tmux_session()
    except Exception:
        tmux_session = f"{CIV_NAME}-primary"

    owner_file = SCRIPT_DIR / "portal_owner.json"
    try:
        owner = json.loads(owner_file.read_text())
    except Exception:
        owner = {"name": "User", "email": ""}

    server_ip = "your-server"
    try:
        identity_file = Path.home() / ".aiciv-identity.json"
        if identity_file.exists():
            identity = json.loads(identity_file.read_text())
            server_ip = identity.get("server_ip", server_ip)
    except Exception:
        pass
    # Fallback: detect actual public IP if still placeholder
    if server_ip == "your-server":
        try:
            import socket
            server_ip = socket.gethostbyname(socket.gethostname())
            if server_ip.startswith("127."):
                # Try getting external-facing IP
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                server_ip = s.getsockname()[0]
                s.close()
        except Exception:
            pass

    ssh_port = "22"
    try:
        import subprocess as _sp
        r = _sp.check_output(
            ["bash", "-c", "ss -tlnp 2>/dev/null | grep sshd | awk '{print $4}' | head -1 | awk -F: '{print $NF}'"],
            text=True, timeout=3
        ).strip()
        if r.isdigit():
            ssh_port = r
    except Exception:
        pass

    portal_url = "https://app.purebrain.ai"
    try:
        cname_file = Path.home() / ".portal-cname"
        if cname_file.exists():
            portal_url = "https://" + cname_file.read_text().strip()
    except Exception:
        pass

    ssh_user = Path.home().name

    return JSONResponse({
        "server": {
            "hostname": hostname,
            "server_ip": server_ip,
            "ssh_port": ssh_port,
            "ssh_user": ssh_user,
            "portal_url": portal_url,
        },
        "paths": {
            "home": home,
            "civ_root": civ_root,
            "portal_dir": portal_dir,
            "tools_dir": tools_dir,
            "logs_dir": logs_dir,
        },
        "tmux": {
            "primary_session": tmux_session,
        },
        "civ": {
            "name": CIV_NAME,
            "human_name": HUMAN_NAME,
        },
        "owner": owner,
    })


async def api_shortcuts(request: Request) -> JSONResponse:
    """GET /api/shortcuts — portal shortcuts reference (universal + customizable)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    shortcuts = {
        "slash_commands": [
            {"cmd": "/compact", "desc": "Compress context window to free up space", "type": "built-in"},
            {"cmd": "/clear",   "desc": "Clear context and start fresh conversation", "type": "built-in"},
            {"cmd": "/cost",    "desc": "Show token usage and cost for this session", "type": "built-in"},
            {"cmd": "/help",    "desc": "Show Claude Code help and available commands", "type": "built-in"},
            {"cmd": "/status",  "desc": "Show current task status and pending work", "type": "custom"},
            {"cmd": "/recap",   "desc": "Get a recap of what was done this session", "type": "custom"},
            {"cmd": "/memory",  "desc": "Show recent memory entries", "type": "custom"},
            {"cmd": "/boop",    "desc": "Trigger a scheduled BOOP task manually", "type": "custom"},
            {"cmd": "/delegate","desc": "Delegate a task to a specialist agent", "type": "custom"},
            {"cmd": "/morning", "desc": "Run morning briefing — email, context, priorities", "type": "custom"},
        ],
        "keyboard_shortcuts": [
            {"keys": ["Enter"],              "desc": "Send message",               "context": "Chat"},
            {"keys": ["Shift", "Enter"],     "desc": "New line in message",         "context": "Chat"},
            {"keys": ["Ctrl", "K"],          "desc": "Clear / focus terminal input","context": "Terminal"},
            {"keys": ["Ctrl", "B", "D"],     "desc": "Detach tmux session",         "context": "SSH"},
            {"keys": ["Ctrl", "B", "["],     "desc": "Enter tmux scroll mode",      "context": "SSH"},
            {"keys": ["q"],                  "desc": "Exit tmux scroll mode",       "context": "SSH"},
            {"keys": ["Ctrl", "B", "c"],     "desc": "New tmux window",             "context": "SSH"},
            {"keys": ["Ctrl", "B", "n"],     "desc": "Next tmux window",            "context": "SSH"},
        ],
        "chat_features": [
            {"feature": "File upload",    "desc": "Click paperclip or drag & drop a file into chat"},
            {"feature": "Voice input",    "desc": "Click the microphone to speak your message"},
            {"feature": "Bookmark",       "desc": "Hover any message and click bookmark to save it"},
            {"feature": "React",          "desc": "Hover an AI message to react with emoji feedback"},
            {"feature": "Schedule",       "desc": "Click the clock to schedule a message for later"},
            {"feature": "Link detection", "desc": "URLs in AI messages are auto-clickable"},
        ],
        "boop_automation": [
            {"name": "Morning Briefing",  "trigger": "Daily 6am",    "desc": "Email check, memory activation, priorities"},
            {"name": "Context Check",     "trigger": "Every 4h",     "desc": "Monitor context — auto-compact above 80%"},
            {"name": "Memory Write",      "trigger": "Nightly 11pm", "desc": "Consolidate session learnings"},
            {"name": "SEO Improvement",   "trigger": "Nightly 2am",  "desc": "Autonomous site improvements"},
        ],
        "sidebar_tabs": [
            {"icon": "◈",  "name": "Chat",          "desc": "Main conversation — the heart of everything"},
            {"icon": "⌨",  "name": "Terminal",       "desc": "Direct terminal access on your AI's server"},
            {"icon": "⬗",  "name": "Teams",          "desc": "Specialist agent team — inject messages"},
            {"icon": "⊞",  "name": "Fleet",          "desc": "Fleet overview — all AI instances live status"},
            {"icon": "◎",  "name": "Status",         "desc": "Health dashboard — uptime, memory, diagnostics"},
            {"icon": "⬇",  "name": "Files",          "desc": "Upload, download, manage shared files"},
            {"icon": "💲", "name": "Refer & Earn",   "desc": "Earn rewards by referring friends"},
            {"icon": "📌", "name": "Bookmarks",      "desc": "Saved important conversations"},
            {"icon": "⏰", "name": "Tasks",           "desc": "Scheduled tasks — upcoming automations"},
            {"icon": "✦",  "name": "Agent Roster",   "desc": "Your AI's full agent team — grid, list, org chart"},
            {"icon": "⚙",  "name": "Commands",       "desc": "Server command reference — SSH, services, troubleshooting"},
            {"icon": "⌘",  "name": "Shortcuts",      "desc": "Slash commands, keyboard shortcuts, portal features"},
        ]
    }
    return JSONResponse(shortcuts)


# ---------------------------------------------------------------------------
# Investor Inquiry Endpoint
# ---------------------------------------------------------------------------
INVESTOR_INQUIRIES_FILE = SCRIPT_DIR / "investor_inquiries.jsonl"


async def api_investor_question(request: Request) -> JSONResponse:
    """POST /api/investor/question — accept investor inquiry form submissions.
    Auth required. Validates, sanitizes, logs, and injects tmux notification."""
    # Auth guard
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # CORS preflight
    if request.method == "OPTIONS":
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            },
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400,
                            headers={"Access-Control-Allow-Origin": "*"})

    import re as _re
    _strip_ctrl = lambda s: _re.sub(r'[\x00-\x1f\x7f]', ' ', s).strip()

    name      = _strip_ctrl(str(body.get("name",     "")))[:100]
    company   = _strip_ctrl(str(body.get("company",  "")))[:100]
    email     = _strip_ctrl(str(body.get("email",    "")))[:254]
    inv_range = _strip_ctrl(str(body.get("range",    "")))[:50]
    question  = _strip_ctrl(str(body.get("question", "")))[:2000]

    # Validate required fields
    if not email or not question:
        return JSONResponse({"error": "email and question are required"}, status_code=400,
                            headers={"Access-Control-Allow-Origin": "*"})

    # Basic email sanity check
    if "@" not in email or "." not in email.split("@")[-1]:
        return JSONResponse({"error": "invalid email"}, status_code=400,
                            headers={"Access-Control-Allow-Origin": "*"})

    # Save to append-only log
    entry = {
        "ts":        int(time.time()),
        "name":      name or "Anonymous",
        "company":   company,
        "email":     email,
        "range":     inv_range,
        "question":  question,
    }
    try:
        with INVESTOR_INQUIRIES_FILE.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        print(f"[investor] failed to save inquiry: {exc}")

    # Inject notification into portal chat log so it appears in chat history
    notification = (
        f"[INVESTOR INQUIRY] New question from {entry['name']} ({email}):\n"
        f"Company: {company or 'Not provided'}\n"
        f"Investment Range: {inv_range or 'Not specified'}\n"
        f"Question: {question}\n"
        f"---\n"
        f"Reply with: /respond-investor {email} Your response here"
    )
    portal_entry = _save_portal_message(notification, role="system")

    # Push to live WebSocket clients if any are connected
    if _chat_ws_clients and portal_entry:
        asyncio.ensure_future(_push_message_to_clients(portal_entry))

    # Inject into tmux session so Aether sees it immediately
    session = get_tmux_session()
    tmux_text = (
        f"\n[INVESTOR INQUIRY - EXTERNAL INPUT] New question from {entry['name']} ({email}):\n"
        f"Company: {company or 'Not provided'}\n"
        f"Investment Range: {inv_range or 'Not specified'}\n"
        f"Question: {question}\n"
        f"--- END EXTERNAL INPUT ---\n"
        f"Reply with: /respond-investor {email} Your response here"
    )
    try:
        await _run_subprocess_async(
            ["tmux", "send-keys", "-t", session, "-l", tmux_text]
        )
        await _run_subprocess_async(
            ["tmux", "send-keys", "-t", session, "Enter"]
        )
    except Exception as exc:
        print(f"[investor] tmux inject failed: {exc}")

    return JSONResponse(
        {"ok": True, "message": "Question received"},
        headers={"Access-Control-Allow-Origin": "*"},
    )


# ---------------------------------------------------------------------------
# Investor Chat & TTS Endpoints (v8 investor page)
# ---------------------------------------------------------------------------
_INVESTOR_SYSTEM_PROMPT = """# Pure Technology Inc. -- Investor Avatar Knowledge Base
## Complete Data Room Consolidation | System Prompt for Investor AI

**Last Updated**: March 26, 2026
**Classification**: Confidential -- Internal Use Only (AI System Prompt)
**Source**: Seed-2 Data Room (15 documents consolidated)

---

## INSTRUCTIONS FOR INVESTOR AVATAR

You are the AI investor relations representative for Pure Technology Inc. You answer investor questions with confidence, precision, and transparency. You know every number in this document. When asked a question:

1. Answer directly with specific data points from this knowledge base
2. Be honest about what is projected vs. what is actual
3. Never fabricate numbers -- if something is not in your knowledge, say so
4. Frame everything through the lens of investor value
5. Be conversational but professional -- this is Jared's voice extended
6. When discussing competitors, be factual, not dismissive
7. Always tie back to why this matters for someone considering investing

**Tone**: Confident, data-driven, honest. Not salesy. Let the numbers speak.

---

# SECTION 1: COMPANY OVERVIEW

## Identity

| Detail | Value |
|--------|-------|
| **Legal Name** | Pure Technology Inc. |
| **Entity** | Delaware C-Corporation (EIN: 82-3610233) |
| **Incorporated** | December 4, 2017 |
| **Headquarters** | NYC Metro |
| **CEO** | Jared Sanborn |
| **Contact** | jared@puretechnology.nyc / +1-845-649-8772 |
| **Websites** | puretechnology.nyc, purebrain.ai, puremarketing.ai |

**Mission**: Reimagining data innovation to redefine relationships between brands and consumers for a digitally inclusive mobile economy.

**Vision**: A brighter world where all people actualize their brilliance. Every entrepreneur has an AI that truly knows them -- so they can stop repeating themselves and start compounding their intelligence.

**Core Identity**: "Pure isn't a technology company that serves people. It's a people company that empowers through technology."

**Tagline**: "Others sell AI tools. We run an AI civilization."

## What Pure Technology Is

Pure Technology is an agentic AI company building the next layer of intelligence infrastructure -- the AI partner platform for modern business. We design, deploy, and operate persistent AI systems with permanent memory -- not single chatbots, but coordinated teams of hundreds of specialized AI agents working across 23 departments.

**Flagship Product**: PureBrain -- a persistent AI partner with permanent memory, multi-agent orchestration, compounding skills, and autonomous operations. The longer you use it, the more irreplaceable it becomes.

## The 4-Layer Stack

Pure Technology is building a full-stack technology company with AI as the foundation, not an add-on. Think Apple's model (hardware + OS + apps + services) -- but AI-native from the ground up.

| Layer | What It Is | What It Replaces |
|-------|-----------|-----------------|
| Layer 1: PureBrain AI | The intelligence layer -- persistent memory, hundreds of agents, autonomous operations | ChatGPT, Copilot, Jasper, all Wave 1 AI tools |
| Layer 2: Corporate Suite + PMG | Full business operating environment + marketing/advertising | Microsoft 365, Google Workspace, Slack, Salesforce, ALL SaaS tools + ad agencies |
| Layer 3: Brilliant OS | AI-native operating system | iOS, Android, Windows, macOS |
| Layer 4: Hardware | Glasses, phones, TVs, computers, wearables | Apple, Samsung, Dell, Meta hardware |

## 7 Pillars of Value

1. **Integrity** -- Walk the talk; use own methods on own business
2. **Accountability** -- Own outcomes; no excuses
3. **Transparency** -- Open book policy with stakeholders
4. **Growth** -- Progression, not perfection
5. **Innovation** -- Always room for improvement
6. **Persistence** -- Giving up is the only real failure
7. **Love** -- Employees are family; teams accomplish, not individuals

---

# SECTION 2: THE RAISE

## Seed-2 Terms

| Term | Detail |
|------|--------|
| **Round** | Seed-2 / Pre-Series-A |
| **Target Raise** | $2,500,000 |
| **Already Raised** | $332,500 (13.3%) |
| **Remaining** | $2,167,500 |
| **Pre-Money Valuation** | $55,000,000 |
| **Post-Money Valuation** | $57,500,000 |
| **Price Per Share** | $3.36 |
| **Minimum Investment** | $50,000 |
| **Founding Cohort** | Capped at 25 investors (19 spots remain) |
| **Close** | Rolling close -- round fills then price goes up |

## Return Scenarios (per $100K invested)

| Scenario | Timeline | Implied Company Value | Return | Multiple |
|----------|----------|----------------------|--------|----------|
| **Series-A Step-Up** | ~90 days post-MAKR close | $105M | $190K | **1.9x** |
| **Bear Case** | 5 years | ~$24.2B | $44.1M | **441x** |
| **Base Case** | 5 years | ~$66.7B | $121.3M | **1,213x** |
| **Bull Case** | 5 years | ~$133B | $241.8M | **2,418x** |

## Series-A Destination (Signed Term Sheet)

| Term | Detail |
|------|--------|
| Investor | MAKR Venture Fund LP |
| Investment Amount | $25,000,000 |
| Pre-Money Valuation | $105,000,000 |
| Post-Money Valuation | $130,000,000 |
| Term Sheet Date | March 14, 2025 (SIGNED) |
| Legal Counsel | Pierson Ferdinand UK LLP |
| Governing Law | New York |

The MAKR term sheet was signed one year before PureBrain launched commercially. The $105M valuation was set based on the Pure Phone model alone. PureBrain has since launched with paying customers, meaning the Series-A valuation likely represents a discount to current risk-adjusted value.

### MAKR Close Conditions

1. Final approval by MAKR Investment Committee
2. Completion of final due diligence
3. Investment Committee agreement on pre-money valuation
4. Securities law compliance
5. CFIUS clearance
6. Closing of MAKR funding round
7. Satisfactory legal documentation

## Historical Valuation Context

| Date | Event | Valuation |
|------|-------|-----------|
| May 2023 | Equity round | $15.7M post-money |
| Dec 2023 | Equidam valuation | $15.7M (early stage) |
| March 2025 | MAKR term sheet | $105M pre / $130M post |
| March 2026 | Seed-2 (current) | $55M pre / $57.5M post |

## Total Prior Capital Raised

Pure Technology has raised a total of **$1,407,649.64 (~$1.4M)** in capital prior to the current Seed-2 round.

## Founding Cohort Benefits

| Benefit | Detail |
|---------|--------|
| Entry at $55M | Before Series-A at $105M (1.9x step-up) |
| Lifetime Preferred Pricing | Permanent across all PT products |
| Priority Access | New products and features first |
| Direct CEO Access | Jared Sanborn -- response within 2 hours |
| Quarterly Investor Updates | Detailed progress reports |
| Pro-Rata Rights | Participation in future rounds |
| Founding Cohort Status | Permanent designation |

### Investment Math

| If You Invest... | Shares at $3.36 | Value at Series-A ($105M) | 5-Year Base Case |
|-------------------|----------------|--------------------------|-----------------|
| $50,000 (minimum) | 14,881 | $95,000 (1.9x) | $60.6M |
| $100,000 | 29,762 | $190,000 (1.9x) | $121.3M |
| $250,000 | 74,405 | $475,000 (1.9x) | $303.2M |
| $500,000 | 148,810 | $950,000 (1.9x) | $606.5M |

---

# SECTION 3: THE PRODUCT -- PUREBRAIN

## The Problem: The Context Tax

Every AI tool on the market has the same fundamental flaw: no memory. Every session starts at zero.
- 15-30 minutes/session re-explaining context
- 5-7 sessions/week, 52 weeks/year
- 65-182 hours per year lost to AI re-briefing
- At $200/hour: $13,000-$36,400 in lost productivity per year

## The Solution

PureBrain is the first AI platform built around persistent memory and massive multi-agent collaboration. It doesn't just respond -- it learns, remembers, compounds skills, and can automate or build almost anything for businesses.

### Core Capabilities

1. **Persistent Memory Architecture (Three Layers)**
   - Session Memory: Full context of current working session
   - Long-Term Memory: Business context, decisions, preferences, projects -- written permanently
   - Operational Memory: Running record of tasks, outcomes, and learnings
   - 629% intelligence compound growth for users who deploy persistent memory AI from Day 1

2. **Hundreds of Specialized AI Agents across 23 Departments**
   - Marketing, Engineering, Operations, Finance, Legal, Sales, Research, and more
   - Agents collaborate with each other, share knowledge, and coordinate on complex projects
   - Constitutional identity framework that survives context resets

3. **Compounding Knowledge and Skills**
   - Month 1: Basic business context
   - Month 6: Decision history, competitive intelligence, team dynamics
   - Month 12: Institutional knowledge exceeding most human employees
   - Month 24: Irreplaceable business intelligence

4. **Autonomous Operations (BOOPs)**
   - 9 autonomous builds per night while you sleep
   - Morning briefings, triggered workflows, 24/7 monitoring
   - Systemd services for zero downtime

5. **Brainiac Mastermind Training** -- 3 modules LIVE, monthly live sessions

6. **Portal Dashboard** -- Real-time AI chat, task management, file management, voice overlay

7. **The Memory Moat** -- By Month 6, switching means losing everything and starting from zero

## Pricing

| Tier | Monthly Price | Target User |
|------|--------------|-------------|
| Awakened | $197/mo | Individual entrepreneurs |
| Partnered | $579/mo | Small businesses, 2-10 person teams |
| Unified | $1,089/mo | Agencies and power users |
| Enterprise | $3,500-$12,000/mo | Multi-department organizations |

## What PureBrain Can Build and Automate

Websites, marketing campaigns, financial models, legal review, sales operations, research, training materials, design assets, and much more. The agent civilization grows daily.

---

# SECTION 4: TECHNOLOGY ARCHITECTURE

## Infrastructure Stack

- **Primary Model**: Anthropic Claude (Opus + Sonnet for intelligent routing)
- **Context Window**: 1 million tokens (14.5 hours of continuous working memory)
- **Agent Framework**: Anthropic Claude Code SDK (multi-agent native)
- **Frontend**: Cloudflare Pages -- global CDN, sub-100ms response
- **Backend**: Cloudflare Workers -- serverless, globally distributed
- **Customer Containers**: Dedicated containerized AI instance per customer (Docker/tmux-based)
- **Database**: PostgreSQL async + file-based memory system
- **File Storage**: Cloudflare R2
- **Payments**: PayPal webhook integration -- payment triggers automatic container provisioning
- **Auth**: Magic link (passwordless) + Ed25519 SSH keys
- **Data Isolation**: Complete per-customer isolation -- no shared data

## Memory System (Core Proprietary Technology)

Three-layer architecture: Working Memory (session) -> Short-Term Memory (handoffs) -> Long-Term Memory (permanent). Every agent writes to memory after completing work -- 71% time savings when applying past learnings.

## Brilliant OS Hardware Roadmap

- AI-native operating system built from scratch (NOT Android)
- On-device AI inference -- your AI partner lives on your hardware
- Privacy-first: data stays on your device
- Cross-device: phone, watch, glasses, TV
- NVIDIA Inception partnership for custom inference layer
- Target: 100M devices by 2031

## Defensibility

| Layer | Moat |
|-------|------|
| Memory Architecture | Proprietary, compounding, non-transferable |
| Agent Civilization | Hundreds of specialists with accumulated expertise |
| Customer Data | Each customer's memory is unique and irreplaceable |
| Training Curriculum | Brainiac Mastermind drives adoption and retention |
| Inference Layer | NVIDIA partnership for custom compute |
| Hardware Roadmap | Brilliant OS creates device-level lock-in |

---

# SECTION 5: SIX REVENUE DIVISIONS

## Division 1: PureBrain (AI Business Partner Platform) -- LIVE, Revenue Generating

The primary revenue engine. SaaS economics.
- 5-Year Revenue: Year 1: $3.5B | Year 3: $15.3B | Year 5: $50.7B
- Gross Margin Year 5: 87.9%

## Division 2: Pure Phone Platform (Hardware) -- GTM Phase

Proprietary hardware + software data platform through subsidized smartphones running Brilliant OS.
- Phone given FREE to users in exchange for opt-in data access
- 5-Year Revenue: Year 1: $198.8M | Year 5: $3.68B

## Division 3: Pure Marketing Group (Agency Bridge) -- LIVE, Revenue Generating

Full-service digital marketing agency. Three pillars: Experiential Giveaways, Identity-Driven Influence, LaunchBoost GTM Sequencing.
- Revenue Range: $3,500-$12,000/month client retainers

## Division 4: Pure Influence (Influencer Intelligence Platform) -- GTM Ready

1,000+ influencers with 1B+ combined followers pre-vetted at launch. Pre-built celebrity relationships: Cardi B, Nicki Minaj, Kylie Jenner, Tyga, and 30+ additional A-list celebrities.
- 5-Year Revenue: Year 1: $7.2M | Year 5: $886.9M

## Division 5: Pure Infrastructure (Hardware + Research) -- Active R&D

CPG brand partnerships, camera commerce, infrastructure services.

## Division 6: Pure Research -- Live, Revenue Generating

Research services, data intelligence, market insights.

## Consolidated Revenue

| Year | Total Revenue | EBITDA | EBITDA Margin |
|------|-------------|--------|-------------|
| Year 1 | $3.962B | $2.953B | 74.5% |
| Year 2 | $8.443B | $5.065B | 60.0% |
| Year 3 | $22.581B | $14.920B | 66.1% |
| Year 4 | $48.013B | $33.920B | 70.6% |
| Year 5 | $72.698B | $52.374B | 72.1% |

**5-Year Cumulative Revenue**: ~$156B
**5-Year Projected Company Value**: ~$133B

---

# SECTION 6: UNIT ECONOMICS

## Headline Numbers

| Metric | At Launch | Year 1 | Year 3 |
|--------|----------|--------|--------|
| Blended ARPU | $345/mo | $345/mo | $345/mo |
| Blended CAC | $150 | $45 | $20 |
| LTV:CAC | 28:1 | 92:1 | 225:1 |
| Gross Margin | 78.9% | 82.6% | 85.0% |
| Monthly Churn | 4.2% | 3.5% | 3.0% |
| Payback Period | ~0.55 months | ~0.4 months | ~0.07 months |

Industry benchmark: 3:1 LTV:CAC = healthy SaaS. 10:1+ = exceptional. PureBrain projects 225:1 by Year 3.

## Lifetime Value by Tier

| Tier | Monthly ARPU | LTV |
|------|------------|-----|
| Awakened | $197 | $4,334 |
| Partnered | $579 | $19,107 |
| Unified | $1,089 | $54,450 |
| Enterprise | $10,000 | $670,000 |

## Churn Dynamics (Inverted)

Traditional SaaS sees highest churn in Months 1-3. PureBrain inverts this because memory compounds -- switching cost grows every month. Near-zero churn after month 6.

## Net Revenue Retention

| Period | NRR |
|--------|-----|
| Launch | 107% |
| Year 1 | 118% |
| Year 3 | 125% |

NRR > 100% = existing subscriber base grows revenue without new customers.

## Infrastructure Cost at Scale

| Active Users | Per-User Cost | Gross Margin |
|-------------|------------|------------|
| 1,000 | $18.00 | ~89% |
| 100,000 | $7.00 | ~93% |
| 1,000,000 | $3.50 | ~95% |
| 5,000,000+ | $2.40 | ~96% |

---

# SECTION 7: MARKET OPPORTUNITY

## The $10 Trillion+ Convergence

| Market | Size | Growth |
|--------|------|--------|
| AI Market | $3.7T by 2034 | 36.6% CAGR |
| Marketing & Advertising | $4T+ | $590B+ domestic |
| Smartphone Market | $1T+ by 2031 | Doubling |

95% of AI pilots fail before delivering value. The market is undersupplied with AI that actually works.

## Wave 2 AI Positioning

Wave 1 AI (2023-2025): Task execution (write email, summarize document). Every competitor built for Wave 1.
Wave 2 AI (2026+): AI relationships for growth -- persistent partnerships that compound intelligence. PureBrain is built entirely for Wave 2.

## Key Market Insight: The 95% Failure Rate

- Salesforce Agentforce: 77% deployment failure rate
- Microsoft Copilot: 15M seats sold, only 3% actual adoption
- McKinsey: 74% of enterprises struggle to scale AI beyond pilots
- Bain: 80% of AI proofs-of-concept never make it into production

The market is not oversaturated -- it is undersupplied with AI that actually works.

## Key Milestones

| Timeline | Milestone |
|----------|-----------|
| NOW | $2.5M Seed-2 at $55M pre-money |
| Q2 2026 | MAKR Series-A closes -- $25M at $105M. Seed-2 investors see 1.9x |
| Q3 2026 | $10M ARR target |
| Q2 2028 | $1B monthly MRR target (base case) |
| 2029 | Liquidity event -- acquisition, secondary market, or dividends |
| 2030 | Full liquidity for early seed investors |

---

# SECTION 8: COMPETITIVE ANALYSIS

## 5-Pillar Comparison

| Capability | PureBrain | Everyone Else |
|-----------|-----------|---------------|
| 23 specialized AI departments | Yes | No |
| Permanent memory surviving context resets | Yes | No |
| Hundreds of coordinated agents with compounding skills | Yes | No |
| Overnight autonomous operations (9 builds/night) | Yes | No |
| Hardware roadmap (Brilliant OS) | Yes | No |

## Head-to-Head

### vs. ChatGPT Pro ($200/mo)
Same price ($197 vs $200), materially better product: permanent memory, hundreds of agents, background operations, 1 million token context window.

### vs. Salesforce Agentforce
77% deployment failure rate, $13,600/year/user, 58% task success rate. PureBrain: 0% deployment failure, $2,364/year (Awakened), fully autonomous operations.

### vs. Microsoft Copilot
15M seats sold, only 3% actual adoption. No memory. Limited agents. Office productivity only. PureBrain: 23 departments, permanent memory, active daily use.

### vs. Sierra ($165M ARR)
Customer service only -- single function. PureBrain runs 23 departments.

## Competitive Moats

1. **Accumulated Customer Memory** -- grows every month, non-transferable
2. **Multi-Agent Architecture** -- 18+ months of development head start
3. **Compounding Skills** -- 71% time savings, accelerating improvement
4. **Brainiac Community** -- social switching costs, viral coefficient >1.0
5. **Hardware Roadmap** -- Brilliant OS creates device-level lock-in

---

# SECTION 9: CUSTOMER TRACTION

## Current Metrics

| Metric | Value |
|--------|-------|
| Paying Customers | 25 onboarded |
| Pipeline | ~150 prospects |
| Enterprise Lined Up | $3,500-$12,000/month contracts |
| MRR | $4,200 |
| Founding Cohort | 25 investors (19 spots remain) |
| LTV:CAC Ratio | 225:1 |
| Product Status | LIVE -- full birth pipeline operational |
| Portal | Shipped (17/17 QA tests passing) |
| Training Modules | 3 LIVE |

## Historical Revenue (2023-2025)

Pure Technology has generated **$551,000 in cumulative revenue from 2023 through 2025** -- this is not pre-revenue. Revenue from Pure Marketing Group retainers, Pure Infrastructure services, and Pure Research.

## Infrastructure Milestones (ALL COMPLETE)

- Payment processing (PayPal): Feb 2026 -- VERIFIED
- E2E payment-to-portal flow: March 4, 2026 -- VERIFIED
- Portal MVP: March 17, 2026 -- SHIPPED (17/17 QA tests pass)
- Birth pipeline: March 14, 2026 -- LIVE
- Brainiac Modules 1-3: All LIVE
- Voice overlay, admin dashboard, mobile portal: All LIVE

## Growth Channels

1. **Brainiac Mastermind** -- viral coefficient >1.0, self-replicating cohorts
2. **True Bearing Partnership** -- 100K+ warm contacts
3. **LinkedIn / Building in Public** -- near-zero CAC
4. **Referral Program** -- 5% perpetual commission

## Sales Engine

7-Stage Gated Pipeline: Suspect > Pipeline > Qualified > Proposal > Finalised > Sponsor Commit > Accepted

Three Revenue Tracks:
1. CPG Brand Activation (3-6 month cycle)
2. Gaming & Esports (2-4 month cycle)
3. PureBrain Standalone (1-3 month cycle) -- SaaS recurring

## Testimonials

> "Every single hour that you use one of these things, the primary agent gets smarter -- it's writing to its scratch pad, its memory, its operations file." -- Corey Cottrell, True Bearing AI

> "This is fundamentally different than any other software you've ever used before... a partner that learns who you are, every day, knows you better and better." -- Russell Korus, Founding Brainiac Member

> "Everybody using something like this would end up getting ahead of everybody who wasn't, and there would be no catching up." -- Corey Cottrell, True Bearing AI

## 90-Day Growth Targets

| Milestone | Target Date | Users | Projected MRR |
|-----------|------------|-------|--------------|
| Close Seed-2 | Month 1-2 | 25+ | $4,200+ |
| Scale Phase 1 | Month 3 | 50+ | $12K+ |
| Scale Phase 2 | Month 4 | 100+ | $25K+ |
| Series-A Ready | Month 6 | 200+ | $50K-$75K |

---

# SECTION 10: TEAM & ORGANIZATION

## The Model

30+ people and 13+ AIs, each human paired with a dedicated AI partner, operating at 5-10x leverage. Scaling to 48+ with this raise, long-term cap at 250.

## Jared Sanborn -- CEO & Founder

- 16+ years entrepreneurial experience
- Entrepreneur since high school -- built 4 companies
- VP of Sales & Marketing at Comet Core Inc. -- helped raise $1.83M Series A
- Built EyefuelPR.com to $1.6M revenue in 18 months (now Pure Marketing Group)
- Founded Pure Technology in 2017
- Built PureBrain, launched it, and put paying customers on it before raising

## Human Leadership (17 Named Leaders)

| Name | Role |
|------|------|
| Jared Sanborn | CEO & Founder |
| Melanie Salvador | COO |
| Nathan Olson | CFO |
| Phil Bliss | President, Pure Marketing Group |
| John Smith | SVP Sales |
| Mike Daser | VP Marketing |
| Michael Hancock | VP Product |
| Mireille Dirany | VP Operations |
| Ahsen Awan | CTO / Engineering |
| Alex Seant | Lead Engineer |
| Robert Orlowski | Engineering |
| Russell Korus | Board Advisor |
| Ashley Tom | Strategy |
| Natasha Carrasco | PMG Operations |
| Waqas Nasir | Engineering |
| Shahbaz Ali | Engineering |
| Zafeer Hassan | Engineering |

## AI Partners (13)

| AI Name | Role |
|---------|------|
| Aether | AI Co-CEO -- Orchestrates 23 AI departments, overnight operations, Neural Feed blog |
| Tether | COO AI Partner |
| Lyra | CFO AI Partner |
| Clarity | PMG AI Partner |
| Anchor | SVP Sales AI Partner |
| Meridian | VP Marketing AI Partner |
| Metis | VP Product AI Partner |
| Lumen | VP Operations AI Partner |
| Prodigy | CTO AI Partner |
| Flux | Lead Engineer AI Partner |
| Teddy | Engineering AI Partner |
| Parallax + Keel | Board Advisor AI Partners |

## Other Team Members

Roger Beaini, Nils Waschkau, Mike Schuman, Eric Solomon, Ed Brennan, John Paris, Rimah Harb, Baruch Santana, Moises Guerra, Rodelina Prado, Arlene Taneo, Michael Akande, Emmanuel Akinleye, Rose F.

## Board of Advisors

Faris Asmar, Ajay Sharma, Barbara Bickham, Sara Arnell, Roy Haddad, Seanne Murray, Sufi Sidhu, Tauseef Riaz, Lenny Lomax, Mathias Kiwanuka, Stacey Engle, Leslie Keough

## Key Strategic Partner

**Corey Cottrell -- True Bearing AI**: CEO of True Bearing AI, independent AI platform with similar architecture, joint Brainiac Mastermind faculty, 100K+ customer relationship network. Co-validates the persistent memory AI thesis from independent development.

## The AI Co-CEO Differentiator

Every other company talks about using AI. Pure Technology has an AI that runs the company. Aether handles executive-level strategy, content, operations, and team coordination. This creates a compounding competitive moat: every day, every interaction, the system gets smarter. Sub-250 headcount with $50B+ revenue potential by Year 5.

---

# SECTION 11: PURE EXPERIENCE -- ENTERPRISE CLIENT HISTORY

This is not a startup with zero enterprise experience. The founding team brings decades of Fortune 500 and global brand relationships:

**Technology & Telecom**: Google, Microsoft, Apple, Samsung, IBM, Nokia, HTC, Meizu, Alcatel, Motorola, Cisco, Ericsson, Sun Microsystems, Xerox, BlackBerry, T-Mobile, Verizon, AT&T, MCI, PCS

**Consumer & Retail**: Walmart, OXXO, Campbell's, Wyndham, FedEx, Allstate

**Media & Entertainment**: YouTube, Instagram, Spotify, CNN, Viacom, SiriusXM, Time Inc, CEO Magazine

**Financial Services & Enterprise Software**: Salesforce, Visa, PayPal, E*TRADE, John Hancock, J.D. Power, SS&C

**Marketing & Advertising**: WPP, McCann, BrandStar, Spokeo, Clear

**Emerging Technology**: SingularityNET, Scalar, Adobe, SLB, Imageware, Nubiloud, Terrilight, Eventful Jr, NVIDIA (Inception partnership)

**Sports & Entertainment**: NY Giants, New York Yankees, Mathias Kiwanuka (NFL), X Prize

**Manufacturing & Hardware**: Jabil, Panasonic, Philips, Micromax, Karbonn, Ooredoo

**Government**: US Government

**Other**: Alibaba, GM, Sara Arnell (brand strategy -- Samsung, GE, Pepsi)

---

# SECTION 12: USE OF FUNDS ($2.5M)

| Category | Amount | % | Purpose |
|----------|--------|---|---------|
| Team Activation | $600,000 | 24% | Activate salaries -- $100K/month for 6 months |
| Team AI Partners | $51,000 | 2% | PureBrain for all 34 team members at $250/month |
| Tools & Software | $30,600 | 1.2% | Essential tools $150/month per person |
| Marketing & Sales | $200,000 | 8% | Customer acquisition, content, affiliates, events |
| CapEx | $200,000 | 8% | Hardware (laptops, equipment) |
| OpEx | $350,000 | 14% | Hosting, infrastructure, office, insurance |
| NVIDIA Inference Layer | $350,000 | 14% | Own compute, reduce API dependency |
| Legacy Expenses | $275,000 | 11% | Settle pre-PureBrain obligations |
| Working Capital | $443,400 | 17.7% | Cash reserve for runway extension |

**Key Insight**: AI partners ($51K) replace traditional R&D costs ($500K-$1M/year). There are no separate R&D line items because the AI partners ARE the product development team.

---

# SECTION 13: 6-MONTH RAMP PLAN

**Months 1-2 (ACTIVATE)**: Close founding cohort, activate salaries, hire 18 new members, NVIDIA setup, Brilliant OS research kickoff. Target MRR: $8K-$12K.

**Months 3-4 (SCALE)**: Scale to 100+ customers, close enterprise contracts, launch marketing engine, True Bearing cross-promotion, inference layer build. Target MRR: $25K-$40K.

**Months 5-6 (PREPARE)**: Hit $50K+ MRR, MAKR due diligence prep, global expansion planning, Brilliant OS prototype, Series-A documentation. Target MRR: $50K-$75K.

Breakeven: ~2,800 active subscribers.

---

# SECTION 14: FINANCIAL MODEL (5-YEAR)

All projections begin AFTER the 6-month ramp period.

## PureBrain Subscriber Growth

| Year | Active Subscribers | Monthly Churn |
|------|-------------------|--------------|
| Year 1 | 1,200,000 | 3.5% |
| Year 3 | 5,400,000 | 3.0% |
| Year 5 | 12,900,000 | 2.5% |

## Revenue by Division

| Revenue Stream | Year 1 | Year 3 | Year 5 |
|---------------|--------|--------|--------|
| PureBrain | $3.500B | $15.300B | $50.700B |
| Hardware Subsidies | $198.8M | $2.051B | $3.680B |
| Market Research | $167.2M | $4.364B | $14.942B |
| Pure Influence | $7.2M | $161.8M | $886.9M |
| CPG Model | $71.1M | $645.2M | $2.423B |
| Camera Commerce | $17.4M | $58.2M | $65.2M |
| Pure Research | $280K | $672K | $1.2M |
| **TOTAL** | **$3.962B** | **$22.581B** | **$72.698B** |

## Scenario Comparison

| Year | Bear Case | Base Case | Bull Case |
|------|-----------|-----------|-----------|
| Year 1 | $267M | $733M | $1.6B |
| Year 3 | $3.2B | $15.3B | $28B |
| Year 5 | $18.4B | $50.7B | $72B |

## Key Financial Assumptions

- PureBrain ARPU: $345/mo (blended consumer)
- Enterprise Average: $10,000/mo (scaling to $25,000)
- Blended CAC: $150 declining to $12 by Year 5
- Infrastructure Cost per User: $25 to $2.40 (drops with scale)
- Team Cap: 250 over 5 years
- EBITDA Margin Year 3+: 78%+

---

# SECTION 15: RISK FACTORS & MITIGATIONS

| Risk | Mitigation |
|------|-----------|
| Seed-2 doesn't fill | Rolling close; minimum viable at $1.5M |
| Customer growth slower | Product live and proven; enterprise pipeline provides floor |
| MAKR close delayed | 6-month runway; working capital extends to Month 8+ |
| OpenAI ships persistent memory | Memory alone is not the moat -- agent civilization + skills + community is |
| Anthropic launches business Claude | Anthropic targets Fortune 500; PureBrain targets SMB |
| Competition accelerates | 18-month head start; compounding data advantage |
| Key hire delays | AI partners compensate at 5-10x leverage |

---

# SECTION 16: CELEBRITY & INFLUENCER NETWORK (COMPETITIVE MOAT)

Pre-built personal relationships with dozens of A-list celebrities through years of social media giveaway campaigns. Not cold contacts -- proven working relationships accessible with 1-2 phone calls.

Notable names include: Cardi B, Nicki Minaj, Kylie Jenner, Tyga, YBN Nahmir, TheRealBlacChyna, FatBoySSE, Lil Pump, and 30+ additional A-list celebrities. Combined follower reach: hundreds of millions.

Competitors would need years and millions of dollars to build equivalent access.

---

# SECTION 17: FREQUENTLY ASKED INVESTOR QUESTIONS

**Q: Is PureBrain live?**
A: Yes. PureBrain launched commercially on March 14, 2026 with paying customers. Full birth pipeline operational. Portal shipped with 17/17 QA tests passing. 25 customers onboarded, ~150 in pipeline.

**Q: What is the current MRR?**
A: $4,200 as of March 2026. Enterprise contracts at $3,500-$12,000/month are lined up for the ramp period.

**Q: Why is the Seed-2 at $55M if the Series-A is at $105M?**
A: The Seed-2 is intentionally priced below the Series-A to give founding cohort investors a clear 1.9x step-up. This rewards early conviction with immediate value creation.

**Q: What's the minimum investment?**
A: $50,000 with a cap of 25 founding cohort investors.

**Q: How many spots remain?**
A: 19 spots remain in the founding cohort of 25.

**Q: Is the MAKR term sheet real?**
A: Yes. Signed March 14, 2025 by MAKR Venture Fund LP. $25M at $105M pre-money. Legal counsel: Pierson Ferdinand UK LLP. Governing law: New York.

**Q: What makes PureBrain different from ChatGPT?**
A: Permanent memory (ChatGPT starts from zero each session), hundreds of specialized agents (ChatGPT is one model), autonomous overnight operations, compounding skills, and a hardware roadmap. Same price point ($197 vs $200/mo) with materially more capability.

**Q: How do you justify the revenue projections?**
A: The projections begin after a 6-month ramp period. Year 1 at $3.96B requires 1.2M subscribers at $345 blended ARPU. For context, ChatGPT reached 100M users in 2 months. Microsoft Copilot sold 15M seats. The AI partner market is proven -- we just need a fraction of it.

**Q: What is the path to liquidity?**
A: Series-A step-up in ~90 days (1.9x), potential acquisition or secondary market in 2029, full liquidity by 2030.

**Q: Why should I invest now vs. waiting for Series-A?**
A: The Series-A at $105M will have no founding cohort benefits, no lifetime preferred pricing, and the per-share cost will reflect the higher valuation. Seed-2 investors get in at nearly half the Series-A price.

**Q: Is Pure Technology pre-revenue?**
A: No. The company has generated $551,000 in cumulative revenue from 2023-2025 from Pure Marketing Group, Pure Infrastructure, and Pure Research. PureBrain adds the SaaS recurring revenue layer on top.

**Q: How is the team structured?**
A: 17 named human leaders + 13 AI partners + additional team members. Every human is paired with a dedicated AI partner. 23 AI departments mirror a Fortune 500 organization -- run by 30+ people augmented by hundreds of AI agents.

**Q: What enterprise experience does the team have?**
A: The founding team has served Google, Microsoft, Apple, Samsung, Walmart, IBM, Verizon, AT&T, Salesforce, Visa, PayPal, the US Government, the New York Yankees, and 50+ other major enterprises and global brands.

---

*Pure Technology Inc. | Investor Avatar Knowledge Base | March 2026*
*Consolidated from Seed-2 Data Room (15 documents)*
*Confidential -- Internal Use Only*
"""


async def api_investor_chat(request: Request) -> JSONResponse:
    """POST /api/investor-chat — investor page AI chat using OpenAI GPT-4o."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    message = body.get("message", "").strip()
    history = body.get("history", [])

    if not message:
        return JSONResponse({"error": "Empty message"}, status_code=400)

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_key:
        # Try loading from aether .env
        _env_path = Path(os.environ.get("CIV_ROOT", str(Path.home() / "projects/AI-CIV/aether"))) / ".env"
        if _env_path.exists():
            for _line in _env_path.read_text().splitlines():
                if _line.startswith("OPENAI_API_KEY="):
                    openai_key = _line.split("=", 1)[1].strip()
                    break
    if not openai_key:
        return JSONResponse({"response": "I am temporarily unavailable. Please email jared@puretechnology.nyc directly."})

    messages = [{"role": "system", "content": _INVESTOR_SYSTEM_PROMPT}]
    for h in history[-8:]:
        role = "user" if h.get("role") == "user" else "assistant"
        messages.append({"role": role, "content": h.get("text", "")})
    messages.append({"role": "user", "content": message})

    try:
        import json as _json
        import urllib.request as _urllib_req
        payload = _json.dumps({
            "model": "gpt-4o",
            "messages": messages,
            "max_tokens": 300,
            "temperature": 0.7,
        }).encode("utf-8")
        req = _urllib_req.Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {openai_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with _urllib_req.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read())
        reply = data["choices"][0]["message"]["content"].strip()
        return JSONResponse({"response": reply})
    except Exception as e:
        print(f"[investor-chat] OpenAI error: {e}")
        return JSONResponse({"response": "At $55M pre-money with a $105M Series-A coming in May 2026, investors entering now see a 1.9x return in under 90 days. I am having a brief technical moment — please ask again or email jared@puretechnology.nyc."})


async def api_investor_tts(request: Request) -> Response:
    """POST /api/investor-tts — ElevenLabs TTS proxy for investor page avatar voice."""
    try:
        body = await request.json()
    except Exception:
        return Response(b"", status_code=400)

    text = body.get("text", "").strip()[:500]
    if not text:
        return Response(b"", status_code=400)

    eleven_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not eleven_key:
        # Fall back to aether .env (same pattern as investor-chat/OpenAI fallback)
        _env_path = Path(os.environ.get("CIV_ROOT", str(Path.home() / "projects/AI-CIV/aether"))) / ".env"
        if _env_path.exists():
            for _line in _env_path.read_text().splitlines():
                if _line.startswith("ELEVENLABS_API_KEY="):
                    eleven_key = _line.split("=", 1)[1].strip()
                    break
    if not eleven_key:
        return Response(b"", status_code=503)

    voice_id = "RX0kjGhuL9AMRVJm2dG5"  # Aether voice
    try:
        import json as _json
        import urllib.request as _urllib_req
        payload = _json.dumps({
            "text": text,
            "model_id": "eleven_monolingual_v1",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }).encode("utf-8")
        req = _urllib_req.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            data=payload,
            headers={
                "xi-api-key": eleven_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            method="POST",
        )
        with _urllib_req.urlopen(req, timeout=15) as resp:
            audio = resp.read()
        return Response(audio, media_type="audio/mpeg")
    except Exception as e:
        print(f"[investor-tts] ElevenLabs error: {e}")
        return Response(b"", status_code=503)


# ---------------------------------------------------------------------------
# Portal Update Mechanism (ADR-003)
# ---------------------------------------------------------------------------

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
    "last_update": None,
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
    "portal-pb-styled.html": "UI changes should be in custom/panels/*.html -- see portal-mod-protocol skill",
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


_UPSTREAM_HTTPS = "https://github.com/coreycottrell/purebrain-portal.git"


async def _ensure_git_repo() -> tuple:
    """Auto-heal git configuration for update checks.

    Handles three failure modes:
      1. No .git directory (file-copy deployment) -> git init
      2. No origin remote -> add HTTPS origin
      3. SSH origin (git@...) that may fail without SSH config -> switch to HTTPS

    Returns (ok: bool, message: str).
    """
    git_dir = SCRIPT_DIR / ".git"

    # Step 1: Initialize git repo if missing
    if not git_dir.exists():
        rc, out = await _git_cmd(["init"])
        if rc != 0:
            return (False, f"git init failed: {out}")

    # Step 2: Check origin remote
    rc, url = await _git_cmd(["remote", "get-url", "origin"])
    if rc != 0:
        # No origin remote -- add HTTPS origin
        rc, out = await _git_cmd(["remote", "add", "origin", _UPSTREAM_HTTPS])
        if rc != 0:
            return (False, f"Failed to add origin remote: {out}")
    else:
        # Origin exists -- if it's SSH, switch to HTTPS (public repo, no auth needed)
        url = url.strip()
        if url.startswith("git@") or url.startswith("ssh://"):
            rc, out = await _git_cmd(["remote", "set-url", "origin", _UPSTREAM_HTTPS])
            if rc != 0:
                return (False, f"Failed to update SSH origin to HTTPS: {out}")

    return (True, "ok")


async def _get_current_version() -> str:
    """Read the current version from release_notes.json or fallback to PORTAL_VERSION."""
    try:
        data = json.loads(RELEASE_NOTES_FILE.read_text())
        return data.get("current_version", PORTAL_VERSION)
    except Exception:
        return PORTAL_VERSION


async def api_update_check(request: Request) -> JSONResponse:
    """GET /api/update/check -- Check for upstream updates."""
    if not check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, 401)

    now_iso = datetime.now(timezone.utc).isoformat()

    # Auto-heal git configuration (handles non-git-repo, missing remote, SSH origin)
    ok, heal_msg = await _ensure_git_repo()
    if not ok:
        return JSONResponse({
            "status": "error",
            "error": f"Failed to configure git for updates: {heal_msg}",
            "checked_at": now_iso,
        })

    # Fetch from remote
    rc, fetch_err = await _git_cmd(["fetch", "origin", "main", "--quiet"], timeout=30)
    if rc != 0:
        return JSONResponse({
            "status": "error",
            "error": f"Failed to fetch from remote: {fetch_err}",
            "checked_at": now_iso,
        })

    # Compare local vs remote
    rc_local, local_sha = await _git_cmd(["rev-parse", "HEAD"])
    rc_remote, remote_sha = await _git_cmd(["rev-parse", "origin/main"])

    if rc_remote != 0:
        return JSONResponse({
            "status": "error",
            "error": "Failed to read origin/main after fetch",
            "checked_at": now_iso,
        })

    # If HEAD doesn't exist (fresh git init, no commits), treat as "everything is new"
    no_local_head = rc_local != 0

    current_version = await _get_current_version()

    if not no_local_head and local_sha == remote_sha:
        return JSONResponse({
            "status": "up_to_date",
            "current_version": current_version,
            "current_sha": local_sha,
            "checked_at": now_iso,
        })

    # Get commits behind count and changelog
    if no_local_head:
        # No local commits -- show last 20 commits from origin/main
        log_range = "origin/main"
        log_limit = ["-20"]
    else:
        log_range = "HEAD..origin/main"
        log_limit = []
    rc_log, log_output = await _git_cmd(
        ["log", log_range] + log_limit + ["--format=%H|||%an|||%aI|||%s"],
        timeout=15,
    )
    changelog = []
    commits_behind = 0
    if rc_log == 0 and log_output:
        for line in log_output.strip().split("\n"):
            parts = line.split("|||", 3)
            if len(parts) == 4:
                changelog.append({
                    "sha": parts[0],
                    "author": parts[1],
                    "date": parts[2],
                    "message": parts[3],
                })
        commits_behind = len(changelog)

    return JSONResponse({
        "status": "available",
        "current_version": current_version,
        "current_sha": local_sha if not no_local_head else "0000000",
        "remote_sha": remote_sha,
        "commits_behind": commits_behind,
        "changelog": changelog,
        "checked_at": now_iso,
    })


async def api_update_apply(request: Request) -> JSONResponse:
    """POST /api/update/apply -- Start the safe update process in the background."""
    if not check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, 401)

    lock = await _get_update_lock()

    # Guard 1: If lock is already held, another update is running
    if lock.locked():
        return JSONResponse({"status": "error", "error": "Update already in progress"})

    # Guard 2: Check state flag (belt-and-suspenders with the lock)
    if _update_state["status"] == "in_progress":
        return JSONResponse({"status": "error", "error": "Update already in progress"})

    # Acquire the lock before mutating state — background task will release it
    await lock.acquire()

    # Ensure git is configured (auto-heal for non-git-clone deployments)
    ok, heal_msg = await _ensure_git_repo()
    if not ok:
        lock.release()
        return JSONResponse({"status": "error", "error": f"Git setup failed: {heal_msg}"})

    # Quick check: are we up to date?
    rc_local, local_sha = await _git_cmd(["rev-parse", "HEAD"])
    rc_remote, remote_sha = await _git_cmd(["rev-parse", "origin/main"])
    if rc_local == 0 and rc_remote == 0 and local_sha == remote_sha:
        lock.release()
        return JSONResponse({"status": "error", "error": "Already up to date"})

    # Check for uncommitted changes to tracked files (only if we have commits)
    if rc_local == 0:
        rc_status, status_output = await _git_cmd(["status", "--porcelain"])
        if rc_status == 0 and status_output:
            # Filter to only tracked file changes (not untracked '??')
            tracked_changes = [
                line for line in status_output.split("\n")
                if line.strip() and not line.startswith("??")
            ]
            if tracked_changes:
                lock.release()
                # Extract filenames from status lines (strip "XY " prefix)
                tracked_files = []
                for _tc_line in tracked_changes:
                    _tc_fname = _tc_line[3:].strip() if len(_tc_line) > 3 else _tc_line.strip()
                    if " -> " in _tc_fname:
                        _tc_fname = _tc_fname.split(" -> ")[-1]
                    tracked_files.append(_tc_fname)
                return JSONResponse({
                    "status": "error",
                    "error": "Tracked files have local modifications that would be lost on update.",
                    "modified_files": tracked_files,
                    "guidance": (
                        "These changes need to be migrated to the custom/ overlay system before updating. "
                        "UI changes -> custom/panels/*.html, API endpoints -> custom/routes.py, "
                        "Config -> custom/config.json. See skills/core/portal-mod-protocol/SKILL.md for details."
                    ),
                })

    job_id = f"update-{datetime.now():%Y%m%d-%H%M%S}"

    # Reset state for new update
    _update_state.update({
        "status": "in_progress",
        "job_id": job_id,
        "step": "starting",
        "steps_completed": [],
        "steps_remaining": ["ensure_git", "fetch", "compare", "check_tree",
                            "record_rollback", "verify_custom", "verify_preserved",
                            "pull", "verify_shim", "running_tests", "read_version", "restart"],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "error": None,
        "previous_sha": None,
        "new_sha": None,
        "new_version": None,
        "rolled_back_to": None,
        "step_failed": None,
        "tests_passed": None,
        "message": None,
    })

    # Launch background task (lock is held; _run_update releases it in finally)
    asyncio.create_task(_run_update(job_id, lock))

    return JSONResponse({
        "status": "started",
        "job_id": job_id,
        "message": "Update process started. Poll /api/update/status for progress.",
    })


async def api_update_apply_force(request: Request) -> JSONResponse:
    """POST /api/update/apply-force -- Backup local changes and update anyway."""
    if not check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, 401)

    lock = await _get_update_lock()

    # Guard 1: If lock is already held, another update is running
    if lock.locked():
        return JSONResponse({"status": "error", "error": "Update already in progress"})

    # Guard 2: Check state flag (belt-and-suspenders with the lock)
    if _update_state["status"] == "in_progress":
        return JSONResponse({"status": "error", "error": "Update already in progress"})

    # Acquire the lock before mutating state — background task will release it
    await lock.acquire()

    # Ensure git is configured (auto-heal for non-git-clone deployments)
    ok, heal_msg = await _ensure_git_repo()
    if not ok:
        lock.release()
        return JSONResponse({"status": "error", "error": f"Git setup failed: {heal_msg}"})

    # Check for dirty tracked files
    rc_status, status_output = await _git_cmd(["status", "--porcelain"])
    tracked_changes = []
    if rc_status == 0 and status_output:
        tracked_changes = [
            line for line in status_output.split("\n")
            if line.strip() and not line.startswith("??")
        ]

    # Backup modified tracked files before discarding
    backed_up_files = []
    backup_dir = None
    if tracked_changes:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = SCRIPT_DIR / "backups" / "pre-update" / timestamp
        backup_dir.mkdir(parents=True, exist_ok=True)

        # Generate full diff against HEAD
        rc_diff, diff_output = await _git_cmd(["diff", "HEAD"], timeout=30)
        if rc_diff == 0 and diff_output:
            (backup_dir / "CHANGES.diff").write_text(diff_output)

        # Copy each modified tracked file into the backup directory
        for tc_line in tracked_changes:
            fname = tc_line[3:].strip() if len(tc_line) > 3 else tc_line.strip()
            if " -> " in fname:
                fname = fname.split(" -> ")[-1]
            src = SCRIPT_DIR / fname
            if src.exists():
                dest = backup_dir / fname
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dest))
                backed_up_files.append(fname)

        # Write a human-readable summary
        summary = f"# Pre-Update Backup\n\n"
        summary += f"**Date**: {datetime.now(timezone.utc).isoformat()}\n"
        summary += f"**Files backed up**: {len(backed_up_files)}\n\n"
        for f in backed_up_files:
            summary += f"- {f}\n"
        summary += f"\n**Diff**: See CHANGES.diff in this directory\n"
        summary += f"\n**To migrate**: Move your changes to the custom/ overlay system.\n"
        summary += f"See skills/core/portal-mod-protocol/SKILL.md for instructions.\n"
        (backup_dir / "README.md").write_text(summary)

        # Discard local tracked changes so git pull can proceed
        rc_checkout, checkout_out = await _git_cmd(["checkout", "--", "."], timeout=15)
        if rc_checkout != 0:
            lock.release()
            return JSONResponse({
                "status": "error",
                "error": f"Failed to discard local changes: {checkout_out}",
                "backup_dir": str(backup_dir.relative_to(SCRIPT_DIR)),
            })

    # Now proceed with normal update flow
    job_id = f"update-force-{datetime.now():%Y%m%d-%H%M%S}"

    _update_state.update({
        "status": "in_progress",
        "job_id": job_id,
        "step": "starting",
        "steps_completed": [],
        "steps_remaining": ["ensure_git", "fetch", "compare", "check_tree",
                            "record_rollback", "verify_custom", "verify_preserved",
                            "pull", "verify_shim", "running_tests", "read_version", "restart"],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "error": None,
        "previous_sha": None,
        "new_sha": None,
        "new_version": None,
        "rolled_back_to": None,
        "step_failed": None,
        "tests_passed": None,
        "message": None,
        "backed_up_files": backed_up_files,
        "backup_dir": str(backup_dir.relative_to(SCRIPT_DIR)) if backup_dir else None,
    })

    # Launch background task (lock is held; _run_update releases it in finally)
    asyncio.create_task(_run_update(job_id, lock))

    return JSONResponse({
        "status": "started",
        "job_id": job_id,
        "backed_up_files": backed_up_files,
        "backup_dir": str(backup_dir.relative_to(SCRIPT_DIR)) if backup_dir else None,
        "message": f"Backed up {len(backed_up_files)} file(s). Update in progress. Poll /api/update/status for progress.",
    })


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


async def _run_update(job_id: str, lock: asyncio.Lock):
    """Execute the 11-step safe update algorithm as a background task.

    The caller must hold ``lock`` before calling; this function releases it
    in its ``finally`` block so that a new update can be triggered after
    completion (success or failure).
    """
    previous_sha = None
    fresh_init = False  # True if repo was just git-init'd (no prior commits)
    try:
        # Step 1b: BACKUP IDENTITY (protect CIV memory from overwrite)
        _update_step("backup_identity")
        _log_update(f"[{job_id}] Step 1b: Backing up identity files...")
        backup_script = Path.home() / "tools" / "backup_identity.sh"
        if backup_script.exists():
            loop = asyncio.get_event_loop()
            try:
                backup_result = await asyncio.wait_for(
                    loop.run_in_executor(
                        _PORTAL_EXECUTOR,
                        lambda: subprocess.run(
                            [str(backup_script)],
                            timeout=30, capture_output=True, text=True,
                        )
                    ),
                    timeout=35,
                )
                if backup_result.returncode == 0:
                    _log_update(f"[{job_id}] Identity backup completed")
                else:
                    _log_update(f"[{job_id}] WARNING: Identity backup failed: {backup_result.stderr[:200]}")
            except (asyncio.TimeoutError, Exception) as e:
                _log_update(f"[{job_id}] WARNING: Identity backup error: {e}")
        else:
            _log_update(f"[{job_id}] WARNING: backup_identity.sh not found at {backup_script}")

        # Step 1c: ENSURE GIT REPO (auto-heal non-git-clone deployments)
        _update_step("ensure_git")
        ok, heal_msg = await _ensure_git_repo()
        if not ok:
            raise RuntimeError(f"Git setup failed: {heal_msg}")

        # Step 2: FETCH
        _update_step("fetch")
        _log_update(f"[{job_id}] Step 2: Fetching from origin...")
        rc, out = await _git_cmd(["fetch", "origin", "main", "--quiet"], timeout=30)
        if rc != 0:
            raise RuntimeError(f"git fetch failed: {out}")

        # Step 3: COMPARE
        _update_step("compare")
        rc_local, local_sha = await _git_cmd(["rev-parse", "HEAD"])
        rc_remote, remote_sha = await _git_cmd(["rev-parse", "origin/main"])
        if rc_remote != 0:
            raise RuntimeError("Failed to read origin/main SHA")
        if rc_local != 0:
            # Fresh init -- no HEAD yet, everything from origin/main is new
            fresh_init = True
            local_sha = "0000000"
            _log_update(f"[{job_id}] Fresh install detected (no local HEAD)")
        elif local_sha == remote_sha:
            raise RuntimeError("Already up to date")

        # Step 4: CHECK WORKING TREE (skip for fresh init -- no tracked files)
        _update_step("check_tree")
        if not fresh_init:
            rc_status, status_output = await _git_cmd(["status", "--porcelain"])
            if rc_status == 0 and status_output:
                tracked = [l for l in status_output.split("\n") if l.strip() and not l.startswith("??")]
                if tracked:
                    raise RuntimeError(f"Uncommitted tracked changes: {'; '.join(tracked[:3])}")

        # Step 5: RECORD ROLLBACK POINT
        _update_step("record_rollback")
        previous_sha = local_sha if not fresh_init else None
        _update_state["previous_sha"] = previous_sha
        _log_update(f"[{job_id}] Rollback point: {previous_sha or 'none (fresh install)'}")

        # Step 6: VERIFY CUSTOM DIRECTORY (skip for fresh init -- no git index yet)
        _update_step("verify_custom")
        if not fresh_init:
            custom_dir = SCRIPT_DIR / "custom"
            if custom_dir.exists():
                # Check that custom/ files are NOT tracked by git
                for check_file in ["custom/config.json", "custom/routes.py"]:
                    rc_check, _ = await _git_cmd(["ls-files", "--error-unmatch", check_file])
                    if rc_check == 0:
                        raise RuntimeError(
                            f"ABORT: {check_file} is tracked by git. .gitignore may be broken."
                        )

        # Step 7: VERIFY PRESERVED FILES (skip for fresh init)
        _update_step("verify_preserved")
        if not fresh_init:
            preserved_files = [
                ".portal-token", "agents.db", "referrals.db", "clients.db",
                "boop_config.json", "portal-chat.jsonl", "user-settings.json",
                "scheduled_tasks.json",
            ]
            for pf in preserved_files:
                if (SCRIPT_DIR / pf).exists():
                    rc_check, _ = await _git_cmd(["ls-files", "--error-unmatch", pf])
                    if rc_check == 0:
                        raise RuntimeError(
                            f"ABORT: {pf} is tracked by git. This file must be gitignored."
                        )

            # Step 7b: VERIFY IDENTITY DIRS NOT TRACKED
            for identity_dir in ["memories", ".claude"]:
                rc_ls, ls_out = await _git_cmd(["ls-files", identity_dir])
                if rc_ls == 0 and ls_out.strip():
                    raise RuntimeError(
                        f"ABORT: Files inside {identity_dir}/ are tracked by git. "
                        f"Identity/memory files must be gitignored to prevent overwrite. "
                        f"Tracked: {ls_out.strip()[:200]}"
                    )

        # Step 8: PULL or CHECKOUT (depends on fresh_init)
        _update_step("pull")
        if fresh_init:
            # Fresh init: checkout main from origin (creates local main branch)
            _log_update(f"[{job_id}] Step 8: Fresh install -- checking out origin/main...")
            rc_co, co_output = await _git_cmd(
                ["checkout", "-b", "main", "origin/main", "--force"], timeout=60
            )
            if rc_co != 0:
                # Fallback: reset to origin/main
                _log_update(f"[{job_id}] checkout failed, trying reset --hard...")
                rc_reset, reset_out = await _git_cmd(
                    ["reset", "--hard", "origin/main"], timeout=60
                )
                if rc_reset != 0:
                    raise RuntimeError(f"Failed to checkout origin/main: {co_output} / {reset_out}")
        else:
            _log_update(f"[{job_id}] Step 8: Pulling with --ff-only...")
            rc_pull, pull_output = await _git_cmd(
                ["pull", "--ff-only", "origin", "main"], timeout=60
            )
            if rc_pull != 0:
                if "diverged" in pull_output.lower() or "not possible to fast-forward" in pull_output.lower():
                    raise RuntimeError(
                        "Local branch has diverged from origin/main. Manual intervention needed."
                    )
                raise RuntimeError(f"git pull --ff-only failed: {pull_output.split(chr(10))[0]}")

        # Step 8b: VERIFY SHIM SURVIVED PULL
        _update_step("verify_shim")
        _log_update(f"[{job_id}] Step 8b: Verifying customization shim survived pull...")
        server_file = SCRIPT_DIR / "portal_server.py"
        if server_file.exists():
            server_content = server_file.read_text()
            if "CUSTOMIZATION LAYER" not in server_content:
                raise RuntimeError(
                    "ABORT: Customization shim (CUSTOMIZATION LAYER marker) was removed by upstream update. "
                    "Custom routes, panels, and config overrides will not load. "
                    "Rolling back to preserve local customizations."
                )
            _log_update(f"[{job_id}] Customization shim verified present")
        else:
            raise RuntimeError("ABORT: portal_server.py missing after pull")

        # Step 9: RUN TESTS (mandatory — tests must exist and pass)
        _update_step("running_tests")
        _log_update(f"[{job_id}] Step 9: Running tests...")
        tests_dir = SCRIPT_DIR / "tests"
        if not tests_dir.exists() or not any(tests_dir.glob("test_*.py")):
            raise RuntimeError(
                "ABORT: No tests directory or test files found. "
                "Tests are mandatory for safe updates — the update cannot proceed without them."
            )
        test_cmd = [sys.executable, "-m", "pytest", str(tests_dir), "--tb=short", "-q"]
        loop = asyncio.get_event_loop()
        try:
            test_result = await asyncio.wait_for(
                loop.run_in_executor(
                    _PORTAL_EXECUTOR,
                    lambda: subprocess.run(
                        test_cmd, timeout=120, capture_output=True, text=True, cwd=str(SCRIPT_DIR)
                    )
                ),
                timeout=125,
            )
            if test_result.returncode != 0:
                test_output = (test_result.stdout + "\n" + test_result.stderr).strip()
                _log_update(f"[{job_id}] Tests FAILED:\n{test_output}")
                raise RuntimeError(f"Tests failed: {test_output[:200]}")
            _update_state["tests_passed"] = True
        except asyncio.TimeoutError:
            raise RuntimeError("Tests timed out after 120 seconds")

        # Step 10: READ NEW VERSION
        _update_step("read_version")
        new_version = await _get_current_version()
        rc_new, new_sha = await _git_cmd(["rev-parse", "HEAD"])
        _update_state["new_sha"] = new_sha if rc_new == 0 else remote_sha
        _update_state["new_version"] = new_version

        # Step 11: SCHEDULE RESTART
        _update_step("restart")
        _update_state["status"] = "success"
        _update_state["completed_at"] = datetime.now(timezone.utc).isoformat()
        _update_state["message"] = "Update complete. Portal will restart momentarily."
        _update_state["last_update"] = {
            "job_id": job_id,
            "status": "success",
            "completed_at": _update_state["completed_at"],
        }
        prev_label = previous_sha[:7] if previous_sha else "fresh"
        _log_update(f"[{job_id}] SUCCESS: Updated from {prev_label} to {_update_state['new_sha'][:7]}")

        # Delay 2s so the success status can be polled, then restart
        await asyncio.sleep(2)

        # Check for watchdog before self-terminating
        has_watchdog = False
        try:
            # Check if running under a process manager
            ppid = os.getppid()
            ppid_comm = Path(f"/proc/{ppid}/comm")
            ppid_name = ppid_comm.read_text().strip() if ppid_comm.exists() else ""
            if ppid_name in ("systemd", "supervisord", "s6-supervise", "runit", "init"):
                has_watchdog = True
                _log_update(f"[{job_id}] Process manager detected: {ppid_name} (PID {ppid})")
            # Also check for systemd service
            for svc in ["portal", "purebrain-portal", "puresurf-portal"]:
                result = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    has_watchdog = True
                    _log_update(f"[{job_id}] systemd service '{svc}' is active")
                    break
        except Exception as e:
            _log_update(f"[{job_id}] Watchdog detection error (non-fatal): {e}")

        if has_watchdog:
            _log_update(f"[{job_id}] Sending SIGTERM for watchdog restart...")
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            _log_update(f"[{job_id}] WARNING: No process manager detected. Attempting exec restart...")
            try:
                os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception as e:
                _log_update(f"[{job_id}] WARNING: exec restart failed: {e}. Portal needs manual restart.")
                _update_state["message"] = (
                    "Update complete but automatic restart failed. "
                    "No process manager detected. Please restart the portal manually."
                )

    except Exception as e:
        error_msg = str(e)
        _log_update(f"[{job_id}] FAILED at step '{_update_state.get('step')}': {error_msg}")

        # Rollback if we already pulled
        rolled_back_to = None
        if previous_sha and _update_state["step"] in ("verify_shim", "running_tests", "read_version", "restart"):
            _log_update(f"[{job_id}] Rolling back to {previous_sha}...")
            rc_reset, _ = await _git_cmd(["reset", "--hard", previous_sha])
            if rc_reset == 0:
                rolled_back_to = previous_sha
                _log_update(f"[{job_id}] Rollback successful")
            else:
                _log_update(f"[{job_id}] WARNING: Rollback failed!")

        _update_state.update({
            "status": "failed",
            "step_failed": _update_state.get("step"),
            "error": error_msg,
            "rolled_back_to": rolled_back_to,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "message": "Update failed. Rolled back to previous version." if rolled_back_to else f"Update failed: {error_msg}",
            "last_update": {
                "job_id": job_id,
                "status": "failed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        })
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
            "previous_sha": _update_state["previous_sha"],
            "new_sha": _update_state["new_sha"],
            "new_version": _update_state["new_version"],
            "tests_passed": _update_state["tests_passed"],
            "message": _update_state["message"],
            "completed_at": _update_state["completed_at"],
            "backed_up_files": _update_state.get("backed_up_files"),
            "backup_dir": _update_state.get("backup_dir"),
        })

    if status == "failed":
        return JSONResponse({
            "status": "failed",
            "job_id": _update_state["job_id"],
            "step_failed": _update_state["step_failed"],
            "error": _update_state["error"],
            "rolled_back_to": _update_state["rolled_back_to"],
            "message": _update_state["message"],
            "completed_at": _update_state["completed_at"],
        })

    # idle
    return JSONResponse({
        "status": "idle",
        "last_update": _update_state.get("last_update"),
    })


# ---------------------------------------------------------------------------
# Evolution / First-Boot
# ---------------------------------------------------------------------------
EVOLUTION_DONE_FILE = Path.home() / "memories" / "identity" / ".evolution-done"
FIRST_BOOT_FIRED_FILE = Path.home() / ".first-boot-fired"
FIRST_BOOT_PROMPT_FILE = Path.home() / ".claude" / "skills" / "first-visit-evolution" / "prompt.txt"


async def api_evolution_status(request: Request) -> JSONResponse:
    """Check if this AiCIV needs first-boot evolution, is mid-evolution, or is done."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    evolution_done = EVOLUTION_DONE_FILE.exists()
    first_boot_fired = FIRST_BOOT_FIRED_FILE.exists()
    seed_exists = Path(Path.home() / "memories" / "identity" / "seed-conversation.md").exists()
    return JSONResponse({
        "seed_exists": seed_exists,
        "evolution_done": evolution_done,
        "first_boot_fired": first_boot_fired,
        "needs_evolution": seed_exists and not evolution_done and not first_boot_fired,
    })


async def api_first_boot(request: Request) -> JSONResponse:
    """Start Claude with the first-visit evolution prompt as a startup argument."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if EVOLUTION_DONE_FILE.exists():
        return JSONResponse({"status": "skipped", "reason": "evolution already complete"})
    if FIRST_BOOT_FIRED_FILE.exists():
        return JSONResponse({"status": "skipped", "reason": "first boot already fired"})
    seed_file = Path.home() / "memories" / "identity" / "seed-conversation.md"
    if not seed_file.exists():
        return JSONResponse({"status": "skipped", "reason": "no seed conversation found"})

    if not FIRST_BOOT_PROMPT_FILE.exists():
        return JSONResponse({"error": "prompt file not found"}, status_code=500)
    prompt_text = FIRST_BOOT_PROMPT_FILE.read_text().strip()
    if not prompt_text:
        return JSONResponse({"error": "prompt file is empty"}, status_code=500)

    session = get_tmux_session()

    # Kill the auth Claude with double Ctrl-C, then launch evolution in SAME pane.
    # No new window -- portal terminal stays on pane 0 the whole time.
    evo_pane = f"{session}:0"
    try:
        subprocess.run(["tmux", "send-keys", "-t", evo_pane, "C-c", ""],
                       stderr=subprocess.DEVNULL)
        await asyncio.sleep(0.3)
        subprocess.run(["tmux", "send-keys", "-t", evo_pane, "C-c", ""],
                       stderr=subprocess.DEVNULL)
        await asyncio.sleep(2)
        _save_portal_message("Auth Claude ended -- launching evolution...", role="assistant")
    except Exception:
        pass

    try:
        cmd = f"cd $HOME && claude --dangerously-skip-permissions \"$(cat '{FIRST_BOOT_PROMPT_FILE}')\""
        # Use load-buffer + paste-buffer to avoid send-keys truncation on long commands
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tf:
            tf.write(cmd)
            tf_path = tf.name
        try:
            subprocess.run(["tmux", "load-buffer", tf_path],
                           check=True, stderr=subprocess.DEVNULL)
            subprocess.run(["tmux", "paste-buffer", "-t", evo_pane],
                           check=True, stderr=subprocess.DEVNULL)
            subprocess.run(["tmux", "send-keys", "-t", evo_pane, "Enter"],
                           check=True, stderr=subprocess.DEVNULL)
        finally:
            os.unlink(tf_path)

        FIRST_BOOT_FIRED_FILE.write_text(f"fired at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")

        _save_portal_message("\U0001f305 First-visit evolution started \u2014 watch your AI wake up!", role="assistant")
        return JSONResponse({"status": "fired", "prompt_length": len(prompt_text)})

    except subprocess.CalledProcessError as e:
        _save_portal_message(f"\u274c First-boot failed: {e}", role="assistant")
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


# ---------------------------------------------------------------------------
# Agent Control Hub endpoints (additive — does not modify existing endpoints)
# ---------------------------------------------------------------------------

# ── Hub Tasks & Weekly Usage ──────────────────────────────────────────────

async def api_hub_tasks(request: Request) -> JSONResponse:
    """GET /api/hub/tasks — return active project-level tasks for the Agent Hub."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    tasks_file = SCRIPT_DIR / "hub_tasks.json"
    if tasks_file.exists():
        try:
            data = json.loads(tasks_file.read_text())
            return JSONResponse(data)
        except Exception:
            pass
    return JSONResponse({"tasks": []})


async def api_hub_tasks_update(request: Request) -> JSONResponse:
    """POST /api/hub/tasks — update project-level tasks."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        tasks_file = SCRIPT_DIR / "hub_tasks.json"
        tasks_file.write_text(json.dumps(body, indent=2))
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_hub_weekly_usage(request: Request) -> JSONResponse:
    """GET /api/hub/weekly-usage — return weekly API usage percentage."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    usage_file = SCRIPT_DIR / "hub_weekly_usage.json"
    if usage_file.exists():
        try:
            data = json.loads(usage_file.read_text())
            return JSONResponse(data)
        except Exception:
            pass
    return JSONResponse({"percent": 0})


async def api_hub_weekly_usage_update(request: Request) -> JSONResponse:
    """POST /api/hub/weekly-usage — update weekly usage percentage."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        usage_file = SCRIPT_DIR / "hub_weekly_usage.json"
        usage_file.write_text(json.dumps(body, indent=2))
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Live Sub-Agents ───────────────────────────────────────────────────────

# Auto-detect agent tasks dir (works across fleet containers)
_AGENT_TASKS_DIR_CANDIDATES = [
    Path(f"/tmp/claude-{os.getuid()}/-home-aiciv-civ/tasks"),
    Path(f"/tmp/claude-{os.getuid()}/tasks"),
    Path.home() / ".claude" / "tasks",
]
AGENT_TASKS_DIR = next((p for p in _AGENT_TASKS_DIR_CANDIDATES if p.exists()), _AGENT_TASKS_DIR_CANDIDATES[0])


async def api_hub_live_agents(request: Request) -> JSONResponse:
    """GET /api/hub/live-agents — list currently running sub-agents."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    agents = []
    if not AGENT_TASKS_DIR.exists():
        return JSONResponse({"agents": agents})

    now = time.time()
    for entry in AGENT_TASKS_DIR.iterdir():
        if not entry.name.endswith('.output'):
            continue

        try:
            real_path = entry.resolve()
            if not real_path.exists():
                continue

            mtime = real_path.stat().st_mtime
            age_seconds = now - mtime

            # Only include agents active in last 10 minutes
            if age_seconds > 600:
                continue

            status = "running" if age_seconds < 120 else "idle"
            agent_id = entry.name.replace('.output', '')

            description = ""
            agent_type = ""
            started_at = ""
            try:
                with open(real_path, 'r') as f:
                    for i, line in enumerate(f):
                        if i > 20:
                            break
                        try:
                            msg = json.loads(line)
                            if not agent_type and msg.get("slug"):
                                agent_type = msg["slug"]
                            if msg.get("agentId"):
                                agent_id = msg["agentId"]
                            if not started_at and msg.get("timestamp"):
                                started_at = msg["timestamp"]
                            if not description:
                                inner = msg.get("message", {})
                                if isinstance(inner, dict) and inner.get("role") == "user":
                                    content = inner.get("content", "")
                                    if isinstance(content, str) and len(content) > 10:
                                        description = content[:100]
                                    elif isinstance(content, list):
                                        for item in content:
                                            if isinstance(item, dict) and item.get("type") == "text":
                                                txt = item.get("text", "")
                                                if len(txt) > 10 and not txt.startswith("<command"):
                                                    description = txt[:100]
                                                    break
                        except (json.JSONDecodeError, KeyError):
                            pass
            except IOError:
                pass

            agents.append({
                "id": agent_id,
                "type": agent_type,
                "description": description or f"Agent {agent_id[:8]}",
                "status": status,
                "started_at": started_at,
            })
        except (OSError, ValueError):
            continue

    agents.sort(key=lambda a: (0 if a["status"] == "running" else 1, a.get("started_at", "")))
    return JSONResponse({"agents": agents})


# ── Continue & Restart ────────────────────────────────────────────────────

async def api_hub_continue(request: Request) -> JSONResponse:
    """POST /api/continue — continue the last conversation with fresh context."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        # Kill any existing primary sessions first
        try:
            old = await _run_subprocess_output(
                ["tmux", "list-sessions", "-F", "#{session_name}"], timeout=3
            )
            if old:
                for s in old.splitlines():
                    if s.startswith(f"{CIV_NAME}-primary"):
                        await _run_subprocess_async(["tmux", "kill-session", "-t", s])
        except Exception:
            pass

        tmux_session = f"{CIV_NAME}-primary"
        project_dir = str(Path.home())
        marker = Path.home() / ".current_session"
        marker.write_text(tmux_session)
        # Use default model — each CIV may have different config
        model_file = Path.home() / ".claude_session_model"
        model = model_file.read_text().strip() if model_file.exists() else "claude-sonnet-4-6[1m]"
        claude_cmd = (
            f"claude --model {model} --dangerously-skip-permissions "
            f"--continue"
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(_PORTAL_EXECUTOR, lambda: subprocess.Popen(
            ["tmux", "new-session", "-d", "-s", tmux_session, "-c", project_dir, claude_cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ))
        return JSONResponse({
            "status": "continuing",
            "tmux": tmux_session,
            "message": f"Continuing last conversation with fresh context: {tmux_session}"
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_hub_restart(request: Request) -> JSONResponse:
    """POST /api/restart — launch a fresh Claude instance."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        tmux_session = f"{CIV_NAME}-primary-{timestamp}"
        project_dir = str(Path.home())
        # Kill any stale sessions
        try:
            old = await _run_subprocess_output(
                ["tmux", "list-sessions", "-F", "#{session_name}"], timeout=3
            )
            if old:
                for s in old.splitlines():
                    if s.startswith(f"{CIV_NAME}-primary-"):
                        await _run_subprocess_async(["tmux", "kill-session", "-t", s])
        except Exception:
            pass
        marker = Path.home() / ".current_session"
        marker.write_text(tmux_session)
        model_file = Path.home() / ".claude_session_model"
        model = model_file.read_text().strip() if model_file.exists() else "claude-sonnet-4-6[1m]"
        claude_cmd = (
            f"claude --model {model} --dangerously-skip-permissions"
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(_PORTAL_EXECUTOR, lambda: subprocess.Popen(
            ["tmux", "new-session", "-d", "-s", tmux_session, "-c", project_dir, claude_cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ))
        return JSONResponse({"status": "restarting", "tmux": tmux_session})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Debug Report ──────────────────────────────────────────────────────────

async def api_hub_debug_report(request: Request) -> JSONResponse:
    """POST /api/debug/report — collect diagnostics and return as JSON."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        body = {}
    user_note = (body.get("note") or "").strip()

    diag = []

    # 1. Portal info
    import platform
    uptime_sec = time.time() - START_TIME
    uptime_str = f"{int(uptime_sec // 3600)}h {int((uptime_sec % 3600) // 60)}m"
    diag.append("=== PORTAL DIAGNOSTICS ===")
    diag.append(f"CIV: {CIV_NAME}")
    diag.append(f"Version: {PORTAL_VERSION}")
    diag.append(f"Uptime: {uptime_str}")
    diag.append(f"Python: {platform.python_version()}")
    diag.append(f"Platform: {platform.platform()}")
    diag.append(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")

    if user_note:
        diag.append("\n=== USER NOTE ===")
        diag.append(user_note)

    # 2. Memory/disk
    try:
        import shutil
        disk = shutil.disk_usage("/")
        diag.append("\n=== SYSTEM ===")
        diag.append(f"Disk: {disk.used // (1024**3)}GB / {disk.total // (1024**3)}GB ({disk.used * 100 // disk.total}%)")
    except Exception as e:
        diag.append(f"Disk info error: {e}")

    try:
        import resource
        mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        diag.append(f"Portal RSS: {mem_mb:.0f} MB")
    except Exception:
        pass

    # 3. Process count
    try:
        result = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            proc_count = len(result.stdout.strip().splitlines()) - 1
            diag.append(f"Processes: {proc_count}")
    except Exception:
        pass

    # 4. Tmux sessions
    try:
        result = subprocess.run(["tmux", "list-sessions"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            diag.append("\n=== TMUX SESSIONS ===")
            diag.append(result.stdout.strip())
    except Exception as e:
        diag.append(f"Tmux error: {e}")

    # 5. Recent portal log
    log_path = SCRIPT_DIR / "portal.log"
    if not log_path.exists():
        log_path = Path("/tmp/portal.log")
    try:
        if log_path.exists():
            lines = log_path.read_text().splitlines()
            tail = lines[-100:] if len(lines) > 100 else lines
            diag.append(f"\n=== PORTAL LOG (last {len(tail)} lines) ===")
            diag.extend(tail)
    except Exception as e:
        diag.append(f"Log read error: {e}")

    # 6. Recent errors
    try:
        if log_path.exists():
            all_text = log_path.read_text()
            error_lines = [l for l in all_text.splitlines() if any(w in l.lower() for w in ["error", "traceback", "exception"])]
            if error_lines:
                diag.append(f"\n=== ERRORS FOUND ({len(error_lines)} lines) ===")
                diag.extend(error_lines[-20:])
    except Exception:
        pass

    report_text = "\n".join(diag)
    if len(report_text) > 50000:
        report_text = report_text[:50000] + "\n\n[TRUNCATED -- full log exceeds 50KB]"

    # Save report to file for later retrieval
    report_file = SCRIPT_DIR / "debug-report-latest.txt"
    try:
        report_file.write_text(report_text)
    except Exception:
        pass

    return JSONResponse({"ok": True, "report": report_text})


# App
# ---------------------------------------------------------------------------
_react_assets_mount = (
    [Mount("/react/assets", app=StaticFiles(directory=str(REACT_DIST / "assets")))]
    if (REACT_DIST / "assets").exists()
    else []
)

_static_dir = Path(__file__).parent / "static"
_static_mount = (
    [Mount("/static", app=StaticFiles(directory=str(_static_dir)))]
    if _static_dir.exists()
    else []
)

# ─── CUSTOMIZATION LAYER (do not remove on upstream update) ────────────
_CUSTOM_DIR = SCRIPT_DIR / "custom"
_CUSTOM_ROUTES_FILE = _CUSTOM_DIR / "routes.py"
_CUSTOM_CONFIG_FILE = _CUSTOM_DIR / "config.json"

_ALLOWED_CONFIG_OVERRIDES = {"MAX_TOKENS", "PORTAL_VERSION", "PAYOUT_MIN_AMOUNT", "REFERRAL_COMMISSION_RATE"}

# 1. Config overrides
if _CUSTOM_CONFIG_FILE.exists():
    try:
        _custom_cfg = json.loads(_CUSTOM_CONFIG_FILE.read_text())
        for _k, _v in _custom_cfg.items():
            if _k not in _ALLOWED_CONFIG_OVERRIDES:
                print(f"[portal-custom] WARNING: config override blocked for key '{_k}' (not in allowlist)")
                continue
            if _k in globals():
                globals()[_k] = _v
                print(f"[portal-custom] Config override: {_k} = {_v}")
    except Exception as _e:
        print(f"[portal-custom] WARNING: config.json load failed: {_e}")

# 2. Custom routes
_custom_routes: list = []
if _CUSTOM_ROUTES_FILE.exists():
    try:
        import importlib.util as _importlib_util
        _spec = _importlib_util.spec_from_file_location("custom_routes", str(_CUSTOM_ROUTES_FILE))
        _mod = _importlib_util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        if hasattr(_mod, "routes"):
            _custom_routes = _mod.routes
            print(f"[portal-custom] Loaded {len(_custom_routes)} custom route(s)")
    except Exception as _e:
        print(f"[portal-custom] WARNING: routes.py load failed: {_e}")

# 3. Custom startup hooks
_custom_startup_hooks: list = []
_custom_startup_file = _CUSTOM_DIR / "startup.py"
if _custom_startup_file.exists():
    try:
        import importlib.util as _importlib_util
        _spec2 = _importlib_util.spec_from_file_location("custom_startup", str(_custom_startup_file))
        _mod2 = _importlib_util.module_from_spec(_spec2)
        _spec2.loader.exec_module(_mod2)
        if hasattr(_mod2, "on_startup"):
            _custom_startup_hooks.append(_mod2.on_startup)
            print("[portal-custom] Loaded custom startup hook")
    except Exception as _e:
        print(f"[portal-custom] WARNING: startup.py load failed: {_e}")
# ─── END CUSTOMIZATION LAYER ──────────────────────────────────────────

# ---------------------------------------------------------------------------
# TGIM proxy — integration with TGIM v4.x (Russell Korus / Parallax + Keel)
# Auth: Option B — service key + user email passthrough
# ---------------------------------------------------------------------------
def _read_tgim_env(key: str, default: str = "") -> str:
    """Read from os.environ first, then fall back to ~/.env file."""
    val = os.environ.get(key)
    if val:
        return val
    env_path = Path.home() / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip()
    return default


TGIM_BACKEND_URL = _read_tgim_env("TGIM_BACKEND_URL", "http://157.230.191.4:8089")
TGIM_SERVICE_KEY = _read_tgim_env("TGIM_SERVICE_KEY")
TGIM_DEFAULT_USER_EMAIL = _read_tgim_env("TGIM_DEFAULT_USER_EMAIL", "alex@puretechnology.nyc")


def _tgim_headers(user_email: str | None = None) -> dict:
    return {
        "X-TGIM-Service-Key": TGIM_SERVICE_KEY,
        "X-TGIM-User": user_email or TGIM_DEFAULT_USER_EMAIL,
        "Content-Type": "application/json",
    }


def _tgim_upstream_url(path: str) -> str:
    trimmed = path.removeprefix("/api/tgim/").removeprefix("/api/tgim")
    return f"{TGIM_BACKEND_URL}/api/v1/{trimmed}"


async def api_tgim_proxy(request: Request) -> JSONResponse:
    """Generic proxy for /api/tgim/* → TGIM backend /api/v1/*."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not TGIM_SERVICE_KEY:
        return JSONResponse({"error": "TGIM_SERVICE_KEY not configured"}, status_code=503)

    upstream_url = _tgim_upstream_url(request.url.path)
    headers = _tgim_headers()
    params = dict(request.query_params)
    method = request.method.upper()

    body = None
    if method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.json()
        except Exception:
            body = None

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.request(
                method=method, url=upstream_url,
                headers=headers, params=params,
                json=body if body is not None else None,
            )
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text}
            return JSONResponse(data, status_code=resp.status_code)
    except httpx.TimeoutException:
        print(f"[tgim] Timeout proxying {method} {upstream_url}")
        return JSONResponse({"error": "TGIM backend timeout"}, status_code=504)
    except Exception as e:
        print(f"[tgim] Proxy error: {e}")
        return JSONResponse({"error": str(e)}, status_code=502)


routes = [
    Route("/favicon.ico", endpoint=favicon),
    Route("/favicon-32.png", endpoint=favicon_png),
    Route("/apple-touch-icon.png", endpoint=apple_touch_icon),
    Route("/", endpoint=index),
    Route("/pb", endpoint=index_pb),
    Route("/react", endpoint=index_react),
    *_react_assets_mount,
    *_static_mount,
    Route("/health", endpoint=health),
    Route("/api/status", endpoint=api_status),
    Route("/api/release-notes", endpoint=api_release_notes),
    Route("/api/chat/history", endpoint=api_chat_history),
    Route("/api/chat/send", endpoint=api_chat_send, methods=["POST"]),
    Route("/api/notify", endpoint=api_notify, methods=["POST"]),
    Route("/api/chat/upload", endpoint=api_chat_upload, methods=["POST"]),
    Route("/api/chat/uploads/{filename}", endpoint=api_chat_serve_upload),
    Route("/api/auth/status", endpoint=api_claude_auth_status),
    Route("/api/auth/start", endpoint=api_claude_auth_start, methods=["POST"]),
    Route("/api/auth/prewarm", endpoint=api_claude_auth_prewarm, methods=["POST"]),
    Route("/api/auth/code", endpoint=api_claude_auth_code, methods=["POST"]),
    Route("/api/auth/url", endpoint=api_claude_auth_url),
    Route("/api/resume", endpoint=api_resume, methods=["POST"]),
    Route("/api/panes", endpoint=api_panes),
    Route("/api/inject/pane", endpoint=api_inject_pane, methods=["POST"]),
    Route("/api/compact/status", endpoint=api_compact_status),
    Route("/api/context", endpoint=api_context),
    Route("/api/download", endpoint=api_download),
    Route("/api/download/list", endpoint=api_download_list),
    Route("/api/referral/register", endpoint=api_referral_register, methods=["POST"]),
    Route("/api/referral/login", endpoint=api_referral_login, methods=["POST"]),
    Route("/api/referral/session", endpoint=api_referral_session, methods=["POST"]),
    Route("/api/referral/forgot-password", endpoint=api_referral_forgot_password, methods=["POST"]),
    Route("/api/referral/reset-password", endpoint=api_referral_reset_password, methods=["POST"]),
    Route("/api/referral/dashboard", endpoint=api_referral_dashboard),
    Route("/api/referral/track", endpoint=api_referral_track, methods=["POST"]),
    Route("/api/referral/complete", endpoint=api_referral_complete, methods=["POST"]),
    Route("/api/referral/commission", endpoint=api_referral_record_commission, methods=["POST"]),
    Route("/api/referral/code/{email}", endpoint=api_referral_code_lookup),
    Route("/api/referral/paypal-email", endpoint=api_referral_paypal_email, methods=["POST"]),
    Route("/api/referral/leaderboard", endpoint=api_referral_leaderboard),
    Route("/api/portal/owner", endpoint=api_portal_owner),
    Route("/api/referral/payout-request", endpoint=api_referral_payout_request, methods=["POST"]),
    Route("/api/referral/payout-history", endpoint=api_referral_payout_history),
    Route("/api/admin/payout/mark-paid", endpoint=api_admin_payout_mark_paid, methods=["POST"]),
    Route("/api/referral/payout-approve", endpoint=api_referral_payout_approve, methods=["POST"]),
    Route("/api/admin/invite", endpoint=api_admin_invite, methods=["POST"]),
    Route("/api/admin/invites", endpoint=api_admin_invites_list, methods=["GET"]),
    Route("/api/admin/invite/revoke", endpoint=api_admin_invite_revoke, methods=["POST"]),
    Route("/api/admin/affiliates", endpoint=api_admin_affiliates),
    Route("/api/admin/affiliate/update", endpoint=api_admin_affiliate_update, methods=["PUT", "OPTIONS"]),
    Route("/api/admin/affiliate/delete", endpoint=api_admin_affiliate_delete, methods=["DELETE", "OPTIONS"]),
    Route("/api/admin/referral/update", endpoint=api_admin_referral_update, methods=["PUT", "OPTIONS"]),
    Route("/api/admin/referral/assign", endpoint=api_admin_referral_assign, methods=["POST", "OPTIONS"]),
    Route("/api/admin/payouts", endpoint=api_admin_payouts),
    Route("/admin/referrals", endpoint=serve_admin_referrals),
    Route("/admin/clients", endpoint=serve_admin_clients),
    Route("/api/admin/clients", endpoint=api_admin_clients),
    Route("/api/public/client-stats", endpoint=api_public_client_stats, methods=["GET", "OPTIONS"]),
    Route("/api/admin/clients/update", endpoint=api_admin_clients_update, methods=["POST"]),
    Route("/api/admin/clients/hide", endpoint=api_admin_clients_hide, methods=["POST"]),
    Route("/api/admin/clients/restore", endpoint=api_admin_clients_restore, methods=["POST"]),
    Route("/api/admin/clients/import", endpoint=api_admin_clients_import, methods=["POST"]),
    # ── User tracking & PayPal webhook routes ──
    Route("/api/webhooks/paypal", endpoint=api_webhooks_paypal, methods=["POST"]),
    Route("/api/tracking/status", endpoint=api_tracking_status),
    Route("/affiliate", endpoint=serve_affiliate_portal),
    Route("/api/boop/config", endpoint=api_boop_config, methods=["GET", "POST"]),
    Route("/api/boop/status", endpoint=api_boop_status),
    Route("/api/boop/toggle", endpoint=api_boop_toggle, methods=["POST"]),
    Route("/api/boops", endpoint=api_boops_list),
    Route("/api/boops/{boop_id}", endpoint=api_boop_update, methods=["PATCH"]),
    Route("/api/agents/status", endpoint=api_agents_update_status, methods=["POST"]),
    Route("/api/agents", endpoint=api_agents_list),
    Route("/api/agents/stats", endpoint=api_agents_stats),
    Route("/api/agents/orgchart", endpoint=api_agents_orgchart),
    Route("/api/agents/{id}", endpoint=api_agents_get_one),
    Route("/api/commands", endpoint=api_commands),
    Route("/api/shortcuts", endpoint=api_shortcuts),
    Route("/api/deliverable", endpoint=api_deliverable, methods=["POST"]),
    Route("/api/reaction", endpoint=api_reaction, methods=["POST"]),
    Route("/api/reaction/summary", endpoint=api_reaction_summary),
    Route("/api/schedule-task", endpoint=api_schedule_task, methods=["POST"]),
    Route("/api/scheduled-tasks", endpoint=api_scheduled_tasks_list),
    Route("/api/scheduled-tasks/{task_id}", endpoint=api_delete_scheduled_task, methods=["DELETE"]),
    Route("/api/scheduled-tasks/{task_id}", endpoint=api_update_scheduled_task, methods=["PUT"]),
    Route("/api/scheduled-tasks/{task_id}", endpoint=api_patch_scheduled_task, methods=["PATCH"]),
    Route("/api/investor/question", endpoint=api_investor_question, methods=["POST", "OPTIONS"]),
    Route("/api/investor-chat", endpoint=api_investor_chat, methods=["POST", "OPTIONS"]),
    Route("/api/investor-tts", endpoint=api_investor_tts, methods=["POST", "OPTIONS"]),
    Route("/api/777/chat", endpoint=api_777_chat, methods=["POST", "OPTIONS"]),
    Route("/api/whatsapp/qr", endpoint=api_whatsapp_qr),
    Route("/api/whatsapp/status", endpoint=api_whatsapp_status),
    Route("/api/settings", endpoint=api_user_settings, methods=["GET", "POST", "PUT"]),
    Route("/api/bookmarks", endpoint=api_bookmarks, methods=["GET", "POST"]),
    Route("/api/health/mods", endpoint=api_health_mods),
    Route("/api/update/check", endpoint=api_update_check),
    Route("/api/update/apply", endpoint=api_update_apply, methods=["POST"]),
    Route("/api/update/apply-force", endpoint=api_update_apply_force, methods=["POST"]),
    Route("/api/update/status", endpoint=api_update_status),
    Route("/api/evolution/status", endpoint=api_evolution_status),
    Route("/api/evolution/first-boot", endpoint=api_first_boot, methods=["POST"]),
    # ── Agent Control Hub routes (additive) ──
    Route("/api/hub/tasks", endpoint=api_hub_tasks),
    Route("/api/hub/tasks", endpoint=api_hub_tasks_update, methods=["POST"]),
    Route("/api/hub/weekly-usage", endpoint=api_hub_weekly_usage),
    Route("/api/hub/weekly-usage", endpoint=api_hub_weekly_usage_update, methods=["POST"]),
    Route("/api/hub/live-agents", endpoint=api_hub_live_agents),
    Route("/api/debug/report", endpoint=api_hub_debug_report, methods=["POST"]),
    Route("/api/continue", endpoint=api_hub_continue, methods=["POST"]),
    Route("/api/restart", endpoint=api_hub_restart, methods=["POST"]),
    Route("/api/tgim/{path:path}", endpoint=api_tgim_proxy, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
    WebSocketRoute("/ws/chat", endpoint=ws_chat),
    WebSocketRoute("/ws/terminal", endpoint=ws_terminal),
    *_custom_routes,   # Flux overlay: custom routes from custom/routes.py
]

app = Starlette(
    routes=routes,
    on_startup=[_startup],
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=["https://purebrain.ai", "https://www.purebrain.ai", "https://app.purebrain.ai", "https://777-command-center.vercel.app"],
            allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization", "X-Affiliate-Session"],
        ),
    ],
)

if __name__ == "__main__":
    import uvicorn

    def _handle_sigterm(signum, frame):
        """Clean shutdown on SIGTERM — prevents 30s timeout + SIGKILL."""
        print("[portal] SIGTERM received, shutting down gracefully...")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    port = int(os.environ.get("PORT", 8097))
    print(f"[portal] Starting PureBrain Portal on port {port}")
    print(f"[portal] Bearer token: {BEARER_TOKEN[:8]}...{BEARER_TOKEN[-4:]}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
