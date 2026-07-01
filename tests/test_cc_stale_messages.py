"""
Tests for 4 stale CC message bugs in cc_bridge.py.

What we're verifying: CC bridge deduplication and cursor integrity
What coder discovered: Hot-reload, queuing, and delivery all have dedup gaps
What descendants inherit: Regression tests for message dedup invariants
Why this matters: Duplicate messages confuse humans and waste agent context

These tests are written in the RED phase of TDD -- they MUST FAIL against
the current production code.  Once the bugs are fixed, they will pass (GREEN).

Bug #1: _load_state() unconditionally overwrites _last_cc_msg_id, causing
        cursor rewind when hot-reload triggers mid-session.
Bug #2: Messages queued (Claude busy) are NOT marked in _delivered_ids,
        so cursor rewind + re-fetch bypasses dedup.
Bug #3: _deliver_message -> _save_portal_message has no dedup; duplicate
        deliveries create duplicate entries in portal-chat.jsonl.
Bug #4: (Frontend) knownMsgIds cleared on reconnect -- documented only.
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Mock portal_server before importing cc_bridge (same pattern as test_cc_queue)
# ---------------------------------------------------------------------------

_real_portal_server = sys.modules.get("portal_server", None)
_mock_portal = mock.MagicMock()
_mock_portal._inject_into_tmux_serialized = mock.AsyncMock()
_mock_portal._save_portal_message = mock.MagicMock()
_mock_portal.PORTAL_CHAT_LOG = Path("/tmp/test-stale-portal-chat.jsonl")

sys.modules["portal_server"] = _mock_portal

# Now safe to import
import cc_bridge

# Restore real module
if _real_portal_server is not None:
    sys.modules["portal_server"] = _real_portal_server
else:
    del sys.modules["portal_server"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_bridge_state():
    """Reset cc_bridge module-level state before each test."""
    cc_bridge._last_cc_msg_id = 0
    cc_bridge._delivered_ids.clear()
    cc_bridge._sent_content_keys.clear()
    cc_bridge._bridge_lock = asyncio.Lock()
    cc_bridge._cc_message_queue = asyncio.Queue(maxsize=100)
    cc_bridge._consecutive_empty_polls = 0
    yield


@pytest.fixture
def state_file(tmp_path):
    """Provide a temp state file and patch _STATE_FILE to point at it."""
    p = tmp_path / "cc-bridge-state-test.json"
    with mock.patch.object(cc_bridge, "_STATE_FILE", p):
        yield p


@pytest.fixture
def chat_log(tmp_path):
    """Provide a temp portal-chat.jsonl and patch PORTAL_CHAT_LOG."""
    p = tmp_path / "portal-chat.jsonl"
    p.write_text("")
    with mock.patch.object(cc_bridge, "PORTAL_CHAT_LOG", p):
        yield p


# ===========================================================================
# Bug #1: Hot-reload regresses since_id
#
# _load_state() at line 124 unconditionally sets:
#     _last_cc_msg_id = int(data.get("since_id", 0))
#
# When hot-reload fires (every 60 polls), if the in-memory cursor has
# advanced beyond the state file value, the cursor REWINDS.
#
# Expected fix: _load_state() should use max(current, file_value).
# ===========================================================================

class TestBug1HotReloadRegressesSinceId:

    def test_load_state_does_not_rewind_cursor(self, state_file):
        """_load_state() must NOT regress _last_cc_msg_id below its
        current in-memory value.

        Scenario: In-memory cursor = 100, state file has since_id = 50.
        After _load_state(), cursor should still be >= 100.

        EXPECTED TO FAIL: current code unconditionally overwrites to 50.
        """
        # Advance in-memory cursor to 100
        cc_bridge._last_cc_msg_id = 100

        # Write state file with an OLDER cursor value
        state_file.write_text(json.dumps({"since_id": 50}))

        # Hot-reload triggers _load_state()
        cc_bridge._load_state()

        # The cursor must NOT have gone backwards
        assert cc_bridge._last_cc_msg_id >= 100, (
            f"Cursor regressed from 100 to {cc_bridge._last_cc_msg_id}! "
            f"_load_state() must not rewind the in-memory cursor."
        )

    def test_load_state_can_advance_cursor_forward(self, state_file):
        """_load_state() SHOULD advance the cursor if the file is ahead.

        Scenario: In-memory cursor = 50, state file has since_id = 100.
        After _load_state(), cursor should be 100.

        This test should PASS even against current code (sanity check).
        """
        cc_bridge._last_cc_msg_id = 50
        state_file.write_text(json.dumps({"since_id": 100}))

        cc_bridge._load_state()

        assert cc_bridge._last_cc_msg_id == 100, (
            f"Expected cursor to advance to 100, got {cc_bridge._last_cc_msg_id}"
        )


# ===========================================================================
# Bug #2: Queued messages not marked delivered
#
# In _cc_poll_loop lines 690-706, when Claude is busy and a message is
# queued via _cc_message_queue.put_nowait(), _mark_delivered() is NOT
# called. Compare with the immediate-delivery path (line 711) which
# DOES call _mark_delivered().
#
# If cursor rewinds (Bug #1) and the same messages are re-fetched, the
# dedup check at line 658 ("if msg_id in _delivered_ids") fails to
# filter them because they were never marked.
#
# Expected fix: Call _mark_delivered(msg_id) when queuing, not just
# when draining.
# ===========================================================================

class TestBug2QueuedMessagesNotMarkedDelivered:

    def test_queued_message_is_marked_delivered(self):
        """When a message is queued (Claude busy), its msg_id must be
        added to _delivered_ids immediately (not deferred to drain).

        Scenario: Message with id=42 arrives while Claude is busy.
        It goes into the queue. Assert 42 is in _delivered_ids.

        EXPECTED TO FAIL: current code only calls _mark_delivered on
        the immediate-delivery path (line 711), not the queue path.
        """
        msg_id = 42
        notification = "[CC-DM from Alex] test message"
        channel_id = 7

        # Simulate the queuing path from _cc_poll_loop lines 690-706
        cc_bridge._cc_message_queue.put_nowait({
            "msg_id": msg_id,
            "notification": notification,
            "channel_id": channel_id,
            "queued_at": time.time(),
            "sender": "Alex",
        })

        # In the actual code, _mark_delivered is NOT called here.
        # We replicate the exact code path: just put_nowait, no _mark_delivered.
        # The assertion checks the invariant that SHOULD hold:
        assert msg_id in cc_bridge._delivered_ids, (
            f"Message {msg_id} was queued but NOT marked as delivered. "
            f"If cursor rewinds, this message will be re-fetched and "
            f"bypass the dedup check at line 658."
        )

    def test_requeued_message_after_cursor_rewind_is_deduplicated(self, state_file):
        """End-to-end scenario: message queued, cursor rewinds, same
        message re-fetched -- it should be caught by dedup.

        Steps:
        1. Queue message id=42 (Claude busy)
        2. Rewind cursor via _load_state() (Bug #1)
        3. Check if msg_id=42 passes the dedup filter

        EXPECTED TO FAIL: since queuing doesn't mark delivered, the
        dedup check at line 658 will NOT catch the re-fetch.
        """
        msg_id = 42

        # Step 1: Queue the message (simulating busy path)
        cc_bridge._cc_message_queue.put_nowait({
            "msg_id": msg_id,
            "notification": "[CC-DM from Alex] hello",
            "channel_id": 7,
            "queued_at": time.time(),
            "sender": "Alex",
        })

        # Step 2: Advance cursor past the message, then rewind
        cc_bridge._last_cc_msg_id = 50
        state_file.write_text(json.dumps({"since_id": 30}))
        cc_bridge._load_state()
        # Now cursor is at 30 (rewound), msg_id=42 would be re-fetched

        # Step 3: Check dedup -- msg_id should be in _delivered_ids
        assert msg_id in cc_bridge._delivered_ids, (
            f"Message {msg_id} was queued but not in _delivered_ids. "
            f"After cursor rewind to {cc_bridge._last_cc_msg_id}, "
            f"this message would be re-fetched and delivered again."
        )


# ===========================================================================
# Bug #3: _save_portal_message has no dedup
#
# _deliver_message() at line 546 calls _save_portal_message(notification,
# role="user") which always writes a new entry to portal-chat.jsonl with
# a unique ID. If the same message is delivered twice (due to Bug #1+#2),
# two identical entries appear in the chat log.
#
# Expected fix: _save_portal_message should check for duplicate content
# or msg_id before writing, or _deliver_message should guard against
# duplicate invocations.
# ===========================================================================

class TestBug3SavePortalMessageNoDedup:

    def test_duplicate_delivery_creates_single_chat_entry(self, chat_log):
        """Calling _deliver_message() twice with the same msg_id and
        notification text should result in only ONE entry in
        portal-chat.jsonl.

        EXPECTED TO FAIL: _save_portal_message always appends a new
        entry regardless of duplicates.
        """
        notification = "[CC-DM from Alex] important message"
        msg_id = 42

        # We need to track what _save_portal_message writes.
        # The real _save_portal_message writes to PORTAL_CHAT_LOG.
        # We'll use a real file and a side_effect that mimics writing.
        entries_written = []

        def mock_save(text, role="user"):
            entry = {
                "id": f"cc-{time.time_ns()}",
                "role": role,
                "text": text,
                "timestamp": time.time(),
            }
            entries_written.append(entry)
            with open(chat_log, "a") as f:
                f.write(json.dumps(entry) + "\n")

        async def _test():
            mock_client = mock.AsyncMock()

            with mock.patch.object(cc_bridge, "_save_portal_message", side_effect=mock_save), \
                 mock.patch.object(cc_bridge, "_inject_into_tmux_serialized", new_callable=mock.AsyncMock), \
                 mock.patch.object(cc_bridge, "_correlated_assistant_message_after", return_value=None), \
                 mock.patch.object(cc_bridge, "_cc_send", new_callable=mock.AsyncMock), \
                 mock.patch("cc_bridge.asyncio.sleep", new_callable=mock.AsyncMock), \
                 mock.patch.object(cc_bridge, "_RESPONSE_WAIT", 0):

                # Deliver the same message twice (simulating rewind + re-fetch)
                await cc_bridge._deliver_message(mock_client, notification, 7, msg_id)
                await cc_bridge._deliver_message(mock_client, notification, 7, msg_id)

        _run(_test())

        # Read the chat log -- should have exactly ONE user entry with this text
        lines = [l for l in chat_log.read_text().strip().splitlines() if l.strip()]
        user_entries = []
        for line in lines:
            entry = json.loads(line)
            # _deliver_message appends a per-request correlation token to the
            # saved text, so match by prefix rather than exact equality.
            if entry.get("role") == "user" and entry.get("text", "").startswith(notification):
                user_entries.append(entry)

        assert len(user_entries) == 1, (
            f"Expected exactly 1 portal-chat entry for the message, "
            f"but found {len(user_entries)}. Duplicate deliveries create "
            f"duplicate entries because _save_portal_message has no dedup."
        )

    def test_deliver_message_guards_against_duplicate_msg_id(self):
        """_deliver_message should check _delivered_ids and skip if the
        msg_id was already delivered.

        EXPECTED TO FAIL: _deliver_message has no such guard.
        """
        msg_id = 42

        # Pre-mark as delivered
        cc_bridge._mark_delivered(msg_id)

        call_count = 0

        def counting_save(text, role="user"):
            nonlocal call_count
            call_count += 1

        async def _test():
            mock_client = mock.AsyncMock()

            with mock.patch.object(cc_bridge, "_save_portal_message", side_effect=counting_save), \
                 mock.patch.object(cc_bridge, "_inject_into_tmux_serialized", new_callable=mock.AsyncMock), \
                 mock.patch.object(cc_bridge, "_correlated_assistant_message_after", return_value=None), \
                 mock.patch.object(cc_bridge, "_cc_send", new_callable=mock.AsyncMock), \
                 mock.patch("cc_bridge.asyncio.sleep", new_callable=mock.AsyncMock), \
                 mock.patch.object(cc_bridge, "_RESPONSE_WAIT", 0):

                # Attempt to deliver a message that's already marked delivered
                await cc_bridge._deliver_message(mock_client, "[CC-DM from Alex] dup", 7, msg_id)

        _run(_test())

        assert call_count == 0, (
            f"_deliver_message was called for msg_id={msg_id} which is already "
            f"in _delivered_ids, but _save_portal_message was still called "
            f"{call_count} time(s). _deliver_message should skip duplicates."
        )
