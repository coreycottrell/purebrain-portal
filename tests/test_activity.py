"""
Tests for portal_activity.py — Activity Logger.

Covers:
  - log_activity() creates entries in the JSONL file
  - Entries have correct fields (ts, action, detail, category)
  - Auto-trim keeps file at max 200 entries
  - GET /api/activity returns recent entries (newest first)
  - GET /api/activity?limit=N respects limit
  - Auth required on the endpoint
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PORTAL_DIR)


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_request(token="valid-token", query_params=None):
    """Create a mock Starlette Request."""
    req = MagicMock()
    req.headers = {"authorization": f"Bearer {token}"}
    req.query_params = query_params or {}
    return req


class TestLogActivity:
    """Test the log_activity() function."""

    def test_creates_entry(self, tmp_path):
        log_file = tmp_path / "activity-log.jsonl"
        with patch("portal_activity.ACTIVITY_LOG", log_file):
            from portal_activity import log_activity
            log_activity("test_action", "some detail", "agent")

        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["action"] == "test_action"
        assert entry["detail"] == "some detail"
        assert entry["category"] == "agent"

    def test_entry_has_required_fields(self, tmp_path):
        log_file = tmp_path / "activity-log.jsonl"
        with patch("portal_activity.ACTIVITY_LOG", log_file):
            from portal_activity import log_activity
            log_activity("boot", "", "system")

        entry = json.loads(log_file.read_text().strip())
        assert "ts" in entry
        assert "action" in entry
        assert "detail" in entry
        assert "category" in entry
        # ts should be ISO format
        assert "T" in entry["ts"]

    def test_multiple_entries_append(self, tmp_path):
        log_file = tmp_path / "activity-log.jsonl"
        with patch("portal_activity.ACTIVITY_LOG", log_file):
            from portal_activity import log_activity
            log_activity("first", "", "system")
            log_activity("second", "", "system")
            log_activity("third", "", "system")

        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 3


class TestTrimLog:
    """Test that _trim_log keeps at most _MAX_ENTRIES."""

    def test_trim_keeps_max_entries(self, tmp_path):
        log_file = tmp_path / "activity-log.jsonl"
        # Write 210 entries (more than _MAX_ENTRIES=200)
        # Make each line large enough so total size > 20_000 bytes (trim threshold)
        entries = []
        for i in range(210):
            entry = {"ts": f"2025-01-01T00:{i:03d}", "action": f"action_{i}",
                     "detail": "x" * 100, "category": "system"}
            entries.append(json.dumps(entry))
        log_file.write_text("\n".join(entries) + "\n")

        with patch("portal_activity.ACTIVITY_LOG", log_file), \
             patch("portal_activity._MAX_ENTRIES", 200):
            from portal_activity import _trim_log
            _trim_log()

        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 200
        # Should keep the LAST 200 entries (action_10 through action_209)
        last = json.loads(lines[-1])
        assert last["action"] == "action_209"


class TestReadRecent:
    """Test _read_recent returns entries newest-first."""

    def test_returns_newest_first(self, tmp_path):
        log_file = tmp_path / "activity-log.jsonl"
        entries = []
        for i in range(5):
            entries.append(json.dumps({"ts": f"2025-01-01T00:0{i}", "action": f"a{i}",
                                       "detail": "", "category": "system"}))
        log_file.write_text("\n".join(entries) + "\n")

        with patch("portal_activity.ACTIVITY_LOG", log_file):
            from portal_activity import _read_recent
            result = _read_recent(5)

        assert len(result) == 5
        # Newest first: a4 should be first
        assert result[0]["action"] == "a4"
        assert result[-1]["action"] == "a0"

    def test_respects_limit(self, tmp_path):
        log_file = tmp_path / "activity-log.jsonl"
        entries = []
        for i in range(10):
            entries.append(json.dumps({"ts": f"t{i}", "action": f"a{i}",
                                       "detail": "", "category": "system"}))
        log_file.write_text("\n".join(entries) + "\n")

        with patch("portal_activity.ACTIVITY_LOG", log_file):
            from portal_activity import _read_recent
            result = _read_recent(3)

        assert len(result) == 3

    def test_empty_file_returns_empty(self, tmp_path):
        log_file = tmp_path / "activity-log.jsonl"
        log_file.write_text("")

        with patch("portal_activity.ACTIVITY_LOG", log_file):
            from portal_activity import _read_recent
            result = _read_recent(5)

        assert result == []

    def test_missing_file_returns_empty(self, tmp_path):
        log_file = tmp_path / "nonexistent.jsonl"

        with patch("portal_activity.ACTIVITY_LOG", log_file):
            from portal_activity import _read_recent
            result = _read_recent(5)

        assert result == []


class TestApiActivity:
    """Test the api_activity endpoint handler."""

    def test_requires_auth(self):
        from portal_activity import api_activity
        req = _make_request(token="bad-token")
        with patch("portal_activity.check_auth", return_value=False):
            resp = run_async(api_activity(req))
        assert resp.status_code == 401

    def test_returns_activity_with_auth(self, tmp_path):
        log_file = tmp_path / "activity-log.jsonl"
        log_file.write_text(json.dumps({"ts": "t1", "action": "test",
                                         "detail": "", "category": "system"}) + "\n")

        from portal_activity import api_activity
        req = _make_request()
        req.query_params = {}
        with patch("portal_activity.check_auth", return_value=True), \
             patch("portal_activity.ACTIVITY_LOG", log_file):
            resp = run_async(api_activity(req))

        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert "activity" in data
        assert len(data["activity"]) == 1

    def test_limit_param(self, tmp_path):
        log_file = tmp_path / "activity-log.jsonl"
        entries = []
        for i in range(10):
            entries.append(json.dumps({"ts": f"t{i}", "action": f"a{i}",
                                       "detail": "", "category": "system"}))
        log_file.write_text("\n".join(entries) + "\n")

        from portal_activity import api_activity
        req = _make_request()
        req.query_params = {"limit": "3"}
        with patch("portal_activity.check_auth", return_value=True), \
             patch("portal_activity.ACTIVITY_LOG", log_file):
            resp = run_async(api_activity(req))

        data = json.loads(resp.body)
        assert len(data["activity"]) == 3
