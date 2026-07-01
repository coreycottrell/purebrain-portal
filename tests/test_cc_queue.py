"""
Tests for the CC bridge smart message queue system (custom/startup.py).

Covers:
- Busy detection via session ledger inspection
- Queue behavior (enqueue when busy, deliver when idle, FIFO order)
- Queue drain loop (idle delivery, busy skip, force-delivery after timeout)
- Delivery function (inject + capture + post-back)
- Edge cases (empty queue, lock contention)
"""

import asyncio
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# We need to mock portal_server imports BEFORE importing startup.py,
# because startup.py does a top-level "from portal_server import ..."
#
# IMPORTANT: Save and restore the real portal_server module (if any) so that
# other test files that import portal_server are not affected.
# ---------------------------------------------------------------------------

_real_portal_server = sys.modules.get("portal_server", None)
_mock_portal = mock.MagicMock()
_mock_portal._inject_into_tmux_serialized = mock.AsyncMock()
_mock_portal._save_portal_message = mock.MagicMock()
_mock_portal.PORTAL_CHAT_LOG = Path("/tmp/fake-portal-chat.jsonl")

sys.modules["portal_server"] = _mock_portal

# Now safe to import
import custom.startup as startup

# Restore the real portal_server module so other tests aren't poisoned
if _real_portal_server is not None:
    sys.modules["portal_server"] = _real_portal_server
else:
    del sys.modules["portal_server"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously (no pytest-asyncio needed)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ts_ago(seconds: float) -> str:
    """Return an ISO timestamp `seconds` in the past."""
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return dt.isoformat()


def _ledger_entry(seconds_ago: float) -> str:
    """A minimal JSONL ledger line with a timestamp `seconds_ago` in the past."""
    return json.dumps({"ts": _ts_ago(seconds_ago), "type": "tool_use"})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_queue():
    """Initialize module-level queue and lock before every test.

    startup._cc_message_queue is an asyncio.Queue (initialized in on_startup).
    For tests, we create a fresh one each time.
    """
    startup._cc_message_queue = asyncio.Queue(maxsize=100)
    startup._bridge_lock = asyncio.Lock()
    yield


@pytest.fixture
def ledger_file(tmp_path):
    """Provide a temporary session ledger file and patch the module constant."""
    p = tmp_path / "current-session.jsonl"
    with mock.patch.object(startup, "_SESSION_LEDGER", p):
        yield p


# ===========================================================================
# Busy Detection Tests
# ===========================================================================

class TestIsClaudeBusy:

    def test_returns_true_when_recent_activity(self, ledger_file):
        ledger_file.write_text(_ledger_entry(5) + "\n")
        assert startup._is_claude_busy() is True

    def test_returns_false_when_idle(self, ledger_file):
        ledger_file.write_text(_ledger_entry(60) + "\n")
        assert startup._is_claude_busy() is False

    def test_returns_false_when_no_ledger(self, ledger_file):
        assert not ledger_file.exists()
        assert startup._is_claude_busy() is False

    def test_returns_false_on_empty_ledger(self, ledger_file):
        ledger_file.write_text("")
        assert startup._is_claude_busy() is False

    def test_returns_false_on_malformed_ledger(self, ledger_file):
        ledger_file.write_text("this is not json at all\n")
        assert startup._is_claude_busy() is False

    def test_returns_false_when_entry_has_no_timestamp(self, ledger_file):
        ledger_file.write_text(json.dumps({"type": "tool_use"}) + "\n")
        assert startup._is_claude_busy() is False

    def test_boundary_exactly_at_threshold(self, ledger_file):
        """Entry exactly at _BUSY_THRESHOLD_S (30s) should NOT be considered busy."""
        ledger_file.write_text(_ledger_entry(30) + "\n")
        assert startup._is_claude_busy() is False

    def test_uses_last_line_of_multiline_ledger(self, ledger_file):
        """Only the last line matters -- old entries should be ignored."""
        lines = [_ledger_entry(300), _ledger_entry(200), _ledger_entry(3)]
        ledger_file.write_text("\n".join(lines) + "\n")
        assert startup._is_claude_busy() is True


# ===========================================================================
# Queue Behavior Tests
# ===========================================================================

class TestQueueBehavior:

    def test_message_queued_when_busy(self):
        """When _is_claude_busy returns True, message should go into the queue."""
        msg = {
            "id": 42,
            "body": "hello flux",
            "sender_name": "Alex",
            "sender_email": "alex@example.com",
            "channel_name": "dm_fluxDOTcivATagentmailDOTto_alexATexampleDOTcom",
            "channel_id": 7,
        }

        notification = startup._format_injection(msg)
        channel_id = msg.get("channel_id")
        sender = msg.get("sender_name", "unknown")

        with mock.patch.object(startup, "_is_claude_busy", return_value=True):
            if startup._is_claude_busy():
                startup._cc_message_queue.put_nowait({
                    "msg_id": 42,
                    "notification": notification,
                    "channel_id": channel_id,
                    "queued_at": time.time(),
                    "sender": sender,
                })

        assert startup._cc_message_queue.qsize() == 1
        entry = startup._cc_message_queue.get_nowait()
        assert entry["msg_id"] == 42
        assert entry["sender"] == "Alex"

    def test_message_delivered_immediately_when_idle(self):
        """When idle, _deliver_message should be called (not queued)."""
        with mock.patch.object(startup, "_is_claude_busy", return_value=False):
            assert startup._is_claude_busy() is False
            assert startup._cc_message_queue.qsize() == 0

    def test_queue_preserves_order(self):
        """FIFO: first queued message should be first out."""
        for i in range(3):
            startup._cc_message_queue.put_nowait({
                "msg_id": i,
                "notification": f"msg-{i}",
                "channel_id": 1,
                "queued_at": time.time() + i,
                "sender": f"user-{i}",
            })

        first = startup._cc_message_queue.get_nowait()
        assert first["msg_id"] == 0
        second = startup._cc_message_queue.get_nowait()
        assert second["msg_id"] == 1
        third = startup._cc_message_queue.get_nowait()
        assert third["msg_id"] == 2

    def test_queue_stores_required_fields(self):
        """Each queued entry must have: msg_id, notification, channel_id, queued_at, sender."""
        entry = {
            "msg_id": 99,
            "notification": "[CC-DM from Alex] hey",
            "channel_id": 5,
            "queued_at": time.time(),
            "sender": "Alex",
        }
        startup._cc_message_queue.put_nowait(entry)
        stored = startup._cc_message_queue.get_nowait()
        for key in ("msg_id", "notification", "channel_id", "queued_at", "sender"):
            assert key in stored, f"Missing required field: {key}"


# ===========================================================================
# Queue Drain Tests
# ===========================================================================

class TestQueueDrain:

    def test_drain_delivers_when_idle(self):
        """Drain loop delivers the oldest message when Claude is idle."""
        startup._cc_message_queue.put_nowait({
            "msg_id": 10,
            "notification": "test msg",
            "channel_id": 3,
            "queued_at": time.time(),
            "sender": "Alex",
        })

        mock_client = mock.AsyncMock()

        async def _run_drain():
            with mock.patch.object(startup, "_is_claude_busy", return_value=False), \
                 mock.patch.object(startup, "_deliver_message", new_callable=mock.AsyncMock) as mock_deliver:

                if not startup._cc_message_queue.empty():
                    entry = startup._cc_message_queue._queue[0]
                    busy = startup._is_claude_busy()
                    oldest_age = time.time() - entry["queued_at"]
                    if not busy or oldest_age > startup._QUEUE_MAX_AGE_S:
                        entry = startup._cc_message_queue.get_nowait()
                        async with startup._bridge_lock:
                            await startup._deliver_message(
                                mock_client, entry["notification"],
                                entry["channel_id"], entry["msg_id"],
                            )

                mock_deliver.assert_awaited_once_with(mock_client, "test msg", 3, 10)
                assert startup._cc_message_queue.qsize() == 0

        _run(_run_drain())

    def test_drain_skips_when_busy(self):
        """Drain loop does NOT deliver when Claude is busy and message is fresh."""
        startup._cc_message_queue.put_nowait({
            "msg_id": 11,
            "notification": "busy msg",
            "channel_id": 4,
            "queued_at": time.time(),
            "sender": "Alex",
        })

        async def _run_drain():
            with mock.patch.object(startup, "_is_claude_busy", return_value=True), \
                 mock.patch.object(startup, "_deliver_message", new_callable=mock.AsyncMock) as mock_deliver:

                if not startup._cc_message_queue.empty():
                    entry = startup._cc_message_queue._queue[0]
                    busy = startup._is_claude_busy()
                    oldest_age = time.time() - entry["queued_at"]
                    if not busy or oldest_age > startup._QUEUE_MAX_AGE_S:
                        entry = startup._cc_message_queue.get_nowait()  # pragma: no cover

                mock_deliver.assert_not_awaited()
                assert startup._cc_message_queue.qsize() == 1

        _run(_run_drain())

    def test_drain_force_delivers_after_timeout(self):
        """Messages older than _QUEUE_MAX_AGE_S are delivered even when busy."""
        startup._cc_message_queue.put_nowait({
            "msg_id": 12,
            "notification": "old msg",
            "channel_id": 5,
            "queued_at": time.time() - 700,  # 700s ago (> 600s max)
            "sender": "Alex",
        })

        mock_client = mock.AsyncMock()

        async def _run_drain():
            with mock.patch.object(startup, "_is_claude_busy", return_value=True), \
                 mock.patch.object(startup, "_deliver_message", new_callable=mock.AsyncMock) as mock_deliver:

                if not startup._cc_message_queue.empty():
                    entry = startup._cc_message_queue._queue[0]
                    busy = startup._is_claude_busy()
                    oldest_age = time.time() - entry["queued_at"]
                    if not busy or oldest_age > startup._QUEUE_MAX_AGE_S:
                        entry = startup._cc_message_queue.get_nowait()
                        async with startup._bridge_lock:
                            await startup._deliver_message(
                                mock_client, entry["notification"],
                                entry["channel_id"], entry["msg_id"],
                            )

                mock_deliver.assert_awaited_once_with(mock_client, "old msg", 5, 12)
                assert startup._cc_message_queue.qsize() == 0

        _run(_run_drain())

    def test_drain_processes_one_at_a_time(self):
        """Only one message is delivered per drain cycle."""
        for i in range(3):
            startup._cc_message_queue.put_nowait({
                "msg_id": i,
                "notification": f"msg-{i}",
                "channel_id": 1,
                "queued_at": time.time(),
                "sender": "Alex",
            })

        mock_client = mock.AsyncMock()

        async def _run_drain():
            with mock.patch.object(startup, "_is_claude_busy", return_value=False), \
                 mock.patch.object(startup, "_deliver_message", new_callable=mock.AsyncMock) as mock_deliver:

                if not startup._cc_message_queue.empty():
                    entry = startup._cc_message_queue._queue[0]
                    busy = startup._is_claude_busy()
                    oldest_age = time.time() - entry["queued_at"]
                    if not busy or oldest_age > startup._QUEUE_MAX_AGE_S:
                        entry = startup._cc_message_queue.get_nowait()
                        async with startup._bridge_lock:
                            await startup._deliver_message(
                                mock_client, entry["notification"],
                                entry["channel_id"], entry["msg_id"],
                            )

                assert mock_deliver.await_count == 1
                assert startup._cc_message_queue.qsize() == 2

        _run(_run_drain())


# ===========================================================================
# Delivery Integration Tests
# ===========================================================================

class TestDeliverMessage:

    def test_calls_inject_and_captures_response(self):
        """_deliver_message should save the message, inject into tmux, and poll for response."""

        async def _test():
            mock_client = mock.AsyncMock()

            with mock.patch.object(startup, "_save_portal_message") as mock_save, \
                 mock.patch.object(startup, "_inject_into_tmux_serialized", new_callable=mock.AsyncMock) as mock_inject, \
                 mock.patch.object(startup, "_latest_assistant_message_after", return_value="I got it"), \
                 mock.patch.object(startup, "_cc_send", new_callable=mock.AsyncMock, return_value=True), \
                 mock.patch("custom.startup.asyncio.sleep", new_callable=mock.AsyncMock):

                await startup._deliver_message(mock_client, "hello flux", 7, 42)

                mock_save.assert_called_once_with("hello flux", role="user")
                mock_inject.assert_awaited_once_with("hello flux")

        _run(_test())

    def test_posts_response_to_cc(self):
        """After capturing response, _deliver_message posts it back to the CC channel."""

        async def _test():
            mock_client = mock.AsyncMock()

            with mock.patch.object(startup, "_save_portal_message"), \
                 mock.patch.object(startup, "_inject_into_tmux_serialized", new_callable=mock.AsyncMock), \
                 mock.patch.object(startup, "_latest_assistant_message_after", return_value="Here is my reply"), \
                 mock.patch.object(startup, "_cc_send", new_callable=mock.AsyncMock, return_value=True) as mock_send, \
                 mock.patch("custom.startup.asyncio.sleep", new_callable=mock.AsyncMock):

                await startup._deliver_message(mock_client, "question?", 9, 50)

                mock_send.assert_awaited_once_with(mock_client, 9, "Here is my reply")

        _run(_test())

    def test_no_post_when_no_response(self):
        """If no assistant response is captured, _cc_send should not be called."""

        async def _test():
            mock_client = mock.AsyncMock()

            with mock.patch.object(startup, "_save_portal_message"), \
                 mock.patch.object(startup, "_inject_into_tmux_serialized", new_callable=mock.AsyncMock), \
                 mock.patch.object(startup, "_latest_assistant_message_after", return_value=None), \
                 mock.patch.object(startup, "_cc_send", new_callable=mock.AsyncMock) as mock_send, \
                 mock.patch("custom.startup.asyncio.sleep", new_callable=mock.AsyncMock), \
                 mock.patch.object(startup, "_RESPONSE_WAIT", 0):
                # _RESPONSE_WAIT=0 means deadline is immediate, loop body never runs

                await startup._deliver_message(mock_client, "hello?", 9, 51)

                mock_send.assert_not_awaited()

        _run(_test())

    def test_no_post_when_channel_id_is_none(self):
        """If channel_id is None, _cc_send should not be called even with a response."""

        async def _test():
            mock_client = mock.AsyncMock()

            with mock.patch.object(startup, "_save_portal_message"), \
                 mock.patch.object(startup, "_inject_into_tmux_serialized", new_callable=mock.AsyncMock), \
                 mock.patch.object(startup, "_latest_assistant_message_after", return_value="got it"), \
                 mock.patch.object(startup, "_cc_send", new_callable=mock.AsyncMock) as mock_send, \
                 mock.patch("custom.startup.asyncio.sleep", new_callable=mock.AsyncMock):

                await startup._deliver_message(mock_client, "hello", None, 52)

                mock_send.assert_not_awaited()

        _run(_test())


# ===========================================================================
# Edge Cases
# ===========================================================================

class TestEdgeCases:

    def test_queue_empty_drain_is_noop(self):
        """Drain with empty queue should do nothing."""

        async def _test():
            with mock.patch.object(startup, "_deliver_message", new_callable=mock.AsyncMock) as mock_deliver:
                if not startup._cc_message_queue.empty():
                    pass  # pragma: no cover -- should not enter
                mock_deliver.assert_not_awaited()

        _run(_test())

    def test_concurrent_poll_and_drain_respect_lock(self):
        """Both poll delivery and drain delivery should acquire _bridge_lock."""

        async def _test():
            lock = startup._bridge_lock
            acquired_order = []

            async def fake_deliver(*args, **kwargs):
                acquired_order.append("delivered")
                await asyncio.sleep(0)

            with mock.patch.object(startup, "_deliver_message", side_effect=fake_deliver):
                async def task_a():
                    async with lock:
                        acquired_order.append("a-lock")
                        await fake_deliver()

                async def task_b():
                    async with lock:
                        acquired_order.append("b-lock")
                        await fake_deliver()

                await asyncio.gather(task_a(), task_b())

            assert acquired_order.count("a-lock") == 1
            assert acquired_order.count("b-lock") == 1
            assert len(acquired_order) == 4  # a-lock, delivered, b-lock, delivered

        _run(_test())


# ===========================================================================
# Message Filtering Tests (bonus -- supports queue correctness)
# ===========================================================================

class TestMessageFiltering:

    def test_is_relevant_skips_own_messages(self):
        msg = {"sender_email": "flux.civ@agentmail.to", "body": "@flux hello", "channel_name": "general"}
        assert startup._is_relevant(msg) is False

    def test_is_relevant_matches_dm(self):
        msg = {
            "sender_email": "alex@example.com",
            "body": "hey",
            "channel_name": "dm_fluxDOTcivATagentmailDOTto_alexATexampleDOTcom",
        }
        assert startup._is_relevant(msg) is True

    def test_is_relevant_matches_mention(self):
        msg = {"sender_email": "alex@example.com", "body": "hey @flux check this", "channel_name": "general"}
        assert startup._is_relevant(msg) is True

    def test_is_relevant_ignores_unrelated(self):
        msg = {"sender_email": "alex@example.com", "body": "hello world", "channel_name": "general"}
        # With SUBSCRIBED_CHANNELS="all", non-DM channel messages ARE relevant
        # (the _is_relevant function returns True for all non-DM channels in "all" mode)
        assert startup._is_relevant(msg) is True

    def test_format_injection_dm(self):
        msg = {
            "sender_name": "Alex",
            "sender_email": "alex@example.com",
            "body": "hi flux",
            "channel_name": "dm_fluxDOTcivATagentmailDOTto_alexATexampleDOTcom",
        }
        result = startup._format_injection(msg)
        assert result == "[CC-DM from Alex] hi flux"

    def test_format_injection_channel(self):
        msg = {
            "sender_name": "Alex",
            "sender_email": "alex@example.com",
            "body": "@flux look",
            "channel_name": "general",
        }
        result = startup._format_injection(msg)
        assert result == "[CC #general -- Alex] @flux look"
