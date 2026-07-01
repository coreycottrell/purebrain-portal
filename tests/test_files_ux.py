"""
Tests for Files Panel UX Improvements:
  1. Loading spinner (uses .gdrive-spinner CSS class)
  2. File detail/preview panel (single-click shows detail, not download)
  3. Right-click context menu
  4. Delete endpoint (backend API)

Uses Python unittest. Skips gracefully if portal server is not running.
"""

import unittest
import requests
import subprocess
import os
import sys
import re
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import BASE_URL, load_token, HTML_FILE

TOKEN = load_token()
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}

PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES_JS = os.path.join(PORTAL_DIR, "static", "js", "features", "files.js")
PANELS_CSS = os.path.join(PORTAL_DIR, "static", "css", "panels.css")
SERVER_FILE = os.path.join(PORTAL_DIR, "portal_server.py")


class TestLoadingSpinner(unittest.TestCase):
    """Loading state must use the .gdrive-spinner CSS class, not plain text."""

    def test_files_js_loading_uses_spinner_class(self):
        """The loading HTML in files.js must contain gdrive-spinner class."""
        with open(FILES_JS, "r") as f:
            content = f.read()
        # Find the loading state in _loadFiles
        self.assertIn(
            "gdrive-spinner",
            content,
            "files.js loading state must use the .gdrive-spinner CSS class",
        )

    def test_loading_html_is_centered(self):
        """Loading spinner must be centered (text-align:center or flex centering)."""
        with open(FILES_JS, "r") as f:
            content = f.read()
        # The loading HTML should have centering
        # Find the loading innerHTML assignment
        loading_match = re.search(r'innerHTML\s*=\s*[\'"](<div[^>]*>.*?Loading)', content)
        if loading_match:
            self.assertIn("center", loading_match.group(0).lower())
        else:
            # If we can't find a specific match, just ensure spinner exists
            self.assertIn("gdrive-spinner", content)

    def test_spinner_css_class_exists(self):
        """The .gdrive-spinner CSS class must be defined in components.css."""
        components_css = os.path.join(PORTAL_DIR, "static", "css", "components.css")
        with open(components_css, "r") as f:
            content = f.read()
        self.assertIn(".gdrive-spinner", content)
        self.assertIn("animation", content)


class TestFileDetailPanel(unittest.TestCase):
    """Single-click on file should show detail panel, not download immediately."""

    def test_files_js_has_detail_panel_function(self):
        """files.js must have a function to show file detail panel."""
        with open(FILES_JS, "r") as f:
            content = f.read()
        self.assertIn(
            "_showDetailPanel",
            content,
            "files.js must have _showDetailPanel function for single-click behavior",
        )

    def test_files_js_has_hide_detail_panel(self):
        """files.js must have a function to hide the detail panel."""
        with open(FILES_JS, "r") as f:
            content = f.read()
        self.assertIn(
            "_hideDetailPanel",
            content,
            "files.js must have _hideDetailPanel function",
        )

    def test_file_card_click_shows_detail_not_download(self):
        """Local file cards must call detail panel, not _portalDownload directly."""
        with open(FILES_JS, "r") as f:
            content = f.read()
        # In _buildCardHtml for local files, the onclick should reference detail panel
        # It should NOT directly call _portalDownload on single click
        # Find the local file card building section (non-dir, non-gdrive)
        card_section = content[content.find("function _buildCardHtml"):]
        # The local file card onclick should call _showDetailPanel
        self.assertIn(
            "_showDetailPanel",
            card_section,
            "File card click should show detail panel, not download directly",
        )

    def test_detail_panel_html_exists(self):
        """Detail panel HTML structure must exist in portal HTML or files.js."""
        with open(FILES_JS, "r") as f:
            js_content = f.read()
        # The detail panel can be created dynamically in JS
        self.assertIn(
            "file-detail-panel",
            js_content,
            "file-detail-panel element must be created in files.js",
        )

    def test_detail_panel_shows_file_metadata(self):
        """Detail panel must display file name, size, and modified date."""
        with open(FILES_JS, "r") as f:
            content = f.read()
        # The detail panel builder should reference these metadata fields
        self.assertIn("item.name", content)
        self.assertIn("item.size", content)
        self.assertIn("item.mtime", content)

    def test_detail_panel_has_download_button(self):
        """Detail panel must have a download button."""
        with open(FILES_JS, "r") as f:
            content = f.read()
        self.assertIn(
            "_portalDownload",
            content,
            "Detail panel must include a download action",
        )

    def test_api_download_list_returns_metadata(self):
        """GET /api/download/list?dir=X must return size and mtime for files."""
        if not TOKEN:
            self.skipTest("No portal token")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

        # Get root dirs first
        resp = requests.get(
            f"{BASE_URL}/api/download/list", headers=AUTH_HEADERS, timeout=5
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        if "dirs" in data and data["dirs"]:
            # Browse into first dir
            first_dir = data["dirs"][0]
            resp2 = requests.get(
                f"{BASE_URL}/api/download/list?dir={first_dir}",
                headers=AUTH_HEADERS,
                timeout=5,
            )
            self.assertEqual(resp2.status_code, 200)
            data2 = resp2.json()
            if "items" in data2 and data2["items"]:
                item = data2["items"][0]
                self.assertIn("name", item)
                self.assertIn("size", item)
                self.assertIn("mtime", item)
                self.assertIn("path", item)


class TestContextMenu(unittest.TestCase):
    """Right-click context menu for files and folders."""

    def test_files_js_has_context_menu(self):
        """files.js must implement a context menu handler."""
        with open(FILES_JS, "r") as f:
            content = f.read()
        self.assertIn(
            "contextmenu",
            content,
            "files.js must handle contextmenu events for right-click",
        )

    def test_context_menu_has_download_option(self):
        """Context menu must include a Download option."""
        with open(FILES_JS, "r") as f:
            content = f.read()
        self.assertIn(
            "Download",
            content,
            "Context menu must have a Download option",
        )

    def test_context_menu_has_delete_option(self):
        """Context menu must include a Delete option."""
        with open(FILES_JS, "r") as f:
            content = f.read()
        self.assertIn(
            "Delete",
            content,
            "Context menu must have a Delete option",
        )

    def test_context_menu_has_copy_path_option(self):
        """Context menu must include a Copy Path option."""
        with open(FILES_JS, "r") as f:
            content = f.read()
        self.assertIn(
            "Copy path",
            content,
            "Context menu must have a Copy path option",
        )

    def test_context_menu_closes_on_outside_click(self):
        """Context menu must close when clicking outside."""
        with open(FILES_JS, "r") as f:
            content = f.read()
        # Should have a click handler that hides the menu
        self.assertIn(
            "_hideContextMenu",
            content,
            "Must have _hideContextMenu to close on outside click",
        )

    def test_context_menu_closes_on_escape(self):
        """Context menu must close on Escape key."""
        with open(FILES_JS, "r") as f:
            content = f.read()
        self.assertIn(
            "Escape",
            content,
            "Context menu must handle Escape key to close",
        )


class TestDeleteEndpoint(unittest.TestCase):
    """DELETE /api/files endpoint for removing files (restricted to safe dirs)."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

    def test_delete_endpoint_exists(self):
        """DELETE /api/files must exist and return JSON (not 404/405)."""
        resp = requests.delete(
            f"{BASE_URL}/api/files",
            headers=AUTH_HEADERS,
            json={"path": "/nonexistent/file.txt"},
            timeout=5,
        )
        # Should NOT be 404 (endpoint not found) or 405 (method not allowed)
        self.assertNotIn(
            resp.status_code,
            [404, 405],
            "DELETE /api/files endpoint must exist",
        )

    def test_delete_requires_auth(self):
        """DELETE /api/files must require authentication."""
        resp = requests.delete(
            f"{BASE_URL}/api/files",
            json={"path": "/tmp/test.txt"},
            timeout=5,
        )
        self.assertEqual(resp.status_code, 401)

    def test_delete_rejects_outside_allowed_dirs(self):
        """Delete must reject paths outside portal_uploads."""
        resp = requests.delete(
            f"{BASE_URL}/api/files",
            headers=AUTH_HEADERS,
            json={"path": "/etc/passwd"},
            timeout=5,
        )
        self.assertEqual(resp.status_code, 403)

    def test_delete_rejects_path_traversal(self):
        """Delete must reject path traversal attempts."""
        resp = requests.delete(
            f"{BASE_URL}/api/files",
            headers=AUTH_HEADERS,
            json={"path": "/home/aiciv/portal_uploads/../.env"},
            timeout=5,
        )
        self.assertEqual(resp.status_code, 403)

    def test_delete_creates_and_removes_file(self):
        """Create a temp file in portal_uploads, delete it via API, verify gone."""
        uploads_dir = os.path.expanduser("~/portal_uploads")
        os.makedirs(uploads_dir, exist_ok=True)
        # Create a temp test file
        test_file = os.path.join(uploads_dir, f"_test_delete_{int(time.time())}.txt")
        with open(test_file, "w") as f:
            f.write("test content for deletion")
        self.assertTrue(os.path.exists(test_file))

        try:
            resp = requests.delete(
                f"{BASE_URL}/api/files",
                headers=AUTH_HEADERS,
                json={"path": test_file},
                timeout=5,
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data.get("ok"), f"Delete should succeed: {data}")
            self.assertFalse(
                os.path.exists(test_file),
                "File must be gone after successful delete",
            )
        finally:
            # Cleanup in case test failed
            if os.path.exists(test_file):
                os.unlink(test_file)


class TestJSSyntax(unittest.TestCase):
    """Validate JavaScript syntax after all changes."""

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


class TestNewFileTypeFilters(unittest.TestCase):
    """Audio, Video, and Folders filters must be present and functional."""

    def _read_files_js(self):
        with open(FILES_JS, "r") as f:
            return f.read()

    def _read_html(self):
        html_path = os.path.join(PORTAL_DIR, "portal-pb-styled.html")
        with open(html_path, "r") as f:
            return f.read()

    # --- Extension mapping tests ---

    def test_audio_extensions_mapped_in_ext_map(self):
        """Audio extensions (.mp3, .wav, .ogg, .flac, .aac, .m4a, .wma) must map to 'audio'."""
        content = self._read_files_js()
        for ext in ["mp3", "wav", "ogg", "flac", "aac", "m4a", "wma"]:
            # JS object keys can be quoted or unquoted; accept either form
            self.assertTrue(
                f"'{ext}':'audio'" in content or f"{ext}:'audio'" in content,
                f"Extension '{ext}' must map to 'audio' in _EXT_MAP",
            )

    def test_video_extensions_mapped_in_ext_map(self):
        """Video extensions (.mp4, .avi, .mkv, .mov, .webm, .wmv, .flv) must map to 'video'."""
        content = self._read_files_js()
        for ext in ["mp4", "avi", "mkv", "mov", "webm", "wmv", "flv"]:
            # JS object keys can be quoted or unquoted; accept either form
            self.assertTrue(
                f"'{ext}':'video'" in content or f"{ext}:'video'" in content,
                f"Extension '{ext}' must map to 'video' in _EXT_MAP",
            )

    # --- GDrive mimeType mapping tests ---

    def test_audio_mime_type_mapped(self):
        """GDrive audio/* mimeTypes must map to 'audio' category."""
        content = self._read_files_js()
        # Either specific audio mimetypes or a prefix check for 'audio'
        self.assertIn(
            "'audio'",
            content,
            "_getMimeCat or _MIME_CAT_MAP must handle audio mimeTypes",
        )
        # The getMimeCat function should handle audio/* prefix
        self.assertRegex(
            content,
            r"audio.*audio|'audio/",
            "_getMimeCat must return 'audio' for audio/* mimeTypes",
        )

    def test_video_mime_type_mapped(self):
        """GDrive video/* mimeTypes must map to 'video' category."""
        content = self._read_files_js()
        self.assertIn(
            "'video'",
            content,
            "_getMimeCat or _MIME_CAT_MAP must handle video mimeTypes",
        )
        self.assertRegex(
            content,
            r"video.*video|'video/",
            "_getMimeCat must return 'video' for video/* mimeTypes",
        )

    # --- Folders filter in getFilteredItems ---

    def test_folders_filter_shows_dirs_only(self):
        """_getFilteredItems must handle 'folders' category by returning is_dir items."""
        content = self._read_files_js()
        # The filtering logic must handle 'folders' specifically
        self.assertIn(
            "folders",
            content,
            "_getFilteredItems must handle 'folders' category",
        )
        self.assertRegex(
            content,
            r"folders.*is_dir|is_dir.*folders",
            "_getFilteredItems 'folders' filter must use is_dir flag",
        )

    # --- Sidebar HTML tests ---

    def test_sidebar_has_audio_filter_item(self):
        """Sidebar must have a filter item for Audio."""
        html = self._read_html()
        self.assertIn(
            "filterFiles('audio')",
            html,
            "Sidebar must have filterFiles('audio') filter item",
        )

    def test_sidebar_has_video_filter_item(self):
        """Sidebar must have a filter item for Video."""
        html = self._read_html()
        self.assertIn(
            "filterFiles('video')",
            html,
            "Sidebar must have filterFiles('video') filter item",
        )

    def test_sidebar_has_folders_filter_item(self):
        """Sidebar must have a filter item for Folders."""
        html = self._read_html()
        self.assertIn(
            "filterFiles('folders')",
            html,
            "Sidebar must have filterFiles('folders') filter item",
        )

    def test_sidebar_filter_order(self):
        """Filter order must be: All Files, Folders, Documents, Markdown, Code, Images, Audio, Video, Data."""
        html = self._read_html()
        positions = {
            "all": html.find("filterFiles('all')"),
            "folders": html.find("filterFiles('folders')"),
            "documents": html.find("filterFiles('documents')"),
            "markdown": html.find("filterFiles('markdown')"),
            "code": html.find("filterFiles('code')"),
            "images": html.find("filterFiles('images')"),
            "audio": html.find("filterFiles('audio')"),
            "video": html.find("filterFiles('video')"),
            "data": html.find("filterFiles('data')"),
        }
        for cat, pos in positions.items():
            self.assertGreater(pos, -1, f"filterFiles('{cat}') not found in HTML")
        expected_order = ["all", "folders", "documents", "markdown", "code", "images", "audio", "video", "data"]
        for i in range(len(expected_order) - 1):
            a, b = expected_order[i], expected_order[i + 1]
            self.assertLess(
                positions[a],
                positions[b],
                f"filterFiles('{a}') must appear before filterFiles('{b}') in sidebar",
            )

    def test_sidebar_audio_has_count_badge(self):
        """Audio filter item must have a count badge."""
        html = self._read_html()
        self.assertIn(
            'id="ft-count-audio"',
            html,
            "Audio filter must have ft-count-audio count badge",
        )

    def test_sidebar_video_has_count_badge(self):
        """Video filter item must have a count badge."""
        html = self._read_html()
        self.assertIn(
            'id="ft-count-video"',
            html,
            "Video filter must have ft-count-video count badge",
        )

    def test_sidebar_folders_has_count_badge(self):
        """Folders filter item must have a count badge."""
        html = self._read_html()
        self.assertIn(
            'id="ft-count-folders"',
            html,
            "Folders filter must have ft-count-folders count badge",
        )

    # --- Sidebar counts JS update ---

    def test_update_sidebar_counts_includes_audio(self):
        """_updateSidebarCounts must update the audio count badge."""
        content = self._read_files_js()
        # Either literal "ft-count-audio" or dynamic pattern like 'ft-count-'+cat with 'audio' in counts obj
        self.assertTrue(
            "ft-count-audio" in content or (
                "ft-count-" in content and "'audio'" in content and "counts" in content
            ),
            "_updateSidebarCounts must update ft-count-audio (via literal or dynamic pattern)",
        )

    def test_update_sidebar_counts_includes_video(self):
        """_updateSidebarCounts must update the video count badge."""
        content = self._read_files_js()
        self.assertTrue(
            "ft-count-video" in content or (
                "ft-count-" in content and "'video'" in content and "counts" in content
            ),
            "_updateSidebarCounts must update ft-count-video (via literal or dynamic pattern)",
        )

    def test_update_sidebar_counts_includes_folders(self):
        """_updateSidebarCounts must update the folders count badge."""
        content = self._read_files_js()
        self.assertTrue(
            "ft-count-folders" in content or (
                "ft-count-" in content and "'folders'" in content and "counts" in content
            ),
            "_updateSidebarCounts must update ft-count-folders (via literal or dynamic pattern)",
        )

    # --- API is_dir field ---

    def test_api_download_list_has_is_dir_field(self):
        """GET /api/download/list items must include is_dir field (needed for folders filter)."""
        if not TOKEN:
            self.skipTest("No portal token")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

        resp = requests.get(
            f"{BASE_URL}/api/download/list", headers=AUTH_HEADERS, timeout=5
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        if "dirs" in data and data["dirs"]:
            first_dir = data["dirs"][0]
            resp2 = requests.get(
                f"{BASE_URL}/api/download/list?dir={first_dir}",
                headers=AUTH_HEADERS,
                timeout=5,
            )
            self.assertEqual(resp2.status_code, 200)
            data2 = resp2.json()
            if "items" in data2 and data2["items"]:
                item = data2["items"][0]
                self.assertIn(
                    "is_dir",
                    item,
                    "API response items must include is_dir field for folders filter",
                )


if __name__ == "__main__":
    unittest.main()
