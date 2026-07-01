"""
Tests for the CC-bridge private-content-leak fix.

Background
----------
``cc_bridge.py`` used to harvest an assistant reply purely by TIMESTAMP:
``_latest_assistant_message_after(inject_ts)`` returned the *first* assistant
message in ``portal-chat.jsonl`` newer than the inject timestamp and posted it
to the CC channel.  That is a privacy leak: a PRIVATE Telegram reply (or any
unrelated assistant output) written to the shared portal log would be scraped
and re-posted to the public CC channel under our sender name.

The fix introduces a correlation token: the injected CC notification carries a
unique ``[cc-req:<id>]`` marker, the agent is asked to echo it when replying to
CC, and only an assistant message that contains THAT marker is forwarded (with
the marker stripped before posting).  A private/unrelated reply lacks the
marker and is therefore never forwarded.

A second bug is also covered here: ``_should_inject_to_tmux`` matched channels
by SUBSTRING, so a channel named ``"vortex-flux"`` (owned by vortex) tripped
the bridge for a CIV named ``"flux"`` that does not own the channel.
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PORTAL_DIR)

import cc_bridge


@pytest.fixture
def portal_log(tmp_path):
    """Point cc_bridge.PORTAL_CHAT_LOG at a temp file for the test."""
    log = tmp_path / "portal-chat.jsonl"
    with patch.object(cc_bridge, "PORTAL_CHAT_LOG", log):
        yield log


def _write_entries(log: Path, entries: list[dict]) -> None:
    with log.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


# ---------------------------------------------------------------------------
# Correlation-token harvesting
# ---------------------------------------------------------------------------

class TestCorrelatedHarvest:
    def test_private_reply_is_not_forwarded(self, portal_log):
        """An assistant message WITHOUT the correlation token must not be
        returned for forwarding to CC (the private-content-leak case)."""
        token = cc_bridge._make_correlation_token()
        inject_ts = time.time()
        # A private telegram reply lands in the log AFTER our inject_ts but
        # carries no correlation marker.
        _write_entries(portal_log, [
            {"role": "assistant",
             "text": "Hey Alex, the bank password is hunter2 (private).",
             "timestamp": inject_ts + 1},
        ])
        result = cc_bridge._correlated_assistant_message_after(inject_ts, token)
        assert result is None

    def test_correlated_reply_is_forwarded(self, portal_log):
        """An assistant message that contains the correlation token IS
        returned, with the marker stripped from the text."""
        token = cc_bridge._make_correlation_token()
        inject_ts = time.time()
        _write_entries(portal_log, [
            {"role": "assistant",
             "text": f"[{token}] Sure, here is the public answer for CC.",
             "timestamp": inject_ts + 1},
        ])
        result = cc_bridge._correlated_assistant_message_after(inject_ts, token)
        assert result is not None
        assert "public answer for CC" in result
        # marker must be stripped before posting to the channel
        assert token not in result
        assert "cc-req" not in result

    def test_wrong_token_not_forwarded(self, portal_log):
        """A reply correlated to a DIFFERENT request must not be harvested."""
        token = cc_bridge._make_correlation_token()
        other = cc_bridge._make_correlation_token()
        assert token != other
        inject_ts = time.time()
        _write_entries(portal_log, [
            {"role": "assistant",
             "text": f"[{other}] Reply to a different CC request.",
             "timestamp": inject_ts + 1},
        ])
        result = cc_bridge._correlated_assistant_message_after(inject_ts, token)
        assert result is None

    def test_no_correlated_reply_does_not_crash(self, portal_log):
        """When there is no correlated reply at all, returns None cleanly."""
        token = cc_bridge._make_correlation_token()
        inject_ts = time.time()
        # empty log
        _write_entries(portal_log, [])
        assert cc_bridge._correlated_assistant_message_after(inject_ts, token) is None
        # missing file
        with patch.object(cc_bridge, "PORTAL_CHAT_LOG", portal_log.parent / "nope.jsonl"):
            assert cc_bridge._correlated_assistant_message_after(inject_ts, token) is None

    def test_token_present_in_injection(self):
        """The formatted injection must embed the correlation token so the
        agent can echo it back."""
        token = cc_bridge._make_correlation_token()
        msg = {"channel_name": "general", "sender_name": "vortex",
               "body": "hello flux"}
        out = cc_bridge._format_injection(msg, token=token)
        assert token in out


# ---------------------------------------------------------------------------
# Exact channel-ownership matching (no substring leak)
# ---------------------------------------------------------------------------

class TestExactChannelMatch:
    def test_substring_channel_not_owned_does_not_inject(self):
        """Channel 'vortex-flux' is owned by vortex; a CIV named 'flux' must
        NOT inject (and therefore not forward) on it just because 'flux' is a
        substring."""
        with patch.object(cc_bridge, "CIV_NAME", "flux"), \
             patch.object(cc_bridge, "_muted_channels", set()), \
             patch.object(cc_bridge, "_is_cc_paused", return_value=False):
            msg = {"channel_name": "vortex-flux", "channel_id": 7,
                   "body": "general chatter, no mention"}
            assert cc_bridge._should_inject_to_tmux(msg) is False

    def test_owned_prefix_channel_injects(self):
        """Channel 'flux-debug' is owned by flux -> inject."""
        with patch.object(cc_bridge, "CIV_NAME", "flux"), \
             patch.object(cc_bridge, "_muted_channels", set()), \
             patch.object(cc_bridge, "_is_cc_paused", return_value=False):
            msg = {"channel_name": "flux-debug", "channel_id": 8,
                   "body": "debug chatter"}
            assert cc_bridge._should_inject_to_tmux(msg) is True

    def test_exact_channel_name_injects(self):
        """Channel named exactly 'flux' -> inject."""
        with patch.object(cc_bridge, "CIV_NAME", "flux"), \
             patch.object(cc_bridge, "_muted_channels", set()), \
             patch.object(cc_bridge, "_is_cc_paused", return_value=False):
            msg = {"channel_name": "flux", "channel_id": 9, "body": "chatter"}
            assert cc_bridge._should_inject_to_tmux(msg) is True

    def test_mention_still_injects_on_unowned_channel(self):
        """An explicit @mention must still inject even on a channel the CIV
        does not own."""
        with patch.object(cc_bridge, "CIV_NAME", "flux"), \
             patch.object(cc_bridge, "_muted_channels", set()), \
             patch.object(cc_bridge, "_is_cc_paused", return_value=False):
            msg = {"channel_name": "vortex-debug", "channel_id": 10,
                   "body": "hey @flux can you help"}
            assert cc_bridge._should_inject_to_tmux(msg) is True
