"""
test_frontend.py -- Structural verification of portal-pb-styled.html

Verifies the HTML portal file is structurally sound: every nav item points
to a real panel div, no inline display styles break JS show/hide, auth tokens
use the standardized key, and switchPanel() references all map to existing panels.

These are STATIC tests — no running server needed. Just the HTML file on disk.
"""

import unittest
import os
import re
import sys
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import HTML_FILE


class PanelExtractor(HTMLParser):
    """Walk the HTML and collect panel-related structural data."""

    def __init__(self):
        super().__init__()
        self.panel_divs = []
        self.nav_panels = []
        self.panel_inline_display = []
        self.all_ids = set()

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        elem_id = attrs_dict.get("id", "")
        if elem_id:
            self.all_ids.add(elem_id)
        if tag == "div":
            if elem_id.startswith("panel-"):
                self.panel_divs.append(elem_id)
                style = attrs_dict.get("style", "")
                if re.search(r"display\s*:", style):
                    self.panel_inline_display.append(elem_id)
        data_panel = attrs_dict.get("data-panel")
        if data_panel:
            self.nav_panels.append(data_panel)


class TestFrontend(unittest.TestCase):
    """Structural verification of the PureBrain portal HTML."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(HTML_FILE):
            raise unittest.SkipTest(f"HTML file not found: {HTML_FILE}")
        with open(HTML_FILE, encoding="utf-8") as f:
            cls.content = f.read()
        cls.extractor = PanelExtractor()
        cls.extractor.feed(cls.content)

    def test_nav_panels_have_matching_divs(self):
        """Every nav item with data-panel='X' must have a div with id='panel-X'."""
        panel_div_set = set(self.extractor.panel_divs)
        unique_nav_panels = sorted(set(self.extractor.nav_panels))
        missing = [p for p in unique_nav_panels if f"panel-{p}" not in panel_div_set]
        self.assertEqual(missing, [],
            f"Nav items reference panels without matching divs: {missing}")

    def test_no_inline_display_on_panels(self):
        """Panel divs must not have inline display: styles (breaks JS show/hide)."""
        self.assertEqual(self.extractor.panel_inline_display, [],
            f"Panel divs with inline display: {self.extractor.panel_inline_display}")

    def test_no_stale_pb_token_references(self):
        """All fetch() calls should use portal_token (not deprecated pb_token)."""
        pb_count = self.content.count("'pb_token'") + self.content.count('"pb_token"')
        self.assertEqual(pb_count, 0,
            f"Found {pb_count} references to pb_token (should be portal_token)")

    def test_portal_token_present(self):
        """portal_token should appear at least once."""
        count = self.content.count("'portal_token'") + self.content.count('"portal_token"')
        self.assertGreater(count, 0, "No portal_token references found")

    def test_switchpanel_references_have_divs(self):
        """Every panel referenced in switchPanel() should have a matching div."""
        match = re.search(
            r"function\s+switchPanel\s*\(panel\)\s*\{(.+?)\n  \}",
            self.content, re.DOTALL)
        if match is None:
            self.skipTest("switchPanel() function not found in HTML")
        panel_refs = re.findall(r"""panel\s*===?\s*['"](\w+)['"]""", match.group(1))
        panel_div_set = set(self.extractor.panel_divs)
        missing = [p for p in sorted(set(panel_refs)) if f"panel-{p}" not in panel_div_set]
        self.assertEqual(missing, [],
            f"switchPanel() references panels without divs: {missing}")

    def test_html_is_parseable(self):
        """HTML file should parse without errors."""
        try:
            HTMLParser().feed(self.content)
        except Exception as exc:
            self.fail(f"HTMLParser.feed() raised: {exc}")

    def test_panel_divs_have_panel_class(self):
        """Every div with id='panel-*' should carry the 'panel' CSS class."""
        class PanelClassChecker(HTMLParser):
            def __init__(self):
                super().__init__()
                self.missing_class = []
            def handle_starttag(self, tag, attrs):
                d = dict(attrs)
                if tag == "div" and d.get("id", "").startswith("panel-"):
                    if "panel" not in d.get("class", "").split():
                        self.missing_class.append(d["id"])
        checker = PanelClassChecker()
        checker.feed(self.content)
        self.assertEqual(checker.missing_class, [],
            f"Panel divs missing 'panel' class: {checker.missing_class}")

    def test_no_orphan_panel_divs(self):
        """Every panel div should be reachable from at least one nav data-panel."""
        nav_set = set(self.extractor.nav_panels)
        orphans = [p for p in self.extractor.panel_divs
                   if p.replace("panel-", "", 1) not in nav_set]
        self.assertEqual(orphans, [],
            f"Panel divs with no nav entry: {orphans}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
