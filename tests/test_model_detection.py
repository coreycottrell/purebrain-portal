"""
Tests for _detect_session_model().

Covers:
  - Reads from fresh model file
  - Falls back to session JSONL when model file is stale
  - Falls back to default when no session exists
  - Never returns empty string
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

from portal_server import _detect_session_model


class TestDetectSessionModel:
    """Test _detect_session_model() fallback chain."""

    def test_reads_fresh_model_file(self, tmp_path):
        model_file = tmp_path / ".claude_session_model"
        model_file.write_text("claude-sonnet-4-6")
        # Touch it so it's recent
        os.utime(model_file, (time.time(), time.time()))

        with patch("portal_server.Path.home", return_value=tmp_path):
            result = _detect_session_model()

        assert result == "claude-sonnet-4-6"

    def test_falls_back_to_session_jsonl_when_stale(self, tmp_path):
        # Create a stale model file (older than 7 days)
        model_file = tmp_path / ".claude_session_model"
        model_file.write_text("claude-old-model")
        stale_time = time.time() - (8 * 86400)  # 8 days ago
        os.utime(model_file, (stale_time, stale_time))

        # Create a session JSONL with model info
        sessions_dir = tmp_path / "memories" / "sessions"
        sessions_dir.mkdir(parents=True)
        session_file = sessions_dir / "current-session.jsonl"
        entry = {"model": "claude-opus-4-6", "ts": "2025-01-01T00:00:00Z"}
        session_file.write_text(json.dumps(entry) + "\n")

        with patch("portal_server.Path.home", return_value=tmp_path):
            result = _detect_session_model()

        assert result == "claude-opus-4-6"

    def test_falls_back_to_stale_file_when_no_session(self, tmp_path):
        # Create a stale model file
        model_file = tmp_path / ".claude_session_model"
        model_file.write_text("claude-stale-model")
        stale_time = time.time() - (8 * 86400)
        os.utime(model_file, (stale_time, stale_time))
        # No session JSONL exists

        with patch("portal_server.Path.home", return_value=tmp_path):
            result = _detect_session_model()

        assert result == "claude-stale-model"

    def test_falls_back_to_default_when_nothing_exists(self, tmp_path):
        # No model file, no session
        with patch("portal_server.Path.home", return_value=tmp_path):
            result = _detect_session_model()

        assert result != ""
        assert "claude" in result.lower()

    def test_never_returns_empty(self, tmp_path):
        # Empty model file, no session
        model_file = tmp_path / ".claude_session_model"
        model_file.write_text("")

        with patch("portal_server.Path.home", return_value=tmp_path):
            result = _detect_session_model()

        assert result != ""
        assert isinstance(result, str)
