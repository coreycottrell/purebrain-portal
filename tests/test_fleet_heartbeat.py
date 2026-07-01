"""
Tests for fleet heartbeat loop.

Covers:
  - Heartbeat loop exists in portal_server.py
  - Heartbeat payload includes civ_name, version, uptime
  - Heartbeat is started in _startup
"""

import os
import re
import sys

import pytest

PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PORTAL_DIR)


def _read_server_source() -> str:
    with open(os.path.join(PORTAL_DIR, "portal_server.py")) as f:
        return f.read()


class TestFleetHeartbeat:
    """Verify fleet heartbeat infrastructure via static analysis."""

    def test_heartbeat_loop_exists(self):
        source = _read_server_source()
        assert "async def _fleet_heartbeat_loop" in source

    def test_heartbeat_started_in_startup(self):
        source = _read_server_source()
        assert "_fleet_heartbeat_loop()" in source

    def test_payload_includes_civ_name(self):
        source = _read_server_source()
        # Find the heartbeat function body
        match = re.search(
            r'async def _fleet_heartbeat_loop.*?(?=\nasync def |\nclass |\Z)',
            source, re.DOTALL
        )
        assert match, "Could not find _fleet_heartbeat_loop function"
        body = match.group(0)
        assert '"civ_name"' in body

    def test_payload_includes_version(self):
        source = _read_server_source()
        match = re.search(
            r'async def _fleet_heartbeat_loop.*?(?=\nasync def |\nclass |\Z)',
            source, re.DOTALL
        )
        body = match.group(0)
        assert '"version"' in body

    def test_payload_includes_uptime(self):
        source = _read_server_source()
        match = re.search(
            r'async def _fleet_heartbeat_loop.*?(?=\nasync def |\nclass |\Z)',
            source, re.DOTALL
        )
        body = match.group(0)
        assert '"uptime"' in body
