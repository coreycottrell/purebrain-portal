"""
Tests for the Files panel directory listing fix.

The backend API GET /api/download/list (no dir param) returns:
    {"dirs": ["/home/aiciv/exports", "/home/aiciv/to-human", ...]}

The frontend JS loadFiles() must handle d.dirs (flat array of path strings)
instead of the old d.directories (array of objects with {name, path, exists}).

These tests verify the frontend logic by simulating the JS behavior in Python,
ensuring the property name and data shape are handled correctly.
"""

import re
import os
import pytest

# Path to the portal HTML file
PORTAL_HTML = os.path.join(os.path.dirname(__file__), '..', 'portal-pb-styled.html')


def read_portal_html():
    """Read the portal HTML file content."""
    with open(PORTAL_HTML, 'r') as f:
        return f.read()


class TestFilesPanelDirectoryListing:
    """Tests that the frontend handles the backend's d.dirs format correctly."""

    def test_frontend_checks_for_dirs_property(self):
        """Frontend should check for d.dirs, not d.directories."""
        html = read_portal_html()
        # The loadFiles function should reference d.dirs for root directory listing
        # Find the loadFiles function body
        match = re.search(r'function loadFiles\(dir\)\s*\{(.*?)\n  \}', html, re.DOTALL)
        assert match, "loadFiles function not found in portal HTML"
        func_body = match.group(1)

        # Should use d.dirs
        assert 'd.dirs' in func_body, (
            "Frontend loadFiles should check for d.dirs (the backend API property). "
            "Found d.directories instead." if 'd.directories' in func_body
            else "Frontend loadFiles does not reference d.dirs."
        )

        # Should NOT use d.directories
        assert 'd.directories' not in func_body, (
            "Frontend loadFiles should NOT reference d.directories; "
            "backend returns d.dirs (flat array of path strings)."
        )

    def test_frontend_extracts_folder_name_from_path(self):
        """Frontend should extract the folder display name from the last path segment."""
        html = read_portal_html()
        match = re.search(r'function loadFiles\(dir\)\s*\{(.*?)\n  \}', html, re.DOTALL)
        assert match, "loadFiles function not found"
        func_body = match.group(1)

        # The code should split the path to get the folder name
        # Look for a pattern that extracts the last segment of a path string
        # e.g., dd.split('/').pop() or dd.substring(dd.lastIndexOf('/') + 1)
        has_name_extraction = (
            '.split(' in func_body and '.pop()' in func_body
        ) or (
            'lastIndexOf' in func_body
        ) or (
            'replace(' in func_body and '/' in func_body
        )
        assert has_name_extraction, (
            "Frontend should extract folder name from path string "
            "(e.g., '/home/aiciv/exports' -> 'exports'). "
            "No path-splitting logic found in loadFiles."
        )

    def test_frontend_uses_path_string_for_click_handler(self):
        """Clicking a directory should call loadFiles with the full path string."""
        html = read_portal_html()
        match = re.search(r'function loadFiles\(dir\)\s*\{(.*?)\n  \}', html, re.DOTALL)
        assert match, "loadFiles function not found"
        func_body = match.group(1)

        # Within the d.dirs forEach block, there should be a loadFiles(dd) call
        # where dd is the path string from the array (not dd.path from an object)
        # Look for loadFiles(dd) pattern (dd is a string, not an object)
        assert 'loadFiles(dd)' in func_body, (
            "Click handler should call loadFiles(dd) where dd is the path string "
            "from the dirs array. Found loadFiles(dd.path) which assumes object format."
            if 'loadFiles(dd.path)' in func_body
            else "No loadFiles(dd) call found in the dirs forEach block."
        )

    def test_frontend_handles_empty_dirs_array(self):
        """Frontend should handle an empty dirs array gracefully."""
        html = read_portal_html()
        match = re.search(r'function loadFiles\(dir\)\s*\{(.*?)\n  \}', html, re.DOTALL)
        assert match, "loadFiles function not found"
        func_body = match.group(1)

        # Should check d.dirs exists AND handle the case where it's empty
        # Look for a length check or empty message
        has_empty_handling = (
            'length === 0' in func_body or
            'length == 0' in func_body or
            '.length < 1' in func_body or
            'No files' in func_body
        )
        assert has_empty_handling, (
            "Frontend should handle empty dirs array (show a message or handle gracefully)."
        )

    def test_frontend_handles_missing_dirs_key(self):
        """Frontend should not crash when the response has no dirs key."""
        html = read_portal_html()
        match = re.search(r'function loadFiles\(dir\)\s*\{(.*?)\n  \}', html, re.DOTALL)
        assert match, "loadFiles function not found"
        func_body = match.group(1)

        # Should use a conditional check like if (d.dirs) before iterating
        # This ensures no crash when dirs key is missing
        has_guard = (
            'if (d.dirs)' in func_body or
            'if(d.dirs)' in func_body or
            'd.dirs &&' in func_body
        )
        assert has_guard, (
            "Frontend should guard against missing dirs key with a conditional check "
            "(e.g., 'if (d.dirs)' or 'd.dirs &&') before iterating."
        )


class TestFilesPanelMobileLoading:
    """Tests that the Files panel loads correctly on mobile (via switchPanel).

    Bug: On iPhone Safari portrait, navigating to Files via hamburger menu
    calls selectMobileMenuItem('files') -> switchPanel('files'), but switchPanel
    had no handler for 'files'. This meant loadFiles() was never called on mobile.

    Fix: Added `if (panel === 'files') loadFiles(null);` to switchPanel().
    """

    def test_switchpanel_calls_loadfiles(self):
        """switchPanel function must include a loadFiles call for 'files' panel."""
        html = read_portal_html()
        # Find the switchPanel function
        match = re.search(r'function switchPanel\(panel\)\s*\{(.*?)\n  \}', html, re.DOTALL)
        assert match, "switchPanel function not found in portal HTML"
        func_body = match.group(1)

        # Must contain a files panel handler that calls loadFiles
        assert "panel === 'files'" in func_body or 'panel === "files"' in func_body, (
            "switchPanel must check for 'files' panel to trigger loading. "
            "Without this, mobile hamburger menu navigation to Files never loads content."
        )
        assert 'loadFiles' in func_body, (
            "switchPanel must call loadFiles() when files panel is activated. "
            "This is required for mobile/portrait mode where hamburger menu is the only path."
        )

    def test_mobile_menu_item_exists_for_files(self):
        """The mobile hamburger menu must have an entry for files panel."""
        html = read_portal_html()
        assert "selectMobileMenuItem('files')" in html, (
            "Mobile hamburger menu must have a files entry that calls selectMobileMenuItem('files')"
        )

    def test_no_duplicate_loadfiles_listeners(self):
        """The old workaround listener for .nav-item/.tab-item click should be removed.

        switchPanel now handles file loading directly, so the separate event listener
        that attached loadFiles to .nav-item and .tab-item clicks is redundant.
        """
        html = read_portal_html()
        # The old pattern attached a click handler checking data-panel === 'files'
        # on .nav-item and .tab-item elements. This should no longer exist.
        old_pattern = re.search(
            r"querySelectorAll\('\.nav-item,\s*\.tab-item'\).*?data-panel.*?files.*?loadFiles",
            html, re.DOTALL
        )
        assert not old_pattern, (
            "Found old workaround: event listener on .nav-item/.tab-item for files panel. "
            "This is redundant now that switchPanel handles loadFiles directly. Remove it."
        )


class TestNameExtractionLogic:
    """Tests for the path-to-name extraction logic used in the frontend.

    These tests verify the Python equivalent of the JS logic that should
    exist in the frontend for extracting folder names from full paths.
    """

    @staticmethod
    def extract_name_from_path(path):
        """Python equivalent of the JS name extraction logic.
        Mirrors: path.split('/').pop() || path
        """
        parts = path.rstrip('/').split('/')
        return parts[-1] if parts else path

    def test_extract_simple_path(self):
        assert self.extract_name_from_path('/home/aiciv/exports') == 'exports'

    def test_extract_nested_path(self):
        assert self.extract_name_from_path('/home/aiciv/to-human') == 'to-human'

    def test_extract_deep_path(self):
        assert self.extract_name_from_path('/home/aiciv/user-civs/witness-corey') == 'witness-corey'

    def test_extract_trailing_slash(self):
        assert self.extract_name_from_path('/home/aiciv/exports/') == 'exports'

    def test_extract_root(self):
        assert self.extract_name_from_path('/') == ''


class TestBackendResponseShape:
    """Tests verifying the expected backend response shape.

    These document what the backend actually returns so the frontend
    fix is grounded in the real API contract.
    """

    def test_root_response_has_dirs_key(self):
        """Backend root response uses 'dirs' key, not 'directories'."""
        # Simulated backend response for GET /api/download/list (no dir param)
        response = {"dirs": ["/home/aiciv/exports", "/home/aiciv/to-human"]}

        assert 'dirs' in response, "Backend response must have 'dirs' key"
        assert 'directories' not in response, "Backend does NOT use 'directories' key"

    def test_dirs_contains_flat_strings(self):
        """Backend dirs array contains flat path strings, not objects."""
        response = {"dirs": ["/home/aiciv/exports", "/home/aiciv/to-human"]}

        for item in response['dirs']:
            assert isinstance(item, str), (
                f"Each item in dirs should be a string path, got {type(item)}: {item}"
            )

    def test_dirs_can_be_empty(self):
        """Backend may return empty dirs array."""
        response = {"dirs": []}
        assert response['dirs'] == []
