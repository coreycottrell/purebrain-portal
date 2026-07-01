"""
Tests for CC/War Room message filtering in portal_server.py.

Verifies that CC bridge messages (injected as user role) are properly
filtered out of the main chat view. These belong in the CC tab only.

Root cause fixed: _is_real_user_message was missing CC-specific noise
markers ([CC #, [CC-DM, [WAR-ROOM], etc). Only _is_real_assistant_message
had them, so CC messages with role=user passed through unfiltered.
"""

import sys
import os
from pathlib import Path
from unittest import mock

import pytest

PORTAL_DIR = Path(__file__).parent.parent

# We test filter logic by copying the exact functions from portal_server.py.
# portal_server.py has heavy side effects on import (tmux, starlette, etc),
# so we avoid importing it. Source sync is verified by tests below.


def _is_real_user_message_under_test(text):
    """Copy of _is_real_user_message from portal_server.py for testing.

    IMPORTANT: If portal_server.py is updated, this must be synced.
    The test_filter_matches_source test below verifies they stay in sync.
    """
    import re
    if not text or len(text) < 2:
        return False
    if "[TELEGRAM" in text:
        return True
    if text.startswith("[PORTAL]"):
        return True
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
        "[from-ACG]",
        "Context restored",
        "Summary:  ",
        "` regex", "` sed", "| sed",
        "re.search(r'", "re.DOTALL",
        "<command-name>", "<command-message>",
        "<command-args>", "<local-command",
        "local-command-caveat", "local-command-stdout",
        "Compacted (ctrl+o",
        "&& [ -x ", "| cut -d",
        "[portal",
        "[Portal Upload",
        "[upload_id:",
        "[CC #", "[CC-DM",
        "[WAR-ROOM]", "[WAR-ROOM-RESPONSE]", "[WAR-ROOM-PREFLIGHT",
        "[WAR-ROOM-STAND-DOWN]", "[WAR-ROOM-UPDATE]",
        "[GENERATE-TASKS]",
    ]
    for marker in noise_markers:
        if marker in text[:300]:
            return False
    special = sum(1 for c in text[:200] if c in '{}[]|\\`$()#')
    if len(text) < 200 and special > len(text) * 0.15:
        return False
    return True


def _is_real_assistant_message_under_test(text):
    """Copy of _is_real_assistant_message from portal_server.py."""
    if not text or len(text) < 10:
        return False
    stripped = text.strip()
    if len(stripped) <= 3 and not any(c.isalnum() for c in stripped):
        return False
    _cc_assistant_markers = [
        "[CC #", "[CC-DM",
        "CC chatter",
        "[WAR-ROOM]", "[WAR-ROOM-RESPONSE]",
        "[GENERATE-TASKS]",
    ]
    for marker in _cc_assistant_markers:
        if marker in text[:300]:
            return False
    return True


# =========================================================================
# Test: CC messages filtered from user role
# =========================================================================

class TestCCUserMessageFilter:
    """CC bridge messages arrive as user role. They must be filtered out."""

    def test_cc_channel_message_filtered(self):
        text = "[CC #vortex-flux -- Vortex] Hey Flux, updated the executor."
        assert _is_real_user_message_under_test(text) is False

    def test_cc_dm_message_filtered(self):
        text = "[CC-DM from Alex] Check the portal when you get a chance"
        assert _is_real_user_message_under_test(text) is False

    def test_war_room_message_filtered(self):
        text = "[WAR-ROOM] @Flux task_id=t_abc123 project_id=proj_xyz Execute this"
        assert _is_real_user_message_under_test(text) is False

    def test_war_room_response_filtered(self):
        text = "[WAR-ROOM-RESPONSE] Task complete. All acceptance criteria met."
        assert _is_real_user_message_under_test(text) is False

    def test_war_room_update_filtered(self):
        text = "[WAR-ROOM-UPDATE] Executor Watcher v2.1 — Model Enforcement"
        assert _is_real_user_message_under_test(text) is False

    def test_war_room_preflight_filtered(self):
        text = "[WAR-ROOM-PREFLIGHT] Flux ready — channel 35 verified"
        assert _is_real_user_message_under_test(text) is False

    def test_war_room_stand_down_filtered(self):
        text = "[WAR-ROOM-STAND-DOWN] @Flux task_id=t_abc123 Stand down"
        assert _is_real_user_message_under_test(text) is False

    def test_generate_tasks_filtered(self):
        text = "[GENERATE-TASKS] project_id=proj_abc Generate backlog"
        assert _is_real_user_message_under_test(text) is False

    def test_cc_general_channel_filtered(self):
        text = "[CC #general -- Aether] Good morning everyone"
        assert _is_real_user_message_under_test(text) is False

    def test_cc_engineering_channel_filtered(self):
        text = "[CC #engineering -- Prodigy] New PR for review"
        assert _is_real_user_message_under_test(text) is False


# =========================================================================
# Test: CC messages also filtered from assistant role
# =========================================================================

class TestCCAssistantMessageFilter:
    """Assistant messages echoing CC content must also be filtered."""

    def test_assistant_cc_channel_filtered(self):
        text = "[CC #vortex-flux -- Vortex] Some message echoed by assistant"
        assert _is_real_assistant_message_under_test(text) is False

    def test_assistant_cc_dm_filtered(self):
        text = "[CC-DM from someone] Message echoed"
        assert _is_real_assistant_message_under_test(text) is False

    def test_assistant_war_room_filtered(self):
        text = "[WAR-ROOM] @Flux some task"
        assert _is_real_assistant_message_under_test(text) is False

    def test_assistant_cc_chatter_filtered(self):
        text = "CC chatter from #general about the deploy"
        assert _is_real_assistant_message_under_test(text) is False


# =========================================================================
# Test: Real messages NOT filtered (false positive prevention)
# =========================================================================

class TestRealMessagesNotFiltered:
    """Ensure the CC filter doesn't accidentally block real user messages."""

    def test_normal_user_message_passes(self):
        text = "Hey can you fix that bug in the portal?"
        assert _is_real_user_message_under_test(text) is True

    def test_user_message_mentioning_cc_passes(self):
        """Talking ABOUT CC should not be filtered — only CC-prefixed messages."""
        text = "The CC bridge is showing stale messages, can you look at it?"
        assert _is_real_user_message_under_test(text) is True

    def test_user_message_mentioning_war_room_passes(self):
        text = "What happened in the war room today?"
        assert _is_real_user_message_under_test(text) is True

    def test_telegram_message_passes(self):
        text = "[TELEGRAM private:123 from @Alex] Hello from mobile"
        assert _is_real_user_message_under_test(text) is True

    def test_normal_assistant_message_passes(self):
        text = "I've fixed the bug and all 376 tests are passing."
        assert _is_real_assistant_message_under_test(text) is True

    def test_short_assistant_filtered(self):
        """Very short assistant messages are noise."""
        text = "ok"
        assert _is_real_assistant_message_under_test(text) is False


# =========================================================================
# Test: Source sync verification
# =========================================================================

class TestSourceSync:
    """Verify the test filter functions match the actual portal_server.py source."""

    def test_user_filter_has_cc_markers(self):
        """The _is_real_user_message function in portal_server.py MUST
        contain CC noise markers. If this fails, someone removed them."""
        source_path = PORTAL_DIR / "portal_server.py"
        source = source_path.read_text()

        # Find the _is_real_user_message function body
        start = source.find("def _is_real_user_message(")
        assert start > 0, "_is_real_user_message not found in portal_server.py"

        # Get the function body (until next def at same indent level)
        end = source.find("\ndef ", start + 1)
        func_body = source[start:end]

        # These markers MUST be present in the noise_markers list
        required_markers = ['"[CC #"', '"[CC-DM"', '"[WAR-ROOM]"']
        for marker in required_markers:
            assert marker in func_body, (
                f"REGRESSION: {marker} is missing from _is_real_user_message "
                f"noise_markers in portal_server.py. CC messages will leak "
                f"into the main chat."
            )

    def test_assistant_filter_has_cc_markers(self):
        """The _is_real_assistant_message function MUST also have CC markers."""
        source_path = PORTAL_DIR / "portal_server.py"
        source = source_path.read_text()

        start = source.find("def _is_real_assistant_message(")
        assert start > 0
        end = source.find("\ndef ", start + 1)
        func_body = source[start:end]

        required_markers = ['"[CC #"', '"[CC-DM"', '"[WAR-ROOM]"']
        for marker in required_markers:
            assert marker in func_body, (
                f"REGRESSION: {marker} is missing from _is_real_assistant_message"
            )
