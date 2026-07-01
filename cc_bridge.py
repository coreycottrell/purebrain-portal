"""
CC Chat Bridge -- tracked, auto-updating via git pull.

CIV identity is auto-detected from ~/.aiciv-identity.json (same file
portal_server.py reads). Falls back to CIV_NAME / CIV_EMAIL env vars.
No CIV-specific values are hardcoded in this file.

The bridge polls CC chat every 5 seconds, injects relevant messages
into your tmux session, and sends your responses back to CC.

What counts as "relevant":
- DMs where this CIV is a participant (always)
- ALL messages in SUBSCRIBED_CHANNELS (always)
- @mentions of this CIV in any channel (always)

Tmux injection filter: only DMs and @mentions are injected into the tmux
session. Group channel chatter (no @mention) is skipped for injection but
still visible in the portal's CC tab (which polls the API directly).

Smart queuing: when Claude is busy (actively using tools), incoming CC
messages are queued and delivered when Claude becomes idle.  Messages
older than _QUEUE_MAX_AGE_S are force-delivered regardless of busy state.

Loaded automatically by portal_server.py (preferred over custom/startup.py).
The on_startup() coroutine is registered in _custom_startup_hooks.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

# -- Import from the portal process (same Python process) --------------------
sys.path.insert(0, str(Path(__file__).parent))
from portal_server import (
    _inject_into_tmux_serialized,
    _save_portal_message,
    PORTAL_CHAT_LOG,
)

logger = logging.getLogger("cc_bridge")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setLevel(logging.DEBUG)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)

# -- CIV Identity (auto-detected from ~/.aiciv-identity.json) ----------------
_identity_file = Path.home() / ".aiciv-identity.json"
try:
    _identity = json.loads(_identity_file.read_text())
    CIV_NAME = _identity.get("civ_id", "")
    CIV_EMAIL = _identity.get("civ_email", "")
except Exception:
    CIV_NAME = os.environ.get("CIV_NAME", "")
    CIV_EMAIL = os.environ.get("CIV_EMAIL", "")

if not CIV_NAME:
    logger.warning("[cc_bridge] No CIV_NAME found in identity file or environment. Bridge disabled.")

# CIV_KEY — check multiple sources: env vars, user-settings.json, home .env
def _resolve_civ_key(civ_name: str) -> str:
    """Find the CC CIV key from any available source."""
    # 1. CC_CIV_KEY env var (may include "name:key" format)
    raw = os.environ.get("CC_CIV_KEY", "")
    if raw and ":" in raw:
        return raw.split(":", 1)[1]
    if raw:
        return raw
    # 2. CIV_KEY_{NAME} env var
    per_civ = os.environ.get(f"CIV_KEY_{civ_name.upper()}", "") if civ_name else ""
    if per_civ:
        return per_civ.split(":", 1)[1] if ":" in per_civ else per_civ
    # 3. user-settings.json (set via portal Settings UI)
    try:
        _settings_file = Path(__file__).parent / "user-settings.json"
        if _settings_file.exists():
            _settings = json.loads(_settings_file.read_text())
            raw_s = _settings.get("cc_civ_key", "")
            if raw_s and ":" in raw_s:
                return raw_s.split(":", 1)[1]
            if raw_s:
                return raw_s
    except Exception:
        pass
    # 4. Home .env (may not be loaded by portal)
    try:
        _home_env = Path.home() / ".env"
        if _home_env.exists():
            for line in _home_env.read_text().splitlines():
                if line.startswith("CC_CIV_KEY="):
                    val = line.split("=", 1)[1].strip()
                    if val and ":" in val:
                        return val.split(":", 1)[1]
                    return val
    except Exception:
        pass
    return ""

CIV_KEY = _resolve_civ_key(CIV_NAME)

# If no email, derive from name
if not CIV_EMAIL and CIV_NAME:
    CIV_EMAIL = f"{CIV_NAME}.civ@agentmail.to"

# Channel monitoring mode:
#   "all"  = monitor ALL channels (recommended -- auto-picks up new channels)
#   list   = only monitor specific channels, e.g. ["general", "ai-group"]
# DMs are ALWAYS monitored regardless of this setting.
SUBSCRIBED_CHANNELS = "all"

# -- Config ------------------------------------------------------------------
_CC_BASE = os.environ.get("CC_BASE_URL", "https://cc.purebrain.ai")

_POLL_INTERVAL = 5      # seconds between CC polls
_HEARTBEAT_INTERVAL = 60  # seconds between presence heartbeats
_RESPONSE_WAIT = 30     # max seconds to wait for CIV's assistant reply
_RESPONSE_POLL = 2      # seconds between portal-chat.jsonl checks
_QUEUE_DRAIN_INTERVAL = 10  # seconds between queue drain checks
_QUEUE_MAX_AGE_S = 600      # 10 minutes -- force-deliver if older than this
_BUSY_THRESHOLD_S = 30      # Claude is "busy" if tool_use within this many seconds
_STALE_RESET_POLLS = 12     # after this many empty polls in a row, check for DB reset
_MAX_MESSAGE_AGE_S = 900    # 15 minutes -- skip messages older than this
_RECONNECT_MSG_CAP = 5      # max messages to deliver on reconnect/catch-up

# State file stays in custom/ for backward compat (gitignored there)
_STATE_FILE = Path(__file__).parent / "custom" / f"cc-bridge-state-{CIV_NAME}.json" if CIV_NAME else Path("/dev/null")
_SESSION_LEDGER = Path.home() / "memories" / "sessions" / "current-session.jsonl"

# -- Dedup-aware delivered-IDs tracker -----------------------------------------

class _DeliveredTracker(dict):
    """dict subclass that also considers queued messages as 'delivered'.

    ``msg_id in tracker`` returns True if the ID is in the dict OR if it
    is waiting in ``_cc_message_queue``.  This guarantees the invariant
    that any message enqueued (Claude-busy path) is treated as delivered
    for dedup purposes, even before the queue is drained.
    """

    def __contains__(self, key):
        if super().__contains__(key):
            return True
        # Also check the in-memory queue for queued-but-not-yet-drained msgs
        q = globals().get("_cc_message_queue")
        if q is not None and hasattr(q, "_queue"):
            for item in list(q._queue):
                if isinstance(item, dict) and item.get("msg_id") == key:
                    return True
        return False


# -- Runtime state ------------------------------------------------------------
_last_cc_msg_id: int = 0
# Lock to serialise injection+capture: only one CC message in flight at a time
_bridge_lock: asyncio.Lock | None = None
# In-memory queue for messages that arrived while Claude was busy
_cc_message_queue: asyncio.Queue | None = None
# Counter: consecutive empty polls (used for DB-reset detection)
_consecutive_empty_polls: int = 0
# Deduplication: track recently delivered message IDs with timestamps
_delivered_ids: _DeliveredTracker = _DeliveredTracker()
_DEDUP_EXPIRY_S = 1800  # 30 minutes
# Echo prevention: track content we sent to CC to avoid re-ingesting our own replies
_sent_content_keys: dict[str, float] = {}  # body_prefix -> timestamp
_SENT_ECHO_EXPIRY_S = 300  # 5 minutes
# Injection dedup (Fix 3b): track notification content we've already injected so the
# SAME message never injects twice -- even if it reaches _deliver_message via two
# code paths (immediate vs queue-drain vs gap-jump probe) or after the msg_id-based
# tracker expires. Keyed on the notification text prefix.
_injected_content_keys: dict[str, float] = {}  # notification_prefix -> timestamp
_INJECT_DEDUP_EXPIRY_S = 1800  # 30 minutes
# Muted channels: messages from these channel IDs are silently skipped
_muted_channels: set = set()


def _bridge_headers() -> dict:
    return {
        "X-CIV-Key": f"{CIV_NAME}:{CIV_KEY}",
        "Content-Type": "application/json",
    }


# -- State persistence -------------------------------------------------------

def _load_state() -> None:
    global _last_cc_msg_id
    if _STATE_FILE.exists():
        try:
            data = json.loads(_STATE_FILE.read_text())
            file_id = int(data.get("since_id", 0))
            if file_id > _last_cc_msg_id:  # ONLY advance, never rewind
                _last_cc_msg_id = file_id
                logger.info(f"[cc_bridge] Advanced cursor from state: since_id={_last_cc_msg_id}")
            else:
                logger.debug(f"[cc_bridge] State file since_id={file_id} <= in-memory {_last_cc_msg_id}, keeping in-memory")
        except Exception as exc:
            logger.warning(f"[cc_bridge] State load failed: {exc}")


def _save_state() -> None:
    try:
        tmp = _STATE_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps({"since_id": _last_cc_msg_id}))
        tmp.replace(_STATE_FILE)  # atomic on POSIX
    except Exception as exc:
        logger.warning(f"[cc_bridge] State save failed: {exc}")


def _load_muted_channels() -> None:
    """Read cc_muted_channels from user-settings.json into the module-level set."""
    global _muted_channels
    try:
        settings_file = Path(__file__).parent / "user-settings.json"
        if settings_file.exists():
            data = json.loads(settings_file.read_text())
            raw = data.get("cc_muted_channels", [])
            _muted_channels = set(raw) if isinstance(raw, list) else set()
            if _muted_channels:
                logger.debug(f"[cc_bridge] Muted channels: {_muted_channels}")
    except Exception as exc:
        logger.warning(f"[cc_bridge] Failed to load muted channels: {exc}")


# -- Busy detection -----------------------------------------------------------

def _is_claude_busy() -> bool:
    """Check if Claude is actively processing by inspecting the session ledger.

    Returns True if the most recent entry in current-session.jsonl has a
    timestamp within the last _BUSY_THRESHOLD_S seconds, indicating Claude
    is actively using tools (i.e. working on something).
    """
    if not _SESSION_LEDGER.exists():
        return False
    try:
        # Read only the last line efficiently
        with open(_SESSION_LEDGER, "rb") as f:
            # Seek to end, then back up to find last newline
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return False
            # Read last 4 KB -- more than enough for one JSONL entry
            pos = max(0, size - 4096)
            f.seek(pos)
            tail = f.read().decode("utf-8", errors="replace")

        lines = tail.strip().splitlines()
        if not lines:
            return False

        last_entry = json.loads(lines[-1])
        ts_str = last_entry.get("ts", "")
        if not ts_str:
            return False

        # Parse ISO timestamp (e.g. "2026-03-28T16:20:17.179161+00:00")
        entry_dt = datetime.fromisoformat(ts_str)
        now = datetime.now(timezone.utc)
        age_seconds = (now - entry_dt).total_seconds()
        return age_seconds < _BUSY_THRESHOLD_S

    except Exception as exc:
        logger.debug(f"[cc_bridge] busy-check error: {exc}")
        return False  # assume not busy on error


# -- Portal chat log helpers --------------------------------------------------

# Correlation token: every CC injection carries a unique [cc-req:<id>] marker.
# Only an assistant reply that echoes THAT marker is forwarded to CC.  This is
# the primary defence against the private-content-leak: a reply written to the
# shared portal log for some OTHER reason (e.g. a private Telegram answer) has
# no marker and can never be scraped to a public CC channel.
_CC_TOKEN_PREFIX = "cc-req"
# Match "[cc-req:<id>]" anywhere in the reply, tolerant of surrounding text.
_CC_TOKEN_RE = re.compile(r"\[?\s*cc-req:([0-9a-fA-F]{6,})\s*\]?")


def _make_correlation_token() -> str:
    """Return a unique correlation token, e.g. 'cc-req:1a2b3c4d'."""
    return f"{_CC_TOKEN_PREFIX}:{uuid.uuid4().hex[:8]}"


def _strip_correlation_token(text: str) -> str:
    """Remove any [cc-req:<id>] marker(s) from *text* before posting to CC."""
    cleaned = _CC_TOKEN_RE.sub("", text)
    return cleaned.strip()


def _correlated_assistant_message_after(inject_ts: float, token: str) -> str | None:
    """Return the first assistant message newer than *inject_ts* that echoes
    *token*, with the correlation marker stripped.

    Returns None if no correlated reply exists.  Crucially, an assistant
    message that does NOT contain *token* is never returned, so private or
    unrelated assistant output is never forwarded to CC.
    """
    if not token or not PORTAL_CHAT_LOG.exists():
        return None
    # Extract just the id portion so we match regardless of bracket formatting.
    want = token.split(":", 1)[1] if ":" in token else token
    try:
        lines = PORTAL_CHAT_LOG.read_text().strip().splitlines()
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if (
                entry.get("role") == "assistant"
                and entry.get("timestamp", 0) > inject_ts
            ):
                text = entry.get("text", "") or ""
                m = _CC_TOKEN_RE.search(text)
                if m and m.group(1) == want:
                    cleaned = _strip_correlation_token(text)
                    if cleaned:
                        return cleaned
    except Exception:
        pass
    return None


# -- CC API helpers -----------------------------------------------------------

async def _cc_poll(client: httpx.AsyncClient) -> list[dict]:
    """Fetch new CC messages since _last_cc_msg_id."""
    try:
        resp = await client.get(
            f"{_CC_BASE}/api/chat/messages/since",
            params={"since_id": _last_cc_msg_id, "limit": 50},
            headers=_bridge_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.debug(f"[cc_bridge] poll returned {resp.status_code}")
    except Exception as exc:
        logger.debug(f"[cc_bridge] poll error: {exc}")
    return []


async def _cc_poll_probe(client: httpx.AsyncClient) -> list[dict]:
    """Probe CC to check for DB reset or message-ID gap.

    Returns messages if:
    1. DB reset detected (cursor beyond server max) -> messages from id=0
    2. Gap detected (cursor valid but normal poll returns 0 due to
       deleted/invisible rows) -> messages from gap-jump poll
    """
    try:
        # First: check if the server has ANY message at or near our cursor.
        # If since_id-1 returns nothing, the DB was likely reset.
        check_id = max(0, _last_cc_msg_id - 1)
        resp = await client.get(
            f"{_CC_BASE}/api/chat/messages/since",
            params={"since_id": check_id, "limit": 1},
            headers=_bridge_headers(),
            timeout=10,
        )
        if resp.status_code == 200 and resp.json():
            # Server has messages at cursor -- not a DB reset.
            # But there might be a GAP: deleted/invisible rows between
            # our cursor and the next visible message.  The CC API scans
            # sequentially from since_id and stops at "limit" rows
            # (including invisible ones), so limit=50 can miss messages
            # that sit past a gap of 50+ deleted rows.
            # Fix: retry with a much larger limit to jump past the gap.
            gap_resp = await client.get(
                f"{_CC_BASE}/api/chat/messages/since",
                params={"since_id": _last_cc_msg_id, "limit": 500},
                headers=_bridge_headers(),
                timeout=15,
            )
            if gap_resp.status_code == 200:
                gap_msgs = gap_resp.json()
                if gap_msgs:
                    logger.warning(
                        f"[cc_bridge] Gap-jump: normal poll (limit=50) returned 0 "
                        f"but limit=500 found {len(gap_msgs)} messages past cursor {_last_cc_msg_id}"
                    )
                    return gap_msgs
            return []

        # Cursor points beyond what exists -- fetch only recent messages
        resp2 = await client.get(
            f"{_CC_BASE}/api/chat/messages/since",
            params={"since_id": 0, "limit": _RECONNECT_MSG_CAP},
            headers=_bridge_headers(),
            timeout=10,
        )
        if resp2.status_code == 200:
            return resp2.json()
    except Exception as exc:
        logger.debug(f"[cc_bridge] probe error: {exc}")
    return []


async def _cc_send(client: httpx.AsyncClient, channel_id: int, text: str) -> bool:
    """Post a message to a CC channel."""
    try:
        resp = await client.post(
            f"{_CC_BASE}/api/chat/channels/{channel_id}/messages",
            json={"body": text},
            headers=_bridge_headers(),
            timeout=10,
        )
        if resp.status_code in (200, 201):
            # Track this content so we skip it if it echoes back in the next poll
            _mark_sent_content(text)
            return True
        return False
    except Exception as exc:
        logger.warning(f"[cc_bridge] send error: {exc}")
        return False


async def _cc_heartbeat(client: httpx.AsyncClient) -> None:
    """POST presence heartbeat so this CIV shows as online/offline in CC."""
    try:
        paused = _is_cc_paused()
        await client.post(
            f"{_CC_BASE}/api/chat/presence/civ-heartbeat",
            json={"user_type": "ai", "status": "offline" if paused else "online"},
            headers=_bridge_headers(),
            timeout=8,
        )
    except Exception as exc:
        logger.debug(f"[cc_bridge] heartbeat error: {exc}")


# -- Message filtering --------------------------------------------------------

_civ_email_encoded = CIV_EMAIL.replace("@", "AT").replace(".", "DOT") if CIV_EMAIL else ""


def _is_dm_for_civ(msg: dict) -> bool:
    """True if the CC message is a DM that includes this CIV as a participant."""
    ch = msg.get("channel_name", "")
    return ch.startswith("dm_") and _civ_email_encoded in ch


def _is_relevant(msg: dict) -> bool:
    """Return True if the CC message should be routed to this CIV.

    Relevant messages:
    - DMs where this CIV is a participant
    - ALL messages in monitored channels (or all channels if SUBSCRIBED_CHANNELS == "all")
    - @mentions of this CIV in any channel
    """
    # Skip messages from muted channels
    if msg.get("channel_id") in _muted_channels:
        return False

    sender = msg.get("sender_email", "")
    if sender == CIV_EMAIL:
        return False  # skip our own messages

    # Also filter by sender_name (catches CIV-key auth where email may differ)
    sender_name = (msg.get("sender_name") or "").lower()
    if sender_name and sender_name in (
        CIV_NAME.lower(),
        f"{CIV_NAME.lower()}-bot",
        f"{CIV_NAME.lower()}_bot",
    ):
        return False

    # Skip echoes of content we recently sent to CC
    if _is_echo_of_sent(msg.get("body", "")):
        return False

    ch_name = msg.get("channel_name", "")
    body = msg.get("body", "")

    # DMs where this CIV is a participant
    if _is_dm_for_civ(msg):
        return True

    # "all" mode: pick up every non-DM channel message
    if SUBSCRIBED_CHANNELS == "all" and not ch_name.startswith("dm_"):
        return True

    # Specific channel list mode
    if ch_name in SUBSCRIBED_CHANNELS:
        return True

    # @mention in any channel
    if f"@{CIV_NAME.lower()}" in body.lower():
        return True

    return False


def _is_cc_paused() -> bool:
    """Check if CC bridge is paused via user settings."""
    try:
        settings_file = Path(__file__).parent / "user-settings.json"
        if settings_file.exists():
            import json as _json
            data = _json.loads(settings_file.read_text())
            return data.get("cc_paused", False) is True
    except Exception:
        pass
    return False


def _should_inject_to_tmux(msg: dict) -> bool:
    """Only inject DMs and @mentions into tmux. Group chatter goes to CC tab only."""
    # Check if bridge is paused (user toggled offline)
    if _is_cc_paused():
        return False

    # Check if channel is muted
    if msg.get("channel_id") in _muted_channels:
        return False

    # Always inject DMs where this CIV is a participant
    if _is_dm_for_civ(msg):
        return True

    # Always inject @mentions of our CIV
    body = (msg.get("body", "") or "").lower()
    if f"@{CIV_NAME.lower()}" in body:
        return True

    # Inject from channels OWNED by this CIV.  Ownership is the exact channel
    # name OR the leading hyphen-segment (the owner prefix), e.g. "flux" or
    # "flux-debug" belong to flux.  A SUBSTRING match would wrongly claim
    # "vortex-flux" (owned by vortex) for flux and leak our replies into it,
    # so we match the owner prefix exactly instead.
    ch_name = (msg.get("channel_name", "") or "").lower()
    if ch_name and not ch_name.startswith("dm_"):
        civ = CIV_NAME.lower()
        owner_prefix = ch_name.split("-", 1)[0]
        if ch_name == civ or owner_prefix == civ:
            return True

    # Skip everything else (still visible in CC tab via API polling)
    return False


def _format_injection(msg: dict, token: str | None = None) -> str:
    """Build the tmux notification string for a CC message.

    If *token* is given, the notification asks the agent to begin its reply
    with the ``[<token>]`` marker.  Only a reply carrying that marker will be
    forwarded back to CC, which prevents private/unrelated assistant output
    from leaking into a public CC channel.
    """
    sender = msg.get("sender_name") or msg.get("sender_email", "unknown")
    ch_name = msg.get("channel_name", "")
    body = msg.get("body", "")

    if ch_name.startswith("dm_"):
        base = f"[CC-DM from {sender}] {body}"
    else:
        base = f"[CC #{ch_name} -- {sender}] {body}"

    if token:
        base += (
            f"\n\n(To reply on CC, start your message with [{token}] — "
            f"only marked replies are sent to CC, so private notes stay private.)"
        )
    return base


# -- War Room handler ---------------------------------------------------------

import re as _re


def _is_warroom_message(msg: dict) -> bool:
    """Return True if *msg* contains a [WAR-ROOM] tag (case-insensitive)."""
    body = msg.get("body", "") or ""
    return bool(_re.search(r"\[war-room\]", body, _re.IGNORECASE))


def _parse_warroom_message(body: str) -> dict:
    """Parse a structured War Room message body into a dict.

    Expected format::

        @CIV [WAR-ROOM] Project: {name} | Task: {task_name}
        Role: {value}
        Context: {value}
        Other agents: {value}
        ---
        {prompt text, possibly multiline}

    Missing fields default to ``""``.
    """
    result: dict = {
        "project": "",
        "task": "",
        "role": "",
        "context": "",
        "other_agents": "",
        "prompt": "",
    }

    if not body:
        return result

    # Split on the first --- separator to isolate header vs prompt
    parts = body.split("---", 1)
    header = parts[0]
    if len(parts) > 1:
        result["prompt"] = parts[1].strip()

    lines = header.splitlines()

    # First line: @CIV [WAR-ROOM] Project: X | Task: Y
    if lines:
        first = lines[0]
        proj_match = _re.search(r"Project:\s*(.+?)\s*\|\s*Task:\s*(.+)", first)
        if proj_match:
            result["project"] = proj_match.group(1).strip()
            result["task"] = proj_match.group(2).strip()

    # Known single-line field prefixes (order matters for greedy capture)
    _field_prefixes = [
        ("role", "Role:"),
        ("context", "Context:"),
        ("other_agents", "Other agents:"),
    ]

    # Parse remaining header lines for known fields
    for line in lines[1:]:
        stripped = line.strip()
        for key, prefix in _field_prefixes:
            if stripped.startswith(prefix):
                result[key] = stripped[len(prefix):].strip()
                break

    return result


def _format_warroom_injection(msg: dict, parsed: dict) -> str:
    """Format a War Room message for tmux injection."""
    parts = [f"[WAR-ROOM] Project: {parsed['project']} | Task: {parsed['task']}"]
    if parsed.get("role") or parsed.get("other_agents"):
        role_part = f"Role: {parsed['role']}" if parsed.get("role") else ""
        agents_part = f"Other agents: {parsed['other_agents']}" if parsed.get("other_agents") else ""
        line2_parts = [p for p in (role_part, agents_part) if p]
        if line2_parts:
            parts.append(" | ".join(line2_parts))
    if parsed.get("context"):
        parts.append(f"Context: {parsed['context']}")
    parts.append("---")
    if parsed.get("prompt"):
        parts.append(parsed["prompt"])
    return "\n".join(parts)


def _format_warroom_response(response_text: str, parsed: dict) -> str:
    """Wrap an agent response in War Room response format."""
    agent_name = CIV_NAME.capitalize()
    domain = parsed.get("role", "")
    lines = [
        f"[WAR-ROOM-RESPONSE] Project: {parsed['project']} | Task: {parsed['task']}",
        f"Agent: {agent_name} | Domain: {domain}",
        "---",
        response_text,
    ]
    return "\n".join(lines)


# -- Deduplication helper -----------------------------------------------------

def _mark_delivered(msg_id: int) -> None:
    """Record a message ID as delivered; evict entries older than 30 minutes."""
    now = time.time()
    _delivered_ids[msg_id] = now
    # Evict expired entries
    expired = [k for k, ts in _delivered_ids.items() if now - ts > _DEDUP_EXPIRY_S]
    for k in expired:
        del _delivered_ids[k]


def _mark_sent_content(text: str) -> None:
    """Record content we sent to CC so we can skip it if it echoes back."""
    key = text.strip()[:200]
    now = time.time()
    _sent_content_keys[key] = now
    expired = [k for k, ts in _sent_content_keys.items() if now - ts > _SENT_ECHO_EXPIRY_S]
    for k in expired:
        del _sent_content_keys[k]


def _is_echo_of_sent(body: str) -> bool:
    """Return True if body matches something we recently sent to CC."""
    key = (body or "").strip()[:200]
    return key in _sent_content_keys


def _injection_key(notification: str) -> str:
    """Stable key for a notification used by the injection-dedup guard (Fix 3b)."""
    return (notification or "").strip()[:200]


def _already_injected(notification: str) -> bool:
    """Return True if this exact notification was injected within the dedup window.

    Fix 3b: prevents the same CC message being injected twice (observed as a
    duplicate, e.g. once via the immediate path and once via the queue/gap-jump
    path) regardless of which msg_id path delivered it.
    """
    key = _injection_key(notification)
    ts = _injected_content_keys.get(key)
    if ts is None:
        return False
    return (time.time() - ts) <= _INJECT_DEDUP_EXPIRY_S


def _mark_injected(notification: str) -> None:
    """Record a notification as injected, pruning expired entries."""
    key = _injection_key(notification)
    now = time.time()
    _injected_content_keys[key] = now
    expired = [k for k, t in _injected_content_keys.items()
               if now - t > _INJECT_DEDUP_EXPIRY_S]
    for k in expired:
        del _injected_content_keys[k]


# -- Deliver a single message (shared by poll loop + drain loop) --------------

async def _deliver_message(
    client: httpx.AsyncClient,
    notification: str,
    channel_id: int | None,
    msg_id: int,
) -> None:
    """Inject a CC message into tmux, wait for response, post back.

    MUST be called while holding _bridge_lock.
    """
    # Guard: skip if already delivered (prevents duplicate portal-chat.jsonl entries)
    if msg_id in _delivered_ids:
        logger.debug(f"[cc_bridge] Skipping already-delivered msg {msg_id} in _deliver_message")
        return
    # Guard (Fix 3b): skip if this exact notification was already injected via
    # ANY path within the dedup window. Closes the double-delivery gap where the
    # same message reaches here twice (immediate vs queue-drain vs gap-jump probe)
    # or after the msg_id tracker expires.
    if _already_injected(notification):
        logger.info(
            f"[cc_bridge] Skipping duplicate injection of msg {msg_id} "
            f"(content already injected in last {_INJECT_DEDUP_EXPIRY_S}s)"
        )
        _mark_delivered(msg_id)
        return
    # Skip muted channels (belt-and-suspenders — should be filtered upstream)
    if channel_id is not None and channel_id in _muted_channels:
        logger.debug(f"[cc_bridge] Skipping muted channel {channel_id} in _deliver_message")
        return
    _mark_delivered(msg_id)  # Mark before delivery to prevent races
    _mark_injected(notification)  # Content-level dedup (Fix 3b)

    # Correlation token: attach a unique marker and ask the agent to echo it
    # when replying to CC.  Only a reply carrying THIS marker is forwarded,
    # so private/unrelated assistant output never leaks to the CC channel.
    token = _make_correlation_token()
    notification_with_token = (
        f"{notification}\n\n(To reply on CC, start your message with [{token}] — "
        f"only marked replies are sent to CC, so private notes stay private.)"
    )

    _save_portal_message(notification_with_token, role="user")

    inject_ts = time.time()
    await _inject_into_tmux_serialized(notification_with_token)

    # Wait up to _RESPONSE_WAIT seconds for an assistant reply that is
    # correlated to THIS request (carries the token).
    response_text: str | None = None
    deadline = time.time() + _RESPONSE_WAIT
    while time.time() < deadline:
        await asyncio.sleep(_RESPONSE_POLL)
        candidate = _correlated_assistant_message_after(inject_ts, token)
        if candidate:
            response_text = candidate
            break

    if response_text and channel_id:
        await _cc_send(client, channel_id, response_text)
        logger.info(
            f"[cc_bridge] Replied to channel {channel_id}: "
            f"{response_text[:60]}..."
        )
    elif not response_text:
        logger.warning(
            f"[cc_bridge] No response captured within "
            f"{_RESPONSE_WAIT}s for msg {msg_id}"
        )


# -- Main bridge loops --------------------------------------------------------

async def _cc_poll_loop() -> None:
    """Poll CC for new messages. Deliver immediately if idle, queue if busy."""
    global _last_cc_msg_id, _consecutive_empty_polls
    lock = _bridge_lock
    logger.info("[cc_bridge] Poll loop started.")
    poll_count = 0
    _poll_backoff = _POLL_INTERVAL  # backoff on consecutive failures
    _consecutive_failures = 0

    async with httpx.AsyncClient() as client:
        while True:
            try:
                # -- Hot-reload: re-read state file every 60 polls (~5 min) --
                poll_count += 1
                if poll_count % 60 == 0:
                    _load_state()
                    _load_muted_channels()

                msgs = await _cc_poll(client)

                # -- Fast-forward: if gap is too large, skip to near-current --
                if msgs and len(msgs) >= 49:  # API returned max batch (likely more exist)
                    max_id = max(m.get("id", 0) for m in msgs)
                    gap = max_id - _last_cc_msg_id
                    if gap > 200:  # More than 200 messages behind
                        old_cursor = _last_cc_msg_id
                        # Extract any @mentions and DMs from this batch first
                        urgent = [m for m in msgs
                                  if _is_dm_for_civ(m) or
                                  f"@{CIV_NAME.lower()}" in (m.get("body", "") or "").lower()]
                        if urgent:
                            for m in urgent:
                                msg_id = m.get("id", 0)
                                if msg_id not in _delivered_ids:
                                    notification = _format_injection(m)
                                    async with lock:
                                        await _deliver_message(client, notification, m.get("channel_id"), msg_id)
                                    _mark_delivered(msg_id)
                        # Fast-forward cursor to max of this batch
                        _last_cc_msg_id = max_id
                        _save_state()
                        logger.warning(
                            f"[cc_bridge] Fast-forwarded cursor from {old_cursor} to {max_id} "
                            f"(gap was {gap} messages, delivered {len(urgent)} urgent)"
                        )
                        _consecutive_failures = 0
                        await asyncio.sleep(_POLL_INTERVAL)
                        continue  # Skip normal processing, poll again from new position

                # -- DB-reset detection ------------------------------------
                # If we got 0 messages and since_id > 0, the CC DB may
                # have been wiped (IDs restart from 1).  After several
                # consecutive empties, probe with since_id=0 to check.
                if not msgs and _last_cc_msg_id > 0:
                    _consecutive_empty_polls += 1
                    if _consecutive_empty_polls >= _STALE_RESET_POLLS:
                        probe = await _cc_poll_probe(client)
                        if probe:
                            # Probe returned messages -- could be DB reset or gap jump.
                            # Check: if returned IDs are below our cursor, it's a DB reset.
                            # If above, it's a gap jump (messages past deleted rows).
                            probe_max_id = max(m.get("id", 0) for m in probe)
                            if probe_max_id < _last_cc_msg_id:
                                # DB reset: IDs restarted from 1
                                logger.warning(
                                    f"[cc_bridge] DB reset detected! "
                                    f"since_id={_last_cc_msg_id} but server has "
                                    f"{len(probe)} messages starting from ID 1. "
                                    f"Resetting to 0."
                                )
                                _last_cc_msg_id = 0
                                _save_state()
                            else:
                                # Gap jump: found messages past deleted rows
                                logger.warning(
                                    f"[cc_bridge] Gap jump: found {len(probe)} messages "
                                    f"past cursor {_last_cc_msg_id} (next IDs start at "
                                    f"{min(m.get("id", 0) for m in probe)})"
                                )
                            _consecutive_empty_polls = 0
                            msgs = probe  # process the probe results
                        else:
                            # Server genuinely has no new messages
                            _consecutive_empty_polls = 0
                else:
                    _consecutive_empty_polls = 0

                # -- Reconnect cap: if catching up, keep important + most recent --
                if len(msgs) > _RECONNECT_MSG_CAP:
                    # Preserve DMs and @mentions even if over the cap
                    important = []
                    rest = []
                    for m in msgs:
                        if _is_dm_for_civ(m) or (f"@{CIV_NAME.lower()}" in (m.get("body", "") or "").lower()):
                            important.append(m)
                        else:
                            rest.append(m)
                    # Fill remaining slots from most recent non-important
                    remaining_slots = max(0, _RECONNECT_MSG_CAP - len(important))
                    kept = important + rest[-remaining_slots:] if remaining_slots else important
                    # Update cursor past all skipped messages
                    kept_ids = {m.get("id", 0) for m in kept}
                    for skipped_msg in msgs:
                        sid = skipped_msg.get("id", 0)
                        if sid not in kept_ids and sid > _last_cc_msg_id:
                            _last_cc_msg_id = sid
                    skipped_count = len(msgs) - len(kept)
                    if skipped_count > 0:
                        logger.info(
                            f"[cc_bridge] Catching up: skipped {skipped_count} old messages, "
                            f"keeping {len(kept)} ({len(important)} important)"
                        )
                    msgs = kept

                for msg in msgs:
                    msg_id = msg.get("id", 0)
                    if msg_id > _last_cc_msg_id:
                        _last_cc_msg_id = msg_id

                    if not _is_relevant(msg):
                        continue

                    # -- Dedup: skip already-delivered messages --
                    if msg_id in _delivered_ids:
                        logger.debug(f"[cc_bridge] Skipping duplicate msg {msg_id}")
                        continue

                    # -- Max-age filter: skip stale messages --
                    # EXCEPTION: @mentions and DMs always deliver regardless of age
                    body_lower = (msg.get("body", "") or "").lower()
                    is_mention = f"@{CIV_NAME.lower()}" in body_lower
                    is_dm = _is_dm_for_civ(msg)
                    created_at_str = msg.get("created_at", "")
                    if created_at_str and not is_mention and not is_dm:
                        try:
                            ts_clean = created_at_str.replace("Z", "+00:00") if created_at_str.endswith("Z") else created_at_str
                            msg_dt = datetime.fromisoformat(ts_clean).astimezone(timezone.utc)
                            age = (datetime.now(timezone.utc) - msg_dt).total_seconds()
                            if age > _MAX_MESSAGE_AGE_S:
                                logger.debug(
                                    f"[cc_bridge] Skipping stale msg {msg_id} "
                                    f"({age:.0f}s old)"
                                )
                                continue
                        except (ValueError, TypeError):
                            pass  # deliver if timestamp unparseable
                    elif created_at_str and (is_mention or is_dm):
                        try:
                            ts_clean = created_at_str.replace("Z", "+00:00") if created_at_str.endswith("Z") else created_at_str
                            msg_dt = datetime.fromisoformat(ts_clean).astimezone(timezone.utc)
                            age = (datetime.now(timezone.utc) - msg_dt).total_seconds()
                            if age > _MAX_MESSAGE_AGE_S:
                                logger.info(
                                    f"[cc_bridge] Delivering stale {'@mention' if is_mention else 'DM'} "
                                    f"msg {msg_id} ({age:.0f}s old) — mentions/DMs bypass age filter"
                                )
                        except (ValueError, TypeError):
                            pass

                    # -- Tmux injection filter: only DMs and @mentions --
                    if not _should_inject_to_tmux(msg):
                        logger.debug(
                            f"[cc_bridge] Skipping non-mention message {msg_id} "
                            f"in {msg.get('channel_name', '?')}"
                        )
                        continue

                    notification = _format_injection(msg)
                    channel_id = msg.get("channel_id")
                    sender = msg.get("sender_name") or msg.get("sender_email", "unknown")

                    if _is_claude_busy():
                        # Queue for later delivery
                        try:
                            _cc_message_queue.put_nowait({
                                "msg_id": msg_id,
                                "notification": notification,
                                "channel_id": channel_id,
                                "queued_at": time.time(),
                                "sender": sender,
                            })
                        except asyncio.QueueFull:
                            logger.warning(f"[cc_bridge] Queue full, dropping message from {sender}")
                        else:
                            _mark_delivered(msg_id)  # dedup even before drain
                            logger.info(
                                f"[cc_bridge] Claude busy, queuing message from {sender} "
                                f"(queue depth: {_cc_message_queue.qsize()})"
                            )
                    else:
                        # Deliver immediately (existing behaviour)
                        async with lock:
                            await _deliver_message(client, notification, channel_id, msg_id)
                        _mark_delivered(msg_id)

                _save_state()
                _consecutive_failures = 0
                _poll_backoff = _POLL_INTERVAL

            except Exception as exc:
                logger.warning(f"[cc_bridge] Poll loop error: {exc}")
                _consecutive_failures += 1
                _poll_backoff = min(_POLL_INTERVAL * (2 ** _consecutive_failures), 30)

            await asyncio.sleep(_poll_backoff)


async def _cc_queue_drain_loop() -> None:
    """Periodically drain queued CC messages when Claude is idle."""
    lock = _bridge_lock
    logger.info("[cc_bridge] Queue drain loop started.")

    async with httpx.AsyncClient() as client:
        while True:
            try:
                if not _cc_message_queue.empty():
                    # Peek at the oldest entry without removing
                    entry = _cc_message_queue._queue[0]
                    busy = _is_claude_busy()
                    oldest_age = time.time() - entry["queued_at"]

                    # Deliver if Claude is idle, OR if oldest message exceeds max age
                    if not busy or oldest_age > _QUEUE_MAX_AGE_S:
                        reason = "idle" if not busy else f"max-age ({oldest_age:.0f}s)"
                        entry = _cc_message_queue.get_nowait()
                        logger.info(
                            f"[cc_bridge] Draining queued message from {entry['sender']} "
                            f"(reason: {reason}, remaining: {_cc_message_queue.qsize()})"
                        )
                        async with lock:
                            await _deliver_message(
                                client,
                                entry["notification"],
                                entry["channel_id"],
                                entry["msg_id"],
                            )
                        _mark_delivered(entry["msg_id"])
                    else:
                        logger.debug(
                            f"[cc_bridge] Queue has {_cc_message_queue.qsize()} message(s), "
                            f"Claude still busy (oldest: {oldest_age:.0f}s)"
                        )
            except Exception as exc:
                logger.warning(f"[cc_bridge] Queue drain error: {exc}")

            await asyncio.sleep(_QUEUE_DRAIN_INTERVAL)


async def _cc_heartbeat_loop() -> None:
    logger.info("[cc_bridge] Heartbeat loop started.")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await _cc_heartbeat(client)  # sends online or offline based on pause state
            except Exception as exc:
                logger.warning(f"[cc_bridge] Heartbeat loop error: {exc}")
            await asyncio.sleep(_HEARTBEAT_INTERVAL)


# -- Entry point (called by portal_server._startup) --------------------------

async def on_startup() -> None:
    """Register CC bridge background tasks. Called once at portal startup."""
    if not CIV_KEY:
        logger.info("[cc_bridge] No CC key configured — bridge disabled. Set your CC key in portal Settings to enable.")
        return
    if not CIV_NAME:
        logger.warning("[cc_bridge] Skipping startup -- no CIV_NAME configured.")
        return

    global _bridge_lock, _cc_message_queue
    _bridge_lock = asyncio.Lock()
    _cc_message_queue = asyncio.Queue(maxsize=100)
    _load_state()
    _load_muted_channels()

    asyncio.create_task(_cc_poll_loop())
    asyncio.create_task(_cc_queue_drain_loop())
    asyncio.create_task(_cc_heartbeat_loop())
    logger.info(
        f"[cc_bridge] Started. CC={_CC_BASE}, civ={CIV_NAME}, "
        f"subscribed={SUBSCRIBED_CHANNELS}, since_id={_last_cc_msg_id}"
    )
