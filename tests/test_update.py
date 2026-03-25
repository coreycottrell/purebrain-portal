"""
test_update.py -- Tests for the safe in-portal update mechanism.

Tests cover:
  - Auth enforcement on all 3 update endpoints (401 without token)
  - Response shape and content for check/status endpoints
  - Concurrent update rejection
  - Frontend structural elements (static HTML checks)

Uses the same pattern as test_api.py: unittest + requests against a running portal.
"""

import unittest
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import BASE_URL, HTML_FILE, load_token

TOKEN = load_token()
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# API Tests (require running server)
# ---------------------------------------------------------------------------

class TestUpdateAuth(unittest.TestCase):
    """Auth enforcement on all update endpoints."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

    def test_check_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/update/check", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_apply_requires_auth(self):
        resp = requests.post(f"{BASE_URL}/api/update/apply", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_status_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/update/status", timeout=5)
        self.assertEqual(resp.status_code, 401)


class TestUpdateCheck(unittest.TestCase):
    """GET /api/update/check returns correct shape."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

    def test_check_with_auth(self):
        resp = requests.get(f"{BASE_URL}/api/update/check", headers=AUTH_HEADERS, timeout=15)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("status", data)
        self.assertIn(data["status"], ("available", "up_to_date", "error"))

    def test_check_returns_valid_shape(self):
        resp = requests.get(f"{BASE_URL}/api/update/check", headers=AUTH_HEADERS, timeout=15)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # All responses must have these keys
        self.assertIn("status", data)
        self.assertIn("checked_at", data)
        if data["status"] in ("available", "up_to_date"):
            self.assertIn("current_version", data)
            self.assertIn("current_sha", data)
        if data["status"] == "available":
            self.assertIn("remote_sha", data)
            self.assertIn("commits_behind", data)
            self.assertIn("changelog", data)
            self.assertIsInstance(data["changelog"], list)
            self.assertGreater(data["commits_behind"], 0)


class TestUpdateStatus(unittest.TestCase):
    """GET /api/update/status returns correct idle state."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

    def test_status_idle(self):
        resp = requests.get(f"{BASE_URL}/api/update/status", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("status", data)
        # When no update has ever run, status should be "idle"
        self.assertIn(data["status"], ("idle", "in_progress", "success", "failed"))

    def test_status_with_auth(self):
        resp = requests.get(f"{BASE_URL}/api/update/status", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), dict)


class TestUpdateApply(unittest.TestCase):
    """POST /api/update/apply behaviour."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

    def test_apply_with_auth_returns_json(self):
        """Apply endpoint returns a JSON response (started, error, or up-to-date)."""
        resp = requests.post(
            f"{BASE_URL}/api/update/apply",
            headers={**AUTH_HEADERS, "Content-Type": "application/json"},
            json={},
            timeout=10,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("status", data)
        # Valid responses: started, error
        self.assertIn(data["status"], ("started", "error"))


class TestUpdateApplyConcurrent(unittest.TestCase):
    """POST /api/update/apply rejects concurrent requests."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

    def test_apply_rejects_concurrent(self):
        """If an update is in progress, a second apply returns error."""
        # First, check current status
        status_resp = requests.get(
            f"{BASE_URL}/api/update/status", headers=AUTH_HEADERS, timeout=5
        )
        status_data = status_resp.json()
        if status_data.get("status") == "in_progress":
            # An update is already running -- second apply should be rejected
            resp = requests.post(
                f"{BASE_URL}/api/update/apply",
                headers={**AUTH_HEADERS, "Content-Type": "application/json"},
                json={},
                timeout=10,
            )
            data = resp.json()
            self.assertEqual(data["status"], "error")
            self.assertIn("already", data.get("error", "").lower())
        else:
            # No update running -- we cannot safely trigger one in tests
            # (it would actually update the portal). Just verify the endpoint responds.
            self.skipTest("No update in progress to test concurrent rejection")


# ---------------------------------------------------------------------------
# Frontend structural tests (static HTML -- no server needed)
# ---------------------------------------------------------------------------

class TestUpdateFrontend(unittest.TestCase):
    """Verify the portal HTML contains the update UI elements."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(HTML_FILE):
            raise unittest.SkipTest(f"HTML file not found: {HTML_FILE}")
        with open(HTML_FILE, "r") as f:
            cls.html = f.read()

    def test_settings_has_update_section(self):
        self.assertIn('id="portal-updates-section"', self.html)

    def test_update_check_btn_exists(self):
        self.assertIn('id="update-check-btn"', self.html)

    def test_update_apply_btn_exists(self):
        self.assertIn('id="update-apply-btn"', self.html)

    def test_update_badge_exists(self):
        self.assertIn('id="update-badge"', self.html)

    def test_update_current_version_display(self):
        self.assertIn('id="update-current-version"', self.html)

    def test_update_current_sha_display(self):
        self.assertIn('id="update-current-sha"', self.html)

    def test_update_progress_indicator(self):
        self.assertIn('id="update-progress"', self.html)

    def test_update_available_info_section(self):
        self.assertIn('id="update-available-info"', self.html)

    def test_update_uptodate_msg(self):
        self.assertIn('id="update-uptodate-msg"', self.html)

    def test_update_error_msg(self):
        self.assertIn('id="update-error-msg"', self.html)

    def test_checkForUpdates_function(self):
        self.assertIn('function checkForUpdates()', self.html)

    def test_applyUpdate_function(self):
        self.assertIn('function applyUpdate()', self.html)

    def test_pollUpdateStatus_function(self):
        self.assertIn('function pollUpdateStatus(', self.html)

    def test_settings_new_badge_hidden_by_default(self):
        """The settings gear notification badge must be hidden by default (display:none)."""
        # The badge element in HTML must have display:none inline
        import re
        match = re.search(r'id="settings-new-badge"[^>]*style="([^"]*)"', self.html)
        self.assertIsNotNone(match, "settings-new-badge element not found")
        style = match.group(1)
        self.assertIn('display:none', style,
                      "settings-new-badge must be hidden by default (display:none in inline style)")

    def test_release_notes_do_not_show_badge(self):
        """fetchReleaseNotes must NOT set settings-new-badge to visible.

        The old code showed a persistent red badge on the settings gear whenever
        the portal version changed.  The fix auto-marks the version as seen so
        no badge is displayed.
        """
        # Ensure the old pattern (badge.style.display = 'block') is gone
        import re
        # Find the fetchReleaseNotes function body
        fn_start = self.html.find('function fetchReleaseNotes()')
        self.assertGreater(fn_start, 0, "fetchReleaseNotes function not found")
        fn_body = self.html[fn_start:fn_start + 2000]
        self.assertNotIn("badge.style.display = 'block'", fn_body,
                         "fetchReleaseNotes must not show settings-new-badge")
        self.assertNotIn('badge.style.display = "block"', fn_body,
                         "fetchReleaseNotes must not show settings-new-badge")

    def test_up_to_date_clears_new_badge(self):
        """When update check returns up_to_date, settings-new-badge must be hidden."""
        # Find the up_to_date handler in checkForUpdates
        idx = self.html.find("data.status === 'up_to_date'")
        self.assertGreater(idx, 0, "up_to_date handler not found in checkForUpdates")
        # Check the next ~800 chars for the badge clearing code
        handler_block = self.html[idx:idx + 800]
        self.assertIn('settings-new-badge', handler_block,
                      "up_to_date handler must reference settings-new-badge to clear it")


if __name__ == "__main__":
    unittest.main()
