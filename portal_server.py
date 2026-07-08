#!/usr/bin/env python3
"""PureBrain Portal Server — per-CIV mini server for purebrain.ai
Auth via Bearer token. JSONL-based chat history (same as TG bot).
"""
# Pre-flight dependency check — fail fast with clear instructions
import importlib.util
_REQUIRED = ["httpx", "aiosqlite", "starlette", "uvicorn"]
_missing = [p for p in _REQUIRED if not importlib.util.find_spec(p)]
if _missing:
    print(f"[portal] FATAL: Missing packages: {', '.join(_missing)}")
    print(f"[portal] Fix: pip install {' '.join(_missing)}")
    import sys; sys.exit(1)

import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import os
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass
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
# Shared config & helpers — imported from portal_config.py
# All constants, auth helpers, subprocess wrappers, and tmux session cache
# are defined there as the single source of truth.
# ---------------------------------------------------------------------------
from portal_config import (
    # Thread pool + background task tracking
    PORTAL_EXECUTOR as _PORTAL_EXECUTOR,
    background_tasks as _background_tasks,
    fire_and_forget as _fire_and_forget,
    # Config constants
    SCRIPT_DIR, TOKEN_FILE, PORTAL_HTML, PORTAL_PB_HTML, REACT_DIST,
    START_TIME, PORTAL_VERSION, RELEASE_NOTES_FILE,
    CIV_NAME, HUMAN_NAME,
    _PROJECTS_DIR, LOG_ROOT, HISTORY_FILE, PORTAL_CHAT_LOG,
    UPLOADS_DIR, UPLOAD_MAX_BYTES,
    PAYOUT_REQUESTS_FILE, PAYOUT_MIN_AMOUNT, PAYOUT_AUTO_APPROVE_LIMIT,
    PAYOUT_COOLDOWN_DAYS,
    REFERRALS_DB, CLIENTS_DB, AGENTS_DB,
    REFERRAL_CODE_PREFIX, REFERRAL_CODE_CHARS, REFERRAL_CODE_LENGTH,
    REFERRAL_COMMISSION_RATE,
    WEB_CONVERSATIONS_LOG, PAYMENTS_LOG, PAY_TEST_LOG,
    DOWNLOAD_ALLOWED_DIRS,
    CREDENTIALS_FILE, OAUTH_URL_PATTERN,
    AUTH_SCREEN_PATTERNS, AUTH_SCREEN_PRIORITY,
    BEARER_TOKEN,
    PAYPAL_SANDBOX, PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET,
    # Helpers
    sanitize_error as _sanitize_error,
    run_subprocess_sync as _run_subprocess_sync,
    run_subprocess_async as _run_subprocess_async,
    run_subprocess_output as _run_subprocess_output,
    get_tmux_session,
    check_auth, check_auth_and_track,
)

# ─── Mutable state that stays in portal_server.py (not shared yet) ─────────
_captured_oauth_url = None
_auth_prewarm_task = None  # background prewarm task handle
_auth_flow_running = False  # lock to prevent concurrent auth flows


def _detect_session_model() -> str:
    """Detect the live model from ~/.claude_session_model or session transcript."""
    model_file = Path.home() / ".claude_session_model"

    # 1. Try the model file first (if fresh — less than 7 days old)
    if model_file.exists():
        try:
            age_days = (time.time() - os.path.getmtime(str(model_file))) / 86400
            if age_days < 7:
                val = model_file.read_text().strip()
                if val:
                    return val
        except Exception:
            pass

    # 2. Try to detect from the current session JSONL transcript
    session_ledger = Path.home() / "memories" / "sessions" / "current-session.jsonl"
    if session_ledger.exists():
        try:
            with open(session_ledger, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 10240))
                tail = f.read().decode("utf-8", errors="replace")
            models = re.findall(r'"model"\s*:\s*"(claude-[^"]+)"', tail)
            if models:
                detected = models[-1]
                try:
                    model_file.write_text(detected)
                except Exception:
                    pass
                return detected
        except Exception:
            pass

    # 3. Try reading the model file even if stale (better than nothing)
    if model_file.exists():
        try:
            val = model_file.read_text().strip()
            if val:
                return val
        except Exception:
            pass

    # 4. Final fallback
    return "claude-opus-4-6[1m]"


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
        parts = []
        if item.get("upload_id"):
            parts.append(f"[upload_id:{item['upload_id']}]")
        parts.append(f"[Portal Upload from {HUMAN_NAME}] File saved to: {item['portal_copy_path']}")
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

        # Embed all upload_ids so each can be deduped against its portal log entry
        upload_ids = [f["upload_id"] for f in batch if f.get("upload_id")]
        parts = []
        for uid in upload_ids:
            parts.append(f"[upload_id:{uid}]")
        parts.append(f"[Portal Upload from {HUMAN_NAME}] {file_count} files saved: {file_names}")

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

    print(f"[upload-inject] Injecting upload notification ({len(notification)} chars)")
    result = await _inject_into_tmux_serialized(notification)
    if not result:
        print(f"[upload-inject] WARNING: tmux injection returned False — notification may not have been delivered")
        print(f"[upload-inject] Notification: {notification[:300]}")
        # Fallback: save to portal log so AI sees it on next history read
        _save_portal_message(notification, role="user")
        print(f"[upload-inject] Fallback: saved notification to portal-chat.jsonl")


def _schedule_upload_batch_item(original_name, portal_copy_path, is_image, caption, upload_id=None):
    """Add one upload to the debounce batch and (re)start the flush timer."""
    global _upload_batch, _upload_batch_task

    _upload_batch.append({
        "original_name": original_name,
        "portal_copy_path": portal_copy_path,
        "is_image": is_image,
        "caption": caption,
        "upload_id": upload_id,
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
        session = await _find_primary_pane_async()
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
        except Exception as e:
            print(f"[upload-inject] tmux injection FAILED: {e}")
            print(f"[upload-inject] Session target was: {session}")
            print(f"[upload-inject] Notification (first 200 chars): {notification[:200]}")
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
        "[Portal Upload",              # Portal upload notifications (case-sensitive variant, already in portal-chat.jsonl)
        "[upload_id:",                 # Upload dedup tags from tmux injection (internal, not user-facing)
        "[CC #", "[CC-DM",            # CC bridge messages — belong in CC tab, not main chat
        "[WAR-ROOM]", "[WAR-ROOM-RESPONSE]", "[WAR-ROOM-PREFLIGHT",
        "[WAR-ROOM-STAND-DOWN]", "[WAR-ROOM-UPDATE]",
        "[GENERATE-TASKS]",            # CC forge messages
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
    # Filter assistant messages that are just echoing/summarizing CC messages.
    # These belong in the CC tab, not the main chat.
    _cc_assistant_markers = [
        "[CC #", "[CC-DM",                     # Raw CC messages echoed
        "CC chatter",                           # Assistant CC summaries
        "[WAR-ROOM]", "[WAR-ROOM-RESPONSE]",
        "[GENERATE-TASKS]",
    ]
    for marker in _cc_assistant_markers:
        if marker in text[:300]:
            return False
    return True


_jsonl_cache: dict = {}  # path -> (mtime, messages, fsize, last_parse_time)
_TAIL_BYTES = 500_000   # read last 500KB of large files (reduced from 2MB — stability fix 2026-03-14)
_CACHE_MIN_INTERVAL = 3.0  # Don't re-parse any file more than once per 3 seconds (reduced from 10s — 10s blind spot caused message delivery failures; 3s is safe with max_files=3 + 500KB tail-read)

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
        _invalidate_msg_cache()
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
                agent_context = None  # Track agent identity from tool_use blocks
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
                    elif isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_name = block.get("name", "")
                        tool_input = block.get("input", {})
                        if tool_name == "Agent" and tool_input.get("name"):
                            agent_context = {"agent": tool_input["name"], "tool": "Agent"}
                        elif tool_name == "SendMessage" and tool_input.get("to"):
                            if not agent_context:
                                agent_context = {"agent": tool_input["to"], "tool": "SendMessage"}
                    elif isinstance(block, dict) and block.get("type") == "thinking":
                        thinking_text = block.get("thinking", "")
                        if thinking_text and len(thinking_text.strip()) > 2:
                            # Only include recent thinking blocks in history (last 5 min)
                            # Older thinking is ephemeral - shown in real-time but not on refresh
                            _think_ts = time.time()
                            try:
                                _raw_ts = entry.get("timestamp")
                                if isinstance(_raw_ts, (int, float)):
                                    _think_ts = _raw_ts / 1000 if _raw_ts > 1e12 else _raw_ts
                                elif isinstance(_raw_ts, str):
                                    _think_ts = datetime.fromisoformat(_raw_ts.replace("Z", "+00:00")).timestamp()
                                if time.time() - _think_ts > 300:
                                    continue  # Skip thinking blocks older than 5 minutes
                            except Exception:
                                pass  # If we can't parse time, include it
                            _entry_id = entry.get("uuid", f"think-{len(messages)}")
                            messages.append({
                                "role": "thinking",
                                "text": thinking_text,
                                "timestamp": int(_think_ts),
                                "id": f"{_entry_id}-thinking-{len(messages)}",
                            })

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

                # Detect and strip [topic:xxx] prefix from message text
                _topic = None
                _topic_match = re.match(r'^\[topic:([^\]]+)\]\s*', combined)
                if _topic_match:
                    _raw_topic = _topic_match.group(1)
                    # Normalize to key format (lowercase, hyphens)
                    _topic = re.sub(r'[^a-z0-9]+', '-', _raw_topic.lower()).strip('-') or None
                    combined = combined[_topic_match.end():]

                msg_dict = {
                    "role": role,
                    "text": combined,
                    "timestamp": int(ts),
                    "id": entry.get("uuid", f"msg-{log_path.stem[:8]}-{len(messages)}")
                }
                if _topic:
                    msg_dict["topic"] = _topic
                # Agent identity: prefer agentName from JSONL entry, fall back to tool_use detection
                _agent_name = entry.get("agentName", "")
                _team_name = entry.get("teamName", "")
                if _agent_name and _agent_name != "main":
                    msg_dict["agent_context"] = {"agent": _agent_name, "tool": "team-member"}
                elif agent_context:
                    msg_dict["agent_context"] = agent_context
                messages.append(msg_dict)
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
                    # Thinking is ephemeral -- never include in history
                    if entry.get("role") == "thinking":
                        continue
                    # Skip mirrored session upload notifications — these are internal tmux
                    # injection artifacts that duplicate the portal-saved [Image: ...] entry.
                    # They have session UUIDs instead of portal IDs, causing duplicate rendering.
                    if msg_text.startswith("[upload_id:") or "[Portal Upload" in msg_text[:200]:
                        continue
                    # Normalize topic to key format (handles legacy display-name topics)
                    if entry.get("topic"):
                        entry["topic"] = re.sub(r'[^a-z0-9]+', '-', entry["topic"].lower()).strip('-') or None
                    messages.append(entry)
                except json.JSONDecodeError:
                    continue
        # Update cache
        _portal_chat_cache = (mtime, fsize, messages)
    except Exception:
        pass
    return messages


def _save_portal_message(text, role="user", topic=None, upload_id=None,
                         reply_to_id=None, reply_to_text=None, reply_to_author=None):
    """Save a message sent via the portal."""
    # Thinking blocks are ephemeral -- push via WS only, never persist
    if role == "thinking":
        return None
    entry = {
        "role": role,
        "text": text,
        "timestamp": int(time.time()),
        "id": f"portal-{int(time.time() * 1000)}-{secrets.token_hex(4)}",
    }
    if topic:
        entry["topic"] = topic
    if upload_id:
        entry["upload_id"] = upload_id
    if reply_to_id:
        entry["reply_to_id"] = reply_to_id
        if reply_to_text:
            entry["reply_to_text"] = reply_to_text
        if reply_to_author:
            entry["reply_to_author"] = reply_to_author
    try:
        with PORTAL_CHAT_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        _portal_log_ids.add(entry["id"])  # Prevent _mirror_to_portal_log from double-writing
    except Exception:
        pass
    return entry


# Simple in-memory cache for _parse_all_messages to avoid re-parsing JSONL within same poll cycle.
# Uses a short TTL (2s) so consecutive WS polls within the same cycle reuse the result.
# The WS poll interval is 1.5s, so a 2s TTL means at most 1 redundant parse per cycle.
_msg_cache: dict = {"result": None, "ts": 0.0, "last_n": 0}
_MSG_CACHE_TTL = 2.0  # seconds — short enough to always pick up new data promptly


def _invalidate_msg_cache():
    """Invalidate the message parse cache (call after writes to chat logs)."""
    _msg_cache["result"] = None
    _msg_cache["ts"] = 0.0


def _parse_all_messages(last_n=100):
    """Parse messages across all recent session logs + portal log."""
    now = time.time()
    if (_msg_cache["result"] is not None
            and _msg_cache["last_n"] == last_n
            and now - _msg_cache["ts"] < _MSG_CACHE_TTL):
        return _msg_cache["result"]
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

    # Secondary dedup: remove portal-log entries that duplicate a session-JSONL entry.
    # Portal log is always subordinate — prefer session JSONL text.
    #
    # Strategy:
    # 1. upload_id match (reliable, no time window needed) — extract from portal JSON
    #    field and session text [upload_id:xxx] tag
    # 2. Fallback: text/pattern match within 30s window (legacy, for old messages)

    # Pass 1: pre-collect all session upload_ids and texts (needed before portal dedup)
    _upload_id_pattern = re.compile(r'\[upload_id:(upload-[^\]]+)\]')
    session_upload_ids: set = set()
    session_texts_by_ts: list = []
    for m in deduped:
        if m['_src'] == 'session':
            session_texts_by_ts.append((m['timestamp'], (m.get('text') or '').strip().lower()))
            for match in _upload_id_pattern.finditer(m.get('text') or ''):
                session_upload_ids.add(match.group(1))

    # Pass 2: filter portal entries that match a session entry
    final: list = []
    for m in deduped:
        if m['_src'] == 'session':
            final.append(m)
        else:
            is_dup = False

            # Primary dedup: match by upload_id (no time window needed)
            portal_upload_id = m.get('upload_id')
            if portal_upload_id and portal_upload_id in session_upload_ids:
                is_dup = True
            else:
                # Fallback: text/pattern match within 30s window (legacy)
                m_ts = m['timestamp']
                m_text = (m.get('text') or '').strip().lower()
                is_upload = '[image:' in m_text or '[file:' in m_text
                for s_ts, s_text in session_texts_by_ts:
                    if abs(m_ts - s_ts) <= 30:
                        if s_text == m_text:
                            is_dup = True
                            break
                        if is_upload and ('[image:' in s_text or 'portal upload' in s_text or 'file saved to' in s_text):
                            is_dup = True
                            break
            if not is_dup:
                final.append(m)

    # Re-sort after secondary dedup (insertion order is already correct but be safe)
    final.sort(key=lambda m: m['timestamp'])

    # Assign topics to ALL messages based on content analysis
    # Keywords are weighted: longer/more-specific phrases score higher
    _TOPIC_KEYWORDS = {
        "channel-partners": [
            "channel partner", "prm", "partner management", "commission engine",
            "deal registration", "partner portal", "partner tier", "mdf",
            "channel-partner-prm", "admin-portal", "pure ledger",
        ],
        "portal": [
            # UI elements
            "portal", "chat tab", "cc tab", "hard refresh", "topic tag", "topic pill",
            "chat box", "chat window", "inbox", "brain stream", "prompt section",
            "leven labs", "chat-messages", "chat-loading",
            # Portal code/files
            "portal-server", "portal_server", "portal-pb-styled", "panels.css",
            "chat.js", "agents.js", "organogram.js", "panel-manager",
            # Portal features
            "topic filter", "topic-dimmed", "topicfilter", "applytopicfilter",
            "addmessage", "msg-bubble", "msg-meta", "data-topic",
            "subtab", "sidebar", "deployment tab", "my deployments",
            # Portal tech
            ".html", ".css", ".js", "frontend", "stylesheet", "css var",
            "dom ", "innerhtml", "queryselector", "getelementby", "onclick",
            "addeventlistener", "classlist",
        ],
        "war-room": [
            "war room", "warroom", "mission control", "d3 graph", "force graph",
            "fleet endpoint", "stats bar", "agent drawer", "peer review",
            "war-room-response", "acceptance criteria", "ac met",
            "task_id", "project_id", "executor", "morphe", "chy ",
        ],
        "infrastructure": [
            "vercel", "deploy", "deployment", "git push", "git pull", "git commit",
            "git add", "git status", "git diff", "git stash",
            "server", "restart", "tmux", "docker", "cloudflare", "netlify",
            "google drive", "npm", "pip install", "requirements.txt",
            "pushed to main", "origin main", "rebase",
        ],
        "product": [
            "prd", "requirement", "prototype", "roadmap", "backlog", "user story",
            "vp product", "pm lead", "po lead", "sprint", "competitive analysis",
            "market fit", "market research", "functional requirement",
            "non-functional", "rice framework", "prioriti",
        ],
        "agents": [
            "organogram", "hierarchy", "reports_to", "conductor", "agent roster",
            "active agents", "my ai fleet", "agent hub", "graphico", "volt", "mailo",
            "agent manifest", "agent creation", ".claude/agents",
        ],
        "cc-operations": [
            "cc message", "cc chat", "civ key", "heartbeat", "presence",
            "cc bridge", "cc proxy", "command center", "cc_bridge",
            "cc dispatcher", "cc-dispatch", "war room channel",
        ],
    }

    def _infer_topic(text):
        """Infer topic from message content using keyword matching with specificity bonus."""
        lower = text.lower()
        scores = {}
        for topic, keywords in _TOPIC_KEYWORDS.items():
            score = 0
            for kw in keywords:
                if kw in lower:
                    # Longer keywords are more specific → higher weight
                    score += 1 + len(kw) // 8
            if score > 0:
                scores[topic] = score
        if scores:
            return max(scores, key=scores.get)
        return "general"

    # Topic assignment rules:
    # 1. When a topic is explicitly set via [topic:xxx] → ALL subsequent messages
    #    get that topic until a NEW explicit [topic:yyy] changes it
    # 2. Keyword inference ONLY runs when active topic is "general"
    # 3. Assistant messages always inherit the current active topic
    _active_topic = "general"
    for m in final:
        if m.get("topic"):
            # Explicit [topic:xxx] from user — set as active, keep it
            _active_topic = m["topic"]
        elif _active_topic != "general":
            # A non-general topic is active — ALL messages stay in it
            m["topic"] = _active_topic
        elif m.get("role") == "user":
            # General is active + user message — infer from content
            inferred = _infer_topic(m.get("text", ""))
            _active_topic = inferred  # could be "general" or a detected topic
            m["topic"] = _active_topic
        else:
            # General is active + assistant message — inherit
            m["topic"] = _active_topic

    result = final[-last_n:] if len(final) > last_n else final
    _msg_cache["result"] = result
    _msg_cache["ts"] = time.time()
    _msg_cache["last_n"] = last_n
    return result


# check_auth, check_auth_and_track, _maybe_track_activity → portal_config.py

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

    Supports two modes per panel file:
      1. **Add** (default): Creates a new sidebar nav item + panel div + mobile menu item.
      2. **Replace** (panel-replace mode): If the first 10 lines contain a comment like
         ``<!-- panel-replace: status -->``, the custom panel *replaces* an existing built-in
         panel instead of adding a new one.  Specifically:
           - The original nav item (``data-panel="{replace_id}"``) is hidden via display:none.
           - A new nav item is injected with the custom label/icon/tooltip but still targeting
             the original panel div ID so existing CSS/JS selectors keep working.
           - The built-in panel div's innerHTML is swapped for the custom panel content.
           - The original mobile menu item is similarly hidden and a replacement injected.
    """
    custom_panels_dir = SCRIPT_DIR / "custom" / "panels"
    if not custom_panels_dir.exists():
        return html

    nav_items = []          # new nav items to inject (additive panels)
    panel_html_parts = []   # new panel divs to inject (additive panels)
    mobile_items = []       # new mobile menu items to inject (additive panels)
    replacements = []       # list of dicts for panel-replace panels

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
        panel_icon = meta.get("icon", "&#x2726;")
        if '<' in panel_icon or '>' in panel_icon:
            print(f"[portal-custom] WARNING: panel icon contains HTML tags, using default: {panel_file.name}")
            panel_icon = "&#x2726;"
        panel_tooltip = escape(meta.get("tooltip", ""), quote=True)

        # --- Panel-replace mode ---
        # If panel-replace metadata is present, this panel replaces an existing built-in panel
        # rather than being added as a new one.
        replace_target = meta.get("replace")
        if replace_target:
            replace_id = escape(replace_target.strip(), quote=True)
            replacements.append({
                "replace_id": replace_id,
                "panel_id": panel_id,
                "label": panel_label,
                "icon": panel_icon,
                "tooltip": panel_tooltip,
                "content": panel_content,
            })
            print(f"[portal-custom] Replacing panel: {replace_id} with {panel_id} ({panel_label})")
            continue

        # --- Normal additive mode ---
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

    # --- Apply panel replacements ---
    for repl in replacements:
        rid = repl["replace_id"]

        # 1. Hide the original nav item and inject a replacement nav item pointing to the
        #    same panel div ID (so the portal's panel switching JS still works).
        old_nav = re.search(
            r'(<div\s+class="nav-item"\s+data-panel="' + re.escape(rid) + r'"[^>]*>)',
            html
        )
        if old_nav:
            original_tag = old_nav.group(1)
            # Insert display:none style into the original tag to hide it
            hidden_tag = original_tag.replace('class="nav-item"', 'class="nav-item" style="display:none"', 1)
            html = html.replace(original_tag, hidden_tag, 1)

            # Inject a replacement nav item (uses the original panel's ID so clicking it
            # activates the same panel div, but with custom label/icon/tooltip)
            replacement_nav = (
                f'    <div class="nav-item" data-panel="{rid}" '
                f'data-tooltip="{repl["tooltip"]}">'
                f'<span class="nav-icon">{repl["icon"]}</span>'
                f'{repl["label"]}</div>'
            )
            if '<!-- /nav-panels -->' in html:
                html = html.replace(
                    '    <!-- /nav-panels -->',
                    f'{replacement_nav}\n    <!-- /nav-panels -->',
                    1
                )
        else:
            print(f"[portal-custom] WARNING: could not find nav item for panel '{rid}' to replace")

        # 2. Replace the built-in panel div's innerHTML with custom content.
        #    The div keeps its original id="panel-{rid}" so all existing CSS/JS targeting works.
        panel_div_pattern = re.compile(
            r'(<div\s+class="panel"\s+id="panel-' + re.escape(rid) + r'"[^>]*>)'
            r'(.*?)'
            r'(</div>\s*(?=\n\s*(?:<div\s+class="panel"|<!--\s*/panels|$)))',
            re.DOTALL
        )
        panel_match = panel_div_pattern.search(html)
        if panel_match:
            # Keep the opening tag, swap the inner content, keep the closing tag
            new_panel = f'{panel_match.group(1)}{repl["content"]}</div>'
            html = html[:panel_match.start()] + new_panel + html[panel_match.end():]
        else:
            print(f"[portal-custom] WARNING: could not find panel div 'panel-{rid}' to replace content")

        # 3. Hide original mobile menu item and inject replacement (if mobile menu exists)
        old_mobile = re.search(
            r'(<div\s+class="tab-menu-item"\s+data-panel="' + re.escape(rid) + r'"[^>]*>)',
            html
        )
        if old_mobile:
            original_mobile_tag = old_mobile.group(1)
            hidden_mobile_tag = original_mobile_tag.replace(
                'class="tab-menu-item"', 'class="tab-menu-item" style="display:none"', 1
            )
            html = html.replace(original_mobile_tag, hidden_mobile_tag, 1)

        if '<!-- /mobile-menu-items -->' in html:
            replacement_mobile = (
                f'    <div class="tab-menu-item" data-panel="{rid}" '
                f'onclick="selectMobileMenuItem(\'{rid}\')">'
                f'<span style="margin-right:10px;">{repl["icon"]}</span>'
                f'{repl["label"]}</div>'
            )
            html = html.replace(
                '    <!-- /mobile-menu-items -->',
                f'{replacement_mobile}\n    <!-- /mobile-menu-items -->',
                1
            )

    # --- Inject additive panels (unchanged behavior) ---
    if not nav_items and not replacements:
        return html

    markers_found = 0
    markers_expected = 3 if nav_items else 0  # only expect markers if there are additive panels

    # Inject nav items among other panel nav items (before <!-- /nav-panels --> marker)
    if nav_items:
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
    if panel_html_parts:
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
    if mobile_items:
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

    if markers_expected > 0 and markers_found < markers_expected:
        print(f"[portal-custom] WARNING: Only {markers_found}/{markers_expected} injection markers found — some custom panels may not display")

    return html

async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "civ": CIV_NAME, "version": PORTAL_VERSION, "uptime": int(time.time() - START_TIME)})


def _react_index_response() -> Response:
    """Serve the React portal's index.html — the ONLY deployed portal frontend.

    The legacy HTML frontends (portal.html / portal-pb-styled.html) were retired
    from the deploy path 2026-07-08 (Corey directive: "the react version and ONLY
    the react version deployed"). Their source is preserved under _retired-html-portal/
    and the full pre-React deploy is recoverable on branch main-html-archive-20260708.
    """
    react_index = REACT_DIST / "index.html"
    if react_index.exists():
        resp = FileResponse(str(react_index), media_type="text/html")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp
    return Response(
        "<h1>React Portal not found — react-portal/dist/index.html is missing</h1>",
        media_type="text/html", status_code=503)


async def index(request: Request) -> Response:
    """Root portal — serves the React portal (the only deployed frontend)."""
    return _react_index_response()


async def index_pb(request: Request) -> Response:
    """Legacy /pb path — retired. Redirect to the React portal at /."""
    from starlette.responses import RedirectResponse
    return RedirectResponse(url="/", status_code=301)


async def index_react(request: Request) -> Response:
    """/react path — kept as an explicit alias for the React portal (same as /)."""
    return _react_index_response()


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


async def api_gateway_status(request: Request) -> JSONResponse:
    """Stub: gateway module not installed in this portal bundle.

    Returns 503 with explicit payload so callers can distinguish
    "endpoint not exposed" (404) from "module not installed" (503).
    When the gateway module ships, this stub will be replaced
    with a real implementation. Approved by Aether on 2026-06-10
    (Morphe audit finding).
    """
    return JSONResponse(
        {"status": "not_installed", "module": "gateway", "version": PORTAL_VERSION},
        status_code=503,
    )


async def api_release_notes(request: Request) -> JSONResponse:
    """Return release notes and current version."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        data = json.loads(RELEASE_NOTES_FILE.read_text())
        data["current_version"] = PORTAL_VERSION
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"current_version": PORTAL_VERSION, "releases": [], "error": _sanitize_error(e, "release notes")})


async def api_chat_topics(request: Request) -> JSONResponse:
    """Return unique topic tags found in recent messages."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    messages = _parse_all_messages(last_n=200)
    topics = sorted({m["topic"] for m in messages if m.get("topic")})
    return JSONResponse({"topics": topics})


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
        topic = str(body.get("topic", "")).strip() or None
        # Normalize to key format (lowercase, hyphens) for consistent filtering
        if topic:
            topic = re.sub(r'[^a-z0-9]+', '-', topic.lower()).strip('-') or None
        # "general" is a valid explicit topic — signals user wants to reset to general
        # Don't convert to None
        reply_to_id = str(body.get("reply_to_id", "")).strip() or None
        reply_to_text = str(body.get("reply_to_text", "")).strip()[:100] or None
        reply_to_author = str(body.get("reply_to_author", "")).strip()[:50] or None
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    # Save to portal chat log for history
    # Return the saved entry's ID so the client can pre-register it in knownMsgIds,
    # preventing the WS poll-loop echo from rendering the message a second time.
    saved_entry = _save_portal_message(message, role="user", topic=topic,
                                       reply_to_id=reply_to_id,
                                       reply_to_text=reply_to_text,
                                       reply_to_author=reply_to_author)
    msg_id = saved_entry["id"]

    # Tag injection source so tmux pane shows where input came from
    host = request.headers.get("referer", "")
    # Prepend topic tag if present (embedded in tmux text for JSONL capture)
    topic_prefix = f"[topic:{topic}] " if topic else ""
    if "react" in host:
        tagged = f"[portal-react] {topic_prefix}{message}"
    else:
        tagged = f"[portal] {topic_prefix}{message}"

    session = await _find_primary_pane_async()
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
        log_activity("Chat: " + message[:60], "", "chat")
        # Return msg_id so the client pre-registers it and WS echo is suppressed
        return JSONResponse({"status": "sent", "timestamp": int(time.time()), "msg_id": msg_id})
    except Exception as e:
        return JSONResponse({"error": _sanitize_error(e, "tmux session")}, status_code=500)


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
    # NOTE (2026-06-03): Do NOT add to stable_sent here. Adding to stable_sent permanently
    # prevents a message from being pushed, even if the frontend never received it (e.g.,
    # the previous WS dropped before delivery). By only setting seen_texts, the message
    # won't trigger the "new message" path (prev_len >= 0), but CAN still be pushed via
    # the stable-final path if its text changes. The frontend's knownMsgIds (now properly
    # cleared on history load) handles dedup for messages already rendered from history.
    messages = _parse_all_messages(last_n=200)
    for msg in messages:
        seen_texts[msg["id"]] = len(msg.get("text", ""))

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
                # Detect [topic:xxx] prefix in assistant messages and extract as topic field
                _raw_text = msg.get("text", "")
                _topic_ws_match = re.match(r'^\[topic:([^\]]+)\]\s*', _raw_text)
                if _topic_ws_match and not msg.get("topic"):
                    _ws_raw_topic = _topic_ws_match.group(1)
                    # Normalize to key format (lowercase, hyphens)
                    msg["topic"] = re.sub(r'[^a-z0-9]+', '-', _ws_raw_topic.lower()).strip('-') or None
                    msg["text"] = _raw_text[_topic_ws_match.end():]

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

            # Prune per-connection dicts every 100 poll cycles to prevent unbounded growth
            if not hasattr(websocket, '_poll_count'):
                websocket._poll_count = 0
            websocket._poll_count += 1
            if websocket._poll_count % 100 == 0:
                if len(seen_texts) > 500:
                    to_remove = sorted(seen_texts.keys())[:-500]
                    for k in to_remove:
                        seen_texts.pop(k, None)
                        first_seen.pop(k, None)
                        stable_counts.pop(k, None)
                        stable_sent.discard(k)
                if len(first_seen) > 500:
                    to_remove = sorted(first_seen.keys())[:-500]
                    for k in to_remove:
                        first_seen.pop(k, None)
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

        # Generate a unique upload_id shared between portal log and tmux injection
        # so _parse_all_messages can reliably dedup the two entries regardless of timing
        upload_id = f"upload-{timestamp_ms}-{secrets.token_hex(4)}"

        # Save ONE combined user message to portal chat log (image + caption together)
        # Include stored_name so frontend can render inline image via /api/chat/uploads/
        chat_text = f"[Image: {stored_name}]" if is_image else f"[File: {stored_name}]"
        if caption:
            chat_text += f"\n{caption}"
        user_entry = _save_portal_message(chat_text, role="user", upload_id=upload_id)

        # Inject notification into AI's tmux session via debounced batch.
        # Multiple files within _DEBOUNCE_WINDOW_S (2.5s) are combined into
        # ONE tmux notification instead of N separate messages (saves tokens).
        _schedule_upload_batch_item(original_name, str(portal_copy_path), is_image, caption, upload_id)
        log_activity(f"File uploaded: {original_name}", "", "file")
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

        # Push both messages to WebSocket clients so they appear live without refresh
        if _chat_ws_clients:
            import asyncio as _asyncio
            _asyncio.create_task(_push_message_to_clients(user_entry))
            _asyncio.create_task(_push_message_to_clients(ack_entry))

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
            "upload_id": upload_id,
        })
    except Exception as e:
        return JSONResponse({"error": _sanitize_error(e, "chat send")}, status_code=500)


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
        # Also check from-portal/ subdirectory
        filepath = UPLOADS_DIR / "from-portal" / filename
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
        try:
            st = item.stat()
        except OSError:
            continue
        items.append({
            "name": item.name,
            "path": str(item),
            "is_dir": item.is_dir(),
            "size": st.st_size if item.is_file() else None,
            "mtime": st.st_mtime,
        })
    return JSONResponse({"dir": str(dirpath), "items": items})


async def api_files_delete(request: Request) -> JSONResponse:
    """Delete a file. Restricted to portal_uploads directory only."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        file_path_str = body.get("path", "").strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not file_path_str:
        return JSONResponse({"error": "missing 'path'"}, status_code=400)
    if ".." in file_path_str:
        return JSONResponse({"error": "path traversal not allowed"}, status_code=403)
    try:
        filepath = Path(file_path_str).resolve()
    except Exception:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    # Security: only allow deletion within portal_uploads
    uploads_dir = Path.home() / "portal_uploads"
    if not (filepath == uploads_dir or uploads_dir in filepath.parents):
        return JSONResponse({"error": "deletion only allowed in portal_uploads"}, status_code=403)
    if not filepath.exists() or not filepath.is_file():
        return JSONResponse({"error": "file not found"}, status_code=404)
    try:
        filepath.unlink()
    except OSError as e:
        return JSONResponse({"error": _sanitize_error(e, "file delete")}, status_code=500)
    return JSONResponse({"ok": True, "deleted": str(filepath)})


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

        # Message count from portal-chat.jsonl
        msg_count = 0
        try:
            if PORTAL_CHAT_LOG.exists():
                with open(PORTAL_CHAT_LOG, "rb") as _f:
                    msg_count = sum(1 for _ in _f)
        except Exception:
            pass

        return JSONResponse({
            "input_tokens": input_tokens,
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "total_tokens": total,
            "max_tokens": MAX_TOKENS,
            "pct": pct,
            "percent": pct,
            "used_tokens": total,
            "session_id": latest.stem,
            "message_count": msg_count,
        })
    except Exception as e:
        return JSONResponse({"error": _sanitize_error(e, "file read")}, status_code=500)


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
        model = _detect_session_model()
        claude_cmd = (
            f"claude --model {model} --dangerously-skip-permissions "
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
        return JSONResponse({"error": _sanitize_error(e, "file write")}, status_code=500)


async def api_panes(request: Request) -> JSONResponse:
    """Return all tmux panes with their current content.
    Includes panes from the primary session AND any agent-team sessions
    (prefixed with 'cc-' or containing the CIV name)."""
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
        primary_session = session.split(":")[0] if ":" in session else session
        civ_lower = CIV_NAME.lower() if CIV_NAME else ""
        # Excluded sessions (not ours)
        _excluded = {"boop-daemon"}
        panes = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t", 2)
            pane_id = parts[0] if len(parts) > 0 else ""
            title = parts[1] if len(parts) > 1 else pane_id
            target = parts[2] if len(parts) > 2 else pane_id
            pane_session = target.split(":")[0] if ":" in target else target
            # Include: primary session, cc- prefixed (agent teams), or civ-name sessions
            is_primary = primary_session in target
            is_agent_team = pane_session.startswith("cc-")
            is_civ = civ_lower and civ_lower in pane_session.lower()
            is_excluded = pane_session in _excluded
            if not (is_primary or is_agent_team or is_civ) or is_excluded:
                continue
            capture = await _run_subprocess_output(
                ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-30"], timeout=3
            )
            panes.append({"id": pane_id, "title": title or pane_id, "target": target, "content": (capture or "").strip()})
        return JSONResponse({"panes": panes})
    except Exception as e:
        return JSONResponse({"error": _sanitize_error(e, "tmux panes"), "panes": []})


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
        return JSONResponse({"error": _sanitize_error(e, "tmux capture")}, status_code=500)


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
            return JSONResponse({"error": _sanitize_error(e, "command exec")}, status_code=500)
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
        return JSONResponse({"error": _sanitize_error(e, "command exec")}, status_code=500)


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
BOOP_TMUX_SESSION = "boop-daemon"  # Legacy fallback
BOOP_POLLER_PROCESS = "boop_poller.py"  # New background process
BOOP_SYSTEM_DIR = Path.home() / "boop-system"
BOOP_DAEMON_SCRIPT = Path.home() / "civ" / "tools" / "boop-daemon.sh"  # Legacy fallback


async def api_boops_active(request: Request) -> JSONResponse:
    """GET /api/boops/active — return all scheduled BOOPs from state file."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    state_file = Path(os.environ.get("CIV_ROOT", str(Path.home()))) / ".claude" / "scheduled-tasks-state.json"
    if not state_file.exists():
        return JSONResponse({"boops": [], "total": 0})
    try:
        data = json.loads(state_file.read_text())
        tasks = data.get("tasks", {})
        boops = []
        for name, info in tasks.items():
            boops.append({
                "id": name,
                "name": name.replace("-", " ").replace("_", " ").title(),
                "description": info.get("description", ""),
                "frequency": info.get("frequency", ""),
                "schedule": info.get("target_time_pkt", info.get("target_time_et", info.get("target_day", ""))),
                "agents": info.get("agents", [info.get("agent", "")]),
                "last_run": info.get("last_run", "never"),
                "status": info.get("status", "unknown"),
                "log_path": info.get("log_path", ""),
            })
        # Also include BOOP poller state
        boop_state_file = BOOP_SYSTEM_DIR / "boop-state.json"
        if boop_state_file.exists():
            try:
                bstate = json.loads(boop_state_file.read_text())
                _poller_running = subprocess.run(
                    ["pgrep", "-f", BOOP_POLLER_PROCESS], capture_output=True
                ).returncode == 0
                boops.append({
                    "id": "boop-poller",
                    "name": "BOOP Grounding Poller",
                    "description": "External cadence enforcer — alternates sprint (ops) and haiku (reflection) cycles",
                    "frequency": "Every 40 min (active) / 60 min (overnight)",
                    "schedule": "Continuous",
                    "agents": ["Primary"],
                    "last_run": bstate.get("last_boop_utc", "never"),
                    "status": "active" if _poller_running else "inactive",
                    "log_path": str(BOOP_SYSTEM_DIR / "boop-log.jsonl"),
                    "boop_count": bstate.get("boop_count", 0),
                    "today_count": bstate.get("today_boop_count", 0),
                })
            except Exception:
                pass
        return JSONResponse({"boops": boops, "total": len(boops)})
    except Exception as e:
        return JSONResponse({"boops": [], "total": 0, "error": _sanitize_error(e, "boop list")})


async def api_boop_status(request: Request) -> JSONResponse:
    """Check if the BOOP daemon/poller is running."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        # Check for new boop_poller.py process first
        r = await _run_subprocess_async(["pgrep", "-f", BOOP_POLLER_PROCESS])
        if r is not None and r.returncode == 0:
            pid_out = await _run_subprocess_output(["pgrep", "-f", BOOP_POLLER_PROCESS], timeout=3)
            pid = int(pid_out.strip().split()[0]) if pid_out and pid_out.strip() else None
            # Load state for extra info
            state_file = BOOP_SYSTEM_DIR / "boop-state.json"
            state = {}
            if state_file.exists():
                try:
                    state = json.loads(state_file.read_text())
                except Exception:
                    pass
            return JSONResponse({
                "active": True, "pid": pid, "type": "poller",
                "boop_count": state.get("boop_count", 0),
                "last_boop": state.get("last_boop_utc"),
                "today_count": state.get("today_boop_count", 0),
            })

        # Legacy: check tmux session
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
        return JSONResponse({"active": running, "pid": pid, "type": "tmux" if running else None})
    except Exception:
        return JSONResponse({"active": False, "pid": None, "type": None})


async def api_boop_toggle(request: Request) -> JSONResponse:
    """Toggle the BOOP poller on/off."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        # Check if new poller is running
        r = await _run_subprocess_async(["pgrep", "-f", BOOP_POLLER_PROCESS])
        poller_running = r is not None and r.returncode == 0

        if poller_running:
            # Stop via stop.sh
            stop_script = BOOP_SYSTEM_DIR / "stop.sh"
            if stop_script.exists():
                await _run_subprocess_async(["bash", str(stop_script)])
            else:
                await _run_subprocess_async(["pkill", "-f", BOOP_POLLER_PROCESS])
            log_activity("BOOP stopped", "", "system")
            return JSONResponse({"active": False, "action": "stopped"})
        else:
            # Start via start.sh
            start_script = BOOP_SYSTEM_DIR / "start.sh"
            if start_script.exists():
                await _run_subprocess_async(["bash", str(start_script)])
                log_activity("BOOP started", "", "system")
                return JSONResponse({"active": True, "action": "started"})

            # Legacy fallback: try tmux approach
            r = await _run_subprocess_async(["tmux", "has-session", "-t", BOOP_TMUX_SESSION])
            currently_running = r is not None and r.returncode == 0
            if currently_running:
                await _run_subprocess_async(["tmux", "kill-session", "-t", BOOP_TMUX_SESSION])
                log_activity("BOOP stopped", "", "system")
                return JSONResponse({"active": False, "action": "stopped"})
            elif BOOP_DAEMON_SCRIPT.exists():
                await _run_subprocess_async(
                    ["tmux", "new-session", "-d", "-s", BOOP_TMUX_SESSION,
                     f"bash {BOOP_DAEMON_SCRIPT} > /tmp/boop-daemon.log 2>&1"]
                )
                log_activity("BOOP started", "", "system")
                return JSONResponse({"active": True, "action": "started"})
            else:
                return JSONResponse(
                    {"error": "No BOOP system found (neither boop_poller.py nor boop-daemon.sh)"},
                    status_code=500
                )
    except Exception as e:
        return JSONResponse({"error": _sanitize_error(e, "boop toggle")}, status_code=500)


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
        return JSONResponse({"error": _sanitize_error(e, "auth flow")}, status_code=500)
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
        return JSONResponse({"error": _sanitize_error(e, "tmux restart")}, status_code=500)


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
    """Background task: tail latest JSONL session files and push thinking blocks to portal."""
    # Track file positions per path (top 3 files, matching _parse_all_messages)
    file_positions: dict[str, int] = {}

    while True:
        try:
            # Find the most recently modified JSONL session files across all projects
            logs = _find_all_project_jsonl()
            if not logs:
                await asyncio.sleep(2)
                continue

            # Check top 3 files (same as _parse_all_messages) — subagent sessions
            # often have newer mtimes, so checking only logs[0] misses the main session.
            all_new_lines: list[str] = []
            for log_path in logs[:3]:
                current_file = str(log_path)
                last_pos = file_positions.get(current_file, 0)

                try:
                    with open(current_file, "rb") as f:
                        f.seek(0, 2)
                        file_size = f.tell()
                        if file_size < last_pos:
                            # File was truncated/rotated — reset
                            last_pos = 0
                        f.seek(last_pos)
                        new_bytes = f.read()
                        file_positions[current_file] = f.tell()
                except Exception:
                    continue

                if new_bytes:
                    all_new_lines.extend(new_bytes.decode("utf-8", errors="replace").splitlines())

            # Prune stale entries from file_positions
            active_paths = {str(p) for p in logs[:3]}
            for stale in [k for k in file_positions if k not in active_paths]:
                del file_positions[stale]

            if not all_new_lines:
                await asyncio.sleep(1.5)
                continue

            lines = all_new_lines
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
                    if len(_sent_thinking_hashes) > 1000:
                        _sent_thinking_hashes.clear()

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

                    # Thinking is ephemeral -- do NOT persist to portal-chat.jsonl
                    # (was causing 414+ stale thinking entries to reload on every refresh)

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
        # Check BOOP state file tasks (Settings panel configured)
        await _check_boop_state_tasks()
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


async def _check_boop_state_tasks() -> None:
    """Check scheduled-tasks-state.json for due tasks and fire them."""
    if not BOOP_STATE_FILE.exists():
        return
    try:
        data = json.loads(BOOP_STATE_FILE.read_text())
        tasks = data.get("tasks", {})
    except Exception:
        return

    now = datetime.now(timezone.utc)
    updated = False

    for task_id, task_info in tasks.items():
        status = task_info.get("status", "pending")
        if status == "disabled":
            continue

        frequency = task_info.get("frequency", "")
        last_run_str = task_info.get("last_run")
        description = task_info.get("description", task_id)

        # Determine if this task is due
        due = False
        if not last_run_str:
            # Never run before — due now
            due = True
        else:
            try:
                last_run = datetime.fromisoformat(last_run_str)
                elapsed = (now - last_run).total_seconds()
                if frequency == "hourly" and elapsed >= 3600:
                    due = True
                elif frequency == "daily" and elapsed >= 86400:
                    due = True
                elif frequency == "weekly" and elapsed >= 604800:
                    due = True
                elif frequency == "every_30_min" and elapsed >= 1800:
                    due = True
                elif frequency == "every_2_hours" and elapsed >= 7200:
                    due = True
            except (ValueError, TypeError):
                due = True  # Can't parse last_run — treat as due

        if due:
            session = get_tmux_session()
            if session:
                msg = f"[SCHEDULED TASK: {task_id}] {description}"
                try:
                    await _run_subprocess_async(
                        ["tmux", "send-keys", "-t", session, "-l", f"\n{msg}"],
                        timeout=5, check=True,
                    )
                    await _run_subprocess_async(
                        ["tmux", "send-keys", "-t", session, "Enter"],
                        timeout=5,
                    )
                    print(f"[sched] Fired BOOP task: {task_id}")
                    task_info["last_run"] = now.isoformat()
                    updated = True
                except Exception as e:
                    print(f"[sched] Failed to fire BOOP task {task_id}: {e}")

    if updated:
        data["last_updated"] = now.isoformat()
        try:
            BOOP_STATE_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            print(f"[sched] Failed to save BOOP state: {e}")


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


BOOP_STATE_FILE = Path(os.environ.get("CIV_ROOT", str(Path.home()))) / ".claude/scheduled-tasks-state.json"


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
        return JSONResponse({"error": _sanitize_error(e, "task update")}, status_code=500)



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
    # Auto-seed module backup if none exists yet
    if not (MODULE_BACKUP_DIR / "manifest.json").exists():
        try:
            _backup_modules()
            print("[portal-startup] Auto-seeded module backup")
        except Exception as _e:
            print(f"[portal-startup] WARNING: module backup auto-seed failed: {_e}")
    log_activity(f"Portal started (v{PORTAL_VERSION})", "", "system")
    asyncio.create_task(_fleet_heartbeat_loop())
    for _hook in _custom_startup_hooks:  # Flux overlay: custom startup hooks
        await _hook()


async def _fleet_heartbeat_loop() -> None:
    """POST portal version + basic metrics to the release server every 5 min."""
    while True:
        await asyncio.sleep(300)
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    "https://cc.purebrain.ai/api/releases/portal/heartbeat",
                    json={
                        "civ_name": CIV_NAME,
                        "version": PORTAL_VERSION,
                        "uptime": int(time.time() - START_TIME),
                    },
                    headers={"User-Agent": f"PureBrain-Portal/{PORTAL_VERSION}"},
                    timeout=10,
                )
        except Exception:
            pass  # Silent fail — heartbeat is best-effort


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
# Referral & Client Admin System — extracted to portal_referrals.py
# ---------------------------------------------------------------------------
from portal_referrals import (
    # DB helpers
    _referral_db, _init_referral_db, _log_financial_event,
    _clients_db, _init_clients_db,
    # Code/auth helpers
    _generate_referral_code, _generate_unique_code, _referral_link,
    _affiliate_login_rate_check, _create_affiliate_session, _verify_affiliate_session,
    _hash_affiliate_password, _verify_affiliate_password,
    _send_reset_email, _send_telegram_notification,
    _paypal_get_access_token, _execute_paypal_payout,
    _is_valid_admin_token, _is_admin_token_readonly,
    # Payout helpers
    _read_payout_requests_legacy, _write_payout_request_legacy, _update_payout_status_legacy,
    _read_payout_requests_db, _write_payout_request_db, _update_payout_status_db,
    # Referral endpoints
    api_referral_register, api_referral_login, api_referral_session,
    api_referral_forgot_password, api_referral_reset_password,
    api_referral_dashboard, api_referral_track, api_referral_complete,
    api_referral_record_commission, api_referral_code_lookup,
    api_referral_paypal_email, api_referral_leaderboard,
    api_portal_owner,
    api_referral_payout_request, api_referral_payout_history,
    api_referral_payout_approve,
    # Admin endpoints
    api_admin_payout_mark_paid,
    api_admin_invite, api_admin_invites_list, api_admin_invite_revoke,
    api_admin_affiliates, api_admin_payouts,
    api_admin_affiliate_update, api_admin_affiliate_delete,
    api_admin_referral_update, api_admin_referral_assign,
    serve_admin_referrals,
    # Client admin endpoints
    api_admin_clients, api_public_client_stats,
    api_admin_clients_update, api_admin_clients_import,
    api_admin_clients_hide, api_admin_clients_restore,
    serve_admin_clients, serve_affiliate_portal,
)

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
# AgentMail Inbox Proxy — exposes email via server-side SDK
# ---------------------------------------------------------------------------

def _migrate_email_accounts(settings: dict) -> bool:
    """Migrate old single-account format to email_accounts array. Returns True if migrated."""
    if settings.get("email_accounts"):
        return False
    api_key = settings.get("agentmail_api_key", "").strip()
    email = settings.get("agentmail_email", "").strip()
    if api_key and email:
        label = email.split("@")[0] if "@" in email else email
        settings["email_accounts"] = [
            {"provider": "agentmail", "api_key": api_key, "address": email, "label": label}
        ]
        _save_settings(settings)
        return True
    return False


class GmailClient:
    """Gmail IMAP/SMTP client providing the same interface shape as AgentMail."""

    def __init__(self, address: str, app_password: str):
        self.address = address
        self.app_password = app_password.replace(" ", "")
        self.imap_host = "imap.gmail.com"
        self.imap_port = 993
        self.smtp_host = "smtp.gmail.com"
        self.smtp_port = 587

    # ── helpers ──────────────────────────────────────────────────────────
    def _imap_connect(self):
        import imaplib
        imaplib._MAXLINE = 10_000_000  # 10MB — Gmail SEARCH can return huge UID lists
        conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        conn.login(self.address, self.app_password)
        return conn

    @staticmethod
    def _decode_header(raw):
        from email.header import decode_header as _dh
        if not raw:
            return ""
        parts = _dh(raw)
        decoded = []
        for data, charset in parts:
            if isinstance(data, bytes):
                decoded.append(data.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(data)
        return " ".join(decoded)

    @staticmethod
    def _parse_addr(raw):
        from email.utils import parseaddr
        _, addr = parseaddr(raw or "")
        return addr or raw or ""

    @staticmethod
    def _msg_date(msg):
        from email.utils import parsedate_to_datetime
        date_str = msg.get("Date")
        if not date_str:
            return datetime.now(timezone.utc)
        try:
            return parsedate_to_datetime(date_str)
        except Exception:
            return datetime.now(timezone.utc)

    @staticmethod
    def _strip_html(html_str: str) -> str:
        """Convert HTML to readable plain text."""
        import re as _re_html, html as html_mod
        text = _re_html.sub(r'<(style|script)[^>]*>.*?</\1>', '', html_str, flags=_re_html.DOTALL | _re_html.IGNORECASE)
        text = _re_html.sub(r'<br\s*/?>', '\n', text, flags=_re_html.IGNORECASE)
        text = _re_html.sub(r'</(p|div|tr|li|h[1-6])>', '\n', text, flags=_re_html.IGNORECASE)
        text = _re_html.sub(r'<[^>]+>', '', text)
        text = html_mod.unescape(text)
        text = _re_html.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    @staticmethod
    def _body_html(msg):
        """Extract raw HTML body from an email.message.Message, or empty string."""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace")
            return ""
        if msg.get_content_type() == "text/html":
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        return ""

    @staticmethod
    def _body_text(msg):
        """Extract plain-text body from an email.message.Message."""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace")
            # Fallback: try text/html, strip tags for plain-text output
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        return GmailClient._strip_html(payload.decode(charset, errors="replace"))
            return ""
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            raw = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                return GmailClient._strip_html(raw)
            return raw
        return ""

    # ── public API ───────────────────────────────────────────────────────
    def list_threads(self, limit: int = 30, folder: str = "inbox"):
        """Return list of thread dicts, grouped by Gmail thread ID (X-GM-THRID).
        folder: 'inbox' (default) or 'sent' to list sent messages."""
        import imaplib
        import email as email_mod
        conn = self._imap_connect()
        try:
            if folder == "sent":
                conn.select("[Gmail]/Sent Mail", readonly=True)
            else:
                conn.select("INBOX", readonly=True)
            # Search recent emails only (last 30 days) to avoid massive UID lists
            from datetime import timedelta
            since_date = (datetime.now() - timedelta(days=30)).strftime("%d-%b-%Y")
            _typ, data = conn.search(None, f'(SINCE {since_date})')
            all_uids = data[0].split() if data[0] else []
            # If no recent emails, try last 90 days
            if not all_uids:
                since_date = (datetime.now() - timedelta(days=90)).strftime("%d-%b-%Y")
                _typ, data = conn.search(None, f'(SINCE {since_date})')
                all_uids = data[0].split() if data[0] else []
            # Take most recent messages (fetch more than limit to group into threads)
            recent_uids = all_uids[-(limit * 3):] if len(all_uids) > limit * 3 else all_uids
            if not recent_uids:
                return []

            uid_range = b",".join(recent_uids)
            # Fetch envelope + flags + Gmail thread ID
            try:
                _typ, fetch_data = conn.fetch(uid_range, "(FLAGS BODY.PEEK[HEADER] X-GM-THRID)")
            except imaplib.IMAP4.error:
                # Fallback if X-GM-THRID not supported
                _typ, fetch_data = conn.fetch(uid_range, "(FLAGS BODY.PEEK[HEADER])")

            # Parse messages and group by thread
            threads_map = {}  # thread_key -> {msgs, flags, ...}
            for item in fetch_data:
                if not isinstance(item, tuple) or len(item) < 2:
                    continue
                header_line = item[0] if isinstance(item[0], (str, bytes)) else b""
                if isinstance(header_line, str):
                    header_line = header_line.encode()
                raw_header = item[1] if isinstance(item[1], bytes) else b""

                # Parse flags
                flags_str = header_line.decode("utf-8", errors="replace")
                is_seen = "\\Seen" in flags_str

                # Extract X-GM-THRID if present
                gm_thrid = None
                import re as _re
                thrid_match = _re.search(r"X-GM-THRID\s+(\d+)", flags_str)
                if thrid_match:
                    gm_thrid = thrid_match.group(1)

                msg = email_mod.message_from_bytes(raw_header)
                subject = self._decode_header(msg.get("Subject")) or "(no subject)"
                from_addr = self._decode_header(msg.get("From", ""))
                to_addr = self._decode_header(msg.get("To", ""))
                msg_id = msg.get("Message-ID", "")
                date = self._msg_date(msg)

                # Thread key: use Gmail thread ID or fall back to subject
                thread_key = gm_thrid or re.sub(r"^(Re|Fwd):\s*", "", subject, flags=re.IGNORECASE).strip().lower()

                if thread_key not in threads_map:
                    threads_map[thread_key] = {
                        "thread_id": gm_thrid or msg_id or str(hash(thread_key)),
                        "subject": subject,
                        "senders": [],
                        "recipients": [],
                        "preview": "",
                        "labels": [],
                        "message_count": 0,
                        "timestamp": date,
                        "unread": False,
                        "_date": date,
                    }
                t = threads_map[thread_key]
                t["message_count"] += 1
                from_email = self._parse_addr(from_addr)
                if from_email and from_email not in t["senders"]:
                    t["senders"].append(from_email)
                to_email = self._parse_addr(to_addr)
                if to_email and to_email not in t["recipients"]:
                    t["recipients"].append(to_email)
                if not is_seen:
                    t["unread"] = True
                if date > t["_date"]:
                    t["_date"] = date
                    t["timestamp"] = date

            # Sort by most recent, limit
            threads = sorted(threads_map.values(), key=lambda x: x["_date"], reverse=True)[:limit]
            for t in threads:
                t["timestamp"] = t["_date"].isoformat()
                if t["unread"]:
                    t["labels"] = ["unread"]
                del t["_date"]
            return threads
        finally:
            try:
                conn.close()
                conn.logout()
            except Exception:
                pass

    def get_thread(self, thread_id: str):
        """Fetch all messages for a thread. thread_id may be X-GM-THRID or Message-ID."""
        import imaplib
        import email as email_mod
        conn = self._imap_connect()
        try:
            conn.select("INBOX", readonly=True)

            # Try Gmail thread search first (X-GM-THRID)
            uids = []
            try:
                _typ, data = conn.search(None, f"X-GM-THRID {thread_id}")
                uids = data[0].split() if data[0] else []
            except imaplib.IMAP4.error:
                pass

            if not uids:
                # Fallback: search by Message-ID header
                _typ, data = conn.search(None, f'HEADER Message-ID "{thread_id}"')
                uids = data[0].split() if data[0] else []

            if not uids:
                # Fallback: search by subject from a broader set
                _typ, data = conn.search(None, "ALL")
                uids = data[0].split()[-50:] if data[0] else []

            if not uids:
                return {"thread_id": thread_id, "subject": "", "senders": [], "recipients": [], "labels": [], "messages": []}

            uid_range = b",".join(uids)
            _typ, fetch_data = conn.fetch(uid_range, "(FLAGS BODY.PEEK[])")

            messages = []
            subject = ""
            senders = []
            recipients = []
            for item in fetch_data:
                if not isinstance(item, tuple) or len(item) < 2:
                    continue
                flags_str = item[0].decode("utf-8", errors="replace") if isinstance(item[0], bytes) else str(item[0])
                is_seen = "\\Seen" in flags_str
                raw = item[1] if isinstance(item[1], bytes) else b""
                msg = email_mod.message_from_bytes(raw)
                msg_subject = self._decode_header(msg.get("Subject")) or "(no subject)"
                from_addr = self._decode_header(msg.get("From", ""))
                to_addr = self._decode_header(msg.get("To", ""))
                cc_addr = self._decode_header(msg.get("Cc", ""))
                msg_id = msg.get("Message-ID", "")
                date = self._msg_date(msg)
                body = self._body_text(msg)
                html_body = self._body_html(msg)

                if not subject:
                    subject = msg_subject
                from_email = self._parse_addr(from_addr)
                if from_email and from_email not in senders:
                    senders.append(from_email)
                to_email = self._parse_addr(to_addr)
                if to_email and to_email not in recipients:
                    recipients.append(to_email)

                messages.append({
                    "message_id": msg_id,
                    "thread_id": thread_id,
                    "from": from_email or from_addr,
                    "to": [self._parse_addr(a) for a in to_addr.split(",") if a.strip()] if to_addr else [],
                    "cc": [self._parse_addr(a) for a in cc_addr.split(",") if a.strip()] if cc_addr else [],
                    "subject": msg_subject,
                    "text": body,
                    "html": html_body,
                    "preview": body[:200] if body else "",
                    "labels": [] if is_seen else ["unread"],
                    "timestamp": date.isoformat(),
                    "unread": not is_seen,
                    "in_reply_to": msg.get("In-Reply-To", ""),
                })

            messages.sort(key=lambda m: m["timestamp"])
            return {
                "thread_id": thread_id,
                "subject": subject or "(no subject)",
                "senders": senders,
                "recipients": recipients,
                "labels": ["unread"] if any(m["unread"] for m in messages) else [],
                "messages": messages,
            }
        finally:
            try:
                conn.close()
                conn.logout()
            except Exception:
                pass

    def send_message(self, to: str, subject: str, body: str, cc: str = None):
        """Send an email via SMTP. Returns dict with message_id."""
        import smtplib
        import email.message
        msg = email.message.EmailMessage()
        msg["From"] = self.address
        msg["To"] = to
        msg["Subject"] = subject or "(no subject)"
        if cc:
            msg["Cc"] = cc
        msg.set_content(body)
        msg_id = msg["Message-ID"]  # auto-generated

        smtp = smtplib.SMTP(self.smtp_host, self.smtp_port)
        try:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(self.address, self.app_password)
            smtp.send_message(msg)
        finally:
            smtp.quit()

        return {"message_id": msg_id or "", "thread_id": ""}

    def reply_to_thread(self, thread_id: str, message_id: str, body: str):
        """Reply to a message via SMTP with In-Reply-To and References headers."""
        import smtplib
        import email.message as email_message_mod

        # Fetch original message to get subject and reply-to info
        original_subject = ""
        original_from = ""
        try:
            thread_data = self.get_thread(thread_id)
            for m in thread_data.get("messages", []):
                if m.get("message_id") == message_id:
                    original_subject = m.get("subject", "")
                    original_from = m.get("from", "")
                    break
            if not original_subject and thread_data.get("messages"):
                last = thread_data["messages"][-1]
                original_subject = last.get("subject", "")
                original_from = last.get("from", "")
                message_id = last.get("message_id", message_id)
        except Exception:
            pass

        msg = email_message_mod.EmailMessage()
        msg["From"] = self.address
        msg["To"] = original_from or self.address
        msg["Subject"] = ("Re: " + original_subject) if original_subject and not original_subject.startswith("Re:") else (original_subject or "Re:")
        if message_id:
            msg["In-Reply-To"] = message_id
            msg["References"] = message_id
        msg.set_content(body)
        new_msg_id = msg["Message-ID"] or ""

        smtp = smtplib.SMTP(self.smtp_host, self.smtp_port)
        try:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(self.address, self.app_password)
            smtp.send_message(msg)
        finally:
            smtp.quit()

        return {"message_id": new_msg_id, "thread_id": thread_id}

    def mark_thread_read(self, thread_id: str):
        """Mark all messages in a thread as read (add \\Seen flag)."""
        import imaplib
        conn = self._imap_connect()
        try:
            conn.select("INBOX")
            uids = []
            try:
                _typ, data = conn.search(None, f"X-GM-THRID {thread_id}")
                uids = data[0].split() if data[0] else []
            except imaplib.IMAP4.error:
                pass
            if not uids:
                _typ, data = conn.search(None, f'HEADER Message-ID "{thread_id}"')
                uids = data[0].split() if data[0] else []
            if uids:
                uid_range = b",".join(uids)
                conn.store(uid_range, "+FLAGS", "\\Seen")
        finally:
            try:
                conn.close()
                conn.logout()
            except Exception:
                pass


def _get_email_client(account_idx: int = 0):
    """Return (client, inbox_email) or (None, None) for given account index.
    Routes to AgentMail or GmailClient based on provider."""
    settings = _load_settings()
    _migrate_email_accounts(settings)
    accounts = settings.get("email_accounts", [])
    if not accounts or account_idx >= len(accounts):
        # Fallback to legacy keys for backward compat (agentmail only)
        api_key = settings.get("agentmail_api_key", "").strip()
        email_addr = settings.get("agentmail_email", "").strip()
        if not api_key or not email_addr:
            return None, None
        try:
            from agentmail import AgentMail
            return AgentMail(api_key=api_key), email_addr
        except Exception:
            return None, None
    acct = accounts[account_idx]
    provider = acct.get("provider", "agentmail")
    api_key = acct.get("api_key", "").strip()
    email_addr = acct.get("address", "").strip()
    if not api_key or not email_addr:
        return None, None

    if provider == "gmail":
        try:
            return GmailClient(email_addr, api_key), email_addr
        except Exception:
            return None, None
    else:
        # Default: AgentMail
        try:
            from agentmail import AgentMail
            return AgentMail(api_key=api_key), email_addr
        except Exception:
            return None, None


def _send_email_notification(to_email: str, subject: str, body: str, cc: str = None) -> dict:
    """Send an email notification using the configured email provider (Gmail or AgentMail).
    Returns {"ok": True, "message_id": ...} on success or {"ok": False, "error": "..."} on failure.
    CIV-agnostic: uses whatever provider the CIV has configured in settings.
    """
    try:
        client, from_addr = _get_email_client()
        if client is None:
            return {"ok": False, "error": "No email provider configured"}
        if isinstance(client, GmailClient):
            result = client.send_message(to=to_email, subject=subject, body=body, cc=cc)
            return {"ok": True, "message_id": result.get("message_id", "")}
        else:
            # AgentMail SDK
            result = client.inboxes.messages.send(
                inbox_id=from_addr,
                to=to_email,
                subject=subject,
                text=body,
            )
            return {"ok": True, "message_id": getattr(result, "message_id", "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _thread_to_dict(t) -> dict:
    """Serialize an AgentMail thread object to a JSON-safe dict."""
    return {
        "thread_id": t.thread_id,
        "subject": t.subject or "(no subject)",
        "senders": t.senders or [],
        "recipients": t.recipients or [],
        "preview": t.preview or "",
        "labels": t.labels or [],
        "message_count": t.message_count or 0,
        "timestamp": t.timestamp.isoformat() if t.timestamp else None,
        "unread": "unread" in (t.labels or []),
    }


def _message_to_dict(m) -> dict:
    """Serialize an AgentMail message object to a JSON-safe dict."""
    return {
        "message_id": m.message_id,
        "thread_id": m.thread_id,
        "from": m.from_ or "",
        "to": m.to or [],
        "cc": m.cc or [],
        "subject": m.subject or "(no subject)",
        "text": m.text or "",
        "html": m.html or "",
        "preview": m.preview or "",
        "labels": m.labels or [],
        "timestamp": m.timestamp.isoformat() if m.timestamp else None,
        "unread": "unread" in (m.labels or []),
        "in_reply_to": m.in_reply_to or "",
    }


async def api_inbox_status(request: Request) -> JSONResponse:
    """GET /api/inbox/status — check if AgentMail is configured. Returns all accounts."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    settings = _load_settings()
    _migrate_email_accounts(settings)
    accounts = settings.get("email_accounts", [])
    if accounts:
        safe_accounts = [
            {"provider": a.get("provider", ""), "address": a.get("address", ""), "label": a.get("label", "")}
            for a in accounts
        ]
        return JSONResponse({
            "configured": True,
            "email": accounts[0].get("address", ""),
            "accounts": safe_accounts,
        })
    # Legacy fallback
    api_key = settings.get("agentmail_api_key", "").strip()
    email = settings.get("agentmail_email", "").strip()
    return JSONResponse({
        "configured": bool(api_key and email),
        "email": email if api_key else "",
        "accounts": [],
    })


_gmail_thread_cache = {}  # {account_idx: {"threads": [...], "ts": float, "email": str}}
_GMAIL_CACHE_TTL = 30  # seconds

async def api_inbox_threads(request: Request) -> JSONResponse:
    """GET /api/inbox/threads — list recent email threads."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    account_idx = int(request.query_params.get("account", "0"))
    client, email = _get_email_client(account_idx)
    if not client:
        return JSONResponse({"error": "Email not configured"}, status_code=400)
    try:
        limit = int(request.query_params.get("limit", "30"))
        limit = min(limit, 100)
        folder = request.query_params.get("folder", "inbox")
        if isinstance(client, GmailClient):
            # Server-side cache for Gmail (IMAP is slow) — keyed by folder
            cache_key = (account_idx, folder)
            cached = _gmail_thread_cache.get(cache_key)
            if cached and (time.time() - cached["ts"]) < _GMAIL_CACHE_TTL:
                return JSONResponse({"threads": cached["threads"], "email": cached["email"]})
            # Run blocking IMAP in thread pool to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            threads = await loop.run_in_executor(None, lambda: client.list_threads(limit=limit, folder=folder))
            _gmail_thread_cache[cache_key] = {"threads": threads, "ts": time.time(), "email": email}
            return JSONResponse({"threads": threads, "email": email})
        else:
            kwargs = {"limit": limit}
            if folder == "sent":
                kwargs["labels"] = ["sent"]
            result = client.inboxes.threads.list(email, **kwargs)
            threads = [_thread_to_dict(t) for t in (result.threads or [])]
            return JSONResponse({"threads": threads, "email": email})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


async def api_inbox_thread_detail(request: Request) -> JSONResponse:
    """GET /api/inbox/threads/{thread_id} — get full thread with messages."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    account_idx = int(request.query_params.get("account", "0"))
    client, email = _get_email_client(account_idx)
    if not client:
        return JSONResponse({"error": "Email not configured"}, status_code=400)
    thread_id = request.path_params.get("thread_id", "")
    try:
        if isinstance(client, GmailClient):
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, lambda: client.get_thread(thread_id))
            return JSONResponse(result)
        else:
            thread = client.threads.get(thread_id)
            messages = [_message_to_dict(m) for m in (thread.messages or [])]
            return JSONResponse({
                "thread_id": thread.thread_id,
                "subject": thread.subject or "(no subject)",
                "senders": thread.senders or [],
                "recipients": thread.recipients or [],
                "labels": thread.labels or [],
                "messages": messages,
            })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


async def api_inbox_send(request: Request) -> JSONResponse:
    """POST /api/inbox/send — send a new email."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    account_idx = int(request.query_params.get("account", "0"))
    client, email = _get_email_client(account_idx)
    if not client:
        return JSONResponse({"error": "Email not configured"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    to = body.get("to", "").strip()
    subject = body.get("subject", "").strip()
    text = body.get("text", "").strip()
    if not to or not text:
        return JSONResponse({"error": "to and text are required"}, status_code=400)
    try:
        if isinstance(client, GmailClient):
            result = client.send_message(to=to, subject=subject or "(no subject)", body=text)
            _gmail_thread_cache.pop(account_idx, None)  # Invalidate cache after send
            return JSONResponse({
                "ok": True,
                "message_id": result.get("message_id", ""),
                "thread_id": result.get("thread_id", ""),
            })
        else:
            result = client.inboxes.messages.send(
                inbox_id=email,
                to=to,
                subject=subject or "(no subject)",
                text=text,
            )
            return JSONResponse({
                "ok": True,
                "message_id": result.message_id,
                "thread_id": result.thread_id,
            })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


async def api_inbox_reply(request: Request) -> JSONResponse:
    """POST /api/inbox/reply/{message_id} — reply to a message."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    account_idx = int(request.query_params.get("account", "0"))
    client, email = _get_email_client(account_idx)
    if not client:
        return JSONResponse({"error": "Email not configured"}, status_code=400)
    message_id = request.path_params.get("message_id", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    text = body.get("text", "").strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)
    try:
        if isinstance(client, GmailClient):
            thread_id = body.get("thread_id", "")
            result = client.reply_to_thread(
                thread_id=thread_id,
                message_id=message_id,
                body=text,
            )
            _gmail_thread_cache.pop(account_idx, None)  # Invalidate cache after reply
            return JSONResponse({
                "ok": True,
                "message_id": result.get("message_id", ""),
                "thread_id": result.get("thread_id", ""),
            })
        else:
            result = client.inboxes.messages.reply(
                inbox_id=email,
                message_id=message_id,
                text=text,
            )
            return JSONResponse({
                "ok": True,
                "message_id": result.message_id,
                "thread_id": result.thread_id,
            })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


async def api_inbox_mark_read(request: Request) -> JSONResponse:
    """POST /api/inbox/threads/{thread_id}/read — mark all messages in thread as read."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    account_idx = int(request.query_params.get("account", "0"))
    client, email = _get_email_client(account_idx)
    if not client:
        return JSONResponse({"error": "Email not configured"}, status_code=400)
    thread_id = request.path_params.get("thread_id", "")
    try:
        if isinstance(client, GmailClient):
            client.mark_thread_read(thread_id)
            return JSONResponse({"ok": True})
        else:
            thread = client.threads.get(thread_id)
            for m in (thread.messages or []):
                if "unread" in (m.labels or []):
                    client.inboxes.messages.update(
                        inbox_id=email,
                        message_id=m.message_id,
                        remove_labels=["unread"],
                    )
            return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


async def api_inbox_mark_all_read(request: Request) -> JSONResponse:
    """POST /api/inbox/mark-all-read — mark all unread threads as read."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    account_idx = int(request.query_params.get("account", "0"))
    client, email = _get_email_client(account_idx)
    if not client:
        return JSONResponse({"error": "Email not configured"}, status_code=400)
    try:
        marked = 0
        if isinstance(client, GmailClient):
            threads = client.list_threads(limit=50, folder="inbox")
            for t in threads:
                if t.get("unread"):
                    client.mark_thread_read(t["thread_id"])
                    marked += 1
        else:
            result = client.inboxes.threads.list(email, labels=["unread"], limit=50)
            for t in (result.threads or []):
                thread_detail = client.threads.get(t.thread_id)
                for m in (thread_detail.messages or []):
                    if "unread" in (m.labels or []):
                        client.inboxes.messages.update(
                            inbox_id=email,
                            message_id=m.message_id,
                            remove_labels=["unread"],
                        )
                marked += 1
        return JSONResponse({"ok": True, "marked": marked})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


async def api_inbox_accounts(request: Request) -> JSONResponse:
    """POST /api/inbox/accounts — add a new email account.
       DELETE /api/inbox/accounts — remove an account by index."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    settings = _load_settings()
    _migrate_email_accounts(settings)
    accounts = settings.get("email_accounts", [])

    if request.method == "DELETE":
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        idx = body.get("index", -1)
        if not isinstance(idx, int) or idx < 0 or idx >= len(accounts):
            return JSONResponse({"error": "invalid account index"}, status_code=400)
        accounts.pop(idx)
        settings["email_accounts"] = accounts
        _save_settings(settings)
        return JSONResponse({"ok": True, "accounts": [
            {"provider": a.get("provider", ""), "address": a.get("address", ""), "label": a.get("label", "")}
            for a in accounts
        ]})

    # POST — add account
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    provider = body.get("provider", "agentmail").strip()
    api_key = body.get("api_key", "").strip()
    address = body.get("address", "").strip()
    label = body.get("label", "").strip()
    if not api_key or not address:
        return JSONResponse({"error": "api_key and address are required"}, status_code=400)
    if not label:
        label = address.split("@")[0] if "@" in address else address
    # Deduplicate by address
    for a in accounts:
        if a.get("address", "").lower() == address.lower():
            return JSONResponse({"error": "account already exists"}, status_code=409)
    accounts.append({"provider": provider, "api_key": api_key, "address": address, "label": label})
    settings["email_accounts"] = accounts
    # Also keep legacy keys pointing at first account for backward compat
    settings["agentmail_api_key"] = accounts[0]["api_key"]
    settings["agentmail_email"] = accounts[0]["address"]
    _save_settings(settings)
    return JSONResponse({"ok": True, "accounts": [
        {"provider": a.get("provider", ""), "address": a.get("address", ""), "label": a.get("label", "")}
        for a in accounts
    ]})


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# User settings (synced across devices via server)
# ---------------------------------------------------------------------------
SETTINGS_FILE = SCRIPT_DIR / "user-settings.json"
CC_CACHE_FILE = SCRIPT_DIR / "cc-cache.json"

_SECRET_FIELDS = ("cc_civ_key", "agentmail_api_key")

def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
    except Exception:
        return {}

def _save_settings(data: dict):
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))

def _load_cc_cache() -> list | None:
    try:
        return json.loads(CC_CACHE_FILE.read_text()) if CC_CACHE_FILE.exists() else None
    except Exception:
        return None

def _save_cc_cache(messages) -> None:
    CC_CACHE_FILE.write_text(json.dumps(messages))

async def api_user_settings(request: Request) -> JSONResponse:
    """GET returns saved settings, POST/PUT merges new settings."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.method == "GET":
        settings = _load_settings()
        # Merge CC message cache from separate file (split for size)
        cc_cache = _load_cc_cache()
        if cc_cache is not None:
            settings["cc_cached_messages"] = cc_cache
        # Mask secrets before returning to browser (defense-in-depth)
        for field in _SECRET_FIELDS:
            val = settings.get(field)
            if val and isinstance(val, str):
                settings[field] = "****" + val[-4:] if len(val) > 4 else "****"
        return JSONResponse(settings)
    # POST/PUT — merge incoming keys
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    settings = _load_settings()
    # Extract cc_cached_messages to separate file (keeps settings small)
    if "cc_cached_messages" in body:
        cc_data = body.pop("cc_cached_messages")
        if cc_data is not None:
            _save_cc_cache(cc_data)
    # Deep-merge nested dicts (e.g. notifications) instead of replacing them
    for key, value in body.items():
        # Skip masked secret values (browser echoes back "****..." from GET)
        if key in _SECRET_FIELDS and isinstance(value, str) and value.startswith("****"):
            continue
        if isinstance(value, dict) and isinstance(settings.get(key), dict):
            settings[key].update(value)
        else:
            settings[key] = value
    _save_settings(settings)
    return JSONResponse({"ok": True, "settings": settings})

# ---------------------------------------------------------------------------
# Profile API — merges identity file + editable settings
# ---------------------------------------------------------------------------

async def api_profile(request: Request) -> JSONResponse:
    """GET returns merged profile (identity + settings), POST saves editable fields."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        settings = _load_settings()
        profile = settings.get("profile", {})

        # Only allow editable fields — identity file fields are read-only
        editable = [
            "role", "model", "architecture", "agent_count",
            "human_title", "human_org", "human_background",
            "human_style", "human_values", "human_role", "human_relationship",
            "website", "civ_email"
        ]
        for key in editable:
            if key in body:
                profile[key] = body[key]

        settings["profile"] = profile
        _save_settings(settings)
        return JSONResponse({"ok": True})

    # GET — merge identity file (read-only) + settings (editable)
    identity = {}
    identity_file = Path.home() / ".aiciv-identity.json"
    if identity_file.exists():
        try:
            identity = json.loads(identity_file.read_text())
        except Exception:
            pass

    settings = _load_settings()
    profile = settings.get("profile", {})

    # Fall back to first email_accounts entry for civ_email
    accounts = settings.get("email_accounts", [])
    civ_email = profile.get("civ_email", "")
    human_email = identity.get("human_email", "")
    if not civ_email and accounts:
        civ_email = accounts[0].get("address", "")

    return JSONResponse({
        # AI Identity (from identity file, non-editable)
        "civ_name": identity.get("civ_name", ""),
        "civ_id": identity.get("civ_id", ""),
        "parent_civ": identity.get("parent_civ", ""),
        "born": identity.get("born", ""),
        "status": identity.get("status", "active"),
        # AI Identity (from settings, editable)
        "role": profile.get("role", "AI Agent"),
        "model": profile.get("model", "Claude"),
        "architecture": profile.get("architecture", ""),
        "agent_count": profile.get("agent_count", ""),
        # Human Partner (identity = non-editable, settings = editable)
        "human_name": identity.get("human_name", ""),
        "human_email": human_email,
        "human_title": profile.get("human_title", ""),
        "human_org": profile.get("human_org", ""),
        "human_background": profile.get("human_background", ""),
        "human_style": profile.get("human_style", ""),
        "human_values": profile.get("human_values", ""),
        "human_role": profile.get("human_role", "Creator & Steward"),
        "human_relationship": profile.get("human_relationship", "Trust-based"),
        # Contact Card
        "civ_email": civ_email,
        "website": profile.get("website", ""),
    })

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

def _fetch_url_metadata(url: str, timeout: float = 5.0) -> dict:
    """Fetch a URL and extract <title> + favicon. Returns dict with 'title' and 'favicon_url'."""
    result = {}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 PureBrain Portal"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read(64 * 1024).decode("utf-8", errors="ignore")
            title_match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
            if title_match:
                result["title"] = title_match.group(1).strip()
            icon_match = re.search(
                r'<link[^>]*rel=["\'](?:shortcut )?icon["\'][^>]*href=["\']([^"\']+)["\']',
                html, re.IGNORECASE,
            )
            if not icon_match:
                icon_match = re.search(
                    r'<link[^>]*href=["\']([^"\']+)["\'][^>]*rel=["\'](?:shortcut )?icon["\']',
                    html, re.IGNORECASE,
                )
            if icon_match:
                favicon = icon_match.group(1)
                if favicon.startswith("//"):
                    favicon = "https:" + favicon
                elif favicon.startswith("/"):
                    parsed = urllib.parse.urlparse(url)
                    favicon = f"{parsed.scheme}://{parsed.netloc}{favicon}"
                result["favicon_url"] = favicon
            else:
                parsed = urllib.parse.urlparse(url)
                result["favicon_url"] = f"{parsed.scheme}://{parsed.netloc}/favicon.ico"
    except Exception:
        pass
    return result

def _enrich_bookmark(bm: dict):
    """Enrich a URL bookmark with title/favicon if missing."""
    if bm.get("type") == "url" and bm.get("url"):
        title = bm.get("name", "")
        if not title or title == "Bookmark":
            meta = _fetch_url_metadata(bm["url"])
            if meta.get("title"):
                bm["name"] = meta["title"]
            elif not title:
                bm["name"] = bm["url"]
            if meta.get("favicon_url"):
                bm["favicon_url"] = meta["favicon_url"]

async def api_bookmarks(request: Request) -> JSONResponse:
    """GET returns bookmarks. POST supports action-based ops or full-array replace."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.method == "GET":
        return JSONResponse(_load_bookmarks())
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    # Action-based: { action: "add", bookmark: {...} } or { action: "delete", id: "..." }
    if isinstance(body, dict):
        action = body.get("action")
        if action == "add":
            bm = body.get("bookmark")
            if not isinstance(bm, dict):
                return JSONResponse({"error": "missing bookmark object"}, status_code=400)
            _enrich_bookmark(bm)
            bms = _load_bookmarks()
            bms.append(bm)
            _save_bookmarks(bms)
            return JSONResponse({"ok": True, "count": len(bms)})
        elif action == "delete":
            bid = body.get("id")
            if not bid:
                return JSONResponse({"error": "missing id"}, status_code=400)
            bms = _load_bookmarks()
            bms = [b for b in bms if b.get("id") != bid]
            _save_bookmarks(bms)
            return JSONResponse({"ok": True, "count": len(bms)})
        return JSONResponse({"error": "unknown action"}, status_code=400)

    # Full-array replace (used for migration from localStorage)
    if not isinstance(body, list):
        return JSONResponse({"error": "expected array or action object"}, status_code=400)
    for bm in body:
        if isinstance(bm, dict):
            _enrich_bookmark(bm)
    _save_bookmarks(body)
    return JSONResponse({"ok": True, "count": len(body)})

# ---------------------------------------------------------------------------
# Deployments CRUD API
# ---------------------------------------------------------------------------

# Deployments start empty — each CIV adds their own via the portal UI or API.
# Stored in user-settings.json (gitignored) so personal deployments never leak to the repo.
#
# Schema for reference:
# {
#     "id": "dep-example-001",        # unique id (auto-generated by UI)
#     "name": "my-app.example.com",   # display name
#     "description": "What it does",  # free text
#     "url": "https://...",           # live URL (optional)
#     "platform": "VPS",              # VPS, Netlify, Cloudflare Pages, Vercel, etc.
#     "stack": "FastAPI + SQLite",    # tech stack summary
#     "status": "live",               # live, staging, maintenance, offline
#     "repo": "org/repo-name",        # GitHub repo (optional)
#     "server": "1.2.3.4",            # server IP (optional)
#     "created_at": "ISO8601",
#     "updated_at": "ISO8601"
# }
_DEFAULT_DEPLOYMENTS = []


_vercel_cache: dict = {"ts": 0, "data": []}
_VERCEL_CACHE_TTL = 300  # 5 min cache

async def _fetch_vercel_projects() -> list:
    """Fetch projects from Vercel API, cached for 5 min."""
    import time as _time
    now = _time.time()
    if now - _vercel_cache["ts"] < _VERCEL_CACHE_TTL and _vercel_cache["data"]:
        return _vercel_cache["data"]

    token = os.environ.get("VERCEL_TOKEN", "")
    team_id = os.environ.get("VERCEL_TEAM_ID", "team_GnbWx7m4NZAjfqGYojakme0k")
    if not token:
        return []

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.vercel.com/v9/projects?limit=100&teamId={team_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                return _vercel_cache.get("data", [])
            projects = resp.json().get("projects", [])

        deployments = []
        for p in projects:
            name = p.get("name", "")
            targets = p.get("targets", {})
            prod = targets.get("production", {})
            url = ""
            if isinstance(prod, dict):
                # Prefer production alias (clean URL like name.vercel.app)
                prod_aliases = prod.get("alias", [])
                if prod_aliases and isinstance(prod_aliases, list):
                    url = "https://" + prod_aliases[0]
                else:
                    url = prod.get("url", "")
                    if url and not url.startswith("http"):
                        url = "https://" + url
            # Override with custom domain if configured
            aliases = p.get("alias", [])
            if aliases:
                if isinstance(aliases[0], dict):
                    domain = aliases[0].get("domain", "")
                elif isinstance(aliases[0], str):
                    domain = aliases[0]
                else:
                    domain = ""
                if domain:
                    url = "https://" + domain

            updated = p.get("updatedAt", 0)
            if isinstance(updated, (int, float)) and updated > 1000000000000:
                updated = updated / 1000
            try:
                from datetime import datetime, timezone
                updated_str = datetime.fromtimestamp(updated, tz=timezone.utc).isoformat() if updated else ""
            except Exception:
                updated_str = ""

            deployments.append({
                "id": p.get("id", name),
                "name": name,
                "description": "",
                "url": url,
                "platform": "Vercel",
                "stack": p.get("framework", "static") or "static",
                "status": "live" if url else "staging",
                "repo": "",
                "server": "",
                "created_at": updated_str,
                "updated_at": updated_str,
            })

        _vercel_cache["ts"] = now
        _vercel_cache["data"] = deployments
        return deployments
    except Exception as e:
        print(f"[deployments] Vercel fetch error: {e}")
        return _vercel_cache.get("data", [])


async def api_deployments_list(request: Request) -> JSONResponse:
    """GET /api/deployments — auto-fetch from Vercel API + any manual entries."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # Fetch live Vercel projects
    vercel_deps = await _fetch_vercel_projects()

    # Also include any manually added deployments from settings
    settings = _load_settings()
    manual_deps = settings.get("deployments", [])

    all_deps = vercel_deps + manual_deps
    return JSONResponse({"deployments": all_deps, "total": len(all_deps)})


async def api_deployments_create(request: Request) -> JSONResponse:
    """POST /api/deployments — add a new deployment."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    now = datetime.now(timezone.utc).isoformat()
    deployment = {
        "id": secrets.token_hex(8),
        "name": body.get("name", ""),
        "description": body.get("description", ""),
        "url": body.get("url", ""),
        "platform": body.get("platform", ""),
        "stack": body.get("stack", ""),
        "status": body.get("status", "staging"),
        "repo": body.get("repo", ""),
        "server": body.get("server", ""),
        "created_at": now,
        "updated_at": now,
    }
    settings = _load_settings()
    if "deployments" not in settings:
        settings["deployments"] = list(_DEFAULT_DEPLOYMENTS)
    settings["deployments"].append(deployment)
    _save_settings(settings)
    return JSONResponse({"ok": True, "deployment": deployment})


async def api_deployments_update(request: Request) -> JSONResponse:
    """PUT /api/deployments/{dep_id} — update a deployment by id."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    dep_id = request.path_params["dep_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    settings = _load_settings()
    deployments = settings.get("deployments", [])
    for dep in deployments:
        if dep.get("id") == dep_id:
            # Update allowed fields, preserve id and created_at
            for key in ("name", "description", "url", "platform", "stack",
                        "status", "repo", "server"):
                if key in body:
                    dep[key] = body[key]
            dep["updated_at"] = datetime.now(timezone.utc).isoformat()
            _save_settings(settings)
            return JSONResponse({"ok": True, "deployment": dep})
    return JSONResponse({"error": "not found"}, status_code=404)


async def api_deployments_delete(request: Request) -> JSONResponse:
    """DELETE /api/deployments/{dep_id} — remove a deployment by id."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    dep_id = request.path_params["dep_id"]
    settings = _load_settings()
    deployments = settings.get("deployments", [])
    original_len = len(deployments)
    settings["deployments"] = [d for d in deployments if d.get("id") != dep_id]
    if len(settings["deployments"]) == original_len:
        return JSONResponse({"error": "not found"}, status_code=404)
    _save_settings(settings)
    return JSONResponse({"ok": True})


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
    """Create agents table and seed with default agent roster on first run."""
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

        # Seed default agent roster if empty
        cur = await db.execute("SELECT COUNT(*) FROM agents")
        row = await cur.fetchone()
        if row and row[0] == 0:
            await _seed_default_agents(db)
            await db.commit()
    print(f"[agents] SQLite DB ready: {AGENTS_DB}")


async def _seed_default_agents(db) -> None:
    """Seed the agents table from .claude/agents/ manifests."""
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
        "meeting-assistant":      ("Productivity & Operations", False),
        "email-drafter":          ("Productivity & Operations", False),
        "document-summarizer":    ("Productivity & Operations", False),
        "productivity-assistant": ("Productivity & Operations", True),
        "report-writer":          ("Productivity & Operations", False),
        "financial-assistant":    ("Productivity & Operations", False),
        "presentation-builder":   ("Productivity & Operations", False),
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
        "Productivity & Operations": "pipeline",
        "Other": "specialist",
    }

    agents_dir = Path(os.environ.get("CIV_ROOT", str(Path.home()))) / ".claude" / "agents"
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

    print(f"[agents] Seeded default agent roster from {agents_dir}")


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

    log_activity(f"Agent {agent_id} → {status}", task if task else "", "agent")
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
    civ_root = os.environ.get("CIV_ROOT", str(Path.home()))
    portal_dir = str(SCRIPT_DIR)
    tools_dir = str(Path(civ_root) / "tools")
    logs_dir = str(Path(civ_root) / "logs")

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

    # Inject into tmux session so the CIV sees it immediately
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
# Investor Chat & TTS — extracted to portal_investor.py
# ---------------------------------------------------------------------------
from portal_investor import (
    _INVESTOR_SYSTEM_PROMPT,
    api_investor_chat,
    api_investor_tts,
)


# ---------------------------------------------------------------------------
# Portal Update & Module Health — extracted to portal_updates.py
# ---------------------------------------------------------------------------
from portal_updates import (
    # Update state + lock
    _update_state, _update_lock, _get_update_lock,
    # Migration guidance
    _MIGRATION_GUIDANCE, _MIGRATION_GUIDANCE_DEFAULT, _check_tracked_modifications,
    # Module health
    PORTAL_MODULES, MODULE_BACKUP_DIR,
    _all_modules, _module_by_name, _check_module_health,
    _backup_modules, _restore_module, _restore_all_modules,
    api_health_mods, api_mods_health, api_mods_backup,
    api_mods_restore_single, api_mods_restore_all,
    # Git + version
    _git_cmd, _get_current_version,
    # Release server config
    RELEASE_SERVER_URL, PORTAL_UPDATE_TOKEN,
    # Update endpoints
    api_update_check, api_update_apply, api_update_apply_force,
    # Update runner internals
    _update_step, _UPDATE_LOG_DIR, _UPDATE_LOG_FILE, _log_update,
    _PRESERVED_FILES, _PRESERVED_DIRS, _run_release_update,
    # Status endpoint
    api_update_status,
)

try:
    from portal_activity import log_activity, api_activity
except ImportError:
    def log_activity(*a, **kw): pass
    async def api_activity(request):
        return JSONResponse({"activities": []})

from portal_constitution import (
    CONSTITUTION_FILE, CLAUDE_MD_SYNC_TARGET,
    _constitution_lock, _load_constitution, _save_constitution,
    _auto_sync_to_claude_md,
    AUDIT_LOG_FILE, _append_audit_log,
    api_constitution_audit_log,
    api_constitution_rules_list, api_constitution_rules_create,
    api_constitution_rules_update, api_constitution_rules_delete,
    api_constitution_governance_list, api_constitution_governance_update,
    api_constitution_sync,
    api_constitution_memory,
    api_constitution_overrides,
    sync_constitution_to_claude_md,
)

from portal_gdrive import (
    GDRIVE_TOKEN_FILE, GDRIVE_CLIENT_ID,
    GDRIVE_REDIRECT_URI,
    _gdrive_oauth_states, _gdrive_load_tokens, _gdrive_save_tokens,
    _gdrive_clear_tokens, _gdrive_ensure_token,
    _extract_subdomain,
    api_gdrive_status, api_gdrive_auth_url, api_gdrive_callback,
    api_gdrive_disconnect, api_gdrive_files, api_gdrive_download,
    api_gdrive_upload, api_gdrive_create_folder, api_gdrive_about,
)

from portal_777 import (
    _777_SYSTEM_PROMPTS, _777_RATE_LIMITS,
    _777_RATE_WINDOW, _777_RATE_MAX, _777_MAX_TURNS, _777_MAX_CHARS,
    api_777_chat,
)

from portal_tgim import (
    TGIM_BACKEND_URL, TGIM_SERVICE_KEY, TGIM_DEFAULT_USER_EMAIL,
    _tgim_headers, _tgim_upstream_url, api_tgim_proxy,
)

from portal_skills import (
    api_skills_list, api_skills_registry, api_skills_detail,
    api_skills_install, api_skills_uninstall,
)


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
        return JSONResponse({"error": _sanitize_error(e, "tmux diagnostics")}, status_code=500)


# ---------------------------------------------------------------------------
# Kanban To Do endpoints
# ---------------------------------------------------------------------------

TODO_TASKS_FILE = SCRIPT_DIR / "todo_tasks.json"

ACTION_KEYWORDS = [
    "please", "need", "fix", "update", "check", "review", "send", "create",
    "deploy", "build", "schedule", "follow up", "respond", "approve",
    "asap", "urgent", "required", "must", "should", "action required",
]


def _load_todo_tasks() -> list:
    if not TODO_TASKS_FILE.exists():
        return []
    try:
        data = json.loads(TODO_TASKS_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_todo_tasks(tasks: list) -> None:
    TODO_TASKS_FILE.write_text(json.dumps(tasks, indent=2))


def _extract_action_items(threads: list) -> list:
    """Scan email threads and return task dicts for action-requiring emails."""
    existing = _load_todo_tasks()
    existing_source_ids = {t.get("source_id") for t in existing}
    new_tasks = []
    for thread in threads:
        thread_id = thread.get("thread_id") or thread.get("id") or ""
        if thread_id in existing_source_ids:
            continue
        subject = thread.get("subject", "(no subject)") or "(no subject)"
        sender = ""
        senders = thread.get("senders") or []
        if senders:
            sender = senders[0] if isinstance(senders[0], str) else str(senders[0])
        preview = thread.get("preview") or thread.get("snippet") or ""
        # Check body from messages if available
        messages = thread.get("messages") or []
        body_text = preview
        if messages:
            msg = messages[0]
            body_text = msg.get("body") or msg.get("text") or msg.get("snippet") or preview
        # Strip HTML tags from body
        body_clean = re.sub(r'<[^>]+>', ' ', str(body_text)).strip()
        body_clean = re.sub(r'\s+', ' ', body_clean)[:2000]
        # Detect action keywords
        combined = (subject + " " + body_clean).lower()
        if any(kw in combined for kw in ACTION_KEYWORDS):
            # Extract first action sentence as title
            title = subject[:120] if subject else "(no subject)"
            task_id = "todo-" + secrets.token_hex(6)
            now = datetime.now(timezone.utc).isoformat()
            new_tasks.append({
                "id": task_id,
                "title": title,
                "description": body_clean,
                "status": "needs-approval",
                "source": "email",
                "source_id": thread_id,
                "from": sender,
                "subject": subject,
                "created_at": now,
                "updated_at": now,
            })
    return new_tasks


async def api_todo_tasks(request: Request) -> JSONResponse:
    """GET /api/todo/tasks — return all tasks; POST — create a manual task."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        title = (body.get("title") or "").strip()
        if not title:
            return JSONResponse({"error": "title required"}, status_code=400)
        task_id = "todo-" + secrets.token_hex(6)
        now = datetime.now(timezone.utc).isoformat()
        task = {
            "id": task_id,
            "title": title,
            "description": (body.get("description") or "").strip(),
            "status": "needs-approval",
            "source": "manual",
            "source_id": None,
            "from": None,
            "subject": None,
            "created_at": now,
            "updated_at": now,
        }
        tasks = _load_todo_tasks()
        tasks.append(task)
        _save_todo_tasks(tasks)
        return JSONResponse({"ok": True, "task": task})
    tasks = _load_todo_tasks()
    return JSONResponse({"tasks": tasks})


async def api_todo_task_update(request: Request) -> JSONResponse:
    """PUT /api/todo/tasks/{task_id} — update task status."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    task_id = request.path_params.get("task_id", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    tasks = _load_todo_tasks()
    for task in tasks:
        if task.get("id") == task_id:
            allowed = {"needs-approval", "pending", "in-progress", "completed"}
            new_status = body.get("status")
            if new_status and new_status in allowed:
                task["status"] = new_status
            # Allow updating title/description too
            if "title" in body:
                task["title"] = str(body["title"])[:200]
            if "description" in body:
                task["description"] = str(body["description"])[:5000]
            task["updated_at"] = datetime.now(timezone.utc).isoformat()
            _save_todo_tasks(tasks)
            return JSONResponse({"ok": True, "task": task})
    return JSONResponse({"error": "task not found"}, status_code=404)


async def api_todo_task_delete(request: Request) -> JSONResponse:
    """DELETE /api/todo/tasks/{task_id} — delete a task."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    task_id = request.path_params.get("task_id", "")
    tasks = _load_todo_tasks()
    new_tasks = [t for t in tasks if t.get("id") != task_id]
    if len(new_tasks) == len(tasks):
        return JSONResponse({"error": "task not found"}, status_code=404)
    _save_todo_tasks(new_tasks)
    return JSONResponse({"ok": True})


async def api_todo_scan_emails(request: Request) -> JSONResponse:
    """POST /api/todo/scan-emails — scan recent emails and extract action items."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # Fetch threads from the existing inbox API (reuse internal logic)
    account_idx = 0
    client, email = _get_email_client(account_idx)
    if not client:
        return JSONResponse({"error": "Email not configured", "created": 0})
    try:
        if isinstance(client, GmailClient):
            loop = asyncio.get_event_loop()
            threads = await loop.run_in_executor(None, lambda: client.list_threads(limit=50))
        else:
            result = client.inboxes.threads.list(email, limit=50)
            threads = [_thread_to_dict(t) for t in (result.threads or [])]
    except Exception as exc:
        return JSONResponse({"error": str(exc), "created": 0}, status_code=502)
    new_tasks = _extract_action_items(threads)
    if new_tasks:
        existing = _load_todo_tasks()
        existing.extend(new_tasks)
        _save_todo_tasks(existing)
    return JSONResponse({"ok": True, "created": len(new_tasks), "scanned": len(threads)})


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
        return JSONResponse({"error": _sanitize_error(e, "diagnostics")}, status_code=500)


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
        return JSONResponse({"error": _sanitize_error(e, "health check")}, status_code=500)


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

    # Update last_active in agents DB for any live agents we found
    if agents:
        now_iso = datetime.utcnow().isoformat()
        try:
            async with _agents_db() as db:
                for a in agents:
                    aid = a.get("id", "")
                    if aid:
                        await db.execute(
                            "UPDATE agents SET last_active=?, status='active' WHERE id=?",
                            (now_iso, aid),
                        )
                await db.commit()
        except Exception:
            pass  # best-effort

    return JSONResponse({"agents": agents})


# ── System Stats ──────────────────────────────────────────────────────────

async def api_system_stats(request: Request) -> JSONResponse:
    """GET /api/system/stats — real memory, CPU load, and disk usage."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # Memory from /proc/meminfo
    mem_total_gb = 0.0
    mem_used_gb = 0.0
    try:
        with open("/proc/meminfo", "r") as f:
            info = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])  # kB
            total_kb = info.get("MemTotal", 0)
            avail_kb = info.get("MemAvailable", info.get("MemFree", 0))
            mem_total_gb = round(total_kb / 1048576, 1)
            mem_used_gb = round((total_kb - avail_kb) / 1048576, 1)
    except Exception:
        pass

    # CPU load from /proc/loadavg
    cpu_load = 0.0
    try:
        with open("/proc/loadavg", "r") as f:
            cpu_load = float(f.read().split()[0])
    except Exception:
        pass

    # Disk usage
    disk_total_gb = 0.0
    disk_used_gb = 0.0
    try:
        usage = shutil.disk_usage("/")
        disk_total_gb = round(usage.total / (1024 ** 3), 1)
        disk_used_gb = round(usage.used / (1024 ** 3), 1)
    except Exception:
        pass

    return JSONResponse({
        "memory_used_gb": mem_used_gb,
        "memory_total_gb": mem_total_gb,
        "cpu_load": round(cpu_load, 2),
        "disk_used_gb": disk_used_gb,
        "disk_total_gb": disk_total_gb,
    })


# ── Integrations Status ──────────────────────────────────────────────────

async def api_integrations_status(request: Request) -> JSONResponse:
    """GET /api/integrations/status — check which APIs have credentials configured."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    settings = _load_settings()
    home = Path.home()

    def _has(key):
        return bool(settings.get(key, "").strip())

    def _env(key):
        return bool(os.environ.get(key, "").strip())

    def _file(path):
        return Path(path).exists()

    integrations = []

    # AgentMail
    am_ok = _has("agentmail_api_key") and _has("agentmail_email")
    am_accounts = settings.get("email_accounts", [])
    am_count = len(am_accounts) if am_accounts else (1 if am_ok else 0)
    integrations.append({
        "name": "AgentMail", "status": "active" if am_ok else "inactive",
        "desc": "Email inboxes for AI agents — send, receive, thread management",
        "detail": f"{am_count} inbox{'es' if am_count != 1 else ''}" if am_ok else "not configured",
    })

    # Telegram Bot
    tg_ok = _file(SCRIPT_DIR / "telegram_config.json")
    if tg_ok:
        try:
            tg_data = json.loads((SCRIPT_DIR / "telegram_config.json").read_text())
            tg_ok = bool(tg_data.get("bot_token", "").strip())
        except Exception:
            tg_ok = False
    integrations.append({
        "name": "Telegram Bot", "status": "active" if tg_ok else "inactive",
        "desc": "Push notifications, message forwarding, voice messages",
        "detail": "connected" if tg_ok else "not configured",
    })

    # Command Center
    cc_ok = _has("cc_civ_key") or _has("cc_url")
    integrations.append({
        "name": "Command Center", "status": "active" if cc_ok else "inactive",
        "desc": "PureBrain Command Center — tasks, chat, coordination",
        "detail": "connected" if cc_ok else "not configured",
    })

    # Google Drive
    gd_ok = _file(SCRIPT_DIR / ".gdrive-tokens.json") or _file(home / ".gdrive-tokens.json")
    integrations.append({
        "name": "Google Drive", "status": "active" if gd_ok else "inactive",
        "desc": "File storage, LinkedIn drafts, export delivery",
        "detail": "OAuth token" if gd_ok else "not configured",
    })

    # Google Gemini (image gen)
    gem_ok = _env("GOOGLE_API_KEY")
    integrations.append({
        "name": "Google Gemini", "status": "active" if gem_ok else "inactive",
        "desc": "Image generation — blog banners, LinkedIn graphics, social media",
        "detail": "API key set" if gem_ok else "not configured",
    })

    # Bluesky
    bsky_ok = _env("BSKY_USERNAME") and _env("BSKY_PASSWORD")
    integrations.append({
        "name": "Bluesky", "status": "active" if bsky_ok else "inactive",
        "desc": "Social media posting, engagement, threads",
        "detail": os.environ.get("BSKY_USERNAME", "") if bsky_ok else "not configured",
    })

    # PayPal
    pp_ok = _has("paypal_client_id") or _env("PAYPAL_CLIENT_ID")
    integrations.append({
        "name": "PayPal", "status": "active" if pp_ok else "inactive",
        "desc": "Payment processing, subscriptions, webhooks",
        "detail": "connected" if pp_ok else "not configured",
    })

    # Supabase
    supa_ok = _env("SUPABASE_URL") and _env("SUPABASE_KEY")
    integrations.append({
        "name": "Supabase", "status": "active" if supa_ok else "inactive",
        "desc": "PostgreSQL database, auth, RLS",
        "detail": "connected" if supa_ok else "not configured",
    })

    # Cloudflare
    cf_ok = _env("CLOUDFLARE_API_TOKEN") or _env("CF_API_TOKEN")
    integrations.append({
        "name": "Cloudflare", "status": "active" if cf_ok else "inactive",
        "desc": "DNS management, Workers, Pages, WAF, CDN",
        "detail": "API token set" if cf_ok else "not configured",
    })

    # Vercel
    vc_ok = _env("VERCEL_TOKEN")
    integrations.append({
        "name": "Vercel", "status": "active" if vc_ok else "inactive",
        "desc": "Deployment platform — build, deploy, and host web projects",
        "detail": "token set" if vc_ok else "not configured",
    })

    active_count = sum(1 for i in integrations if i["status"] == "active")
    return JSONResponse({"integrations": integrations, "active": active_count, "total": len(integrations)})


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
        model = _detect_session_model()
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
        return JSONResponse({"error": _sanitize_error(e, "config read")}, status_code=500)


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
        model = _detect_session_model()
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
        return JSONResponse({"error": _sanitize_error(e, "config write")}, status_code=500)


# ── CC Bridge Status ──────────────────────────────────────────────────────

async def api_cc_status(request: Request) -> JSONResponse:
    """GET /api/cc/status — check if Command Center bridge is reachable."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        import urllib.request
        req = urllib.request.Request("https://cc.purebrain.ai/health", method="GET")
        req.add_header("User-Agent", "PureBrain-Portal/2.0")
        with urllib.request.urlopen(req, timeout=3) as resp:
            available = resp.status == 200
    except Exception:
        available = False
    return JSONResponse({"available": available, "bridge_loaded": _cc_bridge_loaded})


# ── Notification Queue ──────────────────────────────────────────────────

def _get_notifications() -> list:
    """Return the notification list from user-settings.json."""
    settings = _load_settings()
    return settings.get("notifications_list", [])

def _add_notification(title: str, body: str, category: str = "system", link: str = "") -> dict:
    """Add a notification. Categories: email, cc, task, system."""
    settings = _load_settings()
    notifs = settings.get("notifications_list", [])
    notif = {
        "id": secrets.token_hex(6),
        "title": title,
        "body": body,
        "category": category,
        "link": link,
        "read": False,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    notifs.insert(0, notif)
    notifs = notifs[:50]  # Keep max 50
    settings["notifications_list"] = notifs
    _save_settings(settings)
    return notif

async def api_notifications(request: Request) -> JSONResponse:
    """GET /api/notifications — list notifications.
       POST /api/notifications — mark read or create."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if request.method == "GET":
        notifs = _get_notifications()
        # Auto-prune notifications older than 7 days
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        fresh = [n for n in notifs if n.get("timestamp", "") >= cutoff]
        if len(fresh) < len(notifs):
            settings = _load_settings()
            settings["notifications_list"] = fresh
            _save_settings(settings)
            notifs = fresh
        unread = len([n for n in notifs if not n["read"]])
        return JSONResponse({"notifications": notifs, "unread": unread})

    # POST — mark read or create
    try:
        body = await request.json()
    except Exception:
        body = {}

    action = body.get("action", "read")

    if action == "clear":
        settings = _load_settings()
        settings["notifications_list"] = []
        _save_settings(settings)
        return JSONResponse({"ok": True, "unread": 0})

    if action == "create":
        title = body.get("title", "")
        body_text = body.get("body", "")
        category = body.get("category", "system")
        link = body.get("link", "")
        if title:
            _add_notification(title, body_text, category, link)
        notifs = _get_notifications()
        unread = len([n for n in notifs if not n["read"]])
        return JSONResponse({"ok": True, "unread": unread})

    # Default: mark read
    settings = _load_settings()
    notifs = settings.get("notifications_list", [])

    notif_id = body.get("id")
    if notif_id == "all":
        for n in notifs:
            n["read"] = True
    elif notif_id:
        for n in notifs:
            if n["id"] == notif_id:
                n["read"] = True
                break

    settings["notifications_list"] = notifs
    _save_settings(settings)
    unread = len([n for n in notifs if not n["read"]])
    return JSONResponse({"ok": True, "unread": unread})


# ── Email notification endpoints (task assignment, meeting invites) ──

async def api_notification_send_email(request: Request) -> JSONResponse:
    """POST /api/notifications/send-email -- Send an email notification.

    Body: {"to": "email@example.com", "subject": "...", "body": "...", "cc": "optional"}

    Used by CC webhooks for task assignment and meeting invite notifications.
    CIV-agnostic: uses whatever email provider is configured.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        data = await request.json()
        to = data.get("to")
        subject = data.get("subject")
        body = data.get("body")
        cc = data.get("cc")

        if not to or not subject or not body:
            return JSONResponse({"error": "to, subject, and body are required"}, status_code=400)

        result = _send_email_notification(to, subject, body, cc)
        if result.get("ok"):
            log_activity("Email notification sent", f"To: {to}, Subject: {subject}", "email")
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": _sanitize_error(e, "send email")}, status_code=500)


async def api_notification_task_assigned(request: Request) -> JSONResponse:
    """POST /api/notifications/task-assigned -- Notify assignee via email.

    Body: {
        "assignee_email": "person@example.com",
        "assignee_name": "John",
        "task_title": "Review Q3 report",
        "task_description": "Please review the quarterly report",
        "assigned_by": "Alex",
        "task_url": "https://app.purebrain.ai/tasks/123"  (optional)
    }
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        data = await request.json()
        assignee_email = data.get("assignee_email")
        assignee_name = data.get("assignee_name", "")
        task_title = data.get("task_title", "New Task")
        task_desc = data.get("task_description", "")
        assigned_by = data.get("assigned_by", "")
        task_url = data.get("task_url", "")

        if not assignee_email:
            return JSONResponse({"error": "assignee_email is required"}, status_code=400)

        subject = f"Task Assigned: {task_title}"
        body_lines = [
            f"Hi {assignee_name}," if assignee_name else "Hi,",
            "",
            f"You have been assigned a new task: {task_title}",
            "",
        ]
        if task_desc:
            body_lines.append(f"Description: {task_desc}")
            body_lines.append("")
        if assigned_by:
            body_lines.append(f"Assigned by: {assigned_by}")
        if task_url:
            body_lines.append(f"View task: {task_url}")
        body_lines.extend(["", "-- PureBrain Portal"])

        body = "\n".join(body_lines)
        result = _send_email_notification(assignee_email, subject, body)

        if result.get("ok"):
            _add_notification(
                f"Task '{task_title}' assigned to {assignee_name or assignee_email}",
                f"Assigned by {assigned_by}" if assigned_by else "Task assignment sent",
                "task",
            )
            log_activity("Task assignment email sent", f"To: {assignee_email}, Task: {task_title}", "task")

        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": _sanitize_error(e, "task notify")}, status_code=500)


async def api_notification_meeting_invite(request: Request) -> JSONResponse:
    """POST /api/notifications/meeting-invite -- Notify attendees via email.

    Body: {
        "attendees": [{"email": "a@b.com", "name": "Alice"}, ...],
        "meeting_title": "Team Standup",
        "meeting_time": "2026-06-16 10:00 AM ET",
        "meeting_description": "Weekly sync",
        "organizer": "Alex",
        "meeting_url": "https://zoom.us/j/123"  (optional)
    }
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        data = await request.json()
        attendees = data.get("attendees", [])
        title = data.get("meeting_title", "Meeting")
        meeting_time = data.get("meeting_time", "")
        desc = data.get("meeting_description", "")
        organizer = data.get("organizer", "")
        meeting_url = data.get("meeting_url", "")

        if not attendees:
            return JSONResponse({"error": "attendees list is required"}, status_code=400)

        subject = f"Meeting Invite: {title}"
        results = []

        for attendee in attendees:
            email = attendee.get("email")
            name = attendee.get("name", "")
            if not email:
                continue

            body_lines = [
                f"Hi {name}," if name else "Hi,",
                "",
                f"You are invited to: {title}",
                "",
            ]
            if meeting_time:
                body_lines.append(f"When: {meeting_time}")
            if desc:
                body_lines.append(f"Details: {desc}")
            if organizer:
                body_lines.append(f"Organizer: {organizer}")
            if meeting_url:
                body_lines.append(f"Join: {meeting_url}")
            body_lines.extend(["", "-- PureBrain Portal"])

            body = "\n".join(body_lines)
            r = _send_email_notification(email, subject, body)
            results.append({"email": email, **r})

        sent = sum(1 for r in results if r.get("ok"))
        if sent > 0:
            _add_notification(
                f"Meeting '{title}' -- {sent} invite(s) sent",
                f"Organized by {organizer}" if organizer else f"{sent} invites sent",
                "email",
            )
            log_activity("Meeting invites sent", f"{sent}/{len(results)} emails for '{title}'", "email")

        return JSONResponse({"results": results, "sent": sent, "total": len(results)})
    except Exception as e:
        return JSONResponse({"error": _sanitize_error(e, "meeting notify")}, status_code=500)


async def api_cc_proxy(request: Request) -> Response:
    """Proxy CC API requests to avoid CORS issues.
    Routes /api/cc/proxy/{path} → https://cc.purebrain.ai/api/{path}
    Uses the CIV key from portal settings for auth."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # Get CC CIV key: try portal_settings.json first (stable), then user-settings.json
    cc_key = ""
    try:
        _ps = json.loads((SCRIPT_DIR / "portal_settings.json").read_text())
        cc_key = _ps.get("cc_civ_key", "")
    except Exception:
        pass
    if not cc_key or ":" not in cc_key:
        settings = _load_settings()
        cc_key = settings.get("cc_civ_key", "")
    if not cc_key or ":" not in cc_key:
        return JSONResponse({"error": "CC CIV key not configured (need Name:Key format)"}, status_code=400)

    # Build target URL
    path = request.path_params.get("path", "")
    query = str(request.url.query)
    target = f"https://cc.purebrain.ai/{path}"
    if query:
        target += f"?{query}"

    try:
        import httpx
        headers = {"X-CIV-Key": cc_key, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=15) as client:
            if request.method == "GET":
                resp = await client.get(target, headers=headers)
            elif request.method == "POST":
                body = await request.body()
                resp = await client.post(target, headers=headers, content=body)
            elif request.method == "PUT":
                body = await request.body()
                resp = await client.put(target, headers=headers, content=body)
            else:
                return JSONResponse({"error": "method not allowed"}, status_code=405)

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )
    except Exception as e:
        return JSONResponse({"error": f"CC proxy error: {_sanitize_error(e, 'cc proxy')}"}, status_code=502)


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

# 0. Read custom config
_custom_cfg: dict = {}
if _CUSTOM_CONFIG_FILE.exists():
    try:
        _custom_cfg = json.loads(_CUSTOM_CONFIG_FILE.read_text())
    except Exception as _e:
        print(f"[portal-custom] WARNING: config.json load failed: {_e}")

# 1. Config overrides (allowlisted globals only)
for _k, _v in _custom_cfg.items():
    if _k not in _ALLOWED_CONFIG_OVERRIDES:
        print(f"[portal-custom] WARNING: config override blocked for key '{_k}' (not in allowlist)")
        continue
    if _k in globals():
        globals()[_k] = _v
        print(f"[portal-custom] Config override: {_k} = {_v}")


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

# 2b. Endpoint extensions (extend upstream responses from custom/routes.py)
_endpoint_extensions: dict = {}
try:
    if hasattr(_mod, "endpoint_extensions"):
        _endpoint_extensions = _mod.endpoint_extensions
        print(f"[portal-custom] Loaded {len(_endpoint_extensions)} endpoint extension(s)")
except NameError:
    pass  # _mod was not defined (routes.py didn't load or doesn't exist)
except Exception as _e:
    print(f"[portal-custom] WARNING: endpoint_extensions load failed: {_e}")

# 3. Custom startup hooks (CC Bridge: tracked file preferred over custom/startup.py)
_custom_startup_hooks: list = []
_cc_bridge_loaded = False

# Try new tracked location first (auto-updates via git pull)
_cc_bridge_file = SCRIPT_DIR / "cc_bridge.py"
if _cc_bridge_file.exists():
    try:
        import importlib.util as _importlib_util
        _spec_bridge = _importlib_util.spec_from_file_location("cc_bridge", str(_cc_bridge_file))
        _mod_bridge = _importlib_util.module_from_spec(_spec_bridge)
        _spec_bridge.loader.exec_module(_mod_bridge)
        if hasattr(_mod_bridge, "on_startup"):
            _custom_startup_hooks.append(_mod_bridge.on_startup)
            _cc_bridge_loaded = True
            print("[CC Bridge] Loaded from cc_bridge.py (auto-updates via git pull)")
    except Exception as _e:
        print(f"[CC Bridge] ERROR loading cc_bridge.py: {_e}")

# Check for deprecated custom/startup.py
_custom_startup_file = _CUSTOM_DIR / "startup.py"
if _custom_startup_file.exists():
    if _cc_bridge_loaded:
        print("[CC Bridge] WARNING: Found custom/startup.py but using cc_bridge.py instead.")
        print("[CC Bridge] Delete custom/startup.py -- it is no longer needed.")
    else:
        # Fallback to old location with deprecation warning
        print("[CC Bridge] WARNING: custom/startup.py is DEPRECATED.")
        print("[CC Bridge] It will still work, but won't receive updates via git pull.")
        print("[CC Bridge] To fix: delete custom/startup.py -- the built-in cc_bridge.py auto-configures from your identity file.")
        try:
            import importlib.util as _importlib_util
            _spec2 = _importlib_util.spec_from_file_location("custom_startup", str(_custom_startup_file))
            _mod2 = _importlib_util.module_from_spec(_spec2)
            _spec2.loader.exec_module(_mod2)
            if hasattr(_mod2, "on_startup"):
                _custom_startup_hooks.append(_mod2.on_startup)
                print("[portal-custom] Loaded custom startup hook (deprecated location)")
        except Exception as _e:
            print(f"[portal-custom] WARNING: startup.py load failed: {_e}")

# ─── END CUSTOMIZATION LAYER ──────────────────────────────────────────


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
    Route("/api/gateway/status", endpoint=api_gateway_status),
    Route("/api/release-notes", endpoint=api_release_notes),
    Route("/api/chat/history", endpoint=api_chat_history),
    Route("/api/chat/topics", endpoint=api_chat_topics),
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
    Route("/api/files", endpoint=api_files_delete, methods=["DELETE"]),
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
    Route("/api/boops/active", endpoint=api_boops_active),
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
    Route("/api/profile", endpoint=api_profile, methods=["GET", "POST"]),
    Route("/api/notifications", endpoint=api_notifications, methods=["GET", "POST"]),
    Route("/api/notifications/send-email", endpoint=api_notification_send_email, methods=["POST"]),
    Route("/api/notifications/task-assigned", endpoint=api_notification_task_assigned, methods=["POST"]),
    Route("/api/notifications/meeting-invite", endpoint=api_notification_meeting_invite, methods=["POST"]),
    # ── AgentMail Inbox routes ──
    Route("/api/inbox/status", endpoint=api_inbox_status),
    Route("/api/inbox/threads", endpoint=api_inbox_threads),
    Route("/api/inbox/threads/{thread_id}", endpoint=api_inbox_thread_detail),
    Route("/api/inbox/threads/{thread_id}/read", endpoint=api_inbox_mark_read, methods=["POST"]),
    Route("/api/inbox/mark-all-read", endpoint=api_inbox_mark_all_read, methods=["POST"]),
    Route("/api/inbox/send", endpoint=api_inbox_send, methods=["POST"]),
    Route("/api/inbox/reply/{message_id}", endpoint=api_inbox_reply, methods=["POST"]),
    Route("/api/inbox/accounts", endpoint=api_inbox_accounts, methods=["POST", "DELETE"]),
    # ── Kanban To Do routes ──
    Route("/api/todo/tasks", endpoint=api_todo_tasks),
    Route("/api/todo/tasks", endpoint=api_todo_tasks, methods=["POST"]),
    Route("/api/todo/tasks/{task_id}", endpoint=api_todo_task_update, methods=["PUT"]),
    Route("/api/todo/tasks/{task_id}", endpoint=api_todo_task_delete, methods=["DELETE"]),
    Route("/api/todo/scan-emails", endpoint=api_todo_scan_emails, methods=["POST"]),
    Route("/api/bookmarks", endpoint=api_bookmarks, methods=["GET", "POST"]),
    Route("/api/deployments", endpoint=api_deployments_list, methods=["GET"]),
    Route("/api/deployments", endpoint=api_deployments_create, methods=["POST"]),
    Route("/api/deployments/{dep_id}", endpoint=api_deployments_update, methods=["PUT"]),
    Route("/api/deployments/{dep_id}", endpoint=api_deployments_delete, methods=["DELETE"]),
    Route("/api/health/mods", endpoint=api_health_mods),
    Route("/api/mods/health", endpoint=api_mods_health),
    Route("/api/mods/backup", endpoint=api_mods_backup, methods=["POST"]),
    Route("/api/mods/restore-all", endpoint=api_mods_restore_all, methods=["POST"]),
    Route("/api/mods/restore/{module_name}", endpoint=api_mods_restore_single, methods=["POST"]),
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
    Route("/api/activity", endpoint=api_activity),
    Route("/api/system/stats", endpoint=api_system_stats),
    Route("/api/integrations/status", endpoint=api_integrations_status),
    Route("/api/cc/status", endpoint=api_cc_status),
    Route("/api/cc/proxy/{path:path}", endpoint=api_cc_proxy, methods=["GET", "POST", "PUT"]),
    Route("/api/debug/report", endpoint=api_hub_debug_report, methods=["POST"]),
    Route("/api/continue", endpoint=api_hub_continue, methods=["POST"]),
    Route("/api/restart", endpoint=api_hub_restart, methods=["POST"]),
    Route("/api/tgim/{path:path}", endpoint=api_tgim_proxy, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
    # ── Constitution Tab routes ──
    Route("/api/constitution/audit-log", endpoint=api_constitution_audit_log),
    Route("/api/constitution/rules", endpoint=api_constitution_rules_create, methods=["POST"]),
    Route("/api/constitution/rules/{id}", endpoint=api_constitution_rules_update, methods=["PUT"]),
    Route("/api/constitution/rules/{id}", endpoint=api_constitution_rules_delete, methods=["DELETE"]),
    Route("/api/constitution/rules", endpoint=api_constitution_rules_list),
    Route("/api/constitution/governance/{id}", endpoint=api_constitution_governance_update, methods=["PUT"]),
    Route("/api/constitution/governance", endpoint=api_constitution_governance_list),
    Route("/api/constitution/sync", endpoint=api_constitution_sync, methods=["POST"]),
    Route("/api/constitution/memory", endpoint=api_constitution_memory),
    Route("/api/constitution/overrides", endpoint=api_constitution_overrides),
    # ── Google Drive routes ──
    Route("/api/gdrive/status", endpoint=api_gdrive_status),
    Route("/api/gdrive/auth-url", endpoint=api_gdrive_auth_url),
    Route("/api/gdrive/callback", endpoint=api_gdrive_callback),
    Route("/api/gdrive/disconnect", endpoint=api_gdrive_disconnect, methods=["POST"]),
    Route("/api/gdrive/files", endpoint=api_gdrive_files),
    Route("/api/gdrive/download/{file_id}", endpoint=api_gdrive_download),
    Route("/api/gdrive/upload", endpoint=api_gdrive_upload, methods=["POST"]),
    Route("/api/gdrive/create-folder", endpoint=api_gdrive_create_folder, methods=["POST"]),
    Route("/api/gdrive/about", endpoint=api_gdrive_about),
    # ── Skills Shop routes ──
    Route("/api/skills", endpoint=api_skills_list),
    Route("/api/skills/registry", endpoint=api_skills_registry),
    Route("/api/skills/install", endpoint=api_skills_install, methods=["POST"]),
    Route("/api/skills/uninstall", endpoint=api_skills_uninstall, methods=["POST"]),
    Route("/api/skills/detail/{name:path}", endpoint=api_skills_detail),
    WebSocketRoute("/ws/chat", endpoint=ws_chat),
    WebSocketRoute("/ws/terminal", endpoint=ws_terminal),
    *_custom_routes,   # Flux overlay: custom routes from custom/routes.py
]

# ─── Apply endpoint extensions (wrap upstream handlers) ───────────────
if _endpoint_extensions:
    import functools as _functools

    def _make_extended_endpoint(_orig_endpoint, _ext_fn):
        """Create a wrapper that calls the original, then merges extension data."""
        @_functools.wraps(_orig_endpoint)
        async def _wrapped(request: Request) -> Response:
            original_response = await _orig_endpoint(request)
            # Only extend JSON responses
            if not isinstance(original_response, JSONResponse):
                return original_response
            try:
                original_data = json.loads(original_response.body.decode("utf-8"))
                extra_data = await _ext_fn(original_data)
                if extra_data and isinstance(extra_data, dict):
                    original_data.update(extra_data)
                # Strip content-length so JSONResponse recalculates it
                # from the (potentially larger) merged body.
                _fwd_headers = {
                    k: v for k, v in original_response.headers.items()
                    if k.lower() != "content-length"
                }
                return JSONResponse(
                    original_data,
                    status_code=original_response.status_code,
                    headers=_fwd_headers,
                )
            except Exception as _ext_err:
                print(f"[portal-custom] WARNING: endpoint extension failed for {_ext_fn.__name__}: {_ext_err}")
                return original_response
        return _wrapped

    _ext_applied = 0
    for _i, _route in enumerate(routes):
        if isinstance(_route, Route) and _route.path in _endpoint_extensions:
            _ext_fn = _endpoint_extensions[_route.path]
            _orig = _route.endpoint
            routes[_i] = Route(
                _route.path,
                endpoint=_make_extended_endpoint(_orig, _ext_fn),
                methods=_route.methods,
            )
            _ext_applied += 1
            print(f"[portal-custom] Extended endpoint: {_route.path}")
    if _ext_applied:
        print(f"[portal-custom] Applied {_ext_applied} endpoint extension(s)")
# ─── End endpoint extensions ──────────────────────────────────────────

# Build middleware stack
_app_middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["https://purebrain.ai", "https://www.purebrain.ai", "https://app.purebrain.ai", "https://777-command-center.vercel.app"],
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Affiliate-Session"],
    ),
]

app = Starlette(
    routes=routes,
    on_startup=[_startup],
    middleware=_app_middleware,
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
