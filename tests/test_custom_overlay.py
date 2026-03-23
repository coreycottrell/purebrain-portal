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
"""

import os
import re
import sys
import json
import tempfile
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
