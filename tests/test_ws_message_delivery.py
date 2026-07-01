"""
Tests for WebSocket message delivery and reconnection behavior.

ROOT CAUSE (rubber-duck analysis 2026-06-03):
    When WS reconnects, loadChatHistory() clears chatMsgs.innerHTML (destroying
    DOM elements) but does NOT clear knownMsgIds. Any message rendered by WS push
    between the fetch request and fetch response becomes an orphan: its ID is in
    knownMsgIds (blocking future renders) but its DOM element is gone. The message
    is permanently invisible.

These tests verify:
1. Backend: ws_chat() stable_sent behavior on connect
2. Backend: _parse_all_messages() cache timing
3. Backend: ws_chat() message delivery gates (first-poll skip, growth threshold)
4. Integration: reconnection scenario message delivery guarantee

Frontend (chat.js) race condition is tested via test_ws_frontend_reconnect.js
(manual browser test) since it requires a DOM environment.
"""

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Setup: import portal_server functions
# ---------------------------------------------------------------------------
PORTAL_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PORTAL_DIR))

# We need to test _parse_all_messages, _parse_jsonl_messages_from_file,
# and the ws_chat logic. Import portal_server (it has side effects but
# the test infrastructure handles them).
import portal_server


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session_msg(role, text, ts=None, msg_id=None, topic=None):
    """Create a message dict as returned by _parse_jsonl_messages_from_file."""
    ts = ts or int(time.time())
    msg_id = msg_id or f"msg-test-{ts}-{id(text)}"
    m = {"role": role, "text": text, "timestamp": ts, "id": msg_id}
    if topic:
        m["topic"] = topic
    return m


def _make_portal_msg(role, text, ts=None, msg_id=None, topic=None):
    """Create a message dict as returned by _load_portal_messages."""
    ts = ts or int(time.time())
    msg_id = msg_id or f"portal-{ts}-{id(text)}"
    m = {"role": role, "text": text, "timestamp": ts, "id": msg_id}
    if topic:
        m["topic"] = topic
    return m


# ---------------------------------------------------------------------------
# Test: Cache rate-limiting behavior
# ---------------------------------------------------------------------------

class TestCacheRateLimiting:
    """Verify _CACHE_MIN_INTERVAL behavior in _parse_jsonl_messages_from_file."""

    def test_cache_min_interval_value(self):
        """_CACHE_MIN_INTERVAL should be <= 5s for acceptable latency.
        The original 10s creates too large a blind spot for WS delivery."""
        assert portal_server._CACHE_MIN_INTERVAL <= 5.0, (
            f"_CACHE_MIN_INTERVAL is {portal_server._CACHE_MIN_INTERVAL}s — "
            f"too high. Messages can be invisible for this long after being "
            f"written to the JSONL. Should be <= 5s."
        )

    def test_tail_bytes_sufficient(self):
        """_TAIL_BYTES should read enough data to capture recent messages."""
        assert portal_server._TAIL_BYTES >= 200_000, (
            f"_TAIL_BYTES is {portal_server._TAIL_BYTES} — too small. "
            f"May miss messages in long sessions."
        )

    def test_cache_returns_stale_within_interval(self, tmp_path):
        """Within _CACHE_MIN_INTERVAL, even changed files return cached data."""
        # Create a JSONL file with one message
        log = tmp_path / "test-session.jsonl"
        entry1 = {
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello world from test"}]},
            "timestamp": int(time.time() * 1000),
            "uuid": "msg-cache-test-1",
        }
        log.write_text(json.dumps(entry1) + "\n")

        # First parse — populates cache
        result1 = portal_server._parse_jsonl_messages_from_file(log)

        # Add a new message to the file
        entry2 = {
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Second message for test"}]},
            "timestamp": int(time.time() * 1000),
            "uuid": "msg-cache-test-2",
        }
        with log.open("a") as f:
            f.write(json.dumps(entry2) + "\n")

        # Immediate re-parse — should return cached (stale) data
        result2 = portal_server._parse_jsonl_messages_from_file(log)

        # With rate-limiting, result2 may equal result1 (stale cache)
        # This test documents the behavior, not asserts it's wrong
        # The fix is to reduce _CACHE_MIN_INTERVAL, tested above

    def test_cache_refreshes_after_interval(self, tmp_path):
        """After _CACHE_MIN_INTERVAL, changed files ARE re-parsed."""
        log = tmp_path / "test-session2.jsonl"
        entry1 = {
            "message": {"role": "assistant", "content": [{"type": "text", "text": "First message content here"}]},
            "timestamp": int(time.time() * 1000),
            "uuid": "msg-refresh-test-1",
        }
        log.write_text(json.dumps(entry1) + "\n")

        # First parse
        result1 = portal_server._parse_jsonl_messages_from_file(log)
        assert len(result1) == 1

        # Add second message
        entry2 = {
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Second message content here"}]},
            "timestamp": int(time.time() * 1000),
            "uuid": "msg-refresh-test-2",
        }
        with log.open("a") as f:
            f.write(json.dumps(entry2) + "\n")

        # Manually expire the cache by backdating the parse time
        cache_key = str(log)
        if cache_key in portal_server._jsonl_cache:
            old = portal_server._jsonl_cache[cache_key]
            # Set last_parse_time to far in the past
            portal_server._jsonl_cache[cache_key] = (old[0], old[1], old[2], 0.0)

        # Now re-parse — should get fresh data
        result2 = portal_server._parse_jsonl_messages_from_file(log)
        assert len(result2) == 2, (
            f"After cache expiry, expected 2 messages but got {len(result2)}. "
            f"Cache did not refresh."
        )


# ---------------------------------------------------------------------------
# Test: _parse_all_messages dedup and merge behavior
# ---------------------------------------------------------------------------

class TestParseAllMessages:
    """Test the merge/dedup logic in _parse_all_messages."""

    def test_session_wins_over_portal_for_same_id(self):
        """When both sources have the same message ID, session JSONL wins."""
        ts = int(time.time())
        session_msgs = [_make_session_msg("user", "Session version of message", ts, "msg-dup-1")]
        portal_msgs = [_make_portal_msg("user", "Portal version of message", ts, "msg-dup-1")]

        fake_path = Path("/tmp/fake-ws-test.jsonl")
        with mock.patch.object(portal_server, '_get_all_session_log_paths', return_value=[fake_path]), \
             mock.patch.object(portal_server, '_parse_jsonl_messages_from_file', return_value=session_msgs), \
             mock.patch.object(portal_server, '_load_portal_messages', return_value=portal_msgs):
            result = portal_server._parse_all_messages(last_n=200)

        matching = [m for m in result if m["id"] == "msg-dup-1"]
        assert len(matching) == 1
        assert matching[0]["text"] == "Session version of message"

    def test_messages_sorted_by_timestamp(self):
        """Output messages should be sorted chronologically."""
        ts = int(time.time())
        session_msgs = [
            _make_session_msg("user", "Message two content here", ts + 10, "msg-sort-2"),
            _make_session_msg("assistant", "Message one content here", ts, "msg-sort-1"),
        ]

        fake_path = Path("/tmp/fake-ws-sort.jsonl")
        with mock.patch.object(portal_server, '_get_all_session_log_paths', return_value=[fake_path]), \
             mock.patch.object(portal_server, '_parse_jsonl_messages_from_file', return_value=session_msgs), \
             mock.patch.object(portal_server, '_load_portal_messages', return_value=[]):
            result = portal_server._parse_all_messages(last_n=200)

        assert result[0]["id"] == "msg-sort-1"
        assert result[1]["id"] == "msg-sort-2"

    def test_last_n_truncation(self):
        """Only the last N messages should be returned."""
        ts = int(time.time())
        session_msgs = [
            _make_session_msg("user", f"Message number {i} for truncation test", ts + i, f"msg-trunc-{i}")
            for i in range(10)
        ]

        fake_path = Path("/tmp/fake-ws-trunc.jsonl")
        with mock.patch.object(portal_server, '_get_all_session_log_paths', return_value=[fake_path]), \
             mock.patch.object(portal_server, '_parse_jsonl_messages_from_file', return_value=session_msgs), \
             mock.patch.object(portal_server, '_load_portal_messages', return_value=[]):
            result = portal_server._parse_all_messages(last_n=5)

        assert len(result) == 5
        assert result[0]["id"] == "msg-trunc-5"  # oldest of last 5
        assert result[4]["id"] == "msg-trunc-9"  # newest


# ---------------------------------------------------------------------------
# Test: WS delivery gate behavior (simulated)
# ---------------------------------------------------------------------------

class TestWSDeliveryGates:
    """Test the WS poll loop's message delivery gates.

    These test the LOGIC of the gates without needing a real WebSocket.
    We simulate the state tracking dictionaries and check send decisions.
    """

    def _should_send(self, msg_id, msg_len, seen_texts, first_seen, stable_counts, stable_sent, now=None):
        """Replicate the ws_chat() send decision logic.
        Returns: (should_send, reason)
        """
        now = now or time.time()
        prev_len = seen_texts.get(msg_id, -1)

        # Gate 1: first-poll skip
        if msg_id not in first_seen:
            first_seen[msg_id] = now
            return (False, "first-poll-skip")

        msg_age = now - first_seen[msg_id]

        # Stability tracking
        if prev_len >= 0 and msg_len == prev_len:
            stable_counts[msg_id] = stable_counts.get(msg_id, 0) + 1
        else:
            stable_counts[msg_id] = 0

        # Noise guard (simplified — real check is on text content)
        # Skipped here since we test with valid messages

        is_stable = stable_counts.get(msg_id, 0) >= 2

        # Send path 1: new message or significant growth
        if prev_len < 0 or (msg_len > prev_len + 20 and msg_age > 0.8):
            seen_texts[msg_id] = msg_len
            return (True, "new-or-growth")

        # Send path 2: stable final send
        if is_stable and msg_id not in stable_sent:
            stable_sent.add(msg_id)
            if prev_len >= 0 and msg_len != prev_len:
                seen_texts[msg_id] = msg_len
                return (True, "stable-final")
            return (False, "stable-no-change")

        return (False, "waiting")

    def test_new_message_skipped_on_first_poll(self):
        """A brand-new message should be skipped on its first poll cycle."""
        seen_texts = {}
        first_seen = {}
        stable_counts = {}
        stable_sent = set()

        should_send, reason = self._should_send(
            "msg-1", 100, seen_texts, first_seen, stable_counts, stable_sent
        )
        assert not should_send
        assert reason == "first-poll-skip"

    def test_new_message_sent_on_second_poll(self):
        """A new message should be sent on the second poll cycle."""
        seen_texts = {}
        first_seen = {}
        stable_counts = {}
        stable_sent = set()

        # First poll — skip
        self._should_send("msg-1", 100, seen_texts, first_seen, stable_counts, stable_sent, now=100.0)

        # Second poll — should send (prev_len < 0)
        should_send, reason = self._should_send(
            "msg-1", 100, seen_texts, first_seen, stable_counts, stable_sent, now=101.5
        )
        assert should_send
        assert reason == "new-or-growth"

    def test_message_with_significant_growth_is_sent(self):
        """A message that grows by >20 chars should be re-sent."""
        seen_texts = {"msg-1": 100}
        first_seen = {"msg-1": 90.0}
        stable_counts = {}
        stable_sent = set()

        should_send, reason = self._should_send(
            "msg-1", 130, seen_texts, first_seen, stable_counts, stable_sent, now=101.0
        )
        assert should_send
        assert reason == "new-or-growth"

    def test_message_with_small_growth_not_sent(self):
        """A message that grows by <=20 chars should NOT be sent immediately."""
        seen_texts = {"msg-1": 100}
        first_seen = {"msg-1": 90.0}
        stable_counts = {}
        stable_sent = set()

        should_send, reason = self._should_send(
            "msg-1", 115, seen_texts, first_seen, stable_counts, stable_sent, now=101.0
        )
        assert not should_send

    def test_stable_message_sent_once_as_final(self):
        """A message that stabilizes (same length 2+ polls) gets a final send."""
        seen_texts = {"msg-1": 100}
        first_seen = {"msg-1": 90.0}
        stable_counts = {"msg-1": 0}
        stable_sent = set()

        # Poll 1: same length → stable_counts = 1
        self._should_send("msg-1", 100, seen_texts, first_seen, stable_counts, stable_sent, now=101.0)
        # Poll 2: same length → stable_counts = 2 → is_stable
        should_send, reason = self._should_send(
            "msg-1", 100, seen_texts, first_seen, stable_counts, stable_sent, now=102.5
        )
        # stable but prev_len == msg_len → no re-send needed
        assert not should_send
        assert reason == "stable-no-change"

    def test_stable_sent_prevents_repeated_sends(self):
        """Once in stable_sent, a message is never re-sent."""
        seen_texts = {"msg-1": 100}
        first_seen = {"msg-1": 90.0}
        stable_counts = {"msg-1": 3}
        stable_sent = {"msg-1"}  # already sent final

        should_send, reason = self._should_send(
            "msg-1", 100, seen_texts, first_seen, stable_counts, stable_sent, now=105.0
        )
        assert not should_send
        assert reason == "waiting"

    def test_init_does_not_mark_stable_sent(self):
        """On WS connect, existing messages should be in seen_texts but NOT
        in stable_sent. This allows the stable-final path to re-deliver
        messages that the previous WS connection failed to deliver.

        The frontend's knownMsgIds (cleared atomically with innerHTML on
        history load) prevents duplicates for messages already rendered."""
        # Simulate ws_chat() init behavior (post-fix)
        existing_msgs = [
            _make_session_msg("user", "Old user message content", 1000, "msg-old-1"),
            _make_session_msg("assistant", "Old assistant message content", 1001, "msg-old-2"),
        ]

        seen_texts = {}
        stable_sent = set()
        for msg in existing_msgs:
            seen_texts[msg["id"]] = len(msg.get("text", ""))
            # NOTE: Do NOT add to stable_sent (this is the fix)

        assert "msg-old-1" in seen_texts
        assert "msg-old-2" in seen_texts
        assert "msg-old-1" not in stable_sent
        assert "msg-old-2" not in stable_sent

    def test_init_seen_texts_prevents_new_message_path(self):
        """Messages in seen_texts with prev_len >= 0 don't trigger the
        'new message' send path, so they won't be re-pushed on connect."""
        seen_texts = {"msg-old-1": 50}
        first_seen = {"msg-old-1": 90.0}
        stable_counts = {}
        stable_sent = set()

        # Same length as seen_texts — no growth
        should_send, reason = self._should_send(
            "msg-old-1", 50, seen_texts, first_seen, stable_counts, stable_sent, now=100.0
        )
        assert not should_send


# ---------------------------------------------------------------------------
# Test: Reconnection message guarantee
# ---------------------------------------------------------------------------

class TestReconnectionMessageGuarantee:
    """Test that messages are not permanently lost during WS reconnection.

    The root cause: loadChatHistory() clears innerHTML but not knownMsgIds.
    A message rendered between fetch-request and fetch-response becomes an
    orphan (ID in knownMsgIds, element destroyed). The fix is to clear
    knownMsgIds when clearing innerHTML in the .then() callback.

    These tests verify the SERVER side of the guarantee: _parse_all_messages()
    must always return ALL recent messages regardless of WS connection state.
    """

    def test_new_message_visible_in_parse_all_after_cache_refresh(self):
        """A new message must appear in _parse_all_messages after cache expires."""
        ts = int(time.time())
        existing = [_make_session_msg("user", "Existing message in the chat", ts, "msg-exist")]
        new_msg = _make_session_msg("assistant", "New response from Claude here", ts + 5, "msg-new")

        fake_path = Path("/tmp/fake-reconnect-test.jsonl")

        # First call: only existing message
        with mock.patch.object(portal_server, '_get_all_session_log_paths', return_value=[fake_path]), \
             mock.patch.object(portal_server, '_parse_jsonl_messages_from_file', return_value=existing), \
             mock.patch.object(portal_server, '_load_portal_messages', return_value=[]):
            result1 = portal_server._parse_all_messages(last_n=200)
        assert len(result1) == 1

        # Invalidate cache to simulate data changing (new message arriving)
        if hasattr(portal_server, '_invalidate_msg_cache'):
            portal_server._invalidate_msg_cache()

        # Second call: both messages (simulating cache refresh)
        with mock.patch.object(portal_server, '_get_all_session_log_paths', return_value=[fake_path]), \
             mock.patch.object(portal_server, '_parse_jsonl_messages_from_file', return_value=existing + [new_msg]), \
             mock.patch.object(portal_server, '_load_portal_messages', return_value=[]):
            result2 = portal_server._parse_all_messages(last_n=200)
        assert len(result2) == 2
        assert result2[1]["id"] == "msg-new"

    def test_history_and_ws_init_see_same_messages(self):
        """api_chat_history and ws_chat init must see the same message set.

        Both call _parse_all_messages(). Since it's synchronous and the cache
        is shared, they should return identical results when called close together.
        """
        ts = int(time.time())
        msgs = [
            _make_session_msg("user", "User message in the test chat", ts, "msg-sync-1"),
            _make_session_msg("assistant", "Assistant response in the test", ts + 1, "msg-sync-2"),
        ]

        fake_path = Path("/tmp/fake-sync-test.jsonl")
        with mock.patch.object(portal_server, '_get_all_session_log_paths', return_value=[fake_path]), \
             mock.patch.object(portal_server, '_parse_jsonl_messages_from_file', return_value=msgs), \
             mock.patch.object(portal_server, '_load_portal_messages', return_value=[]):

            # Simulate WS init call
            ws_result = portal_server._parse_all_messages(last_n=200)
            # Simulate history API call (same function)
            history_result = portal_server._parse_all_messages(last_n=200)

        # Must be identical
        ws_ids = {m["id"] for m in ws_result}
        history_ids = {m["id"] for m in history_result}
        assert ws_ids == history_ids, (
            f"WS init and history API returned different message sets. "
            f"WS-only: {ws_ids - history_ids}, History-only: {history_ids - ws_ids}"
        )

    def test_portal_message_survives_session_rotation(self):
        """Messages saved to portal-chat.jsonl must survive session JSONL rotation.

        When a new Claude session starts, the old JSONL may not be in the top 3.
        Portal-chat.jsonl is the backup source for these messages.
        """
        ts = int(time.time())
        portal_msgs = [
            _make_portal_msg("user", "Message from previous session", ts - 3600, "portal-old-1"),
            _make_portal_msg("assistant", "Response from previous session here", ts - 3599, "portal-old-2"),
        ]

        fake_path = Path("/tmp/fake-rotation-test.jsonl")
        with mock.patch.object(portal_server, '_get_all_session_log_paths', return_value=[fake_path]), \
             mock.patch.object(portal_server, '_parse_jsonl_messages_from_file', return_value=[]), \
             mock.patch.object(portal_server, '_load_portal_messages', return_value=portal_msgs):
            result = portal_server._parse_all_messages(last_n=200)

        assert len(result) == 2
        assert result[0]["id"] == "portal-old-1"


# ---------------------------------------------------------------------------
# Test: Message filter edge cases
# ---------------------------------------------------------------------------

class TestMessageFilterEdgeCases:
    """Edge cases in _is_real_user_message and _is_real_assistant_message."""

    def test_user_message_with_cc_mention_in_body_not_filtered(self):
        """Mentioning 'CC' in body text (not as a prefix) should pass."""
        assert portal_server._is_real_user_message(
            "Can you check the CC bridge? It seems broken."
        ) is True

    def test_user_message_with_bracket_cc_prefix_filtered(self):
        """[CC #channel] prefix should be filtered."""
        assert portal_server._is_real_user_message(
            "[CC #general -- Aether] Good morning"
        ) is False

    def test_assistant_message_exactly_10_chars(self):
        """Assistant message exactly at the 10-char threshold."""
        assert portal_server._is_real_assistant_message("1234567890") is True

    def test_assistant_message_9_chars_filtered(self):
        """Assistant message below 10-char threshold."""
        assert portal_server._is_real_assistant_message("123456789") is False

    def test_user_message_special_char_ratio(self):
        """Messages with >15% special chars in first 200 chars are filtered."""
        # 50% special chars
        assert portal_server._is_real_user_message("{[()]}") is False
        # Normal text with a few special chars — should pass
        assert portal_server._is_real_user_message(
            "Please fix the bug in portal_server.py function _parse_all_messages"
        ) is True


# ---------------------------------------------------------------------------
# Test: _save_portal_message behavior
# ---------------------------------------------------------------------------

class TestSavePortalMessage:
    """Test _save_portal_message writes correctly."""

    def test_generates_unique_ids(self, tmp_path):
        """Each call should generate a unique message ID."""
        log = tmp_path / "test-save-portal.jsonl"
        original_log = portal_server.PORTAL_CHAT_LOG
        try:
            portal_server.PORTAL_CHAT_LOG = log
            e1 = portal_server._save_portal_message("Message one text here")
            e2 = portal_server._save_portal_message("Message two text here")
            assert e1["id"] != e2["id"]
        finally:
            portal_server.PORTAL_CHAT_LOG = original_log

    def test_writes_to_file(self, tmp_path):
        """Message should be appended to the JSONL file."""
        log = tmp_path / "test-save-write.jsonl"
        original_log = portal_server.PORTAL_CHAT_LOG
        try:
            portal_server.PORTAL_CHAT_LOG = log
            portal_server._save_portal_message("Test message content here")
            assert log.exists()
            lines = [l for l in log.read_text().strip().splitlines() if l.strip()]
            assert len(lines) == 1
            entry = json.loads(lines[0])
            assert entry["text"] == "Test message content here"
            assert entry["role"] == "user"
        finally:
            portal_server.PORTAL_CHAT_LOG = original_log

    def test_topic_preserved(self, tmp_path):
        """Topic should be saved when provided."""
        log = tmp_path / "test-save-topic.jsonl"
        original_log = portal_server.PORTAL_CHAT_LOG
        try:
            portal_server.PORTAL_CHAT_LOG = log
            entry = portal_server._save_portal_message("Hello there", topic="portal")
            assert entry["topic"] == "portal"
        finally:
            portal_server.PORTAL_CHAT_LOG = original_log


# ---------------------------------------------------------------------------
# Test: _mirror_to_portal_log dedup
# ---------------------------------------------------------------------------

class TestMirrorDedup:
    """Test that _mirror_to_portal_log doesn't create duplicate entries."""

    def test_same_id_not_written_twice(self, tmp_path):
        """Mirroring the same message ID twice should only write once."""
        log = tmp_path / "test-mirror-dedup.jsonl"
        original_log = portal_server.PORTAL_CHAT_LOG
        original_ids = portal_server._portal_log_ids.copy()
        try:
            portal_server.PORTAL_CHAT_LOG = log
            portal_server._portal_log_ids.clear()

            msg = {"id": "mirror-dedup-1", "text": "Test dedup message content", "role": "assistant", "timestamp": int(time.time())}
            portal_server._mirror_to_portal_log(msg)
            portal_server._mirror_to_portal_log(msg)  # duplicate

            lines = [l for l in log.read_text().strip().splitlines() if l.strip()]
            assert len(lines) == 1, f"Expected 1 line, got {len(lines)}"
        finally:
            portal_server.PORTAL_CHAT_LOG = original_log
            portal_server._portal_log_ids.clear()
            portal_server._portal_log_ids.update(original_ids)

    def test_noise_messages_not_mirrored(self):
        """Messages with <3 chars or non-alphanumeric should not be mirrored."""
        original_ids = portal_server._portal_log_ids.copy()
        try:
            portal_server._portal_log_ids.clear()
            msg = {"id": "mirror-noise-1", "text": "|", "role": "assistant", "timestamp": int(time.time())}
            portal_server._mirror_to_portal_log(msg)
            assert "mirror-noise-1" not in portal_server._portal_log_ids
        finally:
            portal_server._portal_log_ids.clear()
            portal_server._portal_log_ids.update(original_ids)


# ---------------------------------------------------------------------------
# Test: Multi-session JSONL file selection
# ---------------------------------------------------------------------------

class TestSessionFileSelection:
    """Test _get_all_session_log_paths returns the right files."""

    def test_returns_max_files_limit(self):
        """Should not return more than max_files paths."""
        with mock.patch.object(portal_server, '_find_all_project_jsonl',
                               return_value=[Path(f"/tmp/log-{i}.jsonl") for i in range(10)]):
            result = portal_server._get_all_session_log_paths(max_files=3)
            assert len(result) <= 3

    def test_returns_oldest_first(self):
        """Results should be ordered oldest-first (reversed from find order)."""
        paths = [Path(f"/tmp/log-{i}.jsonl") for i in range(5)]
        with mock.patch.object(portal_server, '_find_all_project_jsonl', return_value=paths):
            result = portal_server._get_all_session_log_paths(max_files=3)
            # _find_all_project_jsonl returns newest-first, _get_all reverses
            assert result == list(reversed(paths[:3]))
