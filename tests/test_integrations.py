"""
Tests for GET /api/integrations/status endpoint.

Covers:
  - Returns a list of integrations
  - Each integration has name, status (active/inactive)
  - Auth required
"""

import asyncio
import json
import os
import sys
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


def _make_request(token="valid-token"):
    req = MagicMock()
    req.headers = {"authorization": f"Bearer {token}"}
    return req


class TestIntegrationsStatus:
    """Test api_integrations_status endpoint."""

    def test_requires_auth(self):
        from portal_server import api_integrations_status
        req = _make_request(token="bad")
        with patch("portal_server.check_auth", return_value=False):
            resp = run_async(api_integrations_status(req))
        assert resp.status_code == 401

    def test_returns_list(self):
        from portal_server import api_integrations_status
        req = _make_request()

        with patch("portal_server.check_auth", return_value=True), \
             patch("portal_server._load_settings", return_value={}):
            resp = run_async(api_integrations_status(req))

        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert "integrations" in data
        assert isinstance(data["integrations"], list)
        assert len(data["integrations"]) > 0

    def test_each_integration_has_name_and_status(self):
        from portal_server import api_integrations_status
        req = _make_request()

        with patch("portal_server.check_auth", return_value=True), \
             patch("portal_server._load_settings", return_value={}):
            resp = run_async(api_integrations_status(req))

        data = json.loads(resp.body)
        for integration in data["integrations"]:
            assert "name" in integration, f"Integration missing 'name': {integration}"
            assert "status" in integration, f"Integration missing 'status': {integration}"
            assert integration["status"] in ("active", "inactive"), \
                f"Bad status: {integration['status']}"

    def test_agentmail_active_when_configured(self):
        from portal_server import api_integrations_status
        req = _make_request()
        settings = {"agentmail_api_key": "test-key-123", "agentmail_email": "test@agentmail.to"}

        with patch("portal_server.check_auth", return_value=True), \
             patch("portal_server._load_settings", return_value=settings):
            resp = run_async(api_integrations_status(req))

        data = json.loads(resp.body)
        am = next(i for i in data["integrations"] if i["name"] == "AgentMail")
        assert am["status"] == "active"

    def test_agentmail_inactive_when_unconfigured(self):
        from portal_server import api_integrations_status
        req = _make_request()

        with patch("portal_server.check_auth", return_value=True), \
             patch("portal_server._load_settings", return_value={}):
            resp = run_async(api_integrations_status(req))

        data = json.loads(resp.body)
        am = next(i for i in data["integrations"] if i["name"] == "AgentMail")
        assert am["status"] == "inactive"
