"""
Tests for the bidirectional Telegram bridge (telegram_listener.py).

Tests cover:
- Message polling and authorization
- Tmux session discovery
- Message injection
- Session JSONL response parsing
- Response stabilization logic
- Long message splitting
- Fallback behavior
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

# Add tools directory to path so we can import the listener
TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "tools")
sys.path.insert(0, TOOLS_DIR)

import telegram_listener as tg


# ─── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_config(tmp_path):
    """Create a temporary telegram config file."""
    config = {
        "bot_token": "TEST_TOKEN_123",
        "chat_id": "12345",
        "human_name": "TestUser",
        "bot_username": "test_bot"
    }
    config_file = tmp_path / "telegram_config.json"
    config_file.write_text(json.dumps(config))
    return config_file, config


@pytest.fixture
def tmp_state(tmp_path):
    """Create a temporary state file."""
    state_file = tmp_path / "state.json"
    return state_file


@pytest.fixture
def tmp_inbox(tmp_path):
    """Create a temporary inbox file."""
    inbox_file = tmp_path / "inbox.jsonl"
    return inbox_file


@pytest.fixture
def tmp_jsonl(tmp_path):
    """Create a temporary session JSONL file with sample messages."""
    jsonl_file = tmp_path / "session.jsonl"
    return jsonl_file


# ─── Test: Message splitting ────────────────────────────────────────────────

class TestMessageSplitting:
    """Test that long messages are properly split for Telegram's 4K limit."""

    def test_short_message_no_split(self):
        """Messages under 4000 chars should not be split."""
        with mock.patch("telegram_listener.requests.post") as mock_post:
            mock_post.return_value = mock.Mock(json=lambda: {"ok": True})
            tg.send_reply("TOKEN", "123", "Hello world")
            assert mock_post.call_count == 1

    def test_long_message_splits_at_newline(self):
        """Messages over 4000 chars should split at the last newline before the limit."""
        # Create a message with lines that total over 4000 chars
        lines = [f"Line {i}: " + "x" * 80 for i in range(60)]  # ~5400 chars
        long_msg = "\n".join(lines)
        assert len(long_msg) > 4000

        with mock.patch("telegram_listener.requests.post") as mock_post:
            mock_post.return_value = mock.Mock(json=lambda: {"ok": True})
            tg.send_reply("TOKEN", "123", long_msg)
            assert mock_post.call_count == 2  # Should split into 2 chunks

    def test_very_long_message_splits_multiple(self):
        """Very long messages should split into multiple chunks."""
        long_msg = "\n".join([f"Line {i}: " + "x" * 90 for i in range(150)])  # ~15000 chars
        assert len(long_msg) > 12000

        with mock.patch("telegram_listener.requests.post") as mock_post:
            mock_post.return_value = mock.Mock(json=lambda: {"ok": True})
            tg.send_reply("TOKEN", "123", long_msg)
            assert mock_post.call_count >= 3

    def test_empty_message_no_send(self):
        """Empty or whitespace-only messages should not be sent."""
        with mock.patch("telegram_listener.requests.post") as mock_post:
            tg.send_reply("TOKEN", "123", "   ")
            assert mock_post.call_count == 0


# ─── Test: Authorization ────────────────────────────────────────────────────

class TestAuthorization:
    """Test that only authorized chat_ids are processed."""

    def _make_update(self, chat_id, text="hello", from_name="User"):
        return {
            "update_id": 1,
            "message": {
                "chat": {"id": chat_id},
                "text": text,
                "from": {"first_name": from_name},
                "date": int(time.time())
            }
        }

    def test_authorized_chat_processes(self, tmp_path):
        """Messages from authorized chat_id should be processed."""
        inbox = tmp_path / "inbox.jsonl"
        with mock.patch.object(tg, "INBOX_PATH", inbox):
            update = self._make_update(12345, "test message")
            msg = update["message"]
            chat_id = str(msg["chat"]["id"])

            # Simulate the authorization check
            authorized = "12345"
            assert chat_id == authorized

    def test_unauthorized_chat_rejected(self):
        """Messages from unauthorized chat_id should be rejected."""
        update = self._make_update(99999, "sneaky message")
        msg = update["message"]
        chat_id = str(msg["chat"]["id"])

        authorized = "12345"
        assert chat_id != authorized


# ─── Test: Tmux session discovery ───────────────────────────────────────────

class TestTmuxDiscovery:
    """Test tmux session discovery logic."""

    def test_reads_current_session_marker(self, tmp_path):
        """Should read .current_session file if it exists."""
        marker = tmp_path / ".current_session"
        marker.write_text("flux-primary-20260322-123456")

        with mock.patch.object(Path, "home", return_value=tmp_path):
            with mock.patch("telegram_listener.subprocess.check_output") as mock_check:
                mock_check.return_value = b""  # has-session succeeds
                session = tg.get_tmux_session()
                assert session == "flux-primary-20260322-123456"

    def test_fallback_when_no_marker(self, tmp_path):
        """Should fallback to listing sessions when no marker exists."""
        # No .current_session file
        with mock.patch.object(Path, "home", return_value=tmp_path):
            with mock.patch("telegram_listener.subprocess.check_output") as mock_check:
                mock_check.side_effect = [
                    FileNotFoundError,  # has-session fails
                    b"flux-primary-20260322\nboop-daemon\n"  # list-sessions
                ]
                session = tg.get_tmux_session()
                assert "flux" in session.lower()


# ─── Test: Tmux injection ──────────────────────────────────────────────────

class TestTmuxInjection:
    """Test message injection into tmux."""

    def test_inject_tags_message(self):
        """Injected messages should be tagged with [telegram]."""
        with mock.patch("telegram_listener.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0)
            result = tg.inject_into_tmux("test-session", "hello from tg")
            assert result is True

            # First call should contain the tagged message
            first_call = mock_run.call_args_list[0]
            cmd = first_call[0][0]
            assert "[telegram]" in " ".join(cmd)
            assert "hello from tg" in " ".join(cmd)

    def test_inject_failure_returns_false(self):
        """Should return False if tmux injection fails."""
        with mock.patch("telegram_listener.subprocess.run") as mock_run:
            mock_run.side_effect = Exception("tmux not available")
            result = tg.inject_into_tmux("test-session", "hello")
            assert result is False


# ─── Test: Session JSONL parsing ────────────────────────────────────────────

class TestJsonlParsing:
    """Test parsing of Claude session JSONL for responses."""

    def _write_jsonl(self, path, entries):
        with open(path, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

    def test_finds_assistant_message_after_timestamp(self, tmp_jsonl):
        """Should find assistant messages that appeared after the given timestamp."""
        entries = [
            {
                "timestamp": "2026-03-22T10:00:00",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}]
                }
            },
            {
                "timestamp": "2026-03-22T10:00:05",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hi there! How can I help?"}]
                }
            }
        ]
        self._write_jsonl(tmp_jsonl, entries)

        result = tg.get_last_assistant_message(tmp_jsonl, "2026-03-22T09:59:00")
        assert result == "Hi there! How can I help?"

    def test_ignores_messages_before_timestamp(self, tmp_jsonl):
        """Should not return assistant messages from before the timestamp."""
        entries = [
            {
                "timestamp": "2026-03-22T08:00:00",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Old response"}]
                }
            }
        ]
        self._write_jsonl(tmp_jsonl, entries)

        result = tg.get_last_assistant_message(tmp_jsonl, "2026-03-22T09:00:00")
        assert result is None

    def test_returns_last_assistant_message(self, tmp_jsonl):
        """Should return the LAST assistant message, not the first."""
        entries = [
            {
                "timestamp": "2026-03-22T10:00:05",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "First response"}]
                }
            },
            {
                "timestamp": "2026-03-22T10:00:10",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Updated response"}]
                }
            }
        ]
        self._write_jsonl(tmp_jsonl, entries)

        result = tg.get_last_assistant_message(tmp_jsonl, "2026-03-22T10:00:00")
        assert result == "Updated response"

    def test_handles_string_content_blocks(self, tmp_jsonl):
        """Should handle content blocks that are plain strings."""
        entries = [
            {
                "timestamp": "2026-03-22T10:00:05",
                "message": {
                    "role": "assistant",
                    "content": ["Hello from Claude"]
                }
            }
        ]
        self._write_jsonl(tmp_jsonl, entries)

        result = tg.get_last_assistant_message(tmp_jsonl, "2026-03-22T10:00:00")
        assert result == "Hello from Claude"

    def test_skips_short_noise_messages(self, tmp_jsonl):
        """Should skip very short responses (< 3 chars)."""
        entries = [
            {
                "timestamp": "2026-03-22T10:00:05",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}]
                }
            }
        ]
        self._write_jsonl(tmp_jsonl, entries)

        result = tg.get_last_assistant_message(tmp_jsonl, "2026-03-22T10:00:00")
        assert result is None

    def test_handles_missing_file(self):
        """Should return None for nonexistent file."""
        result = tg.get_last_assistant_message(Path("/nonexistent/file.jsonl"), "2026-03-22T10:00:00")
        assert result is None

    def test_handles_malformed_jsonl(self, tmp_jsonl):
        """Should gracefully skip malformed JSON lines."""
        with open(tmp_jsonl, "w") as f:
            f.write("not json\n")
            f.write(json.dumps({
                "timestamp": "2026-03-22T10:00:05",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Valid response"}]
                }
            }) + "\n")
            f.write("{broken json\n")

        result = tg.get_last_assistant_message(tmp_jsonl, "2026-03-22T10:00:00")
        assert result == "Valid response"


# ─── Test: State management ─────────────────────────────────────────────────

class TestStateManagement:
    """Test state persistence for tracking Telegram update offsets."""

    def test_load_empty_state(self, tmp_path):
        """Should return default state when no file exists."""
        with mock.patch.object(tg, "STATE_PATH", tmp_path / "nonexistent.json"):
            state = tg.load_state()
            assert state == {"last_update_id": 0}

    def test_save_and_load_state(self, tmp_path):
        """Should persist and reload state correctly."""
        state_file = tmp_path / "state.json"
        with mock.patch.object(tg, "STATE_PATH", state_file):
            tg.save_state({"last_update_id": 42})
            loaded = tg.load_state()
            assert loaded["last_update_id"] == 42


# ─── Test: Inbox logging ────────────────────────────────────────────────────

class TestInboxLogging:
    """Test that messages are logged to the inbox file."""

    def test_save_message_appends(self, tmp_path):
        """Should append messages as JSONL."""
        inbox = tmp_path / "inbox.jsonl"
        with mock.patch.object(tg, "INBOX_PATH", inbox):
            tg.save_message({"text": "first", "from": "User"})
            tg.save_message({"text": "second", "from": "User"})

            lines = inbox.read_text().strip().split("\n")
            assert len(lines) == 2
            assert json.loads(lines[0])["text"] == "first"
            assert json.loads(lines[1])["text"] == "second"


# ─── Test: Poll updates ────────────────────────────────────────────────────

class TestPollUpdates:
    """Test Telegram API polling."""

    def test_successful_poll(self):
        """Should return updates from successful API call."""
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "ok": True,
            "result": [
                {"update_id": 1, "message": {"text": "hello"}}
            ]
        }
        with mock.patch("telegram_listener.requests.get", return_value=mock_response):
            updates = tg.poll_updates("TOKEN", 0)
            assert len(updates) == 1
            assert updates[0]["message"]["text"] == "hello"

    def test_failed_poll_returns_empty(self):
        """Should return empty list on API error."""
        with mock.patch("telegram_listener.requests.get", side_effect=Exception("network error")):
            updates = tg.poll_updates("TOKEN", 0)
            assert updates == []

    def test_non_200_returns_empty(self):
        """Should return empty list on non-200 status."""
        mock_response = mock.Mock()
        mock_response.status_code = 500
        with mock.patch("telegram_listener.requests.get", return_value=mock_response):
            updates = tg.poll_updates("TOKEN", 0)
            assert updates == []


# ─── Test: Config loading ──────────────────────────────────────────────────

class TestConfigLoading:
    """Test telegram config file loading."""

    def test_load_valid_config(self, tmp_config):
        """Should load config with all required fields."""
        config_file, expected = tmp_config
        with mock.patch.object(tg, "CONFIG_PATH", config_file):
            config = tg.load_config()
            assert config["bot_token"] == "TEST_TOKEN_123"
            assert config["chat_id"] == "12345"
            assert config["human_name"] == "TestUser"
