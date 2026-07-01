"""Shared configuration, constants, and helper functions for the PureBrain Portal.

This module is the foundation that all portal submodules import from.
It contains:
  - Immutable config constants (paths, tokens, thresholds)
  - Shared helper functions (auth, subprocess, error handling)
  - Shared mutable state that crosses module boundaries

All constants are set once at startup and never mutated after.
Mutable state is documented explicitly.
"""
import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Thread-pool executor + fire-and-forget task tracking
# ---------------------------------------------------------------------------
PORTAL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=32, thread_name_prefix="portal"
)
background_tasks: set = set()


def fire_and_forget(coro):
    """Schedule a coroutine as a tracked background task that auto-cleans."""
    task = asyncio.ensure_future(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


# ---------------------------------------------------------------------------
# Immutable config constants
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
TOKEN_FILE = SCRIPT_DIR / ".portal-token"
PORTAL_HTML = SCRIPT_DIR / "portal.html"
PORTAL_PB_HTML = SCRIPT_DIR / "portal-pb-styled.html"
REACT_DIST = SCRIPT_DIR / "react-portal" / "dist"
START_TIME = time.time()
PORTAL_VERSION = "2.3.71"
RELEASE_NOTES_FILE = SCRIPT_DIR / "release_notes.json"

# Version mismatch detection — warn if release_notes.json disagrees with PORTAL_VERSION
try:
    _rn_data = json.loads(RELEASE_NOTES_FILE.read_text())
    _rn_version = _rn_data.get("current_version", "")
    if _rn_version and _rn_version != PORTAL_VERSION:
        print(f"[portal] WARNING: Version mismatch — PORTAL_VERSION={PORTAL_VERSION} but release_notes.json says {_rn_version}")
        # Auto-fix: update release_notes.json to match PORTAL_VERSION (source of truth)
        _rn_data["current_version"] = PORTAL_VERSION
        RELEASE_NOTES_FILE.write_text(json.dumps(_rn_data, indent=2))
        print(f"[portal] Auto-fixed release_notes.json to match PORTAL_VERSION={PORTAL_VERSION}")
except Exception:
    pass

# Auto-detect CIV_NAME and HUMAN_NAME from identity file
_identity_file = Path.home() / ".aiciv-identity.json"
try:
    _identity = json.loads(_identity_file.read_text())
    CIV_NAME = _identity.get("civ_id", "portal")
    HUMAN_NAME = _identity.get("human_name", "User")
except Exception:
    CIV_NAME = "portal"
    HUMAN_NAME = "User"

# Claude session log paths
_PROJECTS_DIR = Path.home() / ".claude" / "projects"
LOG_ROOT = _PROJECTS_DIR
HISTORY_FILE = Path.home() / ".claude" / "history.jsonl"
PORTAL_CHAT_LOG = SCRIPT_DIR / "portal-chat.jsonl"

# Uploads
UPLOADS_DIR = Path.home() / "portal_uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
UPLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

# Payout/referral config
PAYOUT_REQUESTS_FILE = SCRIPT_DIR / "payout-requests.jsonl"
PAYOUT_MIN_AMOUNT = 25.0
PAYOUT_AUTO_APPROVE_LIMIT = 1000.0
PAYOUT_COOLDOWN_DAYS = 30

# Database paths
REFERRALS_DB = SCRIPT_DIR / "referrals.db"
CLIENTS_DB = SCRIPT_DIR / "clients.db"
AGENTS_DB = SCRIPT_DIR / "agents.db"

# Referral code generation
REFERRAL_CODE_PREFIX = "PB-"
REFERRAL_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
REFERRAL_CODE_LENGTH = 4
REFERRAL_COMMISSION_RATE = 0.05

# Log paths for client data import (uses CIV_ROOT env var or home directory)
_CIV_ROOT = Path(os.environ.get("CIV_ROOT", str(Path.home())))
_LOG_ROOT = _CIV_ROOT / "logs"
WEB_CONVERSATIONS_LOG = _LOG_ROOT / "purebrain_web_conversations.jsonl"
PAYMENTS_LOG = _LOG_ROOT / "purebrain_payments.jsonl"
PAY_TEST_LOG = _LOG_ROOT / "purebrain_pay_test.jsonl"

# Allowed directories for file downloads
DOWNLOAD_ALLOWED_DIRS = [
    Path.home() / "exports",
    Path.home() / "to-human",
    Path.home() / "purebrain_portal",
    Path.home() / "from-acg",
    Path.home() / "portal_uploads",
]

# OAuth flow config
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
OAUTH_URL_PATTERN = re.compile(r'https://[^\s\x1b\x07\]]*oauth/authorize\?[^\s\x1b\x07\]]+')

# Auth screen patterns for state machine
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

# Bearer token
if TOKEN_FILE.exists():
    BEARER_TOKEN = TOKEN_FILE.read_text().strip()
else:
    BEARER_TOKEN = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(BEARER_TOKEN)
    TOKEN_FILE.chmod(0o600)
    print(f"[portal] Generated new bearer token (saved to {TOKEN_FILE})")

# PayPal credentials
PAYPAL_SANDBOX = os.environ.get("PAYPAL_SANDBOX", "true").lower() != "false"
if PAYPAL_SANDBOX:
    PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_SANDBOX_CLIENT_ID", os.environ.get("PAYPAL_CLIENT_ID", ""))
    PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_SANDBOX_SECRET", os.environ.get("PAYPAL_SECRET", ""))
else:
    PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
    PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_SECRET", "")


# ---------------------------------------------------------------------------
# Shared helper functions
# ---------------------------------------------------------------------------

def sanitize_error(e: Exception, context: str = "") -> str:
    """Return a safe error message for API responses. Log the real error server-side."""
    print(f"[portal] ERROR {context}: {type(e).__name__}: {e}")
    return f"Internal error{': ' + context if context else ''}"


def run_subprocess_sync(cmd, timeout=5, check=False, capture=False, text=False):
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


async def run_subprocess_async(cmd, timeout=5, check=False):
    """Run a subprocess WITHOUT blocking the asyncio event loop."""
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
        return subprocess.CompletedProcess(
            args=cmd, returncode=proc.returncode, stdout=stdout, stderr=stderr,
        )
    except asyncio.TimeoutError:
        print(f"[portal] WARN run_subprocess_async timeout: {' '.join(str(c) for c in cmd)}")
        return None
    except Exception as e:
        print(f"[portal] WARN run_subprocess_async unexpected {type(e).__name__}: {e} cmd={' '.join(str(c) for c in cmd)}")
        return None


async def run_subprocess_output(cmd, timeout=5):
    """Run subprocess and capture output without blocking the event loop."""
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


# ---------------------------------------------------------------------------
# Tmux session cache (shared across chat, terminal, auth modules)
# ---------------------------------------------------------------------------
_tmux_session_cache: tuple = (0.0, "")
TMUX_CACHE_TTL = 30.0


def get_tmux_session() -> str:
    """Find the live primary Claude Code session for this container.
    Result is cached for 30s to avoid hammering tmux."""
    global _tmux_session_cache
    now = time.time()
    if now - _tmux_session_cache[0] < TMUX_CACHE_TTL and _tmux_session_cache[1]:
        return _tmux_session_cache[1]

    def alive(name):
        try:
            subprocess.check_output(["tmux", "has-session", "-t", name],
                                    stderr=subprocess.DEVNULL, timeout=3)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False

    result = None

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
# Auth helpers (used by every endpoint module)
# ---------------------------------------------------------------------------
from starlette.requests import Request


def check_auth(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return hmac.compare_digest(auth[7:], BEARER_TOKEN)
    path = request.url.path
    if "/ws" in path or "/api/chat/uploads/" in path or "/api/download" in path:
        return hmac.compare_digest(request.query_params.get("token", ""), BEARER_TOKEN)
    return False


# Activity tracking state
_last_activity_track_time: float = 0.0
_ACTIVITY_TRACK_INTERVAL = 60

_login_recorded_this_process: bool = False


def _maybe_track_activity() -> None:
    """Fire-and-forget: track portal owner activity (throttled to 1x/min)."""
    global _last_activity_track_time
    now = time.time()
    if now - _last_activity_track_time < _ACTIVITY_TRACK_INTERVAL:
        return
    _last_activity_track_time = now

    try:
        from tracking import record_activity
        owner_file = SCRIPT_DIR / "portal_owner.json"
        if owner_file.exists():
            owner = json.loads(owner_file.read_text())
            email = owner.get("human_email", "")
            if email:
                record_activity(str(CLIENTS_DB), email)
    except Exception as e:
        print(f"[tracking] activity track error: {e}")


def check_auth_and_track(request: Request) -> bool:
    """check_auth + activity tracking + first-request login recording."""
    global _login_recorded_this_process
    authed = check_auth(request)
    if authed:
        if not _login_recorded_this_process:
            _login_recorded_this_process = True
            try:
                from tracking import record_login
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
