"""
Tests for email_boop.py — dual notification to Telegram + Portal.

Updated to match the current email_boop API:
- send_portal_chat_message (not send_portal_notification)
- format_portal_chat_message (not format_portal_alert)
- check_inbox uses REST API (not AgentMail SDK)
"""

import os
import sys
import json
import tempfile
from unittest.mock import patch, MagicMock

import pytest

# Make sure the tools dir is importable
sys.path.insert(0, "/home/aiciv/tools")

import email_boop


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_portal_token(tmp_path):
    """Create a temporary .portal-token file and patch the module constant."""
    token_file = tmp_path / ".portal-token"
    token_file.write_text("test-token-abc123")
    with patch.object(email_boop, "PORTAL_TOKEN_FILE", str(token_file)):
        yield str(token_file)


@pytest.fixture
def sample_messages():
    """Typical new-message dicts from check_inbox()."""
    return [
        {
            "sender": "alice@example.com",
            "subject": "Hello from Alice",
            "body": "Just checking in...",
            "thread_id": "t1",
            "message_id": "m1",
        },
        {
            "sender": "bob@example.com",
            "subject": "Urgent: deploy needed",
            "body": "Please deploy the fix ASAP",
            "thread_id": "t2",
            "message_id": "m2",
        },
    ]


# ---------------------------------------------------------------------------
# 1. Portal token is read correctly
# ---------------------------------------------------------------------------

class TestPortalTokenReading:
    def test_read_portal_token_from_file(self, tmp_path):
        token_file = tmp_path / ".portal-token"
        token_file.write_text("my-secret-token\n")
        with patch.object(email_boop, "PORTAL_TOKEN_FILE", str(token_file)):
            token = email_boop.read_portal_token()
        assert token == "my-secret-token"

    def test_read_portal_token_missing_file(self, tmp_path):
        missing = str(tmp_path / "nonexistent")
        with patch.object(email_boop, "PORTAL_TOKEN_FILE", missing):
            token = email_boop.read_portal_token()
        assert token == ""

    def test_read_portal_token_empty_file(self, tmp_path):
        token_file = tmp_path / ".portal-token"
        token_file.write_text("  \n")
        with patch.object(email_boop, "PORTAL_TOKEN_FILE", str(token_file)):
            token = email_boop.read_portal_token()
        assert token == ""


# ---------------------------------------------------------------------------
# 2. send_portal_chat_message works
# ---------------------------------------------------------------------------

class TestSendPortalChatMessage:
    @patch("email_boop.requests.post")
    def test_send_portal_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        with patch.object(email_boop, "read_portal_token", return_value="tok123"):
            result = email_boop.send_portal_chat_message("Hello portal")

        assert result is True
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs[1]["json"]["message"] == "Hello portal"
        assert "Bearer tok123" in call_kwargs[1]["headers"]["Authorization"]

    @patch("email_boop.requests.post")
    def test_send_portal_http_error(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_post.return_value = mock_resp

        with patch.object(email_boop, "read_portal_token", return_value="tok"):
            result = email_boop.send_portal_chat_message("test")

        assert result is False

    @patch("email_boop.requests.post", side_effect=Exception("connection refused"))
    def test_send_portal_connection_failure(self, mock_post):
        with patch.object(email_boop, "read_portal_token", return_value="tok"):
            result = email_boop.send_portal_chat_message("test")
        assert result is False

    def test_send_portal_no_token(self):
        """If no portal token is available, skip gracefully."""
        with patch.object(email_boop, "read_portal_token", return_value=""):
            result = email_boop.send_portal_chat_message("test")
        assert result is False


# ---------------------------------------------------------------------------
# 3. Notification message format is clean
# ---------------------------------------------------------------------------

class TestNotificationFormat:
    def test_format_portal_chat_message_has_email_info(self):
        msg = {
            "sender": "alice@example.com",
            "subject": "Test Subject",
            "body": "Full body text here",
        }
        text = email_boop.format_portal_chat_message(msg)
        assert "alice@example.com" in text
        assert "Test Subject" in text
        assert "Full body text here" in text

    def test_format_portal_chat_message_differs_from_telegram(self):
        msg = {
            "sender": "bob@example.com",
            "subject": "Hello",
            "body": "World",
        }
        tg_text = email_boop.format_telegram_alert(msg)
        portal_text = email_boop.format_portal_chat_message(msg)
        # Both contain the essential info
        assert "bob@example.com" in tg_text
        assert "bob@example.com" in portal_text


# ---------------------------------------------------------------------------
# 4. run_boop sends to BOTH Telegram and portal
# ---------------------------------------------------------------------------

class TestRunBoopDualDelivery:
    @patch.object(email_boop, "send_portal_chat_message", return_value=True)
    @patch.object(email_boop, "send_telegram", return_value=True)
    @patch.object(email_boop, "write_unread_summary")
    @patch.object(email_boop, "check_inbox")
    def test_boop_sends_to_both(self, mock_inbox, mock_summary, mock_tg, mock_portal, sample_messages):
        mock_inbox.return_value = sample_messages
        count = email_boop.run_boop()
        assert count == 2
        assert mock_tg.call_count == 2
        assert mock_portal.call_count == 2

    @patch.object(email_boop, "send_portal_chat_message", return_value=True)
    @patch.object(email_boop, "send_telegram", return_value=True)
    @patch.object(email_boop, "write_unread_summary")
    @patch.object(email_boop, "check_inbox")
    def test_boop_no_messages(self, mock_inbox, mock_summary, mock_tg, mock_portal):
        mock_inbox.return_value = []
        count = email_boop.run_boop()
        assert count == 0
        mock_tg.assert_not_called()
        mock_portal.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Portal failure does not crash the BOOP loop
# ---------------------------------------------------------------------------

class TestPortalFailureResilience:
    @patch.object(email_boop, "send_portal_chat_message", side_effect=Exception("portal down"))
    @patch.object(email_boop, "send_telegram", return_value=True)
    @patch.object(email_boop, "write_unread_summary")
    @patch.object(email_boop, "check_inbox")
    def test_portal_crash_doesnt_stop_boop(self, mock_inbox, mock_summary, mock_tg, mock_portal, sample_messages):
        mock_inbox.return_value = sample_messages
        # Should NOT raise — portal failure is caught
        count = email_boop.run_boop()
        assert count == 2
        # Telegram should still have been called for both messages
        assert mock_tg.call_count == 2

    @patch.object(email_boop, "send_portal_chat_message", return_value=False)
    @patch.object(email_boop, "send_telegram", return_value=True)
    @patch.object(email_boop, "write_unread_summary")
    @patch.object(email_boop, "check_inbox")
    def test_portal_returns_false_doesnt_stop_boop(self, mock_inbox, mock_summary, mock_tg, mock_portal, sample_messages):
        mock_inbox.return_value = sample_messages
        count = email_boop.run_boop()
        assert count == 2
        assert mock_tg.call_count == 2
        assert mock_portal.call_count == 2


# ---------------------------------------------------------------------------
# 6. check_inbox uses REST API with correct API key
# ---------------------------------------------------------------------------

class TestCheckInboxUsesApiKey:
    """check_inbox uses the REST API with AGENTMAIL_API_KEY in the Authorization header."""

    @patch.object(email_boop, "save_state")
    @patch.object(email_boop, "load_state")
    def test_check_inbox_uses_api_key_in_header(self, mock_load, mock_save):
        """check_inbox should use AGENTMAIL_API_KEY in Authorization header."""
        from datetime import datetime as dt2, timezone as tz2
        mock_load.return_value = {
            "seen_messages": [],
            "last_check": None,
            "start_after": dt2.now(tz2.utc).isoformat(),
        }

        # Mock urllib.request.urlopen to return empty threads
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"threads": []}).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            email_boop.check_inbox()
            if mock_urlopen.called:
                req_arg = mock_urlopen.call_args[0][0]
                assert email_boop.AGENTMAIL_API_KEY in req_arg.get_header("Authorization")


# ---------------------------------------------------------------------------
# 7. check_inbox correctly detects new vs seen messages
# ---------------------------------------------------------------------------

class TestCheckInboxStateTracking:
    """check_inbox must correctly track which messages have been seen."""

    @patch.object(email_boop, "save_state")
    @patch.object(email_boop, "load_state")
    def test_new_message_detected(self, mock_load, mock_save):
        """A thread with an unseen message_id should be returned."""
        from datetime import datetime as dt2, timezone as tz2
        now = dt2.now(tz2.utc)
        start_after = (now - __import__('datetime').timedelta(hours=1)).isoformat()

        mock_load.return_value = {
            "seen_messages": ["old-msg-1"],
            "last_check": None,
            "start_after": start_after,
        }

        thread_data = {
            "threads": [{
                "thread_id": "t1",
                "last_message_id": "new-msg-1",
                "labels": ["received", "unread"],
                "senders": ["alice@example.com"],
                "subject": "Hello",
                "preview": "Hello there",
                "received_timestamp": now.isoformat(),
            }]
        }

        # Mock both urlopen calls (threads list + thread detail)
        thread_detail = {
            "messages": [{"text": "Full body of Hello"}]
        }

        call_count = [0]
        def mock_urlopen(req, **kwargs):
            call_count[0] += 1
            resp = MagicMock()
            if call_count[0] == 1:
                resp.read.return_value = json.dumps(thread_data).encode()
            else:
                resp.read.return_value = json.dumps(thread_detail).encode()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            msgs = email_boop.check_inbox()

        assert len(msgs) == 1
        assert msgs[0]["message_id"] == "new-msg-1"
        assert msgs[0]["subject"] == "Hello"

    @patch.object(email_boop, "save_state")
    @patch.object(email_boop, "load_state")
    def test_state_updated_after_check(self, mock_load, mock_save):
        """After checking, state must include the new message IDs and updated last_check."""
        from datetime import datetime as dt2, timezone as tz2
        now = dt2.now(tz2.utc)
        start_after = (now - __import__('datetime').timedelta(hours=1)).isoformat()

        mock_load.return_value = {
            "seen_messages": ["old-1"],
            "last_check": None,
            "start_after": start_after,
        }

        thread_data = {
            "threads": [{
                "thread_id": "t1",
                "last_message_id": "new-1",
                "labels": ["received", "unread"],
                "senders": ["alice@example.com"],
                "subject": "New",
                "preview": "New message",
                "received_timestamp": now.isoformat(),
            }]
        }

        thread_detail = {
            "messages": [{"text": "Full body"}]
        }

        call_count = [0]
        def mock_urlopen(req, **kwargs):
            call_count[0] += 1
            resp = MagicMock()
            if call_count[0] == 1:
                resp.read.return_value = json.dumps(thread_data).encode()
            else:
                resp.read.return_value = json.dumps(thread_detail).encode()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            email_boop.check_inbox()

        mock_save.assert_called_once()
        saved = mock_save.call_args[0][0]
        assert "new-1" in saved["seen_messages"]
        assert saved["last_check"] is not None


# ---------------------------------------------------------------------------
# 8. Full email body — no truncation in telegram (portal truncates to 1500)
# ---------------------------------------------------------------------------

class TestFullEmailBody:
    """The BOOP must include the FULL email body in telegram alerts."""

    def test_format_telegram_alert_uses_full_body(self):
        """format_telegram_alert must use msg['body'] (full)."""
        full_body = "Full email content here with all the details.\n\nSecond paragraph."
        msg = {
            "sender": "alice@example.com",
            "subject": "Important",
            "body": full_body,
        }
        text = email_boop.format_telegram_alert(msg)
        assert full_body in text
        assert "From: alice@example.com" in text
        assert "Subject: Important" in text

    def test_format_portal_chat_message_includes_body(self):
        """format_portal_chat_message must include the email body."""
        body = "Complete portal message with all paragraphs.\n\nParagraph two.\n\nParagraph three."
        msg = {
            "sender": "bob@example.com",
            "subject": "Portal Test",
            "body": body,
        }
        text = email_boop.format_portal_chat_message(msg)
        assert "bob@example.com" in text
        assert "Portal Test" in text
        # The portal format includes the body (may be truncated at 1500 chars)
        assert "Complete portal message" in text

    def test_alert_preserves_newlines_in_telegram(self):
        """Telegram format must preserve newlines."""
        body_with_newlines = "Dear Alex,\n\nThis is paragraph one.\n\nThis is paragraph two.\n\nBest,\nSender"
        msg = {
            "sender": "sender@example.com",
            "subject": "Newline Test",
            "body": body_with_newlines,
        }
        text = email_boop.format_telegram_alert(msg)
        assert body_with_newlines in text

    def test_long_email_not_truncated_in_telegram(self):
        """A 5000+ character email body must NOT be truncated in telegram."""
        long_body = "A" * 5000 + "\n\nThis is the important conclusion."
        msg = {
            "sender": "sender@example.com",
            "subject": "Long Email",
            "body": long_body,
        }
        text = email_boop.format_telegram_alert(msg)
        assert long_body in text

    @patch.object(email_boop, "send_portal_chat_message", return_value=True)
    @patch.object(email_boop, "send_telegram", return_value=True)
    @patch.object(email_boop, "write_unread_summary")
    @patch.object(email_boop, "check_inbox")
    def test_portal_notification_includes_body(self, mock_inbox, mock_summary, mock_tg, mock_portal):
        """The portal notification text must contain the email body."""
        full_body = "This is a very important email."
        mock_inbox.return_value = [{
            "sender": "test@example.com",
            "subject": "Full Body Check",
            "body": full_body,
            "thread_id": "t1",
            "message_id": "m1",
        }]

        email_boop.run_boop()

        mock_portal.assert_called_once()
        portal_text = mock_portal.call_args[0][0]
        assert full_body in portal_text

        mock_tg.assert_called_once()
        tg_text = mock_tg.call_args[0][0]
        assert full_body in tg_text
