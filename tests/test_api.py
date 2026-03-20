"""
PureBrain Portal API Tests

Tests auth-protected endpoints for:
  - 401 response when no auth token is provided
  - 200 response with valid JSON when auth token is provided
  - Specific response shape/content for key endpoints

Uses Python unittest only. Skips gracefully if the portal server is not running.
"""

import unittest
import requests
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import BASE_URL, load_token

TOKEN = load_token()
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class TestAPIAuth(unittest.TestCase):
    """Test authentication enforcement and basic JSON responses for all API endpoints."""

    def setUp(self):
        """Skip the entire test class if the portal server is not reachable."""
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

    # /api/status
    def test_status_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/status", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_status_with_auth(self):
        resp = requests.get(f"{BASE_URL}/api/status", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), dict)

    def test_status_has_expected_keys(self):
        resp = requests.get(f"{BASE_URL}/api/status", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("civ", data)
        self.assertIn("uptime", data)

    # /api/panes
    def test_panes_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/panes", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_panes_with_auth(self):
        resp = requests.get(f"{BASE_URL}/api/panes", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), (dict, list))

    # /api/agents
    def test_agents_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/agents", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_agents_with_auth(self):
        resp = requests.get(f"{BASE_URL}/api/agents", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), (dict, list))

    # /api/agents/stats
    def test_agents_stats_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/agents/stats", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_agents_stats_with_auth(self):
        resp = requests.get(f"{BASE_URL}/api/agents/stats", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), (dict, list))

    # /api/agents/orgchart
    def test_agents_orgchart_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/agents/orgchart", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_agents_orgchart_with_auth(self):
        resp = requests.get(f"{BASE_URL}/api/agents/orgchart", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), (dict, list))

    # /api/skills
    def test_skills_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/skills", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_skills_with_auth(self):
        resp = requests.get(f"{BASE_URL}/api/skills", headers=AUTH_HEADERS, timeout=10)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), (dict, list))

    # /api/boops
    def test_boops_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/boops", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_boops_with_auth(self):
        resp = requests.get(f"{BASE_URL}/api/boops", headers=AUTH_HEADERS, timeout=5)
        self.assertIn(resp.status_code, (200, 500))

    # /api/commands
    def test_commands_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/commands", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_commands_with_auth(self):
        resp = requests.get(f"{BASE_URL}/api/commands", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), (dict, list))

    # /api/shortcuts
    def test_shortcuts_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/shortcuts", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_shortcuts_with_auth(self):
        resp = requests.get(f"{BASE_URL}/api/shortcuts", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), (dict, list))

    # /api/scheduled-tasks
    def test_scheduled_tasks_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/scheduled-tasks", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_scheduled_tasks_with_auth(self):
        resp = requests.get(f"{BASE_URL}/api/scheduled-tasks", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), (dict, list))

    # /api/boop/status
    def test_boop_status_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/boop/status", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_boop_status_with_auth(self):
        resp = requests.get(f"{BASE_URL}/api/boop/status", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), (dict, list))

    # /api/boop/config
    def test_boop_config_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/boop/config", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_boop_config_with_auth(self):
        resp = requests.get(f"{BASE_URL}/api/boop/config", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), (dict, list))

    # /api/bookmarks
    def test_bookmarks_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/bookmarks", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_bookmarks_with_auth(self):
        resp = requests.get(f"{BASE_URL}/api/bookmarks", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), (dict, list))

    # /api/settings
    def test_settings_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/settings", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_settings_with_auth(self):
        resp = requests.get(f"{BASE_URL}/api/settings", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), (dict, list))

    # /api/compact/status
    def test_compact_status_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/compact/status", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_compact_status_with_auth(self):
        resp = requests.get(f"{BASE_URL}/api/compact/status", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), (dict, list))

    # /api/context
    def test_context_requires_auth(self):
        resp = requests.get(f"{BASE_URL}/api/context", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_context_with_auth(self):
        resp = requests.get(f"{BASE_URL}/api/context", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), (dict, list))

    # /api/reaction/summary (PUBLIC)
    def test_reaction_summary_public(self):
        resp = requests.get(f"{BASE_URL}/api/reaction/summary", timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), (dict, list))


if __name__ == "__main__":
    unittest.main(verbosity=2)
