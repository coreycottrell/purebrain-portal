"""
Tests for GET /api/system/stats endpoint.

Covers:
  - Returns memory, cpu_load, disk fields
  - All values are numeric (not null/NaN)
  - Auth required
"""

import asyncio
import json
import math
import os
import sys
from unittest.mock import patch, MagicMock, mock_open

import pytest

PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PORTAL_DIR)


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_request(token="valid-token"):
    req = MagicMock()
    req.headers = {"authorization": f"Bearer {token}"}
    return req


FAKE_MEMINFO = """\
MemTotal:       16384000 kB
MemFree:         2048000 kB
MemAvailable:    4096000 kB
Buffers:          512000 kB
Cached:          2048000 kB
"""

FAKE_LOADAVG = "1.25 0.95 0.80 3/150 12345\n"


class TestSystemStats:
    """Test api_system_stats endpoint."""

    def test_requires_auth(self):
        from portal_server import api_system_stats
        req = _make_request(token="bad")
        with patch("portal_server.check_auth", return_value=False):
            resp = run_async(api_system_stats(req))
        assert resp.status_code == 401

    def test_returns_all_fields(self):
        from portal_server import api_system_stats
        req = _make_request()

        with patch("portal_server.check_auth", return_value=True), \
             patch("builtins.open", side_effect=lambda f, *a, **kw:
                   mock_open(read_data=FAKE_MEMINFO)() if "meminfo" in str(f)
                   else mock_open(read_data=FAKE_LOADAVG)() if "loadavg" in str(f)
                   else open(f, *a, **kw)):
            resp = run_async(api_system_stats(req))

        assert resp.status_code == 200
        data = json.loads(resp.body)
        expected_keys = ["memory_used_gb", "memory_total_gb", "cpu_load",
                         "disk_used_gb", "disk_total_gb"]
        for key in expected_keys:
            assert key in data, f"Missing key: {key}"

    def test_values_are_numeric(self):
        from portal_server import api_system_stats
        req = _make_request()

        with patch("portal_server.check_auth", return_value=True), \
             patch("builtins.open", side_effect=lambda f, *a, **kw:
                   mock_open(read_data=FAKE_MEMINFO)() if "meminfo" in str(f)
                   else mock_open(read_data=FAKE_LOADAVG)() if "loadavg" in str(f)
                   else open(f, *a, **kw)):
            resp = run_async(api_system_stats(req))

        data = json.loads(resp.body)
        for key, val in data.items():
            assert isinstance(val, (int, float)), f"{key} is not numeric: {val}"
            assert not math.isnan(val), f"{key} is NaN"
            assert val is not None, f"{key} is None"

    def test_graceful_on_missing_proc(self):
        """When /proc files don't exist, values should be 0 (not crash)."""
        from portal_server import api_system_stats
        req = _make_request()

        with patch("portal_server.check_auth", return_value=True), \
             patch("builtins.open", side_effect=FileNotFoundError):
            resp = run_async(api_system_stats(req))

        assert resp.status_code == 200
        data = json.loads(resp.body)
        # Should return 0s, not crash
        assert data["cpu_load"] == 0.0
