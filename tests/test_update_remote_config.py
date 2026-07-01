"""
test_update_remote_config.py -- Unit tests for release-server update configuration.

Tests that api_update_check correctly communicates with the release server,
handles missing tokens, and returns appropriate error/success responses.

(Replaces old git-remote auto-config tests which tested _ensure_git_repo logic
that was removed in the release-server migration.)
"""

import asyncio
import json
import sys
import os
import unittest
from unittest.mock import AsyncMock, patch

# Ensure portal_server is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestUpdateCheckReleaseServer(unittest.IsolatedAsyncioTestCase):
    """Unit tests for api_update_check with release server backend."""

    async def test_returns_available_when_remote_version_is_newer(self):
        """When the release server returns a newer version, status is 'available'."""
        import portal_updates

        class FakeRequest:
            headers = {"Authorization": "Bearer test-token"}

        fake_resp_data = json.dumps({
            "version": "99.0.0",
            "sha256": "abc123def456",
            "size_bytes": 2048000,
        }).encode()

        class FakeHTTPResponse:
            def read(self):
                return fake_resp_data

        with patch.object(portal_updates, "check_auth", return_value=True), \
             patch.object(portal_updates, "_get_current_version", new=AsyncMock(return_value="1.0.0")), \
             patch.object(portal_updates, "PORTAL_UPDATE_TOKEN", "test-token-123"), \
             patch("urllib.request.urlopen", return_value=FakeHTTPResponse()):
            resp = await portal_updates.api_update_check(FakeRequest())

        body = json.loads(resp.body)
        self.assertEqual(body["status"], "available")
        self.assertEqual(body["remote_version"], "99.0.0")
        self.assertEqual(body["current_version"], "1.0.0")

    async def test_returns_up_to_date_when_versions_match(self):
        """When the release server returns the same version, status is 'up_to_date'."""
        import portal_updates

        class FakeRequest:
            headers = {"Authorization": "Bearer test-token"}

        fake_resp_data = json.dumps({
            "version": "2.0.0",
            "sha256": "abc123",
            "size_bytes": 1024000,
        }).encode()

        class FakeHTTPResponse:
            def read(self):
                return fake_resp_data

        with patch.object(portal_updates, "check_auth", return_value=True), \
             patch.object(portal_updates, "_get_current_version", new=AsyncMock(return_value="2.0.0")), \
             patch.object(portal_updates, "PORTAL_UPDATE_TOKEN", "test-token-123"), \
             patch("urllib.request.urlopen", return_value=FakeHTTPResponse()):
            resp = await portal_updates.api_update_check(FakeRequest())

        body = json.loads(resp.body)
        self.assertEqual(body["status"], "up_to_date")

    async def test_returns_error_when_token_is_missing(self):
        """When PORTAL_UPDATE_TOKEN is empty, error response is returned."""
        import portal_updates

        class FakeRequest:
            headers = {"Authorization": "Bearer test-token"}

        with patch.object(portal_updates, "check_auth", return_value=True), \
             patch.object(portal_updates, "_get_current_version", new=AsyncMock(return_value="1.0.0")), \
             patch.object(portal_updates, "PORTAL_UPDATE_TOKEN", ""):
            resp = await portal_updates.api_update_check(FakeRequest())

        body = json.loads(resp.body)
        self.assertEqual(body["status"], "error")
        self.assertIn("PORTAL_UPDATE_TOKEN", body.get("error", ""))

    async def test_returns_error_when_server_unreachable(self):
        """If release server is unreachable, error response is returned."""
        import portal_updates

        class FakeRequest:
            headers = {"Authorization": "Bearer test-token"}

        with patch.object(portal_updates, "check_auth", return_value=True), \
             patch.object(portal_updates, "_get_current_version", new=AsyncMock(return_value="1.0.0")), \
             patch.object(portal_updates, "PORTAL_UPDATE_TOKEN", "test-token-123"), \
             patch("urllib.request.urlopen", side_effect=ConnectionError("Connection refused")):
            resp = await portal_updates.api_update_check(FakeRequest())

        body = json.loads(resp.body)
        self.assertEqual(body["status"], "error")

    async def test_returns_error_when_server_returns_no_version(self):
        """If release server returns empty version, error response is returned."""
        import portal_updates

        class FakeRequest:
            headers = {"Authorization": "Bearer test-token"}

        fake_resp_data = json.dumps({
            "version": "",
            "sha256": "",
            "size_bytes": 0,
        }).encode()

        class FakeHTTPResponse:
            def read(self):
                return fake_resp_data

        with patch.object(portal_updates, "check_auth", return_value=True), \
             patch.object(portal_updates, "_get_current_version", new=AsyncMock(return_value="1.0.0")), \
             patch.object(portal_updates, "PORTAL_UPDATE_TOKEN", "test-token-123"), \
             patch("urllib.request.urlopen", return_value=FakeHTTPResponse()):
            resp = await portal_updates.api_update_check(FakeRequest())

        body = json.loads(resp.body)
        self.assertEqual(body["status"], "error")


if __name__ == "__main__":
    unittest.main()
