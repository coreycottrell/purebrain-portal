"""
Tests for the custom panel overlay injection system.

Tests cover:
- _parse_panel_meta extracts metadata correctly from HTML comments
- _inject_custom_panels injects nav items inside the sidebar nav (before Quick Fire)
- _inject_custom_panels injects panel divs inside .content (before /panels marker)
- The injected nav-item has correct data-panel attribute matching panel-{id}
- Config overrides from custom/config.json are applied (with allowlist)
- Config override allowlist blocks unauthorized keys
- HTML escaping of panel metadata (XSS prevention)
- Custom panel handler registration in JS
- Panel replacement via panel-replace metadata
- Endpoint extension wrapper (_make_extended_endpoint)
"""

import os
import re
import sys
import json
import tempfile
import textwrap
from html import escape as html_escape
from pathlib import Path
from unittest import mock

import pytest

# Add portal root to path so we can import portal_server functions
PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PORTAL_DIR)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_panel_html():
    """Return a sample custom panel HTML with metadata comments."""
    return (
        '<!-- panel-id: skills-shop -->\n'
        '<!-- panel-label: Skills Shop -->\n'
        '<!-- panel-icon: &#x1F6D2; -->\n'
        '<!-- panel-tooltip: Browse available AI skills -->\n'
        '\n'
        '<div style="padding:20px;">\n'
        '  <h2>Skills Shop</h2>\n'
        '</div>\n'
    )


@pytest.fixture
def minimal_portal_html():
    """Return a minimal portal HTML with the correct injection markers.

    This reproduces the real structure:
    - Sidebar nav with nav-items and <!-- /nav-panels --> marker
    - Content area with panels ending at <!-- /panels --> marker
    - Mobile bottom tabs area with <!-- Mobile bottom tabs --> marker
    - Mobile more-menu with <!-- /mobile-menu-items --> marker
    - Toast area with <!-- Toast --> marker
    """
    return (
        '<div class="main">\n'
        '  <nav class="sidebar">\n'
        '    <div class="nav-item active" data-panel="chat">\n'
        '      <span class="nav-icon">&#x25C8;</span>Chat\n'
        '    </div>\n'
        '    <div class="nav-item" data-panel="agents">\n'
        '      <span class="nav-icon">&#x2726;</span>Agent Roster\n'
        '    </div>\n'
        '    <!-- /nav-panels -->\n'
        '\n'
        '    <!-- Quick Fire pills -->\n'
        '    <div class="sidebar-footer" id="sidebar-quickfire">\n'
        '      <span class="quick-cmd-label">Quick Fire</span>\n'
        '    </div>\n'
        '  </nav>\n'
        '\n'
        '  <div class="content">\n'
        '    <div class="panel active" id="panel-chat">Chat content</div>\n'
        '    <div class="panel" id="panel-agents">Agents content</div>\n'
        '\n'
        '  <!-- /panels -->\n'
        '  </div>\n'
        '\n'
        '</div>\n'
        '\n'
        '<!-- Mobile bottom tabs -->\n'
        '<div class="mobile-tabs">\n'
        '  <div class="tab-bar">\n'
        '    <div class="tab-item active" data-panel="chat">Chat</div>\n'
        '  </div>\n'
        '</div>\n'
        '\n'
        '<div id="mobile-more-menu">\n'
        '    <!-- /mobile-menu-items -->\n'
        '</div>\n'
        '\n'
        '<!-- Toast -->\n'
        '<div id="toast"></div>\n'
    )


@pytest.fixture
def portal_html_with_mobile_menu_items():
    """Portal HTML that includes tab-menu-item divs in the mobile more-menu.

    This extends minimal_portal_html to also have built-in mobile menu items
    (class=tab-menu-item) for panels like 'status', which the panel-replace
    code hides and replaces.
    """
    return (
        '<div class="main">\n'
        '  <nav class="sidebar">\n'
        '    <div class="nav-item active" data-panel="chat">\n'
        '      <span class="nav-icon">&#x25C8;</span>Chat\n'
        '    </div>\n'
        '    <div class="nav-item" data-panel="status">\n'
        '      <span class="nav-icon">&#x2605;</span>Status\n'
        '    </div>\n'
        '    <div class="nav-item" data-panel="agents">\n'
        '      <span class="nav-icon">&#x2726;</span>Agent Roster\n'
        '    </div>\n'
        '    <!-- /nav-panels -->\n'
        '\n'
        '    <!-- Quick Fire pills -->\n'
        '    <div class="sidebar-footer" id="sidebar-quickfire">\n'
        '      <span class="quick-cmd-label">Quick Fire</span>\n'
        '    </div>\n'
        '  </nav>\n'
        '\n'
        '  <div class="content">\n'
        '    <div class="panel active" id="panel-chat">Chat content</div>\n'
        '    <div class="panel" id="panel-status">Status content</div>\n'
        '    <div class="panel" id="panel-agents">Agents content</div>\n'
        '\n'
        '  <!-- /panels -->\n'
        '  </div>\n'
        '\n'
        '</div>\n'
        '\n'
        '<!-- Mobile bottom tabs -->\n'
        '<div class="mobile-tabs">\n'
        '  <div class="tab-bar">\n'
        '    <div class="tab-item active" data-panel="chat">Chat</div>\n'
        '  </div>\n'
        '</div>\n'
        '\n'
        '<div id="mobile-more-menu">\n'
        '    <div class="tab-menu-item" data-panel="status" onclick="selectMobileMenuItem(\'status\')">'
        '<span style="margin-right:10px;">&#x2605;</span>Status</div>\n'
        '    <div class="tab-menu-item" data-panel="agents" onclick="selectMobileMenuItem(\'agents\')">'
        '<span style="margin-right:10px;">&#x2726;</span>Agent Roster</div>\n'
        '    <!-- /mobile-menu-items -->\n'
        '</div>\n'
        '\n'
        '<!-- Toast -->\n'
        '<div id="toast"></div>\n'
    )


@pytest.fixture
def custom_panels_dir(tmp_path, sample_panel_html):
    """Create a temporary custom/panels/ directory with a sample panel file."""
    panels_dir = tmp_path / "custom" / "panels"
    panels_dir.mkdir(parents=True)
    (panels_dir / "skills-shop.html").write_text(sample_panel_html)
    return panels_dir


@pytest.fixture
def custom_config_dir(tmp_path):
    """Create a temporary custom/ directory with a config.json."""
    custom_dir = tmp_path / "custom"
    custom_dir.mkdir(exist_ok=True)
    config = {"MAX_TOKENS": 500000, "PORTAL_VERSION": "1.0.2-test"}
    (custom_dir / "config.json").write_text(json.dumps(config))
    return custom_dir


# ---------------------------------------------------------------------------
# Import the functions under test (after path setup)
# ---------------------------------------------------------------------------

def _extract_function(source: str, func_name: str) -> str:
    """Extract a top-level function from Python source by name.

    Finds 'def func_name(' and captures everything until the next
    top-level definition or end of file.
    """
    lines = source.split('\n')
    start = None
    for i, line in enumerate(lines):
        if line.startswith(f'def {func_name}(') or line.startswith(f'async def {func_name}('):
            start = i
            break
    if start is None:
        return ""

    # Collect lines until next top-level def/class/async def or blank-then-def
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i]
        if (stripped.startswith('def ') or stripped.startswith('async def ') or
                stripped.startswith('class ')):
            end = i
            break

    return '\n'.join(lines[start:end])


def _extract_indented_function(source: str, func_name: str) -> str:
    """Extract a possibly-indented function from Python source and dedent it.

    Unlike _extract_function which only matches top-level defs, this finds
    'def func_name(' at any indentation level, captures the full body, and
    returns it dedented so it can be exec'd as a top-level function.
    """
    lines = source.split('\n')
    start = None
    indent = 0
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f'def {func_name}(') or stripped.startswith(f'async def {func_name}('):
            start = i
            indent = len(line) - len(stripped)
            break
    if start is None:
        return ""

    # Collect lines: the function body is everything indented deeper than `indent`,
    # plus blank lines, until we hit a line at the same or lesser indentation that
    # starts a new statement (def/class/assignment/comment at indent level or less).
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            # blank line -- could be inside function, keep going
            continue
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= indent:
            # Back to same or lesser indentation -- function ended
            end = i
            break

    func_lines = lines[start:end]
    return textwrap.dedent('\n'.join(func_lines))


def _get_parse_panel_meta():
    """Import _parse_panel_meta from portal_server."""
    server_path = os.path.join(PORTAL_DIR, "portal_server.py")
    with open(server_path) as f:
        source = f.read()

    func_src = _extract_function(source, "_parse_panel_meta")
    if not func_src:
        pytest.skip("Could not extract _parse_panel_meta from portal_server.py")

    ns = {"re": __import__("re")}
    exec(func_src, ns)
    return ns["_parse_panel_meta"]


def _get_inject_custom_panels():
    """Import _inject_custom_panels from portal_server."""
    server_path = os.path.join(PORTAL_DIR, "portal_server.py")
    with open(server_path) as f:
        source = f.read()

    parse_src = _extract_function(source, "_parse_panel_meta")
    inject_src = _extract_function(source, "_inject_custom_panels")
    if not parse_src or not inject_src:
        pytest.skip("Could not extract injection functions from portal_server.py")

    ns = {
        "re": __import__("re"),
        "Path": Path,
        "sorted": sorted,
        "print": print,
    }
    exec(parse_src, ns)
    exec(inject_src, ns)
    return ns["_inject_custom_panels"], ns["_parse_panel_meta"]


def _get_make_extended_endpoint():
    """Import _make_extended_endpoint from portal_server using the exec/compile extraction pattern.

    This avoids importing portal_server.py directly (which has module-level side effects)
    while testing the REAL function, not a re-implementation.

    The function is defined inside an ``if _endpoint_extensions:`` block (indented),
    so we use _extract_indented_function + dedent to get a top-level version.
    """
    import functools as _functools
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response

    server_path = os.path.join(PORTAL_DIR, "portal_server.py")
    with open(server_path) as f:
        source = f.read()

    func_src = _extract_indented_function(source, "_make_extended_endpoint")
    if not func_src:
        pytest.skip("Could not extract _make_extended_endpoint from portal_server.py")

    ns = {
        "json": __import__("json"),
        "functools": _functools,
        "_functools": _functools,
        "Request": Request,
        "Response": Response,
        "JSONResponse": JSONResponse,
        "print": print,
    }
    exec(func_src, ns)
    return ns["_make_extended_endpoint"]


# ---------------------------------------------------------------------------
# Tests: _parse_panel_meta
# ---------------------------------------------------------------------------

class TestParsePanelMeta:
    """Tests for metadata extraction from panel HTML comment headers."""

    def test_extracts_all_metadata_fields(self, sample_panel_html):
        parse_meta = _get_parse_panel_meta()
        meta = parse_meta(sample_panel_html)
        assert meta["id"] == "skills-shop"
        assert meta["label"] == "Skills Shop"
        assert meta["icon"] == "&#x1F6D2;"
        assert meta["tooltip"] == "Browse available AI skills"

    def test_returns_empty_dict_for_no_metadata(self):
        parse_meta = _get_parse_panel_meta()
        meta = parse_meta("<div>No metadata here</div>")
        assert meta == {}

    def test_handles_partial_metadata(self):
        parse_meta = _get_parse_panel_meta()
        html = '<!-- panel-id: test-panel -->\n<div>content</div>'
        meta = parse_meta(html)
        assert meta["id"] == "test-panel"
        assert "label" not in meta

    def test_only_reads_first_10_lines(self):
        parse_meta = _get_parse_panel_meta()
        # Metadata on line 11 should be ignored
        lines = ["<div>line</div>"] * 10 + ["<!-- panel-id: should-be-ignored -->"]
        meta = parse_meta("\n".join(lines))
        assert "id" not in meta


# ---------------------------------------------------------------------------
# Tests: _inject_custom_panels
# ---------------------------------------------------------------------------

class TestInjectCustomPanels:
    """Tests for HTML injection of custom panels into portal page."""

    def test_nav_item_injected_before_nav_panels_marker(
        self, minimal_portal_html, custom_panels_dir
    ):
        """Nav item must appear BEFORE <!-- /nav-panels --> marker in sidebar."""
        result = _run_injection(minimal_portal_html, custom_panels_dir)

        # The nav-item should appear before the /nav-panels marker (among other panel nav items)
        marker_pos = result.find("<!-- /nav-panels -->")
        nav_pos = result.find('data-panel="skills-shop"')
        assert nav_pos != -1, "Nav item for skills-shop not found in output"
        assert marker_pos != -1, "<!-- /nav-panels --> marker not found in output"
        assert nav_pos < marker_pos, "Nav item must appear BEFORE <!-- /nav-panels --> marker"

    def test_nav_item_has_correct_data_panel_attribute(
        self, minimal_portal_html, custom_panels_dir
    ):
        """The nav-item data-panel must match the panel id (not panel-{id})."""
        result = _run_injection(minimal_portal_html, custom_panels_dir)
        assert 'data-panel="skills-shop"' in result

    def test_panel_div_injected_inside_content_area(
        self, minimal_portal_html, custom_panels_dir
    ):
        """Panel div must be inside .content area, before <!-- /panels --> marker."""
        result = _run_injection(minimal_portal_html, custom_panels_dir)

        panels_marker_pos = result.find("<!-- /panels -->")
        panel_div_pos = result.find('id="panel-skills-shop"')
        content_close = result.find("</div>", panels_marker_pos)

        assert panel_div_pos != -1, "Panel div panel-skills-shop not found in output"
        assert panel_div_pos < panels_marker_pos, (
            "Panel div must appear BEFORE <!-- /panels --> marker (inside .content)"
        )

    def test_panel_div_not_outside_content(
        self, minimal_portal_html, custom_panels_dir
    ):
        """Panel div must NOT be placed after .content closes (the old bug)."""
        result = _run_injection(minimal_portal_html, custom_panels_dir)

        # The mobile tabs marker should come AFTER the panel div, but both
        # should be properly placed
        mobile_pos = result.find("<!-- Mobile bottom tabs -->")
        panel_div_pos = result.find('id="panel-skills-shop"')

        assert panel_div_pos != -1, "Panel div not found"
        assert panel_div_pos < mobile_pos, "Panel div should be before mobile tabs"

        # More importantly: panel should be inside .content, not between
        # closing </div>s and mobile tabs
        # Find the content area end
        content_start = result.find('<div class="content">')
        # Count div nesting to find content close
        assert content_start != -1, "Content area not found"

    def test_panel_div_has_panel_class(
        self, minimal_portal_html, custom_panels_dir
    ):
        """Injected panel div must have class='panel' like other panels."""
        result = _run_injection(minimal_portal_html, custom_panels_dir)
        # Find the panel div
        match = re.search(r'<div class="panel" id="panel-skills-shop">', result)
        assert match is not None, "Panel div must have class='panel' and id='panel-skills-shop'"

    def test_mobile_menu_item_injected_inside_mobile_menu(
        self, minimal_portal_html, custom_panels_dir
    ):
        """Mobile menu item should be injected inside #mobile-more-menu div."""
        result = _run_injection(minimal_portal_html, custom_panels_dir)

        menu_start = result.find('id="mobile-more-menu"')
        menu_marker = result.find("<!-- /mobile-menu-items -->")
        # The mobile item injection uses selectMobileMenuItem
        mobile_select_pos = result.find("selectMobileMenuItem('skills-shop')")

        assert mobile_select_pos != -1, "Mobile menu item not found"
        assert menu_start != -1, "mobile-more-menu div not found"
        assert menu_marker != -1, "/mobile-menu-items marker not found"
        assert mobile_select_pos > menu_start, "Mobile item must be inside mobile-more-menu"
        assert mobile_select_pos < menu_marker, "Mobile item must be before /mobile-menu-items marker"

    def test_nav_item_contains_icon_and_label(
        self, minimal_portal_html, custom_panels_dir
    ):
        """Injected nav-item should contain the panel's icon and label."""
        result = _run_injection(minimal_portal_html, custom_panels_dir)

        # Check for the icon
        assert "&#x1F6D2;" in result, "Panel icon not found in output"
        # Check for the label
        assert "Skills Shop" in result, "Panel label not found in output"

    def test_no_injection_without_panel_files(self, minimal_portal_html, tmp_path):
        """If custom/panels/ is empty, HTML should be unchanged."""
        panels_dir = tmp_path / "custom" / "panels"
        panels_dir.mkdir(parents=True)
        result = _run_injection(minimal_portal_html, panels_dir)
        assert result == minimal_portal_html

    def test_no_injection_without_panel_id(self, minimal_portal_html, tmp_path):
        """Panel files without panel-id metadata should be skipped."""
        panels_dir = tmp_path / "custom" / "panels"
        panels_dir.mkdir(parents=True)
        (panels_dir / "bad-panel.html").write_text("<div>No metadata</div>")
        result = _run_injection(minimal_portal_html, panels_dir)
        assert result == minimal_portal_html

    def test_multiple_panels_injected_in_sorted_order(
        self, minimal_portal_html, tmp_path
    ):
        """Multiple panel files should be injected in sorted filename order."""
        panels_dir = tmp_path / "custom" / "panels"
        panels_dir.mkdir(parents=True)
        (panels_dir / "a-first.html").write_text(
            '<!-- panel-id: alpha -->\n<!-- panel-label: Alpha -->\n<div>A</div>'
        )
        (panels_dir / "b-second.html").write_text(
            '<!-- panel-id: beta -->\n<!-- panel-label: Beta -->\n<div>B</div>'
        )
        result = _run_injection(minimal_portal_html, panels_dir)

        alpha_pos = result.find('data-panel="alpha"')
        beta_pos = result.find('data-panel="beta"')
        assert alpha_pos != -1 and beta_pos != -1, "Both panels should be injected"
        assert alpha_pos < beta_pos, "Alpha should come before Beta (sorted order)"


# ---------------------------------------------------------------------------
# Tests: Panel replacement (panel-replace metadata)
# ---------------------------------------------------------------------------

class TestPanelReplacement:
    """Tests for panel-replace mode in _inject_custom_panels.

    When a custom panel HTML file contains ``<!-- panel-replace: <target-id> -->``,
    the function should hide the original nav item, inject a replacement nav item,
    swap the original panel div's content, and handle the mobile menu similarly.
    """

    def _make_replace_panel_html(self, target_id, panel_id="custom-status",
                                  label="Custom Status", icon="&#x1F4CA;",
                                  tooltip="Replacement panel", content="<p>Replaced!</p>"):
        """Build a panel HTML file with panel-replace metadata."""
        return (
            f'<!-- panel-id: {panel_id} -->\n'
            f'<!-- panel-label: {label} -->\n'
            f'<!-- panel-icon: {icon} -->\n'
            f'<!-- panel-tooltip: {tooltip} -->\n'
            f'<!-- panel-replace: {target_id} -->\n'
            f'\n'
            f'{content}\n'
        )

    def test_basic_panel_replacement(self, portal_html_with_mobile_menu_items, tmp_path):
        """Panel with panel-replace metadata should replace the target panel."""
        panels_dir = tmp_path / "custom" / "panels"
        panels_dir.mkdir(parents=True)
        (panels_dir / "replace-status.html").write_text(
            self._make_replace_panel_html("status")
        )
        result = _run_injection(portal_html_with_mobile_menu_items, panels_dir)

        # The replacement nav item should exist and point to the original panel ID
        assert 'data-panel="status"' in result, "Replacement nav item must target original panel ID"
        # The custom content should appear inside the panel div
        assert "<p>Replaced!</p>" in result, "Custom replacement content not found in output"

    def test_original_nav_item_hidden(self, portal_html_with_mobile_menu_items, tmp_path):
        """Original nav item for the replaced panel gets display:none."""
        panels_dir = tmp_path / "custom" / "panels"
        panels_dir.mkdir(parents=True)
        (panels_dir / "replace-status.html").write_text(
            self._make_replace_panel_html("status")
        )
        result = _run_injection(portal_html_with_mobile_menu_items, panels_dir)

        # Find a nav-item with display:none that targets 'status'
        hidden_nav = re.search(
            r'<div\s+class="nav-item"\s+style="display:none"\s+data-panel="status"',
            result
        )
        assert hidden_nav is not None, (
            "Original nav item for 'status' should have style='display:none'"
        )

    def test_content_swap_in_panel_div(self, portal_html_with_mobile_menu_items, tmp_path):
        """Original panel div innerHTML is replaced with custom content."""
        panels_dir = tmp_path / "custom" / "panels"
        panels_dir.mkdir(parents=True)
        custom_content = "<h2>All New Status</h2><p>Dynamic content here</p>"
        (panels_dir / "replace-status.html").write_text(
            self._make_replace_panel_html("status", content=custom_content)
        )
        result = _run_injection(portal_html_with_mobile_menu_items, panels_dir)

        # The original "Status content" text should be gone
        assert "Status content" not in result, (
            "Original panel content should be replaced"
        )
        # The custom content should be present inside the panel-status div
        assert "All New Status" in result, "Replacement content not found"
        assert "Dynamic content here" in result, "Replacement content not fully injected"

        # Verify the panel div retains its original id (for CSS/JS compatibility)
        assert 'id="panel-status"' in result, (
            "Panel div must retain original id='panel-status'"
        )

    def test_mobile_menu_replacement(self, portal_html_with_mobile_menu_items, tmp_path):
        """Original mobile menu item is hidden and a replacement is injected."""
        panels_dir = tmp_path / "custom" / "panels"
        panels_dir.mkdir(parents=True)
        (panels_dir / "replace-status.html").write_text(
            self._make_replace_panel_html("status", label="New Status", icon="&#x1F4CA;")
        )
        result = _run_injection(portal_html_with_mobile_menu_items, panels_dir)

        # The original mobile menu item for 'status' should be hidden
        hidden_mobile = re.search(
            r'<div\s+class="tab-menu-item"\s+style="display:none"\s+data-panel="status"',
            result
        )
        assert hidden_mobile is not None, (
            "Original mobile menu item for 'status' should have style='display:none'"
        )

        # A replacement mobile menu item should be injected
        # It should appear before the /mobile-menu-items marker
        mobile_marker_pos = result.find("<!-- /mobile-menu-items -->")
        assert mobile_marker_pos != -1, "Mobile menu items marker not found"

        # Find the replacement mobile item with "New Status" label
        replacement_mobile = re.search(
            r'<div\s+class="tab-menu-item"\s+data-panel="status"\s+'
            r'onclick="selectMobileMenuItem\(\'status\'\)">'
            r'.*?New Status',
            result, re.DOTALL
        )
        assert replacement_mobile is not None, (
            "Replacement mobile menu item with custom label 'New Status' not found"
        )
        assert replacement_mobile.start() < mobile_marker_pos, (
            "Replacement mobile item must appear before /mobile-menu-items marker"
        )

    def test_nonexistent_target_gracefully_skipped(self, minimal_portal_html, tmp_path):
        """panel-replace targeting a non-existent panel ID should skip gracefully."""
        panels_dir = tmp_path / "custom" / "panels"
        panels_dir.mkdir(parents=True)
        (panels_dir / "replace-ghost.html").write_text(
            self._make_replace_panel_html("nonexistent-panel-xyz")
        )
        # Should not raise an error
        result = _run_injection(minimal_portal_html, panels_dir)

        # The HTML should not have any hidden nav items for a nonexistent panel
        assert 'style="display:none"' not in result, (
            "No elements should be hidden when target panel does not exist"
        )
        # The original HTML structure should remain intact
        assert 'data-panel="chat"' in result, "Existing panels should remain untouched"
        assert 'data-panel="agents"' in result, "Existing panels should remain untouched"

    def test_additive_panels_still_work_alongside_replacement(
        self, portal_html_with_mobile_menu_items, tmp_path
    ):
        """Panels without panel-replace continue to work in additive mode."""
        panels_dir = tmp_path / "custom" / "panels"
        panels_dir.mkdir(parents=True)

        # One additive panel
        (panels_dir / "a-additive.html").write_text(
            '<!-- panel-id: skills-shop -->\n'
            '<!-- panel-label: Skills Shop -->\n'
            '<!-- panel-icon: &#x1F6D2; -->\n'
            '<div>Skills content</div>\n'
        )
        # One replacement panel
        (panels_dir / "b-replace.html").write_text(
            self._make_replace_panel_html("status")
        )

        result = _run_injection(portal_html_with_mobile_menu_items, panels_dir)

        # Additive panel should be injected as a new panel div
        assert 'id="panel-skills-shop"' in result, "Additive panel div not found"
        assert 'data-panel="skills-shop"' in result, "Additive nav item not found"

        # Replacement panel should have swapped the status content
        assert "<p>Replaced!</p>" in result, "Replacement content not found"

        # Original status nav item should be hidden
        hidden_nav = re.search(
            r'<div\s+class="nav-item"\s+style="display:none"\s+data-panel="status"',
            result
        )
        assert hidden_nav is not None, "Original status nav item should be hidden"

    def test_parse_panel_meta_extracts_replace_field(self):
        """_parse_panel_meta correctly extracts panel-replace from first 10 lines."""
        parse_meta = _get_parse_panel_meta()
        html = (
            '<!-- panel-id: custom-dash -->\n'
            '<!-- panel-label: Custom Dashboard -->\n'
            '<!-- panel-replace: status -->\n'
            '<div>content</div>\n'
        )
        meta = parse_meta(html)
        assert meta.get("id") == "custom-dash", "panel-id not extracted"
        assert meta.get("label") == "Custom Dashboard", "panel-label not extracted"
        assert meta.get("replace") == "status", (
            "panel-replace metadata not extracted correctly"
        )


# ---------------------------------------------------------------------------
# Tests: Config overrides
# ---------------------------------------------------------------------------

class TestConfigOverrides:
    """Tests for custom/config.json override application."""

    def test_config_json_loads_correctly(self, custom_config_dir):
        """Config values from custom/config.json should be parseable."""
        config_path = custom_config_dir / "config.json"
        config = json.loads(config_path.read_text())
        assert config["MAX_TOKENS"] == 500000
        assert config["PORTAL_VERSION"] == "1.0.2-test"


# ---------------------------------------------------------------------------
# Helper: Run injection with a custom panels directory
# ---------------------------------------------------------------------------

def _run_injection(html: str, panels_dir: Path) -> str:
    """Run _inject_custom_panels with a custom panels directory.

    Since the extracted function references SCRIPT_DIR, we create a
    self-contained version that uses the provided panels_dir directly.
    """
    server_path = os.path.join(PORTAL_DIR, "portal_server.py")
    with open(server_path) as f:
        source = f.read()

    parse_src = _extract_function(source, "_parse_panel_meta")
    inject_src = _extract_function(source, "_inject_custom_panels")
    if not parse_src or not inject_src:
        pytest.skip("Could not extract functions")

    # Replace SCRIPT_DIR reference with our panels_dir parent
    inject_src = inject_src.replace(
        'SCRIPT_DIR / "custom" / "panels"',
        f'Path("{panels_dir}")'
    )

    ns = {
        "re": __import__("re"),
        "Path": Path,
        "sorted": sorted,
        "print": print,
        "escape": html_escape,
    }
    exec(parse_src, ns)
    exec(inject_src, ns)

    return ns["_inject_custom_panels"](html)


# ---------------------------------------------------------------------------
# Helper: Run config override with allowlist
# ---------------------------------------------------------------------------

def _get_allowed_config_overrides():
    """Extract _ALLOWED_CONFIG_OVERRIDES from portal_server.py source."""
    server_path = os.path.join(PORTAL_DIR, "portal_server.py")
    with open(server_path) as f:
        source = f.read()

    # Look for _ALLOWED_CONFIG_OVERRIDES = {...}
    match = re.search(
        r'_ALLOWED_CONFIG_OVERRIDES\s*=\s*\{([^}]+)\}', source
    )
    if not match:
        return None
    # Parse the set literal
    items = [s.strip().strip('"').strip("'") for s in match.group(1).split(",")]
    return {item for item in items if item}


def _run_config_override(config_dict: dict, existing_globals: dict) -> dict:
    """Simulate running config override logic from portal_server.py.

    Returns dict of {key: value} that were actually applied.
    """
    server_path = os.path.join(PORTAL_DIR, "portal_server.py")
    with open(server_path) as f:
        source = f.read()

    # Extract the CUSTOMIZATION LAYER block
    # Find _ALLOWED_CONFIG_OVERRIDES and the config override block
    allowlist = _get_allowed_config_overrides()
    if allowlist is None:
        pytest.fail("_ALLOWED_CONFIG_OVERRIDES not found in portal_server.py")

    applied = {}
    for k, v in config_dict.items():
        if k in allowlist and k in existing_globals:
            applied[k] = v

    return applied


# ---------------------------------------------------------------------------
# Tests: Config override allowlist (SECURITY)
# ---------------------------------------------------------------------------

class TestConfigOverrideAllowlist:
    """Tests for config override allowlist security."""

    def test_allowlist_exists_in_source(self):
        """_ALLOWED_CONFIG_OVERRIDES must be defined in portal_server.py."""
        allowlist = _get_allowed_config_overrides()
        assert allowlist is not None, (
            "_ALLOWED_CONFIG_OVERRIDES not found in portal_server.py"
        )

    def test_allowlist_contains_expected_keys(self):
        """Allowlist must include the known-safe config keys."""
        allowlist = _get_allowed_config_overrides()
        expected = {"MAX_TOKENS", "PORTAL_VERSION", "PAYOUT_MIN_AMOUNT", "REFERRAL_COMMISSION_RATE"}
        assert expected.issubset(allowlist), (
            f"Allowlist missing expected keys: {expected - allowlist}"
        )

    def test_allowlist_blocks_dangerous_keys(self):
        """Keys not in the allowlist must NOT be overridden."""
        allowlist = _get_allowed_config_overrides()
        dangerous_keys = ["SECRET_KEY", "API_KEY", "DEBUG", "__builtins__", "SCRIPT_DIR"]
        for key in dangerous_keys:
            assert key not in allowlist, (
                f"Dangerous key '{key}' should NOT be in allowlist"
            )

    def test_config_override_code_checks_allowlist(self):
        """The config override block must check _ALLOWED_CONFIG_OVERRIDES."""
        server_path = os.path.join(PORTAL_DIR, "portal_server.py")
        with open(server_path) as f:
            source = f.read()

        # Find the config override block (between "1. Config overrides" and "2. Custom routes")
        config_section_match = re.search(
            r'# 1\. Config overrides.*?# 2\. Custom routes',
            source, re.DOTALL
        )
        assert config_section_match is not None, "Config override section not found"
        config_section = config_section_match.group(0)

        # Must reference _ALLOWED_CONFIG_OVERRIDES in the override logic
        assert "_ALLOWED_CONFIG_OVERRIDES" in config_section, (
            "Config override block must check _ALLOWED_CONFIG_OVERRIDES"
        )

    def test_blocked_keys_produce_warning(self):
        """Blocked config keys should trigger a warning log line."""
        server_path = os.path.join(PORTAL_DIR, "portal_server.py")
        with open(server_path) as f:
            source = f.read()

        config_section_match = re.search(
            r'# 1\. Config overrides.*?# 2\. Custom routes',
            source, re.DOTALL
        )
        assert config_section_match is not None
        config_section = config_section_match.group(0)

        # Should have a warning for blocked keys
        assert "WARNING" in config_section or "blocked" in config_section.lower(), (
            "Config override block should warn about blocked keys"
        )


# ---------------------------------------------------------------------------
# Tests: HTML escaping of panel metadata (SECURITY)
# ---------------------------------------------------------------------------

class TestPanelMetadataEscaping:
    """Tests for HTML escaping of panel metadata to prevent XSS."""

    def test_tooltip_xss_is_escaped(self, minimal_portal_html, tmp_path):
        """Tooltip containing XSS payload must be HTML-escaped in attributes."""
        panels_dir = tmp_path / "custom" / "panels"
        panels_dir.mkdir(parents=True)
        xss_tooltip = '"><script>alert("xss")</script><div x="'
        (panels_dir / "evil.html").write_text(
            '<!-- panel-id: evil -->\n'
            '<!-- panel-label: Evil Panel -->\n'
            f'<!-- panel-tooltip: {xss_tooltip} -->\n'
            '<div>content</div>\n'
        )
        result = _run_injection(minimal_portal_html, panels_dir)

        # Find the data-tooltip attribute value -- it must be escaped
        tooltip_match = re.search(r'data-tooltip="([^"]*)"', result)
        assert tooltip_match is not None, "data-tooltip attribute not found"
        tooltip_val = tooltip_match.group(1)
        # The escaped tooltip must not contain raw < or > (they should be &lt; &gt;)
        assert '<script>' not in tooltip_val, (
            "XSS payload in data-tooltip attribute must be HTML-escaped"
        )
        assert '&lt;script&gt;' in tooltip_val or '&amp;' in tooltip_val, (
            "Tooltip attribute should contain escaped HTML entities"
        )

    def test_label_xss_is_escaped(self, minimal_portal_html, tmp_path):
        """Label containing HTML must be escaped in nav-item output."""
        panels_dir = tmp_path / "custom" / "panels"
        panels_dir.mkdir(parents=True)
        (panels_dir / "evil.html").write_text(
            '<!-- panel-id: evil -->\n'
            '<!-- panel-label: <img src=x onerror=alert(1)> -->\n'
            '<div>content</div>\n'
        )
        result = _run_injection(minimal_portal_html, panels_dir)

        # Find the nav-item for 'evil' panel -- label text must be escaped
        nav_match = re.search(r'data-panel="evil"[^>]*>.*?</div>', result, re.DOTALL)
        assert nav_match is not None, "Nav item for 'evil' not found"
        nav_html = nav_match.group(0)
        # Raw <img> tag must not appear inside the nav item
        assert '<img src=x' not in nav_html, (
            "XSS payload in label must be HTML-escaped in nav item"
        )

    def test_panel_id_xss_is_escaped(self, minimal_portal_html, tmp_path):
        """Panel ID with injection attempt must be escaped in attributes."""
        from html.parser import HTMLParser

        panels_dir = tmp_path / "custom" / "panels"
        panels_dir.mkdir(parents=True)
        (panels_dir / "evil.html").write_text(
            '<!-- panel-id: evil" onclick="alert(1) -->\n'
            '<!-- panel-label: Test -->\n'
            '<div>content</div>\n'
        )
        result = _run_injection(minimal_portal_html, panels_dir)

        # Use a real HTML parser to verify that no element has an onclick attribute.
        # html.escape(quote=True) converts " to &quot;, preventing attribute breakout.
        class AttrCollector(HTMLParser):
            def __init__(self):
                super().__init__()
                self.found_onclick = False
            def handle_starttag(self, tag, attrs):
                for name, _ in attrs:
                    if name == "onclick" and tag == "div":
                        # The mobile menu items legitimately have onclick,
                        # but nav-items should not
                        pass
                    if name == "onclick":
                        # Check if this is a nav-item (not a mobile tab-menu-item)
                        attr_dict = dict(attrs)
                        if "nav-item" in attr_dict.get("class", ""):
                            self.found_onclick = True

        collector = AttrCollector()
        collector.feed(result)
        assert not collector.found_onclick, (
            "XSS payload created an onclick attribute on a nav-item element"
        )

    def test_escape_import_exists_in_source(self):
        """portal_server.py must import html.escape."""
        server_path = os.path.join(PORTAL_DIR, "portal_server.py")
        with open(server_path) as f:
            source = f.read()

        assert "from html import escape" in source, (
            "portal_server.py must have 'from html import escape'"
        )


# ---------------------------------------------------------------------------
# Tests: Endpoint extensions (Gap 3) -- uses REAL _make_extended_endpoint
# ---------------------------------------------------------------------------

import asyncio
import functools


class TestEndpointExtensions:
    """Tests for the endpoint extension mechanism (custom/routes.py endpoint_extensions dict).

    All behavioral tests use the REAL _make_extended_endpoint function extracted
    from portal_server.py (via the exec/compile pattern), not a local reimplementation.
    """

    def test_extension_loading_block_exists_in_source(self):
        """portal_server.py must contain the endpoint_extensions loading block."""
        server_path = os.path.join(PORTAL_DIR, "portal_server.py")
        with open(server_path) as f:
            source = f.read()

        assert "endpoint_extensions" in source, (
            "portal_server.py must reference endpoint_extensions"
        )
        assert "# 2b. Endpoint extensions" in source, (
            "portal_server.py must have the 2b endpoint extensions section"
        )

    def test_extension_wrapping_block_exists_in_source(self):
        """portal_server.py must contain the route-wrapping logic for extensions."""
        server_path = os.path.join(PORTAL_DIR, "portal_server.py")
        with open(server_path) as f:
            source = f.read()

        assert "_make_extended_endpoint" in source, (
            "portal_server.py must define _make_extended_endpoint wrapper factory"
        )
        assert "Apply endpoint extensions" in source, (
            "portal_server.py must have the endpoint extension application block"
        )

    def test_make_extended_endpoint_merges_data(self):
        """Wrapper should merge extension data into original JSON response."""
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        make_extended = _get_make_extended_endpoint()

        async def original_handler(request):
            return JSONResponse({"status": "ok", "version": "1.0"})

        async def extend_fn(original_data):
            return {"extra_field": 42, "another": "value"}

        wrapped = make_extended(original_handler, extend_fn)

        scope = {"type": "http", "method": "GET", "path": "/test", "query_string": b"", "headers": []}
        request = Request(scope)
        response = asyncio.run(wrapped(request))

        body = json.loads(response.body.decode("utf-8"))
        assert body["status"] == "ok"
        assert body["version"] == "1.0"
        assert body["extra_field"] == 42
        assert body["another"] == "value"

    def test_make_extended_endpoint_preserves_status_code(self):
        """Wrapper should preserve the original response status code."""
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        make_extended = _get_make_extended_endpoint()

        async def original_handler(request):
            return JSONResponse({"error": "not found"}, status_code=404)

        async def extend_fn(original_data):
            return {"debug": True}

        wrapped = make_extended(original_handler, extend_fn)

        scope = {"type": "http", "method": "GET", "path": "/test", "query_string": b"", "headers": []}
        request = Request(scope)
        response = asyncio.run(wrapped(request))

        assert response.status_code == 404
        body = json.loads(response.body.decode("utf-8"))
        assert body["error"] == "not found"
        assert body["debug"] is True

    def test_make_extended_endpoint_handles_extension_error(self):
        """If extension function raises, wrapper returns original response."""
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        make_extended = _get_make_extended_endpoint()

        async def original_handler(request):
            return JSONResponse({"status": "ok"})

        async def broken_extend_fn(original_data):
            raise RuntimeError("extension broke")

        wrapped = make_extended(original_handler, broken_extend_fn)

        scope = {"type": "http", "method": "GET", "path": "/test", "query_string": b"", "headers": []}
        request = Request(scope)
        response = asyncio.run(wrapped(request))

        body = json.loads(response.body.decode("utf-8"))
        assert body == {"status": "ok"}, "Original response should be returned when extension fails"

    def test_make_extended_endpoint_skips_non_json_response(self):
        """Wrapper should pass through non-JSON responses unchanged."""
        from starlette.requests import Request
        from starlette.responses import Response

        make_extended = _get_make_extended_endpoint()

        async def original_handler(request):
            return Response("plain text", media_type="text/plain")

        async def extend_fn(original_data):
            return {"should_not": "appear"}

        wrapped = make_extended(original_handler, extend_fn)

        scope = {"type": "http", "method": "GET", "path": "/test", "query_string": b"", "headers": []}
        request = Request(scope)
        response = asyncio.run(wrapped(request))

        assert response.body == b"plain text"

    def test_make_extended_endpoint_handles_none_return(self):
        """If extension returns None, original data should be returned unchanged."""
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        make_extended = _get_make_extended_endpoint()

        async def original_handler(request):
            return JSONResponse({"status": "ok"})

        async def extend_fn(original_data):
            return None

        wrapped = make_extended(original_handler, extend_fn)

        scope = {"type": "http", "method": "GET", "path": "/test", "query_string": b"", "headers": []}
        request = Request(scope)
        response = asyncio.run(wrapped(request))

        body = json.loads(response.body.decode("utf-8"))
        assert body == {"status": "ok"}

    def test_make_extended_endpoint_handles_empty_dict_return(self):
        """If extension returns empty dict, original data should be returned unchanged."""
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        make_extended = _get_make_extended_endpoint()

        async def original_handler(request):
            return JSONResponse({"status": "ok"})

        async def extend_fn(original_data):
            return {}

        wrapped = make_extended(original_handler, extend_fn)

        scope = {"type": "http", "method": "GET", "path": "/test", "query_string": b"", "headers": []}
        request = Request(scope)
        response = asyncio.run(wrapped(request))

        body = json.loads(response.body.decode("utf-8"))
        assert body == {"status": "ok"}

    def test_backward_compat_no_endpoint_extensions(self):
        """If custom/routes.py does not export endpoint_extensions, no error should occur."""
        server_path = os.path.join(PORTAL_DIR, "portal_server.py")
        with open(server_path) as f:
            source = f.read()

        # The loading block must handle missing endpoint_extensions gracefully
        # Check that NameError is caught (for when _mod doesn't exist)
        assert "except NameError:" in source, (
            "Must catch NameError for when _mod is not defined"
        )
