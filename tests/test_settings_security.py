"""
Tests for settings secret masking.

Covers:
  - GET /api/settings masks cc_civ_key (returns ****+last4)
  - GET /api/settings masks agentmail_api_key
  - POST /api/settings with masked value does NOT overwrite real key
  - POST /api/settings with real new value DOES update
"""

import asyncio
import json
import os
import sys
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PORTAL_DIR)


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_request(method="GET", token="valid-token", body=None):
    req = MagicMock()
    req.method = method
    req.headers = {"authorization": f"Bearer {token}"}
    req.query_params = {}
    if body is not None:
        req.json = AsyncMock(return_value=body)
    return req


class TestSecretMasking:
    """Test that GET /api/settings masks secret fields."""

    def test_masks_cc_civ_key(self):
        from portal_server import api_user_settings
        req = _make_request(method="GET")
        settings = {"cc_civ_key": "my-super-secret-key-abcd"}

        with patch("portal_server.check_auth", return_value=True), \
             patch("portal_server._load_settings", return_value=settings.copy()), \
             patch("portal_server._load_cc_cache", return_value=None):
            resp = run_async(api_user_settings(req))

        data = json.loads(resp.body)
        assert data["cc_civ_key"] == "****abcd"
        assert "my-super-secret-key" not in json.dumps(data)

    def test_masks_agentmail_api_key(self):
        from portal_server import api_user_settings
        req = _make_request(method="GET")
        settings = {"agentmail_api_key": "am-key-xyz-5678"}

        with patch("portal_server.check_auth", return_value=True), \
             patch("portal_server._load_settings", return_value=settings.copy()), \
             patch("portal_server._load_cc_cache", return_value=None):
            resp = run_async(api_user_settings(req))

        data = json.loads(resp.body)
        assert data["agentmail_api_key"] == "****5678"

    def test_masks_short_key(self):
        from portal_server import api_user_settings
        req = _make_request(method="GET")
        settings = {"cc_civ_key": "abc"}

        with patch("portal_server.check_auth", return_value=True), \
             patch("portal_server._load_settings", return_value=settings.copy()), \
             patch("portal_server._load_cc_cache", return_value=None):
            resp = run_async(api_user_settings(req))

        data = json.loads(resp.body)
        assert data["cc_civ_key"] == "****"


class TestMaskedValueProtection:
    """Test POST /api/settings with masked vs real values."""

    def test_masked_value_does_not_overwrite(self):
        from portal_server import api_user_settings
        req = _make_request(method="POST", body={"cc_civ_key": "****abcd"})
        saved = {}
        real_settings = {"cc_civ_key": "my-super-secret-key-abcd"}

        def capture_save(data):
            saved.update(data)

        with patch("portal_server.check_auth", return_value=True), \
             patch("portal_server._load_settings", return_value=real_settings.copy()), \
             patch("portal_server._save_settings", side_effect=capture_save):
            resp = run_async(api_user_settings(req))

        assert resp.status_code == 200
        # The real key should be preserved, not overwritten with the masked value
        assert saved.get("cc_civ_key") == "my-super-secret-key-abcd"

    def test_real_new_value_does_update(self):
        from portal_server import api_user_settings
        req = _make_request(method="POST", body={"cc_civ_key": "brand-new-key-9999"})
        saved = {}
        real_settings = {"cc_civ_key": "old-key-abcd"}

        def capture_save(data):
            saved.update(data)

        with patch("portal_server.check_auth", return_value=True), \
             patch("portal_server._load_settings", return_value=real_settings.copy()), \
             patch("portal_server._save_settings", side_effect=capture_save):
            resp = run_async(api_user_settings(req))

        assert resp.status_code == 200
        assert saved["cc_civ_key"] == "brand-new-key-9999"

    def test_masked_agentmail_key_preserved(self):
        from portal_server import api_user_settings
        req = _make_request(method="POST", body={
            "agentmail_api_key": "****5678",
            "some_other_setting": "new_value"
        })
        saved = {}
        real_settings = {"agentmail_api_key": "real-am-key-5678", "some_other_setting": "old"}

        def capture_save(data):
            saved.update(data)

        with patch("portal_server.check_auth", return_value=True), \
             patch("portal_server._load_settings", return_value=real_settings.copy()), \
             patch("portal_server._save_settings", side_effect=capture_save):
            resp = run_async(api_user_settings(req))

        assert saved["agentmail_api_key"] == "real-am-key-5678"
        assert saved["some_other_setting"] == "new_value"
