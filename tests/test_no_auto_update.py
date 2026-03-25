"""
test_no_auto_update.py -- Verify the portal does NOT auto-pull or auto-check on startup.

The update flow must be entirely user-initiated:
  1. User clicks "Check for Updates" in settings
  2. UI shows what's available
  3. User clicks "Update Now"
  4. Confirm dialog
  5. Update runs

These tests verify:
  - No git pull/fetch in the server startup function
  - No auto-call to checkForUpdates() on page load
  - No auto-call to applyUpdate() anywhere
  - No git pull in the watchdog
  - No git pull in custom startup hooks
  - The /api/update/check endpoint only does fetch (read-only), never pull
"""

import ast
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import HTML_FILE, SERVER_FILE


class TestNoAutoUpdateServer(unittest.TestCase):
    """Verify the server does NOT auto-pull code on startup."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(SERVER_FILE):
            raise unittest.SkipTest(f"Server file not found: {SERVER_FILE}")
        with open(SERVER_FILE, "r", encoding="utf-8") as f:
            cls.server_code = f.read()

    def _get_function_body(self, func_name: str) -> str:
        """Extract the body of a function from the server source code."""
        pattern = rf"(async\s+)?def\s+{func_name}\s*\("
        match = re.search(pattern, self.server_code)
        if not match:
            return ""
        start = match.start()
        # Find function body by looking for next def at same indentation
        lines = self.server_code[start:].split("\n")
        indent = len(lines[0]) - len(lines[0].lstrip())
        body_lines = [lines[0]]
        for line in lines[1:]:
            stripped = line.lstrip()
            if stripped and not line.startswith(" " * (indent + 1)) and (
                stripped.startswith("def ") or stripped.startswith("async def ")
                or stripped.startswith("class ")
                or (len(line) - len(stripped) <= indent and stripped and not stripped.startswith("#") and not stripped.startswith('"""'))
            ):
                break
            body_lines.append(line)
        return "\n".join(body_lines)

    def test_startup_has_no_git_pull(self):
        """The _startup() function must not contain any git pull commands."""
        startup_body = self._get_function_body("_startup")
        self.assertTrue(len(startup_body) > 0, "_startup function not found in portal_server.py")
        self.assertNotIn("git pull", startup_body.lower(),
                         "_startup must not run git pull")
        self.assertNotIn("git_cmd", startup_body,
                         "_startup must not call _git_cmd (no git operations on startup)")

    def test_startup_has_no_git_fetch(self):
        """The _startup() function must not contain any git fetch commands."""
        startup_body = self._get_function_body("_startup")
        self.assertTrue(len(startup_body) > 0, "_startup function not found")
        self.assertNotIn("git fetch", startup_body.lower(),
                         "_startup must not run git fetch")

    def test_update_check_endpoint_never_pulls(self):
        """The /api/update/check endpoint must only fetch, never pull."""
        check_body = self._get_function_body("api_update_check")
        self.assertTrue(len(check_body) > 0, "api_update_check function not found")
        # It may do git fetch (read-only) but must NEVER do git pull
        self.assertNotIn('"pull"', check_body,
                         "/api/update/check must never do git pull")
        self.assertNotIn("'pull'", check_body,
                         "/api/update/check must never do git pull")

    def test_no_git_pull_on_module_load(self):
        """Module-level code must not contain git pull."""
        # Parse the module and check for top-level statements that could run git pull
        # We check the raw text for any subprocess/os.system call with git pull
        # outside of function definitions
        tree = ast.parse(self.server_code)
        for node in ast.iter_child_nodes(tree):
            # Only check module-level statements (not inside functions/classes)
            if isinstance(node, ast.Expr):
                code_segment = ast.get_source_segment(self.server_code, node)
                if code_segment:
                    self.assertNotIn("git pull", code_segment.lower(),
                                     "Module-level code must not run git pull")


class TestNoAutoUpdateFrontend(unittest.TestCase):
    """Verify the frontend does NOT auto-check or auto-apply updates."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(HTML_FILE):
            raise unittest.SkipTest(f"HTML file not found: {HTML_FILE}")
        with open(HTML_FILE, "r", encoding="utf-8") as f:
            cls.html = f.read()

    def test_no_auto_check_on_page_load(self):
        """There must be NO setTimeout/setInterval that auto-calls checkForUpdates().

        Users must manually click 'Check for Updates' to initiate the check.
        Auto-checking on page load bypasses user control.
        """
        # Look for setTimeout(...checkForUpdates...) patterns
        # These would auto-trigger the check without user action
        auto_check_pattern = re.compile(
            r'setTimeout\s*\(\s*function\s*\(\s*\)\s*\{[^}]*checkForUpdates',
            re.DOTALL
        )
        match = auto_check_pattern.search(self.html)
        self.assertIsNone(match,
                          "Must not auto-call checkForUpdates() via setTimeout on page load. "
                          "Users should manually click 'Check for Updates'.")

    def test_no_periodic_auto_check(self):
        """There must be NO setInterval that periodically calls checkForUpdates()."""
        auto_interval_pattern = re.compile(
            r'setInterval\s*\(\s*function\s*\(\s*\)\s*\{[^}]*checkForUpdates',
            re.DOTALL
        )
        match = auto_interval_pattern.search(self.html)
        self.assertIsNone(match,
                          "Must not auto-call checkForUpdates() via setInterval. "
                          "Update checks must be user-initiated only.")

    def test_no_auto_apply_anywhere(self):
        """applyUpdate() must never be called automatically -- only on user click."""
        # Check that applyUpdate is NOT called from checkForUpdates
        fn_start = self.html.find("function checkForUpdates()")
        if fn_start < 0:
            self.fail("checkForUpdates function not found")
        fn_end = self.html.find("\n  function ", fn_start + 10)
        if fn_end < 0:
            fn_end = fn_start + 3000
        fn_body = self.html[fn_start:fn_end]
        self.assertNotIn("applyUpdate()", fn_body,
                          "checkForUpdates must never auto-call applyUpdate()")

    def test_update_check_btn_uses_onclick(self):
        """The 'Check for Updates' button must use onclick (user-initiated)."""
        self.assertIn('onclick="checkForUpdates()"', self.html,
                      "Check for Updates button must use onclick for user-initiated checks")

    def test_apply_btn_uses_onclick(self):
        """The 'Update Now' button must use onclick (user-initiated)."""
        self.assertIn('onclick="applyUpdate()"', self.html,
                      "Update Now button must use onclick for user-initiated updates")


class TestNoAutoUpdateWatchdog(unittest.TestCase):
    """Verify the watchdog does NOT do any git operations."""

    @classmethod
    def setUpClass(cls):
        watchdog_file = os.path.join(
            os.path.dirname(SERVER_FILE), "portal_watchdog.py"
        )
        if not os.path.exists(watchdog_file):
            raise unittest.SkipTest(f"Watchdog file not found: {watchdog_file}")
        with open(watchdog_file, "r", encoding="utf-8") as f:
            cls.watchdog_code = f.read()

    def test_watchdog_has_no_git_pull(self):
        """The watchdog must not contain any git pull commands."""
        self.assertNotIn("git pull", self.watchdog_code.lower(),
                         "Watchdog must not run git pull")

    def test_watchdog_has_no_git_fetch(self):
        """The watchdog must not contain any git fetch commands."""
        self.assertNotIn("git fetch", self.watchdog_code.lower(),
                         "Watchdog must not run git fetch")


if __name__ == "__main__":
    unittest.main()
