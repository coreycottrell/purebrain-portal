"""
Tests for the War Room handler in the CC bridge (custom/startup.py).

What we're verifying: War Room message detection, parsing, injection formatting,
and response formatting for multi-agent collaborative task analysis via CC.

TDD RED Phase: These tests define the contract for functions that do not yet
exist in startup.py. They should ALL FAIL until the implementation is written.

Covers:
- Detection: identifying [WAR-ROOM] tagged messages
- Parsing: extracting structured fields from war room message body
- Injection formatting: preparing war room messages for tmux injection
- Response formatting: wrapping agent analysis in [WAR-ROOM-RESPONSE] format
- Integration: end-to-end detect -> parse -> format pipeline
"""

import asyncio
import json
import time
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

import sys

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


def _make_warroom_body(
    project="Acme Redesign",
    task="Homepage Hero Section",
    role="Engineering",
    context="Client wants a modern, responsive hero section with animated background.",
    other_agents="@Anchor (Sales), @Lyra (Marketing)",
    prompt="Analyze this task from your domain perspective.",
):
    """Build a well-formed war room message body."""
    lines = [f"@Flux [WAR-ROOM] Project: {project} | Task: {task}"]
    if role:
        lines.append(f"Role: {role}")
    if context:
        lines.append(f"Context: {context}")
    if other_agents:
        lines.append(f"Other agents: {other_agents}")
    lines.append("---")
    if prompt:
        lines.append(prompt)
    return "\n".join(lines)


def _make_cc_message(body, sender_name="WarRoomBot", sender_email="warroom@purebrain.ai",
                     channel_name="war-room", channel_id=10, msg_id=100):
    """Build a CC message dict with war room body."""
    return {
        "id": msg_id,
        "body": body,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "channel_name": channel_name,
        "channel_id": channel_id,
    }


# ===========================================================================
# War Room Detection Tests
# ===========================================================================

class TestWarRoomDetection:

    def test_detects_warroom_tag_in_body(self):
        """Message with [WAR-ROOM] tag returns True."""
        body = _make_warroom_body()
        msg = _make_cc_message(body)
        assert startup._is_warroom_message(msg) is True

    def test_ignores_normal_mention(self):
        """Message with @Flux but no [WAR-ROOM] tag returns False."""
        msg = _make_cc_message("@Flux can you review this PR?")
        assert startup._is_warroom_message(msg) is False

    def test_ignores_message_without_tag(self):
        """Random message without any war room tag returns False."""
        msg = _make_cc_message("Hey everyone, standup in 5 minutes.")
        assert startup._is_warroom_message(msg) is False

    def test_detects_warroom_case_insensitive(self):
        """[war-room] in lowercase should also be detected."""
        body = "@Flux [war-room] Project: Test | Task: Test\nRole: Dev\n---\nDo the thing."
        msg = _make_cc_message(body)
        assert startup._is_warroom_message(msg) is True

    def test_detects_warroom_in_channel_message(self):
        """Works in non-DM channel messages too."""
        body = _make_warroom_body()
        msg = _make_cc_message(body, channel_name="general")
        assert startup._is_warroom_message(msg) is True

    def test_empty_body_returns_false(self):
        """Message with empty body returns False."""
        msg = _make_cc_message("")
        assert startup._is_warroom_message(msg) is False

    def test_none_body_returns_false(self):
        """Message with None body returns False."""
        msg = {"id": 1, "body": None, "channel_name": "general"}
        assert startup._is_warroom_message(msg) is False

    def test_missing_body_key_returns_false(self):
        """Message dict with no 'body' key returns False."""
        msg = {"id": 1, "channel_name": "general"}
        assert startup._is_warroom_message(msg) is False


# ===========================================================================
# War Room Parser Tests
# ===========================================================================

class TestWarRoomParser:

    def test_parses_full_warroom_message(self):
        """Extracts all fields from a well-formed war room message."""
        body = _make_warroom_body()
        parsed = startup._parse_warroom_message(body)

        assert parsed["project"] == "Acme Redesign"
        assert parsed["task"] == "Homepage Hero Section"
        assert parsed["role"] == "Engineering"
        assert parsed["context"] == "Client wants a modern, responsive hero section with animated background."
        assert parsed["other_agents"] == "@Anchor (Sales), @Lyra (Marketing)"
        assert parsed["prompt"] == "Analyze this task from your domain perspective."

    def test_parses_message_with_missing_fields(self):
        """Returns empty strings for missing optional fields."""
        body = "@Flux [WAR-ROOM] Project: Minimal | Task: Quick Check\n---\nJust do it."
        parsed = startup._parse_warroom_message(body)

        assert parsed["project"] == "Minimal"
        assert parsed["task"] == "Quick Check"
        assert parsed["role"] == ""
        assert parsed["context"] == ""
        assert parsed["other_agents"] == ""
        assert parsed["prompt"] == "Just do it."

    def test_extracts_prompt_after_separator(self):
        """Gets the prompt text after the --- separator."""
        body = _make_warroom_body(prompt="Please provide a technical feasibility assessment.")
        parsed = startup._parse_warroom_message(body)
        assert parsed["prompt"] == "Please provide a technical feasibility assessment."

    def test_handles_multiline_context(self):
        """Context that spans multiple lines is captured correctly."""
        multiline_context = (
            "Client wants a modern hero section.\n"
            "Budget is $50k.\n"
            "Deadline is Q3."
        )
        # Build manually to embed multiline context
        body = (
            "@Flux [WAR-ROOM] Project: Big Project | Task: Planning\n"
            f"Role: Engineering\n"
            f"Context: {multiline_context}\n"
            "Other agents: @Anchor (Sales)\n"
            "---\n"
            "Analyze this."
        )
        parsed = startup._parse_warroom_message(body)
        # The parser should capture at least the first line of context
        assert "modern hero section" in parsed["context"]

    def test_handles_no_other_agents(self):
        """other_agents field is optional and defaults to empty string."""
        body = _make_warroom_body(other_agents="")
        # Remove the "Other agents:" line since helper builds it empty
        body = body.replace("Other agents: \n", "")
        parsed = startup._parse_warroom_message(body)
        assert parsed["other_agents"] == ""

    def test_handles_multiline_prompt(self):
        """Prompt text after --- can span multiple lines."""
        body = (
            "@Flux [WAR-ROOM] Project: Multi | Task: Prompt Test\n"
            "Role: Dev\n"
            "---\n"
            "First line of prompt.\n"
            "Second line of prompt.\n"
            "Third line of prompt."
        )
        parsed = startup._parse_warroom_message(body)
        assert "First line of prompt." in parsed["prompt"]
        assert "Second line of prompt." in parsed["prompt"]
        assert "Third line of prompt." in parsed["prompt"]

    def test_returns_dict_with_all_keys(self):
        """Parsed result always contains all expected keys."""
        body = _make_warroom_body()
        parsed = startup._parse_warroom_message(body)
        expected_keys = {"project", "task", "role", "context", "other_agents", "prompt"}
        assert set(parsed.keys()) >= expected_keys


# ===========================================================================
# War Room Injection Formatting Tests
# ===========================================================================

class TestWarRoomFormatInjection:

    def test_formats_with_warroom_prefix(self):
        """Output starts with [WAR-ROOM] prefix."""
        msg = _make_cc_message(_make_warroom_body())
        parsed = startup._parse_warroom_message(msg["body"])
        result = startup._format_warroom_injection(msg, parsed)
        assert result.startswith("[WAR-ROOM]")

    def test_includes_project_and_task(self):
        """Project and task name appear in formatted output."""
        msg = _make_cc_message(_make_warroom_body(project="Alpha", task="Build API"))
        parsed = startup._parse_warroom_message(msg["body"])
        result = startup._format_warroom_injection(msg, parsed)
        assert "Alpha" in result
        assert "Build API" in result

    def test_includes_role_assignment(self):
        """Role is visible in formatted output."""
        msg = _make_cc_message(_make_warroom_body(role="Engineering"))
        parsed = startup._parse_warroom_message(msg["body"])
        result = startup._format_warroom_injection(msg, parsed)
        assert "Engineering" in result

    def test_includes_prompt(self):
        """The actual prompt text is included in the formatted output."""
        prompt_text = "Analyze the security implications of this feature."
        msg = _make_cc_message(_make_warroom_body(prompt=prompt_text))
        parsed = startup._parse_warroom_message(msg["body"])
        result = startup._format_warroom_injection(msg, parsed)
        assert prompt_text in result

    def test_includes_other_agents(self):
        """Other agents info is included so the CIV knows who else is involved."""
        msg = _make_cc_message(_make_warroom_body(other_agents="@Anchor (Sales), @Lyra (Marketing)"))
        parsed = startup._parse_warroom_message(msg["body"])
        result = startup._format_warroom_injection(msg, parsed)
        assert "@Anchor" in result or "Anchor" in result

    def test_includes_context(self):
        """Context field is included in the injection."""
        ctx = "The client needs this by end of sprint."
        msg = _make_cc_message(_make_warroom_body(context=ctx))
        parsed = startup._parse_warroom_message(msg["body"])
        result = startup._format_warroom_injection(msg, parsed)
        assert ctx in result


# ===========================================================================
# War Room Response Formatting Tests
# ===========================================================================

class TestWarRoomResponseFormat:

    def test_formats_response_with_prefix(self):
        """Response starts with [WAR-ROOM-RESPONSE] prefix."""
        parsed = {
            "project": "Acme Redesign",
            "task": "Homepage Hero Section",
            "role": "Engineering",
            "context": "",
            "other_agents": "",
            "prompt": "",
        }
        response_text = "The hero section should use a CSS grid layout."
        result = startup._format_warroom_response(response_text, parsed)
        assert "[WAR-ROOM-RESPONSE]" in result

    def test_includes_project_and_task_in_response(self):
        """Project and task appear in the formatted response."""
        parsed = {
            "project": "Beta Launch",
            "task": "Deploy Pipeline",
            "role": "DevOps",
            "context": "",
            "other_agents": "",
            "prompt": "",
        }
        result = startup._format_warroom_response("Use blue-green deployment.", parsed)
        assert "Beta Launch" in result
        assert "Deploy Pipeline" in result

    def test_includes_agent_identity(self):
        """Response includes the CIV identity (Agent: Flux or similar)."""
        parsed = {
            "project": "Test",
            "task": "Identity Check",
            "role": "Engineering",
            "context": "",
            "other_agents": "",
            "prompt": "",
        }
        result = startup._format_warroom_response("My analysis.", parsed)
        # Should reference the CIV_NAME ("flux") or formatted version
        lower_result = result.lower()
        assert "flux" in lower_result or "agent:" in lower_result

    def test_includes_response_content(self):
        """The actual response text is included in the formatted output."""
        parsed = {
            "project": "Test",
            "task": "Content Check",
            "role": "Engineering",
            "context": "",
            "other_agents": "",
            "prompt": "",
        }
        analysis = "Based on my engineering analysis, we should use React Server Components."
        result = startup._format_warroom_response(analysis, parsed)
        assert analysis in result

    def test_includes_domain_from_role(self):
        """Response includes the domain/role assignment."""
        parsed = {
            "project": "Test",
            "task": "Domain Check",
            "role": "Security",
            "context": "",
            "other_agents": "",
            "prompt": "",
        }
        result = startup._format_warroom_response("All clear.", parsed)
        assert "Security" in result

    def test_response_has_separator(self):
        """Response format includes a --- separator before content."""
        parsed = {
            "project": "Test",
            "task": "Separator Check",
            "role": "Engineering",
            "context": "",
            "other_agents": "",
            "prompt": "",
        }
        result = startup._format_warroom_response("Content here.", parsed)
        assert "---" in result


# ===========================================================================
# War Room Integration Tests
# ===========================================================================

class TestWarRoomIntegration:

    def test_warroom_message_detected_and_formatted(self):
        """End-to-end: detect -> parse -> format injection pipeline."""
        body = _make_warroom_body(
            project="Integration Test",
            task="Full Pipeline",
            role="Engineering",
            context="Testing the full war room flow.",
            prompt="Provide your analysis.",
        )
        msg = _make_cc_message(body)

        # Step 1: Detect
        assert startup._is_warroom_message(msg) is True

        # Step 2: Parse
        parsed = startup._parse_warroom_message(msg["body"])
        assert parsed["project"] == "Integration Test"
        assert parsed["task"] == "Full Pipeline"
        assert parsed["role"] == "Engineering"

        # Step 3: Format for injection
        injection = startup._format_warroom_injection(msg, parsed)
        assert "[WAR-ROOM]" in injection
        assert "Integration Test" in injection
        assert "Provide your analysis." in injection

    def test_warroom_response_wrapped_correctly(self):
        """Response text gets wrapped in [WAR-ROOM-RESPONSE] format before posting back."""
        body = _make_warroom_body(
            project="Response Test",
            task="Wrap Check",
            role="Engineering",
        )
        parsed = startup._parse_warroom_message(body)
        raw_response = "I recommend using a microservices architecture for better scalability."

        formatted = startup._format_warroom_response(raw_response, parsed)

        assert "[WAR-ROOM-RESPONSE]" in formatted
        assert "Response Test" in formatted
        assert "Wrap Check" in formatted
        assert raw_response in formatted

    def test_non_warroom_message_not_detected(self):
        """Normal CC messages do not trigger war room handling."""
        msg = _make_cc_message("@Flux hey, can you check the logs?")
        assert startup._is_warroom_message(msg) is False

    def test_detect_parse_response_roundtrip(self):
        """Full roundtrip: build message -> detect -> parse -> respond -> verify format."""
        body = _make_warroom_body(
            project="Roundtrip",
            task="E2E Verify",
            role="Architecture",
            context="Verify the entire war room message lifecycle.",
            other_agents="@Anchor (Sales)",
            prompt="What architectural patterns apply here?",
        )
        msg = _make_cc_message(body)

        # Detect
        assert startup._is_warroom_message(msg) is True

        # Parse
        parsed = startup._parse_warroom_message(msg["body"])
        assert parsed["project"] == "Roundtrip"
        assert parsed["other_agents"] == "@Anchor (Sales)"

        # Format injection
        injection = startup._format_warroom_injection(msg, parsed)
        assert "[WAR-ROOM]" in injection

        # Format response
        response = startup._format_warroom_response(
            "I recommend the Strategy pattern combined with Event Sourcing.",
            parsed,
        )
        assert "[WAR-ROOM-RESPONSE]" in response
        assert "Roundtrip" in response
        assert "E2E Verify" in response
        assert "Strategy pattern" in response
