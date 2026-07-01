"""
Tests for upload message deduplication.

When a user uploads a file via portal chat, TWO entries are created:
  1. Portal chat log: [Image: stored_name]\ncaption
  2. Session JSONL (via tmux injection): [Portal Upload from X] File saved to: /path ...

_parse_all_messages() must deduplicate these into a single message.

The current dedup uses text pattern matching within a 30s window, which fails when:
  - Claude is busy and processes the tmux injection >30s after the portal log write
  - Two uploads happen close together, causing false matches
  - Text formats change

The fix uses a shared upload_id tag embedded in both sources for reliable matching.
"""

import json
import os
import sys
import time
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_portal_upload_entry(stored_name, caption="", upload_id=None, ts=None, id_suffix="abcd1234"):
    """Create a portal-chat.jsonl entry as produced by _save_portal_message for uploads."""
    ts = ts or int(time.time())
    text = f"[Image: {stored_name}]"
    if caption:
        text += f"\n{caption}"
    entry = {
        "role": "user",
        "text": text,
        "timestamp": ts,
        "id": f"portal-{int(ts * 1000)}-{id_suffix}",
    }
    if upload_id:
        entry["upload_id"] = upload_id
    return entry


def _make_portal_ack_entry(original_name, caption="", ts=None):
    """Create a portal-chat.jsonl ack entry (assistant)."""
    ts = ts or int(time.time())
    parts = [f"Received your file: {original_name}", "(image — viewing now)"]
    if caption:
        parts.append(f'Instructions noted: "{caption}"')
    parts.append("Processing...")
    return {
        "role": "assistant",
        "text": " ".join(parts),
        "timestamp": ts,
        "id": f"portal-{int(ts * 1000)}-ack00001",
    }


def _make_session_upload_entry(original_name, portal_copy_path, caption="", upload_id=None, ts=None, id_suffix="0"):
    """Create a session JSONL entry as captured from tmux injection."""
    ts = ts or int(time.time())
    parts = []
    if upload_id:
        parts.append(f"[upload_id:{upload_id}]")
    parts.append(f"[Portal Upload from TestUser] File saved to: {portal_copy_path}")
    if caption:
        parts.append(f"INSTRUCTIONS from TestUser: {caption}")
    parts.append(f"[Image: {original_name} — USE Read tool on {portal_copy_path} TO VIEW]")
    return {
        "role": "user",
        "text": " ".join(parts),
        "timestamp": ts,
        "id": f"msg-session1-{id_suffix}",
    }


class TestUploadDedupHelper:
    """Shared helper for running _parse_all_messages with mocked data."""

    @staticmethod
    def parse_with_mocked_sources(portal_msgs, session_msgs):
        import portal_server
        fake_path = Path("/tmp/fake-session.jsonl")
        with patch.object(portal_server, '_get_all_session_log_paths', return_value=[fake_path]), \
             patch.object(portal_server, '_parse_jsonl_messages_from_file', return_value=session_msgs), \
             patch.object(portal_server, '_load_portal_messages', return_value=portal_msgs):
            return portal_server._parse_all_messages(last_n=200)


class TestUploadDedupBasic(unittest.TestCase):
    """Basic upload dedup tests (within 30s window — current code handles these)."""

    def _parse(self, portal_msgs, session_msgs):
        return TestUploadDedupHelper.parse_with_mocked_sources(portal_msgs, session_msgs)

    def test_upload_produces_single_message(self):
        """An upload should produce exactly 1 user message, not 2."""
        ts = int(time.time())
        upload_id = f"upload-{int(ts * 1000)}-deadbeef"
        stored_name = f"{int(ts * 1000)}_abcd1234_test.png"

        portal_msgs = [_make_portal_upload_entry(stored_name, upload_id=upload_id, ts=ts)]
        session_msgs = [_make_session_upload_entry("test.png", "/path/test.png", upload_id=upload_id, ts=ts)]

        result = self._parse(portal_msgs, session_msgs)
        user_msgs = [m for m in result if m["role"] == "user"]
        self.assertEqual(len(user_msgs), 1, f"Expected 1 user message, got {len(user_msgs)}")

    def test_ack_message_appears_once(self):
        """The assistant ack message should appear exactly once."""
        ts = int(time.time())
        upload_id = f"upload-{int(ts * 1000)}-ack0test"
        stored_name = f"{int(ts * 1000)}_abcd1234_file.png"

        portal_msgs = [
            _make_portal_upload_entry(stored_name, upload_id=upload_id, ts=ts),
            _make_portal_ack_entry("file.png", ts=ts),
        ]
        session_msgs = [_make_session_upload_entry("file.png", "/path/file.png", upload_id=upload_id, ts=ts)]

        result = self._parse(portal_msgs, session_msgs)
        ack_msgs = [m for m in result if m["role"] == "assistant" and "Received your file" in m.get("text", "")]
        self.assertEqual(len(ack_msgs), 1)

    def test_old_messages_without_upload_id_still_render(self):
        """Messages from before the fix (no upload_id) should still appear."""
        ts = int(time.time()) - 3600
        portal_msgs = [
            {"role": "user", "text": "Hello, regular message", "timestamp": ts, "id": f"portal-{ts}-old1"},
            {"role": "user", "text": "[Image: old_stored.png]\nold caption", "timestamp": ts + 10, "id": f"portal-{ts+10}-old2"},
        ]
        result = self._parse(portal_msgs, [])
        user_msgs = [m for m in result if m["role"] == "user"]
        self.assertEqual(len(user_msgs), 2)

    def test_mixed_regular_and_upload_messages(self):
        """Regular messages + upload should produce correct count."""
        ts = int(time.time())
        upload_id = f"upload-{int(ts * 1000)}-mixtest1"
        stored_name = f"{int(ts * 1000)}_abcd_mixed.png"

        portal_msgs = [
            {"role": "user", "text": "Hello, how are you?", "timestamp": ts - 30, "id": f"portal-{ts-30}-reg"},
            _make_portal_upload_entry(stored_name, caption="look", upload_id=upload_id, ts=ts),
            _make_portal_ack_entry("mixed.png", caption="look", ts=ts),
        ]
        session_msgs = [_make_session_upload_entry("mixed.png", "/path/mixed.png", caption="look", upload_id=upload_id, ts=ts)]

        result = self._parse(portal_msgs, session_msgs)
        user_msgs = [m for m in result if m["role"] == "user"]
        self.assertEqual(len(user_msgs), 2)  # 1 regular + 1 upload (deduped)


class TestUploadDedupTimingEdgeCases(unittest.TestCase):
    """Tests for timing edge cases where the current 30s window dedup FAILS.
    These tests should FAIL before the upload_id fix and PASS after."""

    def _parse(self, portal_msgs, session_msgs):
        return TestUploadDedupHelper.parse_with_mocked_sources(portal_msgs, session_msgs)

    def test_delayed_session_entry_over_30s(self):
        """Upload where Claude processes tmux injection >30s after portal log write.
        This happens when Claude is busy with a long tool call or generation.
        Current 30s window dedup FAILS — produces 2 messages instead of 1."""
        ts_portal = int(time.time())
        ts_session = ts_portal + 45  # 45 seconds later — outside 30s window
        upload_id = f"upload-{int(ts_portal * 1000)}-delayed1"
        stored_name = f"{int(ts_portal * 1000)}_abcd1234_delayed.png"

        portal_msgs = [_make_portal_upload_entry(stored_name, caption="check this", upload_id=upload_id, ts=ts_portal)]
        session_msgs = [_make_session_upload_entry("delayed.png", "/path/delayed.png", caption="check this", upload_id=upload_id, ts=ts_session)]

        result = self._parse(portal_msgs, session_msgs)
        user_msgs = [m for m in result if m["role"] == "user"]
        self.assertEqual(len(user_msgs), 1,
                         f"Delayed upload should still dedup to 1 msg, got {len(user_msgs)}")

    def test_delayed_session_entry_over_60s(self):
        """Extreme case: Claude takes >60s to process the injection."""
        ts_portal = int(time.time())
        ts_session = ts_portal + 90
        upload_id = f"upload-{int(ts_portal * 1000)}-delay90s"
        stored_name = f"{int(ts_portal * 1000)}_abcd_extreme.png"

        portal_msgs = [_make_portal_upload_entry(stored_name, upload_id=upload_id, ts=ts_portal)]
        session_msgs = [_make_session_upload_entry("extreme.png", "/path/extreme.png", upload_id=upload_id, ts=ts_session)]

        result = self._parse(portal_msgs, session_msgs)
        user_msgs = [m for m in result if m["role"] == "user"]
        self.assertEqual(len(user_msgs), 1,
                         f"90s delayed upload should dedup to 1 msg, got {len(user_msgs)}")

    def test_two_uploads_within_30s_no_false_match(self):
        """Two different uploads within 30s of each other.
        Current loose pattern match could wrongly dedup the second portal entry
        against the first session entry (both contain [Image:] and are within 30s)."""
        ts = int(time.time())
        uid1 = f"upload-{int(ts * 1000)}-first001"
        uid2 = f"upload-{int((ts+5) * 1000)}-second02"

        portal_msgs = [
            _make_portal_upload_entry(f"{int(ts*1000)}_a_img1.png", upload_id=uid1, ts=ts, id_suffix="p1"),
            _make_portal_upload_entry(f"{int((ts+5)*1000)}_b_img2.png", upload_id=uid2, ts=ts + 5, id_suffix="p2"),
        ]
        session_msgs = [
            _make_session_upload_entry("img1.png", "/path/img1.png", upload_id=uid1, ts=ts, id_suffix="s1"),
            _make_session_upload_entry("img2.png", "/path/img2.png", upload_id=uid2, ts=ts + 5, id_suffix="s2"),
        ]

        result = self._parse(portal_msgs, session_msgs)
        user_msgs = [m for m in result if m["role"] == "user"]
        self.assertEqual(len(user_msgs), 2,
                         f"Two different uploads should produce 2 msgs, got {len(user_msgs)}: "
                         f"{[m['text'][:60] for m in user_msgs]}")

    def test_upload_id_extraction_from_portal_entry(self):
        """Portal entries with upload_id field should have it accessible in parsed messages."""
        ts = int(time.time())
        upload_id = f"upload-{int(ts * 1000)}-extract1"
        stored_name = f"{int(ts * 1000)}_abcd_extract.png"

        portal_msgs = [_make_portal_upload_entry(stored_name, upload_id=upload_id, ts=ts)]
        result = self._parse(portal_msgs, [])
        user_msgs = [m for m in result if m["role"] == "user"]
        self.assertEqual(len(user_msgs), 1)
        # The upload_id should be preserved through parsing
        self.assertEqual(user_msgs[0].get("upload_id"), upload_id,
                         "upload_id field should be preserved in parsed portal messages")


class TestSavePortalMessageUploadId(unittest.TestCase):
    """Test that _save_portal_message accepts and stores upload_id."""

    def test_save_portal_message_with_upload_id(self):
        """_save_portal_message should accept optional upload_id parameter."""
        import portal_server
        import tempfile
        from pathlib import Path

        # Use a temp file to avoid modifying production log
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            tmp_path = Path(f.name)

        original_log = portal_server.PORTAL_CHAT_LOG
        try:
            portal_server.PORTAL_CHAT_LOG = tmp_path
            upload_id = "upload-1234567890-testtest"
            entry = portal_server._save_portal_message(
                "[Image: test.png]\ncaption", role="user", upload_id=upload_id
            )
            self.assertEqual(entry.get("upload_id"), upload_id)

            # Verify it was written to the file
            with tmp_path.open("r") as f:
                line = f.readline()
                data = json.loads(line)
                self.assertEqual(data.get("upload_id"), upload_id)
        finally:
            portal_server.PORTAL_CHAT_LOG = original_log
            tmp_path.unlink(missing_ok=True)

    def test_save_portal_message_without_upload_id(self):
        """_save_portal_message without upload_id should work as before (no upload_id key)."""
        import portal_server
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            tmp_path = Path(f.name)

        original_log = portal_server.PORTAL_CHAT_LOG
        try:
            portal_server.PORTAL_CHAT_LOG = tmp_path
            entry = portal_server._save_portal_message("Hello world", role="user")
            self.assertNotIn("upload_id", entry)
        finally:
            portal_server.PORTAL_CHAT_LOG = original_log
            tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
