#!/usr/bin/env python3
"""PureBrain Portal Server — per-CIV mini server for purebrain.ai
Auth via Bearer token. JSONL-based chat history (same as TG bot).
"""
import asyncio
import hashlib
import json
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
import httpx
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiosqlite

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
TOKEN_FILE = SCRIPT_DIR / ".portal-token"
PORTAL_HTML = SCRIPT_DIR / "portal.html"
PORTAL_PB_HTML = SCRIPT_DIR / "portal-pb-styled.html"
REACT_DIST = SCRIPT_DIR / "react-portal" / "dist"
START_TIME = time.time()
PORTAL_VERSION = "1.0.1"
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

if TOKEN_FILE.exists():
    BEARER_TOKEN = TOKEN_FILE.read_text().strip()
else:
    BEARER_TOKEN = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(BEARER_TOKEN)
    TOKEN_FILE.chmod(0o600)
    print(f"[portal] Generated new bearer token: {BEARER_TOKEN}")

# ─── Affiliate login rate-limiting (in-memory, resets on restart) ───────────
# { ip_hash: {"count": N, "window_start": epoch_float} }
_AFFILIATE_LOGIN_ATTEMPTS: dict = {}
_LOGIN_MAX_ATTEMPTS = 10          # per window
_LOGIN_WINDOW_SECS  = 900         # 15 minutes

# ─── Affiliate session tokens: { token: {"code": ..., "expires": epoch} } ───
_AFFILIATE_SESSIONS: dict = {}
_SESSION_TTL_SECS = 86400 * 7     # 7 days

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
    This is the ONLY way subprocess should be called from async code."""
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd, timeout=timeout, check=check,
                    stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                )
            ),
            timeout=timeout + 2  # extra 2s for executor overhead
        )
    except (asyncio.TimeoutError, subprocess.TimeoutExpired,
            subprocess.CalledProcessError, Exception):
        return None


async def _run_subprocess_output(cmd, timeout=5):
    """Run subprocess and capture output without blocking the event loop."""
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd, timeout=timeout, capture_output=True,
                    text=True, check=False,
                )
            ),
            timeout=timeout + 2
        )
        return result.stdout if result and result.returncode == 0 else ""
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
    if text.startswith("[PORTAL] "):
        return text[9:]
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
    for log_path in _get_all_session_log_paths(max_files=1):
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

    return deduped[-last_n:] if len(deduped) > last_n else deduped


def check_auth(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:] == BEARER_TOKEN
    return request.query_params.get("token") == BEARER_TOKEN


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
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "civ": CIV_NAME, "uptime": int(time.time() - START_TIME)})


async def index(request: Request) -> Response:
    if PORTAL_PB_HTML.exists():
        resp = FileResponse(str(PORTAL_PB_HTML), media_type="text/html")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp
    if PORTAL_HTML.exists():
        return FileResponse(str(PORTAL_HTML), media_type="text/html")
    return Response("<h1>Portal HTML not found</h1>", media_type="text/html", status_code=503)


async def index_pb(request: Request) -> Response:
    """Serve PureBrain-styled portal at /pb path."""
    if PORTAL_PB_HTML.exists():
        resp = FileResponse(str(PORTAL_PB_HTML), media_type="text/html")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp
    return Response("<h1>PB Portal not found</h1>", media_type="text/html", status_code=503)


async def index_react(request: Request) -> Response:
    """Serve React portal at /react path."""
    react_index = REACT_DIST / "index.html"
    if react_index.exists():
        return FileResponse(str(react_index), media_type="text/html")
    return Response("<h1>React Portal not found — run npm run build in react-portal/</h1>",
                    media_type="text/html", status_code=503)


async def api_status(request: Request) -> JSONResponse:
    if not check_auth(request):
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
    if not check_auth(request):
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
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        message = str(body.get("message", "")).strip()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    # Save to portal chat log for history
    _save_portal_message(message, role="user")

    # Tag injection source so tmux pane shows where input came from
    host = request.headers.get("referer", "")
    if "react" in host:
        tagged = f"[portal-react] {message}"
    else:
        tagged = f"[portal] {message}"

    session = get_tmux_session()
    try:
        # Leading newline clears any partial input in buffer
        # All subprocess calls use async wrapper to avoid blocking the event loop
        r = await _run_subprocess_async(["tmux", "send-keys", "-t", session, "-l", f"\n{tagged}"], check=True)
        if r is None:
            return JSONResponse({"error": "tmux send-keys timed out"}, status_code=500)
        await _run_subprocess_async(["tmux", "send-keys", "-t", session, "Enter"], check=True)
        # 5x Enter retries (matches Telegram bridge pattern) — ensures Claude
        # processes the message even if busy with tool calls or generation
        async def _retry_enters():
            for _ in range(5):
                await asyncio.sleep(0.5)
                await _run_subprocess_async(["tmux", "send-keys", "-t", session, "Enter"])
        asyncio.ensure_future(_retry_enters())
        return JSONResponse({"status": "sent", "timestamp": int(time.time())})
    except Exception as e:
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
    if not check_auth(request):
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

        # Inject notification into AI's tmux session (mirrors Telegram bridge pattern)
        # CRITICAL: Must be SINGLE LINE — multi-line paste triggers Claude Code's
        # "Pasted text" confirmation prompt and blocks automatic processing.
        notify_parts = [f"[Portal Upload from {HUMAN_NAME}] File saved to: {portal_copy_path}"]
        if caption:
            notify_parts.append(f"INSTRUCTIONS from {HUMAN_NAME}: {caption}")
        if is_image:
            notify_parts.append(f"[Image: {original_name} — USE Read tool on {portal_copy_path} TO VIEW]")
        notification = " ".join(notify_parts)

        session = get_tmux_session()
        tmux_ok = False
        try:
            # Leading newline clears any partial input in buffer — async to avoid blocking event loop
            await _run_subprocess_async(
                ["tmux", "send-keys", "-t", session, "-l", f"\n{notification}"],
                timeout=5, check=True,
            )
            await _run_subprocess_async(
                ["tmux", "send-keys", "-t", session, "Enter"],
                timeout=5, check=True,
            )
            tmux_ok = True
            # 5x Enter retries — ensures Claude processes even if busy
            async def _retry_enters():
                for _ in range(5):
                    await asyncio.sleep(0.5)
                    await _run_subprocess_async(["tmux", "send-keys", "-t", session, "Enter"])
            asyncio.ensure_future(_retry_enters())
        except Exception:
            pass  # Don't fail the upload if tmux injection fails

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
    Result cached for 10s to avoid subprocess calls on every poll."""
    global _pane_cache
    now = time.time()
    if now - _pane_cache[0] < _PANE_CACHE_TTL and _pane_cache[1]:
        return _pane_cache[1]
    session = get_tmux_session()
    try:
        # List all panes with their IDs
        out = subprocess.check_output(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_id}"],
            stderr=subprocess.DEVNULL, text=True, timeout=3
        )
        panes = [p.strip() for p in out.splitlines() if p.strip()]
        if not panes:
            _pane_cache = (now, session)
            return session
        _pane_cache = (now, panes[0])
        return panes[0]
    except Exception:
        _pane_cache = (now, session)
        return session


async def _find_primary_pane_async():
    """Async version of _find_primary_pane — use from async functions."""
    global _pane_cache
    now = time.time()
    if now - _pane_cache[0] < _PANE_CACHE_TTL and _pane_cache[1]:
        return _pane_cache[1]
    session = get_tmux_session()
    out = await _run_subprocess_output(
        ["tmux", "list-panes", "-t", session, "-F", "#{pane_id}"], timeout=3
    )
    panes = [p.strip() for p in out.splitlines() if p.strip()] if out else []
    if not panes:
        _pane_cache = (now, session)
        return session
    _pane_cache = (now, panes[0])
    return panes[0]


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
        MAX_TOKENS = 170_000  # ~30k reserved for responses/summaries
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
        await loop.run_in_executor(None, lambda: subprocess.Popen(
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


async def api_claude_auth_start(request: Request) -> JSONResponse:
    """Inject /login into the Claude tmux session to start OAuth flow."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global _captured_oauth_url
    _captured_oauth_url = None
    pane = await _find_primary_pane_async()
    _save_portal_message(f"Auth flow started — injecting /login into {get_tmux_session()}", role="assistant")
    try:
        await _run_subprocess_async(["tmux", "resize-window", "-t", pane, "-x", "500"])
        await asyncio.sleep(0.3)
        r = await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "-l", "/login"], check=True)
        if r is None:
            return JSONResponse({"error": "tmux send-keys timed out"}, status_code=500)
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        await asyncio.sleep(2)
        await _run_subprocess_async(["tmux", "send-keys", "-t", pane, "Enter"])
        _save_portal_message("/login sent — waiting for OAuth URL to appear in terminal...", role="assistant")
        return JSONResponse({"started": True})
    except Exception as e:
        _save_portal_message(f"Auth start failed: tmux error — pane={pane}, err={e}", role="assistant")
        return JSONResponse({"error": f"tmux error: {e}"}, status_code=500)


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

                # Extract thinking blocks (skip tool_use/tool_result, keep thinking even when tools present)
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "thinking":
                        continue
                    text = block.get("thinking", "").strip()
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


BOOP_STATE_FILE = Path("/home/jared/projects/AI-CIV/aether/.claude/scheduled-tasks-state.json")

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
        origin.endswith(".vercel.app") or origin == "https://777-command-center.vercel.app"
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

    # Rate limit by IP
    ip = request.headers.get("x-forwarded-for", request.client.host or "unknown").split(",")[0].strip()
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
        env_path = Path("/home/jared/projects/AI-CIV/aether/.env")
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
    return JSONResponse({"tasks": _scheduled_tasks})


async def api_delete_scheduled_task(request) -> JSONResponse:
    """DELETE /api/scheduled-tasks/{task_id} — cancel a pending scheduled task."""
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
    _init_portal_log_ids()
    await _init_referral_db()
    await _init_clients_db()
    await _init_agents_db()
    asyncio.create_task(_thinking_monitor_loop())
    asyncio.create_task(_trim_portal_log_periodically())
    asyncio.create_task(_scheduled_task_checker())
    _load_scheduled_tasks()


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
        await db.commit()
    print(f"[referral] SQLite DB ready: {REFERRALS_DB}")


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
    """SHA-256 hash of password+salt. Returns hex digest."""
    if not salt:
        salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"{salt}:{h}"


def _verify_affiliate_password(password: str, stored_hash: str) -> bool:
    """Verify password against stored salt:hash string."""
    if not stored_hash or ":" not in stored_hash:
        return False
    parts = stored_hash.split(":", 1)
    if len(parts) != 2:
        return False
    salt, _ = parts
    return _hash_affiliate_password(password, salt) == stored_hash


# ── Password reset tokens (in-memory, expire after 1 hour) ──────────────────
_password_reset_tokens: dict = {}  # token -> {"email": str, "expires": float}
_PASSWORD_RESET_EXPIRY = 3600  # 1 hour


def _send_reset_email(to_email: str, reset_url: str) -> bool:
    """Send a password reset email via Gmail SMTP."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    smtp_user = "purebrain@puremarketing.ai"
    smtp_pass = "mldvztmeligxhyaw"

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


def _check_admin_auth(request: Request) -> bool:
    """Returns True if request has main bearer token OR a valid admin_token query param."""
    if check_auth(request):
        return True
    # Also accept ?admin_token=XXX or header X-Admin-Token
    admin_token = (
        request.query_params.get("admin_token", "").strip()
        or request.headers.get("x-admin-token", "").strip()
    )
    return bool(admin_token)  # full validation done in endpoint (needs async DB)


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
    elif not _verify_affiliate_password(password, stored_hash):
        return JSONResponse({"error": "incorrect password"}, status_code=401)

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
    elif not _verify_affiliate_password(password, stored_hash):
        return JSONResponse({"error": "incorrect password"}, status_code=401)

    referral_code = row["referral_code"]
    session_token = _create_affiliate_session(referral_code)

    return JSONResponse({
        "ok":           True,
        "session_token": session_token,
        "referral_code": referral_code,
        "expires_in":    _SESSION_TTL_SECS,
    })


async def api_referral_dashboard(request: Request) -> JSONResponse:
    """GET /api/referral/dashboard?code=PB-XXXX — referrer stats. Requires ?password= or portal token."""
    code     = request.query_params.get("code", "").strip().upper()
    email    = request.query_params.get("email", "").strip().lower()
    password = request.query_params.get("password", "").strip()
    # Portal owner (Jared) can see any dashboard without affiliate password
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
        #   1. Portal bearer token (Jared / admin)
        #   2. A valid affiliate session token (?session=TOKEN, or X-Affiliate-Session header)
        #   3. Direct password param (?password=...) as fallback for API callers
        if not portal_authed:
            session_token = (
                request.query_params.get("session", "").strip()
                or request.headers.get("x-affiliate-session", "").strip()
            )
            password_param = request.query_params.get("password", "").strip()
            session_code = _verify_affiliate_session(session_token)
            if session_code:
                # Session token valid — verify it belongs to this referrer
                if session_code.upper() != referrer["referral_code"].upper():
                    return JSONResponse({"error": "session token does not match this account"}, status_code=403)
            elif password_param:
                stored_hash = referrer["password_hash"]
                if not stored_hash:
                    # Account has no password yet — deny, prompt to set one via /api/referral/login
                    return JSONResponse({"error": "no password set for this account. Please login via the referral portal to set one."}, status_code=401)
                if not _verify_affiliate_password(password_param, stored_hash):
                    return JSONResponse({"error": "incorrect password"}, status_code=401)
            else:
                return JSONResponse({"error": "authentication required. Please login at purebrain.ai/refer/"}, status_code=401)

        referrer_id   = referrer["id"]
        referral_code = referrer["referral_code"]

        # Referral counts
        cur = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (referrer_id,)
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
        cur = await db.execute(
            """SELECT r.referred_name, r.referred_email, r.status, r.created_at,
                      COALESCE(SUM(cp.commission_value), 0) AS earnings,
                      COUNT(cp.id) AS payment_count
               FROM referrals r
               LEFT JOIN commission_payments cp ON cp.referral_id = r.id
               WHERE r.referrer_id = ?
               GROUP BY r.id
               ORDER BY r.created_at DESC""",
            (referrer_id,)
        )
        history = [dict(row) async for row in cur]

    reward_tiers = [
        {"label": "Commission Rate", "reward": f"{REFERRAL_COMMISSION_RATE * 100:.0f}% of every payment"},
        {"label": "Frequency", "reward": "Every month, for as long as they are a member"},
        {"label": "Awakened ($197/mo)", "reward": "$9.85/month per referral"},
        {"label": "Partnered ($579/mo)", "reward": "$28.95/month per referral"},
        {"label": "Unified ($1,089/mo)", "reward": "$54.45/month per referral"},
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
    """POST /api/referral/complete — mark a referral as completed and issue reward."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    referral_code  = str(body.get("referral_code", "")).strip().upper()
    referred_email = str(body.get("referred_email", "")).strip().lower()
    referred_name  = str(body.get("referred_name", "")).strip()

    if not referral_code:
        return JSONResponse({"error": "missing referral_code"}, status_code=400)
    if not referred_email or "@" not in referred_email:
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

        # Prevent double-completion for same referred email under this referrer
        cur = await db.execute(
            """SELECT id, status FROM referrals
               WHERE referrer_id = ? AND referred_email = ? COLLATE NOCASE""",
            (referrer_id, referred_email)
        )
        existing = await cur.fetchone()
        if existing:
            if existing[1] == "completed":
                return JSONResponse({"ok": True, "message": "already completed"})
            # Update existing pending row
            referral_id = existing[0]
            await db.execute(
                "UPDATE referrals SET status='completed', completed_at=? WHERE id=?",
                (now, referral_id)
            )
        else:
            cur = await db.execute(
                """INSERT INTO referrals (referrer_id, referred_email, referred_name, status, created_at, completed_at)
                   VALUES (?, ?, ?, 'completed', ?, ?)""",
                (referrer_id, referred_email, referred_name, now, now)
            )
            referral_id = cur.lastrowid

        await db.commit()

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
            return JSONResponse({"error": "referrer not found"}, status_code=404)

    return JSONResponse({"ok": True})


async def api_referral_leaderboard(request: Request) -> JSONResponse:
    """GET /api/referral/leaderboard — top referrers by completed referrals."""
    limit = min(int(request.query_params.get("limit", "10")), 50)

    async with _referral_db() as db:
        cur = await db.execute(
            """SELECT r.user_name, r.referral_code,
                      COUNT(ref.id) AS completed_count,
                      COALESCE(SUM(rw.reward_value), 0) AS total_earned
               FROM referrers r
               LEFT JOIN referrals ref ON ref.referrer_id = r.id AND ref.status = 'completed'
               LEFT JOIN rewards rw ON rw.referrer_id = r.id
               GROUP BY r.id
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


def _read_payout_requests() -> list:
    """Read all payout requests from JSONL file."""
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


def _write_payout_request(entry: dict) -> None:
    """Append a payout request to JSONL file."""
    with PAYOUT_REQUESTS_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _update_payout_status(request_id: str, status: str, batch_id: str = "") -> bool:
    """Update the status of an existing payout request in the JSONL file."""
    all_requests = _read_payout_requests()
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

    if amount < PAYOUT_MIN_AMOUNT:
        return JSONResponse(
            {"error": f"minimum payout is ${PAYOUT_MIN_AMOUNT:.0f}"},
            status_code=400
        )

    existing = _read_payout_requests()
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
    except Exception:
        actual_earnings = amount  # allow through on DB error

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
    _write_payout_request(entry)

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
                _update_payout_status(request_id, "completed", payout_result.get("batch_id", ""))
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

    all_requests = _read_payout_requests()
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

    all_requests = _read_payout_requests()
    found = False
    updated = []
    paid_entry = None
    for req in all_requests:
        if req.get("request_id") == request_id:
            req["status"] = "paid"
            req["paid_at"] = datetime.now(timezone.utc).isoformat()
            if notes:
                req["notes"] = notes
            paid_entry = req
            found = True
        updated.append(req)

    if not found:
        return JSONResponse({"error": "request_id not found"}, status_code=404)

    try:
        with PAYOUT_REQUESTS_FILE.open("w") as f:
            for req in updated:
                f.write(json.dumps(req) + "\n")
    except Exception as e:
        return JSONResponse({"error": f"failed to update file: {e}"}, status_code=500)

    if paid_entry:
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
        "paid_at": paid_entry.get("paid_at") if paid_entry else None,
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

    all_requests = _read_payout_requests()
    found = False
    target = None
    for req in all_requests:
        if req.get("request_id") == request_id:
            target = req
            found  = True
            break

    if not found:
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

    now_iso = datetime.now(timezone.utc).isoformat()
    updated = []
    for req in all_requests:
        if req.get("request_id") == request_id:
            if payout_result["ok"]:
                req["status"]          = "completed"
                req["paid_at"]         = now_iso
                req["paypal_batch_id"] = payout_result.get("batch_id", "")
                req["notes"]           = f"Auto-paid via PayPal Payouts API. Batch: {payout_result.get('batch_id', '')}"
            else:
                req["status"] = "failed"
                req["notes"]  = f"PayPal error: {payout_result.get('error', 'unknown')}"
        updated.append(req)

    try:
        with PAYOUT_REQUESTS_FILE.open("w") as f:
            for req in updated:
                f.write(json.dumps(req) + "\n")
    except Exception as e:
        return JSONResponse({"error": f"failed to persist payout status: {e}"}, status_code=500)

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
                """SELECT ref.referred_name, ref.referred_email, ref.status, ref.created_at,
                          COALESCE(rw.reward_value, 0) AS earnings
                   FROM referrals ref
                   LEFT JOIN rewards rw ON rw.referral_id = ref.id
                   WHERE ref.referrer_id = ?
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

    requests_list = _read_payout_requests()
    requests_list.sort(key=lambda r: r.get("created_at_ts", 0), reverse=True)
    return JSONResponse({"requests": requests_list, "count": len(requests_list)})


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
                updated_at            TEXT NOT NULL DEFAULT ''
            )
        """)
        await db.commit()


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

    async with _clients_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM clients ORDER BY first_seen_at DESC")
        rows = await cur.fetchall()
        clients = [dict(r) for r in rows]

        # Aggregate stats
        total   = len(clients)
        active  = sum(1 for c in clients if c.get("status") == "active")
        onboard = sum(1 for c in clients if c.get("status") == "onboarding")
        churned = sum(1 for c in clients if c.get("status") == "churned")
        total_rev = sum(float(c.get("total_paid") or 0) for c in clients)

        # MRR: subscription_active clients, estimate by tier
        tier_prices = {"awakened": 79, "bonded": 149, "partnered": 499, "brainiac": 299}
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
    }
    return JSONResponse({"clients": clients, "stats": stats})


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
    allowed_tiers = {"awakened", "bonded", "partnered", "brainiac", "unknown", ""}
    if tier and tier not in allowed_tiers:
        return JSONResponse({"error": "invalid tier — must be one of: awakened, bonded, partnered, brainiac, unknown"}, status_code=400)

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
                "jared",
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
    No auth required so hook scripts can call it without a bearer token.
    """
    import json as _j
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
                (agent_id, "jared", name, status, task, "", now, now),
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
    No auth required (public endpoint). Validates, logs, and injects tmux notification."""
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

    name     = str(body.get("name",     "")).strip()
    company  = str(body.get("company",  "")).strip()
    email    = str(body.get("email",    "")).strip()
    inv_range = str(body.get("range",    "")).strip()
    question = str(body.get("question", "")).strip()

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
        f"\n[INVESTOR INQUIRY] New question from {entry['name']} ({email}):\n"
        f"Company: {company or 'Not provided'}\n"
        f"Investment Range: {inv_range or 'Not specified'}\n"
        f"Question: {question}\n"
        f"---\nReply with: /respond-investor {email} Your response here"
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
    Route("/api/admin/payouts", endpoint=api_admin_payouts),
    Route("/admin/referrals", endpoint=serve_admin_referrals),
    Route("/admin/clients", endpoint=serve_admin_clients),
    Route("/api/admin/clients", endpoint=api_admin_clients),
    Route("/api/admin/clients/update", endpoint=api_admin_clients_update, methods=["POST"]),
    Route("/api/admin/clients/import", endpoint=api_admin_clients_import, methods=["POST"]),
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
    Route("/api/777/chat", endpoint=api_777_chat, methods=["POST", "OPTIONS"]),
    Route("/api/whatsapp/qr", endpoint=api_whatsapp_qr),
    Route("/api/whatsapp/status", endpoint=api_whatsapp_status),
    Route("/api/settings", endpoint=api_user_settings, methods=["GET", "POST", "PUT"]),
    WebSocketRoute("/ws/chat", endpoint=ws_chat),
    WebSocketRoute("/ws/terminal", endpoint=ws_terminal),
]

app = Starlette(
    routes=routes,
    on_startup=[_startup],
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=["https://purebrain.ai", "https://www.purebrain.ai", "https://app.purebrain.ai", "https://777-command-center.vercel.app"],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type"],
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
    print(f"[portal] Bearer token: {BEARER_TOKEN}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
