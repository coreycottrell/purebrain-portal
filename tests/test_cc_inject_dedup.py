"""TDD tests for Fix 3b: CC double-delivery / injection dedup.

Bug: the same CC message was injected into the agent twice (observed once as
`[CC #general]` and once as `[CC:]`). The msg_id-based tracker can miss this
when a message reaches `_deliver_message` via two paths (immediate vs
queue-drain vs gap-jump probe) or after the msg_id tracker expires.

Fix: a content-level injection guard (`_already_injected` / `_mark_injected`)
ensures the SAME notification text injects ONCE within the dedup window.
"""
import asyncio
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

# Mock portal_server before importing cc_bridge (same pattern as test_cc_stale_messages)
_real_portal_server = sys.modules.get("portal_server", None)
_mock_portal = mock.MagicMock()
_mock_portal._inject_into_tmux_serialized = mock.AsyncMock()
_mock_portal._save_portal_message = mock.MagicMock()
_mock_portal._latest_assistant_message_after = mock.MagicMock(return_value=None)
_mock_portal._correlated_assistant_message_after = mock.MagicMock(return_value=None)
_mock_portal.PORTAL_CHAT_LOG = Path("/tmp/test-inject-dedup-chat.jsonl")
sys.modules["portal_server"] = _mock_portal

import cc_bridge

if _real_portal_server is not None:
    sys.modules["portal_server"] = _real_portal_server
else:
    del sys.modules["portal_server"]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _reset_state():
    cc_bridge._delivered_ids.clear()
    cc_bridge._sent_content_keys.clear()
    cc_bridge._injected_content_keys.clear()
    cc_bridge._bridge_lock = asyncio.Lock()
    cc_bridge._muted_channels = set()
    cc_bridge._RESPONSE_WAIT = 0  # don't actually wait for a reply
    yield


# -- Unit-level guard tests --------------------------------------------------

def test_already_injected_false_for_new_content():
    assert cc_bridge._already_injected("[CC #general -- alice] hello") is False


def test_mark_then_already_injected_true():
    note = "[CC #general -- alice] hello"
    cc_bridge._mark_injected(note)
    assert cc_bridge._already_injected(note) is True


def test_injection_dedup_expires():
    note = "[CC #general -- alice] hello"
    cc_bridge._mark_injected(note)
    # Force the timestamp to be older than the window
    key = cc_bridge._injection_key(note)
    cc_bridge._injected_content_keys[key] = time.time() - (cc_bridge._INJECT_DEDUP_EXPIRY_S + 10)
    assert cc_bridge._already_injected(note) is False


# -- Integration: _deliver_message injects the SAME content only once --------

def test_deliver_message_injects_same_content_once():
    """Two delivery attempts with identical notification text but different
    code-path msg_ids must result in exactly ONE tmux injection."""
    inject_mock = mock.AsyncMock()
    save_mock = mock.MagicMock()
    notification = "[CC #general -- alice] please review PR"

    with mock.patch.object(cc_bridge, "_inject_into_tmux_serialized", inject_mock), \
         mock.patch.object(cc_bridge, "_save_portal_message", save_mock), \
         mock.patch.object(cc_bridge, "_cc_send", mock.AsyncMock()), \
         mock.patch.object(cc_bridge, "_correlated_assistant_message_after", return_value=None):

        client = mock.MagicMock()

        async def deliver_twice():
            # First path delivers it
            await cc_bridge._deliver_message(client, notification, 1, 100)
            # Second path (e.g. gap-jump/probe) re-delivers same content, new msg_id
            await cc_bridge._deliver_message(client, notification, 1, 101)

        _run(deliver_twice())

    assert inject_mock.await_count == 1, (
        f"expected ONE injection of identical content, got {inject_mock.await_count}"
    )
    assert save_mock.call_count == 1, (
        f"expected ONE portal-chat save, got {save_mock.call_count}"
    )


def test_deliver_message_different_content_injects_both():
    """Sanity: genuinely different messages are NOT deduped."""
    inject_mock = mock.AsyncMock()

    with mock.patch.object(cc_bridge, "_inject_into_tmux_serialized", inject_mock), \
         mock.patch.object(cc_bridge, "_save_portal_message", mock.MagicMock()), \
         mock.patch.object(cc_bridge, "_cc_send", mock.AsyncMock()), \
         mock.patch.object(cc_bridge, "_correlated_assistant_message_after", return_value=None):

        client = mock.MagicMock()

        async def deliver_two():
            await cc_bridge._deliver_message(client, "[CC #general -- alice] first", 1, 200)
            await cc_bridge._deliver_message(client, "[CC #general -- bob] second", 1, 201)

        _run(deliver_two())

    assert inject_mock.await_count == 2
