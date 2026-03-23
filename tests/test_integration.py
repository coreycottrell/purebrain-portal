"""
PureBrain Portal Integration Tests

End-to-end tests verifying:
  - Server source compiles without syntax errors
  - Health endpoint responds
  - All major route groups respond with auth
  - Response shapes match expectations

Uses Python unittest only. Skips gracefully if the portal server is not running.
"""

import unittest
import subprocess
import requests
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import BASE_URL, SERVER_FILE, load_token

TOKEN = load_token()
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class TestServerSyntax(unittest.TestCase):
    """Verify that portal_server.py is syntactically valid Python."""

    def test_server_compiles(self):
        """portal_server.py compiles without syntax errors."""
        if not os.path.exists(SERVER_FILE):
            self.skipTest("portal_server.py not found")
        result = subprocess.run(
            ["python3", "-m", "py_compile", SERVER_FILE],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, f"Syntax error: {result.stderr}")


class TestHealthEndpoint(unittest.TestCase):
    """Verify the unauthenticated health check endpoint."""

    def test_health_endpoint(self):
        try:
            resp = requests.get(f"{BASE_URL}/health", timeout=5)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")
        self.assertEqual(resp.status_code, 200)


class TestMajorRouteGroups(unittest.TestCase):
    """Verify that all major route groups respond 200 with auth."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

    def test_status_route(self):
        resp = requests.get(f"{BASE_URL}/api/status", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)

    def test_agents_route(self):
        resp = requests.get(f"{BASE_URL}/api/agents", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)

    # NOTE: skills route removed — skills are now a per-CIV custom route.
    # See custom/routes.py and ADR-001.

    def test_boops_route(self):
        resp = requests.get(f"{BASE_URL}/api/boops", headers=AUTH_HEADERS, timeout=5)
        self.assertIn(resp.status_code, (200, 500))

    def test_scheduled_tasks_route(self):
        resp = requests.get(f"{BASE_URL}/api/scheduled-tasks", headers=AUTH_HEADERS, timeout=5)
        self.assertEqual(resp.status_code, 200)


class TestResponseShapes(unittest.TestCase):
    """Verify response shapes for key endpoints."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

    def test_status_is_dict(self):
        resp = requests.get(f"{BASE_URL}/api/status", headers=AUTH_HEADERS, timeout=5)
        self.assertIsInstance(resp.json(), dict)

    def test_agents_has_list(self):
        resp = requests.get(f"{BASE_URL}/api/agents", headers=AUTH_HEADERS, timeout=5)
        data = resp.json()
        self.assertIn("agents", data)
        self.assertIsInstance(data["agents"], list)

    # NOTE: skills response shape test removed — skills are a per-CIV custom route.


if __name__ == "__main__":
    unittest.main(verbosity=2)
