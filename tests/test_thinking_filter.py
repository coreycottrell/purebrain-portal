"""Test that thinking blocks are filtered from chat history.

Thinking blocks are ephemeral -- they should be pushed via WebSocket in real-time
but NEVER persisted to portal-chat.jsonl or included in history responses.

Three layers of defense:
1. _save_portal_message should NOT be called with role="thinking"
2. _load_portal_messages should filter out any thinking entries that slip through
3. portal-chat.jsonl should contain zero thinking entries (cleanup verification)
"""
import json
import os
import sys
import time

import pytest

# Add portal root to path for imports
PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PORTAL_DIR not in sys.path:
    sys.path.insert(0, PORTAL_DIR)


class TestThinkingNotPersistedToPortalChat:
    """Verify that thinking entries never appear in portal-chat.jsonl."""

    def test_no_thinking_entries_in_portal_chat_jsonl(self):
        """portal-chat.jsonl should contain zero role=thinking entries."""
        portal_log = os.path.join(PORTAL_DIR, "portal-chat.jsonl")
        if not os.path.exists(portal_log):
            pytest.skip("No portal-chat.jsonl present")

        thinking_count = 0
        with open(portal_log) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("role") == "thinking":
                        thinking_count += 1
                except json.JSONDecodeError:
                    continue

        assert thinking_count == 0, (
            f"Found {thinking_count} thinking entries in portal-chat.jsonl -- "
            f"should be 0 (thinking is ephemeral, not persisted)"
        )


class TestLoadPortalMessagesFiltersThinking:
    """Verify _load_portal_messages filters out thinking entries."""

    def test_thinking_entries_excluded_from_loaded_messages(self, tmp_path):
        """_load_portal_messages must skip entries with role=thinking."""
        import portal_server

        # Create a temporary portal-chat.jsonl with mixed entries
        fake_log = tmp_path / "portal-chat.jsonl"
        entries = [
            {"role": "user", "text": "Hello world", "timestamp": int(time.time()), "id": "portal-1"},
            {"role": "thinking", "text": "Let me analyze this...", "timestamp": int(time.time()), "id": "portal-2"},
            {"role": "assistant", "text": "Hi there!", "timestamp": int(time.time()), "id": "portal-3"},
            {"role": "thinking", "text": "Considering options...", "timestamp": int(time.time()), "id": "portal-4"},
            {"role": "user", "text": "Another message", "timestamp": int(time.time()), "id": "portal-5"},
        ]
        with open(fake_log, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        # Monkey-patch the PORTAL_CHAT_LOG path
        original_log = portal_server.PORTAL_CHAT_LOG
        original_cache = portal_server._portal_chat_cache
        try:
            portal_server.PORTAL_CHAT_LOG = fake_log
            portal_server._portal_chat_cache = (0, 0, None)  # Invalidate cache

            messages = portal_server._load_portal_messages()

            # Should have 3 messages (user + assistant + user), NOT 5
            roles = [m.get("role") for m in messages]
            assert "thinking" not in roles, (
                f"_load_portal_messages returned thinking entries: {roles}"
            )
            assert len(messages) == 3, (
                f"Expected 3 non-thinking messages, got {len(messages)}: {roles}"
            )
        finally:
            portal_server.PORTAL_CHAT_LOG = original_log
            portal_server._portal_chat_cache = original_cache


class TestSavePortalMessageRejectsThinking:
    """Verify _save_portal_message refuses to persist thinking entries."""

    def test_save_portal_message_skips_thinking_role(self, tmp_path):
        """_save_portal_message should return None or skip writing for role=thinking."""
        import portal_server

        fake_log = tmp_path / "portal-chat.jsonl"
        fake_log.touch()

        original_log = portal_server.PORTAL_CHAT_LOG
        try:
            portal_server.PORTAL_CHAT_LOG = fake_log

            # Try to save a thinking message
            result = portal_server._save_portal_message("Deep thought...", role="thinking")

            # Read back the file -- should be empty (no thinking persisted)
            content = fake_log.read_text().strip()
            if content:
                entries = [json.loads(l) for l in content.split("\n") if l.strip()]
                thinking = [e for e in entries if e.get("role") == "thinking"]
                assert len(thinking) == 0, (
                    f"_save_portal_message persisted {len(thinking)} thinking entries "
                    f"to portal-chat.jsonl -- thinking must not be persisted"
                )
        finally:
            portal_server.PORTAL_CHAT_LOG = original_log
