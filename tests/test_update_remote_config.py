"""
test_update_remote_config.py -- Unit tests for auto-configuring git remote in api_update_check.

Tests that when `origin` remote is not configured, the endpoint automatically adds it
before attempting the fetch -- fixing silent failures on fresh (non-git-clone) deployments.
"""

import asyncio
import importlib
import sys
import os
import unittest
from unittest.mock import AsyncMock, patch, call

# Ensure portal_server is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


EXPECTED_REMOTE_URL = "https://github.com/coreycottrell/purebrain-portal.git"


class TestUpdateCheckAutoConfigRemote(unittest.IsolatedAsyncioTestCase):
    """Unit tests for auto-remote-configuration logic in api_update_check."""

    async def test_auto_adds_remote_when_missing(self):
        """When origin is not configured, remote add is called before fetch."""
        import portal_server

        call_sequence = []

        async def mock_git_cmd(args, timeout=15):
            call_sequence.append(args)
            if args == ["remote", "get-url", "origin"]:
                return (1, "error: No such remote 'origin'")
            if args[:2] == ["remote", "add"]:
                return (0, "")
            if args[:3] == ["fetch", "origin", "main"]:
                return (0, "")
            if args == ["rev-parse", "HEAD"]:
                return (0, "abc123")
            if args == ["rev-parse", "origin/main"]:
                return (0, "abc123")
            return (0, "")

        class FakeRequest:
            def __init__(self):
                self.headers = {"Authorization": "Bearer test-token"}

        with patch.object(portal_server, "_git_cmd", side_effect=mock_git_cmd), \
             patch.object(portal_server, "check_auth", return_value=True), \
             patch.object(portal_server, "_get_current_version", new=AsyncMock(return_value="1.0.0")):
            resp = await portal_server.api_update_check(FakeRequest())

        # remote add must have been called with the correct URL
        self.assertIn(
            ["remote", "add", "origin", EXPECTED_REMOTE_URL],
            call_sequence,
            "Expected 'git remote add origin <url>' to be called when remote is missing",
        )
        # fetch must have happened after remote add
        add_idx = call_sequence.index(["remote", "add", "origin", EXPECTED_REMOTE_URL])
        fetch_args = [a for a in call_sequence if a[:3] == ["fetch", "origin", "main"]]
        self.assertTrue(fetch_args, "Expected fetch to be called after remote add")
        fetch_idx = call_sequence.index(fetch_args[0])
        self.assertGreater(fetch_idx, add_idx, "fetch must happen AFTER remote add")

    async def test_skips_remote_add_when_origin_exists(self):
        """When origin is already configured, remote add is NOT called."""
        import portal_server

        call_sequence = []

        async def mock_git_cmd(args, timeout=15):
            call_sequence.append(args)
            if args == ["remote", "get-url", "origin"]:
                return (0, EXPECTED_REMOTE_URL)
            if args[:3] == ["fetch", "origin", "main"]:
                return (0, "")
            if args == ["rev-parse", "HEAD"]:
                return (0, "abc123")
            if args == ["rev-parse", "origin/main"]:
                return (0, "abc123")
            return (0, "")

        class FakeRequest:
            def __init__(self):
                self.headers = {"Authorization": "Bearer test-token"}

        with patch.object(portal_server, "_git_cmd", side_effect=mock_git_cmd), \
             patch.object(portal_server, "check_auth", return_value=True), \
             patch.object(portal_server, "_get_current_version", new=AsyncMock(return_value="1.0.0")):
            resp = await portal_server.api_update_check(FakeRequest())

        remote_adds = [a for a in call_sequence if a[:2] == ["remote", "add"]]
        self.assertEqual(remote_adds, [], "remote add must NOT be called when origin already exists")

    async def test_returns_error_when_fetch_fails_even_after_remote_add(self):
        """If fetch fails even after auto-adding remote, error response is returned."""
        import portal_server

        async def mock_git_cmd(args, timeout=15):
            if args == ["remote", "get-url", "origin"]:
                return (1, "")
            if args[:2] == ["remote", "add"]:
                return (0, "")
            if args[:3] == ["fetch", "origin", "main"]:
                return (1, "fatal: repository not found")
            return (0, "")

        class FakeRequest:
            def __init__(self):
                self.headers = {"Authorization": "Bearer test-token"}

        with patch.object(portal_server, "_git_cmd", side_effect=mock_git_cmd), \
             patch.object(portal_server, "check_auth", return_value=True):
            resp = await portal_server.api_update_check(FakeRequest())

        data = resp.body if hasattr(resp, "body") else None
        import json
        body = json.loads(resp.body)
        self.assertEqual(body["status"], "error")


if __name__ == "__main__":
    unittest.main()
