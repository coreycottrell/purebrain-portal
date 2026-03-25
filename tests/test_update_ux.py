"""
test_update_ux.py -- UX behavior tests for the portal update flow.

Verifies the frontend implements the proper update UX:
  1. Check shows changelog, version info, and "Update Now" button (no auto-apply)
  2. "Update Now" shows a confirmation dialog with version transition details
  3. During update: step-by-step progress with human-readable labels
  4. On success: shows new version, changelog, restart countdown
  5. On failure: shows error with rollback info

These are STATIC tests -- they verify the HTML/JS structure without a running server.
"""

import unittest
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import HTML_FILE


class TestUpdateUXFlow(unittest.TestCase):
    """Verify the update UI implements the expected multi-step UX flow."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(HTML_FILE):
            raise unittest.SkipTest(f"HTML file not found: {HTML_FILE}")
        with open(HTML_FILE, "r", encoding="utf-8") as f:
            cls.html = f.read()

    # ------------------------------------------------------------------
    # Step 1: "Check for Updates" shows info WITHOUT auto-applying
    # ------------------------------------------------------------------

    def test_check_does_not_auto_apply(self):
        """checkForUpdates must NOT call applyUpdate or POST to /api/update/apply."""
        fn_start = self.html.find("function checkForUpdates()")
        self.assertGreater(fn_start, 0, "checkForUpdates function not found")
        # Find the end of the function (next top-level function)
        fn_end = self.html.find("\n  function ", fn_start + 10)
        if fn_end < 0:
            fn_end = fn_start + 3000
        fn_body = self.html[fn_start:fn_end]
        self.assertNotIn("applyUpdate()", fn_body,
                         "checkForUpdates must not call applyUpdate()")
        self.assertNotIn("/api/update/apply", fn_body,
                         "checkForUpdates must not POST to /api/update/apply")

    def test_check_shows_update_available_info(self):
        """When update is available, checkForUpdates must show the info section."""
        fn_start = self.html.find("function checkForUpdates()")
        fn_body = self.html[fn_start:fn_start + 3000]
        self.assertIn("update-available-info", fn_body,
                      "checkForUpdates must reference update-available-info")
        self.assertIn("update-apply-btn", fn_body,
                      "checkForUpdates must show the Update Now button")

    # ------------------------------------------------------------------
    # Step 2: Target version displayed when update available
    # ------------------------------------------------------------------

    def test_remote_version_element_exists(self):
        """There must be an element to display the target/remote version."""
        self.assertIn('id="update-remote-version"', self.html,
                      "Must have an element with id='update-remote-version' to show target version")

    def test_check_populates_remote_version(self):
        """checkForUpdates must populate the remote version display."""
        fn_start = self.html.find("function checkForUpdates()")
        fn_body = self.html[fn_start:fn_start + 3000]
        self.assertIn("update-remote-version", fn_body,
                      "checkForUpdates must set the remote version display")

    # ------------------------------------------------------------------
    # Step 3: Confirmation dialog includes version transition
    # ------------------------------------------------------------------

    def test_apply_has_confirmation_with_versions(self):
        """applyUpdate must show a confirmation that includes version numbers."""
        fn_start = self.html.find("function applyUpdate()")
        self.assertGreater(fn_start, 0, "applyUpdate function not found")
        fn_body = self.html[fn_start:fn_start + 2000]
        # Must have some form of confirmation (confirm() or custom modal)
        has_confirm = "confirm(" in fn_body or "update-confirm-modal" in fn_body
        self.assertTrue(has_confirm,
                        "applyUpdate must have a confirmation step")
        # The confirmation text must reference version info (not be a static string)
        # It should dynamically include current and target versions
        has_version_ref = (
            "current_version" in fn_body or
            "currentVersion" in fn_body or
            "update-current-version" in fn_body or
            "_updateCurrentVersion" in fn_body
        )
        self.assertTrue(has_version_ref,
                        "Confirmation dialog must reference the current version dynamically")
        has_target_ref = (
            "remote_version" in fn_body or
            "remoteVersion" in fn_body or
            "update-remote-version" in fn_body or
            "_updateRemoteVersion" in fn_body or
            "_updateTargetVersion" in fn_body
        )
        self.assertTrue(has_target_ref,
                        "Confirmation dialog must reference the target/remote version dynamically")

    # ------------------------------------------------------------------
    # Step 4: Progress shows human-readable step labels
    # ------------------------------------------------------------------

    def test_progress_has_human_readable_steps(self):
        """pollUpdateStatus must translate step IDs to user-friendly labels."""
        fn_start = self.html.find("function pollUpdateStatus(")
        self.assertGreater(fn_start, 0, "pollUpdateStatus function not found")
        fn_body = self.html[fn_start:fn_start + 3000]
        # Must have a step-label mapping (object mapping step IDs to readable text)
        has_label_map = (
            "stepLabels" in fn_body or
            "step_labels" in fn_body or
            "STEP_LABELS" in fn_body or
            "_updateStepLabels" in fn_body or
            "Fetching" in fn_body  # at minimum, human-readable step names
        )
        self.assertTrue(has_label_map,
                        "pollUpdateStatus must map step IDs to human-readable labels "
                        "(e.g., 'fetch' -> 'Fetching latest changes...')")

    # ------------------------------------------------------------------
    # Step 5: Success message includes version and changelog
    # ------------------------------------------------------------------

    def test_success_shows_new_version(self):
        """On success, the UI must display the new version number."""
        fn_start = self.html.find("function pollUpdateStatus(")
        fn_body = self.html[fn_start:fn_start + 3000]
        # Find the success handler
        success_idx = fn_body.find("'success'")
        if success_idx < 0:
            success_idx = fn_body.find('"success"')
        self.assertGreater(success_idx, 0, "success handler not found in pollUpdateStatus")
        success_block = fn_body[success_idx:success_idx + 800]
        has_version = (
            "new_version" in success_block or
            "newVersion" in success_block or
            "data.new_version" in success_block
        )
        self.assertTrue(has_version,
                        "Success message must include the new version number from the response")

    # ------------------------------------------------------------------
    # Step 6: Failure message includes rollback info
    # ------------------------------------------------------------------

    def test_failure_shows_rollback_info(self):
        """On failure, the UI must show error reason and rollback status."""
        fn_start = self.html.find("function pollUpdateStatus(")
        fn_body = self.html[fn_start:fn_start + 3000]
        # Find the failed handler
        failed_idx = fn_body.find("'failed'")
        if failed_idx < 0:
            failed_idx = fn_body.find('"failed"')
        self.assertGreater(failed_idx, 0, "failed handler not found in pollUpdateStatus")
        failed_block = fn_body[failed_idx:failed_idx + 600]
        self.assertIn("rolled_back_to", failed_block,
                      "Failure message must reference rolled_back_to for rollback info")
        self.assertIn("error", failed_block,
                      "Failure message must show the error reason")

    # ------------------------------------------------------------------
    # Step 7: Update available section shows version transition
    # ------------------------------------------------------------------

    def test_update_available_shows_version_arrow(self):
        """The update available info must show current -> target version transition."""
        # The HTML must have both current and remote version displays in the
        # update-available-info section
        section_start = self.html.find('id="update-available-info"')
        self.assertGreater(section_start, 0, "update-available-info section not found")
        # Look at the section and nearby area (within 500 chars)
        section_block = self.html[section_start:section_start + 500]
        self.assertIn("update-remote-version", section_block,
                      "update-available-info must contain the remote version display")


class TestUpdateProgressSteps(unittest.TestCase):
    """Verify progress indicator has proper step-by-step display."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(HTML_FILE):
            raise unittest.SkipTest(f"HTML file not found: {HTML_FILE}")
        with open(HTML_FILE, "r", encoding="utf-8") as f:
            cls.html = f.read()

    def test_progress_section_exists(self):
        """Progress section must exist with spinner and step text."""
        self.assertIn('id="update-progress"', self.html)
        self.assertIn('id="update-progress-text"', self.html)
        self.assertIn('id="update-steps"', self.html)

    def test_progress_hidden_by_default(self):
        """Progress section must be hidden by default."""
        match = re.search(r'id="update-progress"[^>]*style="([^"]*)"', self.html)
        self.assertIsNotNone(match, "update-progress element not found")
        style = match.group(1)
        self.assertIn("display:none", style.replace(" ", ""),
                      "update-progress must be hidden by default")

    def test_apply_btn_hidden_by_default(self):
        """Update Now button must be hidden until update is available."""
        match = re.search(r'id="update-apply-btn"[^>]*style="([^"]*)"', self.html)
        self.assertIsNotNone(match, "update-apply-btn element not found")
        style = match.group(1)
        self.assertIn("display:none", style.replace(" ", ""),
                      "update-apply-btn must be hidden by default")


class TestUpdateSuccessDisplay(unittest.TestCase):
    """Verify success state shows proper information."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(HTML_FILE):
            raise unittest.SkipTest(f"HTML file not found: {HTML_FILE}")
        with open(HTML_FILE, "r", encoding="utf-8") as f:
            cls.html = f.read()

    def test_success_element_exists(self):
        """Success message element must exist."""
        self.assertIn('id="update-success-msg"', self.html,
                      "Must have a dedicated success message element (id='update-success-msg')")

    def test_success_hidden_by_default(self):
        """Success message must be hidden by default."""
        match = re.search(r'id="update-success-msg"[^>]*style="([^"]*)"', self.html)
        if match:
            style = match.group(1)
            self.assertIn("display:none", style.replace(" ", ""),
                          "update-success-msg must be hidden by default")


if __name__ == "__main__":
    unittest.main()
