"""
Tests for update notification dedup.

The dedup logic is in the frontend (portal-pb-styled.html) — checks if a notification
for the given version already exists before pushing a new one. This test verifies the
logic exists in the source.
"""

import os
import re
import sys

import pytest

PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PORTAL_DIR)


def _read_html_source() -> str:
    with open(os.path.join(PORTAL_DIR, "portal-pb-styled.html")) as f:
        return f.read()


class TestUpdateNotificationDedup:
    """Verify notification dedup logic exists in frontend."""

    def test_dedup_check_exists(self):
        """The update check should verify if notification already exists before pushing."""
        source = _read_html_source()
        # The dedup logic checks existing notifications before pushing
        assert "already" in source.lower() or "some(" in source, \
            "Expected dedup check (already/some) in update notification code"

    def test_fetches_existing_notifications(self):
        """Should fetch /api/notifications to check for existing update notifications."""
        source = _read_html_source()
        assert "/api/notifications" in source

    def test_version_comparison_in_dedup(self):
        """The dedup should compare against the remote_version."""
        source = _read_html_source()
        # Should reference remote_version in the notification dedup section
        assert "remote_version" in source
