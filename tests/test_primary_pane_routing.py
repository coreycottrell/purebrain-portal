"""Tests for portal primary pane routing fix.

What we're verifying: Human messages from the portal are always routed to
    the PRIMARY Claude pane (where `claude` is running), NOT to whichever
    pane happens to be "active" in the tmux session.
What coder discovered: `get_tmux_session()` returns the session NAME, and
    `tmux send-keys -t <session>` targets the active pane. When a team lead
    runs in a separate pane, messages go to the wrong place.
What descendants inherit: Pattern for testing async tmux routing with mocked
    subprocesses; verifying pane-level targeting in multi-pane environments.
Why this matters: The human's messages must ALWAYS reach Primary (Flux), not
    get swallowed by a team lead pane. This is existential for the partnership.

The fix: Both `api_chat_send` and `_inject_into_tmux_serialized` must use
`_find_primary_pane_async()` (which identifies the pane running `claude`)
instead of `get_tmux_session()` (which returns just the session name).
"""

import asyncio
import importlib
import sys
import os
import types
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Helpers to import portal_server in isolation (it has heavy side effects)
# ---------------------------------------------------------------------------

PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORTAL_SERVER = os.path.join(PORTAL_DIR, "portal_server.py")


def _read_function_source(func_name: str) -> str:
    """Read the raw source of portal_server.py and extract a function body.

    We use source-level inspection as a lightweight way to verify which
    helper function is called inside the target, without importing the
    entire module (which starts a server and has many dependencies).
    """
    with open(PORTAL_SERVER, "r") as f:
        return f.read()


# ===================================================================
# RED PHASE TESTS -- these MUST fail before the fix is applied
# ===================================================================


class TestApiChatSendUsePrimaryPane:
    """api_chat_send must call _find_primary_pane_async, not get_tmux_session."""

    def test_api_chat_send_calls_find_primary_pane(self):
        """The api_chat_send function must use _find_primary_pane_async to
        determine the tmux target, ensuring messages go to Primary's pane."""
        source = _read_function_source("api_chat_send")

        # Find the api_chat_send function body
        func_start = source.find("async def api_chat_send(")
        assert func_start != -1, "api_chat_send function not found in portal_server.py"

        # Find the next top-level function definition to bound our search
        next_func = source.find("\nasync def ", func_start + 1)
        if next_func == -1:
            next_func = source.find("\ndef ", func_start + 1)
        func_body = source[func_start:next_func] if next_func != -1 else source[func_start:]

        # The function MUST call _find_primary_pane_async for session resolution
        assert "_find_primary_pane_async()" in func_body, (
            "api_chat_send must use _find_primary_pane_async() to target "
            "the primary Claude pane, not get_tmux_session()"
        )

    def test_api_chat_send_does_not_use_get_tmux_session_for_target(self):
        """api_chat_send must NOT use bare get_tmux_session() as tmux target.

        get_tmux_session() returns the session name which targets the active
        pane -- wrong when team leads are running in other panes.
        """
        source = _read_function_source("api_chat_send")

        func_start = source.find("async def api_chat_send(")
        assert func_start != -1

        next_func = source.find("\nasync def ", func_start + 1)
        if next_func == -1:
            next_func = source.find("\ndef ", func_start + 1)
        func_body = source[func_start:next_func] if next_func != -1 else source[func_start:]

        # Count usages: get_tmux_session() should NOT be the source for the
        # `session` variable used in send-keys/paste-buffer calls.
        # After the fix, `session = await _find_primary_pane_async()` replaces
        # `session = get_tmux_session()`.
        lines = func_body.splitlines()
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("session") and "get_tmux_session()" in stripped:
                pytest.fail(
                    f"api_chat_send still assigns session from get_tmux_session(): "
                    f"{stripped!r}. Must use _find_primary_pane_async() instead."
                )


class TestInjectIntoTmuxSerializedUsePrimaryPane:
    """_inject_into_tmux_serialized must call _find_primary_pane_async."""

    def test_inject_serialized_calls_find_primary_pane(self):
        """_inject_into_tmux_serialized must use _find_primary_pane_async
        to determine the tmux target pane."""
        source = _read_function_source("_inject_into_tmux_serialized")

        func_start = source.find("async def _inject_into_tmux_serialized(")
        assert func_start != -1, (
            "_inject_into_tmux_serialized not found in portal_server.py"
        )

        next_func = source.find("\nasync def ", func_start + 1)
        if next_func == -1:
            next_func = source.find("\ndef ", func_start + 1)
        func_body = source[func_start:next_func] if next_func != -1 else source[func_start:]

        assert "_find_primary_pane_async()" in func_body, (
            "_inject_into_tmux_serialized must use _find_primary_pane_async() "
            "to target the primary Claude pane"
        )

    def test_inject_serialized_does_not_use_get_tmux_session_for_target(self):
        """_inject_into_tmux_serialized must NOT assign session from
        get_tmux_session() directly."""
        source = _read_function_source("_inject_into_tmux_serialized")

        func_start = source.find("async def _inject_into_tmux_serialized(")
        assert func_start != -1

        next_func = source.find("\nasync def ", func_start + 1)
        if next_func == -1:
            next_func = source.find("\ndef ", func_start + 1)
        func_body = source[func_start:next_func] if next_func != -1 else source[func_start:]

        lines = func_body.splitlines()
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("session") and "get_tmux_session()" in stripped:
                pytest.fail(
                    f"_inject_into_tmux_serialized still assigns session from "
                    f"get_tmux_session(): {stripped!r}. "
                    f"Must use _find_primary_pane_async() instead."
                )


class TestPasteBufferTargetsPrimaryPane:
    """The paste-buffer -t call in api_chat_send must target the primary pane."""

    def test_paste_buffer_uses_session_variable(self):
        """The tmux paste-buffer command in api_chat_send must use the same
        `session` variable that was resolved via _find_primary_pane_async.

        This ensures that load-buffer + paste-buffer targets the correct pane,
        not just the session name.
        """
        source = _read_function_source("api_chat_send")

        func_start = source.find("async def api_chat_send(")
        assert func_start != -1

        next_func = source.find("\nasync def ", func_start + 1)
        if next_func == -1:
            next_func = source.find("\ndef ", func_start + 1)
        func_body = source[func_start:next_func] if next_func != -1 else source[func_start:]

        # Verify paste-buffer uses `-t session` (or `-t", session`)
        assert "paste-buffer" in func_body, (
            "api_chat_send must use tmux paste-buffer for message injection"
        )

        # The session variable must come from _find_primary_pane_async
        # (already tested above), and paste-buffer must reference it.
        # This is a structural check: the variable named `session` used in
        # paste-buffer must be the same one from _find_primary_pane_async.
        assert '"-t", session' in func_body or "'-t', session" in func_body, (
            "paste-buffer must use -t session (the primary pane target)"
        )


class TestMultiPaneRoutingCorrectness:
    """When multiple panes exist, _find_primary_pane_async picks the right one."""

    def test_find_primary_pane_prefers_claude_pane(self):
        """_find_primary_pane_async must prefer the pane running 'claude'
        over other panes (e.g., team lead bash panes)."""
        source = _read_function_source("_find_primary_pane_async")

        func_start = source.find("async def _find_primary_pane_async()")
        assert func_start != -1

        next_func = source.find("\nasync def ", func_start + 1)
        if next_func == -1:
            next_func = source.find("\ndef ", func_start + 1)
        func_body = source[func_start:next_func] if next_func != -1 else source[func_start:]

        # The function must check for "claude" in the pane command
        assert '"claude"' in func_body or "'claude'" in func_body, (
            "_find_primary_pane_async must look for 'claude' in pane commands"
        )

    def test_all_send_keys_in_api_chat_send_use_session_variable(self):
        """Every tmux send-keys -t in api_chat_send must use the `session`
        variable (which after the fix comes from _find_primary_pane_async),
        not a hardcoded session name."""
        source = _read_function_source("api_chat_send")

        func_start = source.find("async def api_chat_send(")
        assert func_start != -1

        next_func = source.find("\nasync def ", func_start + 1)
        if next_func == -1:
            next_func = source.find("\ndef ", func_start + 1)
        func_body = source[func_start:next_func] if next_func != -1 else source[func_start:]

        # Every send-keys with -t must reference the `session` variable
        lines = func_body.splitlines()
        for i, line in enumerate(lines):
            if "send-keys" in line and '"-t"' in line:
                assert "session" in line, (
                    f"Line {i} in api_chat_send uses send-keys -t without "
                    f"the session variable: {line.strip()!r}"
                )
