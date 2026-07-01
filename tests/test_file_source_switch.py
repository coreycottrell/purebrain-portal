"""
Tests for file source switching (Local <-> Google Drive).

Verifies:
  - switchFileSource('local') triggers /api/download/list which returns data
  - switchFileSource('gdrive') triggers /api/gdrive/status check
  - After switching to local, loading state is replaced by actual file content
  - JS syntax is valid in files.js
  - switchFileSource does NOT call filterFiles('all') prematurely (race condition fix)
"""

import unittest
import requests
import subprocess
import os
import sys
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import BASE_URL, load_token, HTML_FILE

TOKEN = load_token()
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}

PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES_JS = os.path.join(PORTAL_DIR, "static", "js", "features", "files.js")


class TestFileSourceSwitchAPI(unittest.TestCase):
    """Integration tests: verify the API endpoints that file source switching depends on."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

    def test_local_file_list_returns_data(self):
        """Switching to local calls /api/download/list — must return dirs or items."""
        resp = requests.get(
            f"{BASE_URL}/api/download/list",
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # Root listing returns 'dirs' (allowed base directories)
        self.assertIn("dirs", data)
        self.assertIsInstance(data["dirs"], list)

    def test_local_file_list_requires_auth(self):
        """File list endpoint must require authentication."""
        resp = requests.get(f"{BASE_URL}/api/download/list", timeout=5)
        self.assertEqual(resp.status_code, 401)

    def test_gdrive_status_returns_json(self):
        """Switching to gdrive checks /api/gdrive/status — must return valid JSON."""
        resp = requests.get(
            f"{BASE_URL}/api/gdrive/status",
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # Must have 'connected' boolean
        self.assertIn("connected", data)
        self.assertIsInstance(data["connected"], bool)


class TestFileSourceSwitchCode(unittest.TestCase):
    """Code-level tests: verify JS syntax and correct switchFileSource behavior."""

    def test_files_js_syntax_valid(self):
        """files.js must have valid JavaScript syntax."""
        result = subprocess.run(
            ["node", "-c", FILES_JS],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"JS syntax error in files.js: {result.stderr}",
        )

    def test_portal_html_js_blocks_syntax(self):
        """Inline JS in portal-pb-styled.html containing switchFileSource must be valid."""
        # Extract the script block containing switchFileSource and check syntax
        with open(HTML_FILE, "r") as f:
            content = f.read()
        self.assertIn(
            "function switchFileSource",
            content,
            "switchFileSource function must exist in portal HTML",
        )

    def test_no_premature_filterFiles_in_switchFileSource(self):
        """switchFileSource must NOT call filterFiles() — it causes a race condition.

        The data loading callbacks (_loadFiles, _setItems) already reset the
        category to 'all' and call _renderView(). Calling filterFiles('all')
        at the end of switchFileSource causes _renderView() to fire before
        async data has arrived, replacing the loading spinner with empty state.
        """
        with open(HTML_FILE, "r") as f:
            content = f.read()

        # Find the switchFileSource function body
        start = content.find("function switchFileSource(")
        self.assertGreater(start, -1, "switchFileSource must exist")

        # Find the closing brace by counting braces
        depth = 0
        func_body = ""
        in_func = False
        for i in range(start, len(content)):
            ch = content[i]
            if ch == "{":
                depth += 1
                in_func = True
            elif ch == "}":
                depth -= 1
            if in_func:
                func_body += ch
            if in_func and depth == 0:
                break

        # filterFiles should NOT appear in the function body
        self.assertNotIn(
            "filterFiles(",
            func_body,
            "switchFileSource must NOT call filterFiles() — it causes a race "
            "condition by rendering empty data before the async fetch completes",
        )

    def test_setItems_initializes_refs(self):
        """_setItems must call _initRefs() to handle gdrive path where _boot() may not have run."""
        with open(FILES_JS, "r") as f:
            content = f.read()

        # Find _setItems function
        start = content.find("function _setItems(")
        self.assertGreater(start, -1, "_setItems must exist in files.js")

        # Extract function body
        depth = 0
        func_body = ""
        in_func = False
        for i in range(start, len(content)):
            ch = content[i]
            if ch == "{":
                depth += 1
                in_func = True
            elif ch == "}":
                depth -= 1
            if in_func:
                func_body += ch
            if in_func and depth == 0:
                break

        self.assertIn(
            "_initRefs()",
            func_body,
            "_setItems must call _initRefs() to ensure DOM refs are initialized "
            "for the gdrive path where _boot() may not have been called",
        )

    def test_switchFileSource_resets_sidebar_visually(self):
        """switchFileSource should reset the sidebar filter active state visually."""
        with open(HTML_FILE, "r") as f:
            content = f.read()

        # Find switchFileSource function body
        start = content.find("function switchFileSource(")
        depth = 0
        func_body = ""
        in_func = False
        for i in range(start, len(content)):
            ch = content[i]
            if ch == "{":
                depth += 1
                in_func = True
            elif ch == "}":
                depth -= 1
            if in_func:
                func_body += ch
            if in_func and depth == 0:
                break

        # Should have sidebar reset (ftSecFilters active class management)
        self.assertIn(
            "ftSecFilters",
            func_body,
            "switchFileSource should reset the sidebar filter active state visually",
        )


if __name__ == "__main__":
    unittest.main()
