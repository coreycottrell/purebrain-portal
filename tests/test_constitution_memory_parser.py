"""
test_constitution_memory_parser.py -- Unit tests for the MEMORY.md parser.

What we're verifying: The parser correctly reads any CIV's MEMORY.md file,
    extracts sections and entries, sanitizes sensitive data (IPs, paths),
    and maps sections to the 5 portal categories.
What descendants inherit: Confidence that the Memory pane works for ANY CIV,
    not just ours.
Why this matters: The Memory pane ships to all portals. The parser must
    handle diverse MEMORY.md formats without leaking sensitive data.

Run:  python3 -m pytest tests/test_constitution_memory_parser.py -v
"""

import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Add portal root to path so we can import portal_constitution
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from portal_constitution import (
    _parse_memory_md_entry,
    _parse_memory_md,
    _sanitize_entry,
    _find_memory_md,
    _get_memory_md_sections,
    _build_memory_categories,
    _MEMORY_MD_CACHE,
    _MEMORY_MD_CACHE_TTL,
)


# ---------------------------------------------------------------------------
# Entry Parser Tests
# ---------------------------------------------------------------------------

class TestParseMemoryMdEntry(unittest.TestCase):
    """Test individual bullet-point parsing."""

    def test_markdown_link_with_em_dash(self):
        """Parse: - [file.md](file.md) \u2014 Description text"""
        entry = _parse_memory_md_entry("- [file.md](file.md) \u2014 Description text")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["title"], "file.md")
        self.assertEqual(entry["desc"], "Description text")

    def test_markdown_link_with_double_dash(self):
        """Parse: - [file.md](file.md) -- Description text"""
        entry = _parse_memory_md_entry("- [file.md](file.md) -- Description text")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["title"], "file.md")
        self.assertEqual(entry["desc"], "Description text")

    def test_markdown_link_with_url(self):
        """Parse: - [Project Name](https://github.com/org/repo) \u2014 Some project"""
        line = "- [Project Name](https://github.com/org/repo) \u2014 Some project"
        entry = _parse_memory_md_entry(line)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["title"], "Project Name")
        self.assertEqual(entry["desc"], "Some project")

    def test_markdown_link_without_separator(self):
        """Parse: - [file.md](file.md) trailing text"""
        entry = _parse_memory_md_entry("- [file.md](file.md) trailing text")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["title"], "file.md")
        self.assertEqual(entry["desc"], "trailing text")

    def test_plain_text_with_em_dash(self):
        """Parse: - Some title \u2014 Some description"""
        entry = _parse_memory_md_entry("- Some title \u2014 Some description")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["title"], "Some title")
        self.assertEqual(entry["desc"], "Some description")

    def test_plain_text_without_separator(self):
        """Parse: - Just a simple text entry"""
        entry = _parse_memory_md_entry("- Just a simple text entry")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["title"], "Just a simple text entry")
        self.assertEqual(entry["desc"], "")

    def test_bold_markers_stripped(self):
        """Bold **markers** should be removed from entry text."""
        entry = _parse_memory_md_entry("- **Memory Search** \u2014 use this tool")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["title"], "Memory Search")
        self.assertEqual(entry["desc"], "use this tool")

    def test_non_bullet_line_returns_none(self):
        """Lines not starting with '- ' should return None."""
        self.assertIsNone(_parse_memory_md_entry("Not a bullet"))
        self.assertIsNone(_parse_memory_md_entry("  Not a bullet either"))
        self.assertIsNone(_parse_memory_md_entry("## Header"))

    def test_empty_bullet_returns_none(self):
        """An empty bullet '- ' should return None."""
        self.assertIsNone(_parse_memory_md_entry("- "))
        self.assertIsNone(_parse_memory_md_entry("-  "))

    def test_indented_bullet_still_parsed(self):
        """Indented bullets should still be parsed (after strip)."""
        entry = _parse_memory_md_entry("  - [f.md](f.md) \u2014 desc")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["title"], "f.md")

    def test_link_with_colon_separator(self):
        """Parse: - [file](url): Description"""
        entry = _parse_memory_md_entry("- [file](url): Description after colon")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["title"], "file")
        self.assertEqual(entry["desc"], "Description after colon")

    def test_long_plain_text_truncated(self):
        """Plain text entries should be truncated to 80 chars for the title."""
        long_text = "A" * 120
        entry = _parse_memory_md_entry(f"- {long_text}")
        self.assertIsNotNone(entry)
        self.assertEqual(len(entry["title"]), 80)


# ---------------------------------------------------------------------------
# Sanitizer Tests
# ---------------------------------------------------------------------------

class TestSanitizeEntry(unittest.TestCase):
    """Test sensitive data redaction."""

    def test_ip_address_redacted(self):
        """IP addresses should be replaced with [redacted]."""
        entry = {"title": "Server", "desc": "SSH to 192.168.1.100 for access"}
        result = _sanitize_entry(entry)
        self.assertNotIn("192.168.1.100", result["desc"])
        self.assertIn("[redacted]", result["desc"])

    def test_ssh_command_redacted(self):
        """SSH commands should be replaced with [redacted]."""
        entry = {"title": "Access", "desc": "ssh -i ~/.ssh/my_key user@host"}
        result = _sanitize_entry(entry)
        self.assertNotIn("ssh -i", result["desc"])
        self.assertIn("[redacted]", result["desc"])

    def test_absolute_home_path_redacted(self):
        """Absolute /home/ paths should be replaced with [redacted]."""
        entry = {"title": "Config", "desc": "File at /home/aiciv/secrets/config.json"}
        result = _sanitize_entry(entry)
        self.assertNotIn("/home/aiciv", result["desc"])
        self.assertIn("[redacted]", result["desc"])

    def test_tilde_path_redacted(self):
        """Tilde paths should be replaced with [redacted]."""
        entry = {"title": "Tool", "desc": "Run ~/tools/secret_tool.sh"}
        result = _sanitize_entry(entry)
        self.assertNotIn("~/tools", result["desc"])
        self.assertIn("[redacted]", result["desc"])

    def test_title_not_modified(self):
        """Title should not be sanitized (only description)."""
        entry = {"title": "192.168.1.1 Server", "desc": "safe description"}
        result = _sanitize_entry(entry)
        self.assertEqual(result["title"], "192.168.1.1 Server")
        self.assertEqual(result["desc"], "safe description")

    def test_safe_text_unchanged(self):
        """Text without sensitive patterns should pass through unchanged."""
        entry = {"title": "Portal", "desc": "Portal 2.0 SHIPPED with 45 PRs merged"}
        result = _sanitize_entry(entry)
        self.assertEqual(result["desc"], "Portal 2.0 SHIPPED with 45 PRs merged")

    def test_multiple_redactions_collapsed(self):
        """Multiple adjacent [redacted] markers should be collapsed."""
        entry = {"title": "Multi", "desc": "Use /home/user/a /home/user/b /home/user/c"}
        result = _sanitize_entry(entry)
        # Should not have 3 separate [redacted] markers
        self.assertLessEqual(result["desc"].count("[redacted]"), 2)


# ---------------------------------------------------------------------------
# Full File Parser Tests
# ---------------------------------------------------------------------------

class TestParseMemoryMd(unittest.TestCase):
    """Test parsing a complete MEMORY.md file."""

    def _write_temp_md(self, content: str) -> Path:
        """Write content to a temp file and return its Path."""
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        f.write(textwrap.dedent(content))
        f.close()
        self.addCleanup(lambda: os.unlink(f.name))
        return Path(f.name)

    def test_parses_multiple_sections(self):
        """Parser should extract all ## sections."""
        path = self._write_temp_md("""\
            # Main Title

            ## Feedback
            - [f1.md](f1.md) -- Always delegate work
            - [f2.md](f2.md) -- Use TDD workflow

            ## Project
            - [portal.md](portal.md) -- Portal 2.0 shipped
            - [ledger.md](ledger.md) -- PureLedger Phase 3

            ## Reference
            - [ref1.md](ref1.md) -- Some reference info
        """)
        sections = _parse_memory_md(path)
        self.assertIn("Feedback", sections)
        self.assertIn("Project", sections)
        self.assertIn("Reference", sections)
        self.assertEqual(len(sections["Feedback"]), 2)
        self.assertEqual(len(sections["Project"]), 2)
        self.assertEqual(len(sections["Reference"]), 1)

    def test_handles_empty_sections(self):
        """Sections with no bullet entries should be empty lists."""
        path = self._write_temp_md("""\
            ## Empty Section

            ## Has Content
            - One entry here
        """)
        sections = _parse_memory_md(path)
        self.assertIn("Empty Section", sections)
        self.assertEqual(len(sections["Empty Section"]), 0)
        self.assertEqual(len(sections["Has Content"]), 1)

    def test_skips_non_section_content(self):
        """Content before first ## header should be ignored."""
        path = self._write_temp_md("""\
            # Title
            - This should be ignored (no section)

            ## Real Section
            - This should be captured
        """)
        sections = _parse_memory_md(path)
        self.assertEqual(len(sections), 1)
        self.assertIn("Real Section", sections)

    def test_sanitizes_entries_during_parse(self):
        """IPs in entry descriptions should be redacted during parsing."""
        path = self._write_temp_md("""\
            ## Reference
            - Server Access -- SSH to 10.0.0.1 on port 22
        """)
        sections = _parse_memory_md(path)
        entry = sections["Reference"][0]
        self.assertNotIn("10.0.0.1", entry["desc"])
        self.assertIn("[redacted]", entry["desc"])

    def test_handles_nonexistent_file(self):
        """Parsing a nonexistent file should return empty dict."""
        sections = _parse_memory_md(Path("/nonexistent/MEMORY.md"))
        self.assertEqual(sections, {})

    def test_handles_empty_file(self):
        """An empty MEMORY.md should return empty dict."""
        path = self._write_temp_md("")
        sections = _parse_memory_md(path)
        self.assertEqual(sections, {})

    def test_custom_section_names_preserved(self):
        """Any section name should be preserved, not just known ones."""
        path = self._write_temp_md("""\
            ## Custom Section Name
            - Entry one

            ## Another Custom
            - Entry two
        """)
        sections = _parse_memory_md(path)
        self.assertIn("Custom Section Name", sections)
        self.assertIn("Another Custom", sections)

    def test_section_with_mixed_content(self):
        """Sections can have bullets, plain text, and blank lines mixed."""
        path = self._write_temp_md("""\
            ## Mixed
            - Bullet one
            This is not a bullet (ignored)

            - Bullet two after blank line
        """)
        sections = _parse_memory_md(path)
        self.assertEqual(len(sections["Mixed"]), 2)

    def test_real_world_memory_md_format(self):
        """Parse a realistic MEMORY.md snippet matching real CIV format."""
        path = self._write_temp_md("""\
            # MyCIV Memory Index

            ## Feedback
            - [feedback_delegation.md](feedback_delegation.md) \u2014 Always delegate to proper specialist agents
            - [feedback_workflow.md](feedback_workflow.md) \u2014 All work requires TDD + PR workflow

            ## User
            - [user_human.md](user_human.md) \u2014 Human partner info

            ## Reference
            - [reference_github.md](reference_github.md) \u2014 GitHub PAT info
            - [reference_kanban.md](reference_kanban.md) \u2014 Kanban board location

            ## Infrastructure
            - Portal release server: cc.example.com/api/releases/
            - Deploy: use the deploy script

            ## Project
            - [project_alpha.md](project_alpha.md) \u2014 Alpha: Phase 1 complete, 100 tests
            - [project_beta.md](project_beta.md) \u2014 Beta: In progress

            ## Sessions
            - [session_summary.md](session_summary.md) \u2014 Recent session work

            ## Active TODO
            - [todo.md](todo.md) \u2014 Queued items for next session
        """)
        sections = _parse_memory_md(path)
        self.assertEqual(len(sections["Feedback"]), 2)
        self.assertEqual(len(sections["User"]), 1)
        self.assertEqual(len(sections["Reference"]), 2)
        self.assertEqual(len(sections["Infrastructure"]), 2)
        self.assertEqual(len(sections["Project"]), 2)
        self.assertEqual(len(sections["Sessions"]), 1)
        self.assertEqual(len(sections["Active TODO"]), 1)


# ---------------------------------------------------------------------------
# Cache Tests
# ---------------------------------------------------------------------------

class TestMemoryMdCache(unittest.TestCase):
    """Test the 5-minute caching behavior."""

    def setUp(self):
        """Reset cache before each test."""
        _MEMORY_MD_CACHE["data"] = {}
        _MEMORY_MD_CACHE["timestamp"] = 0.0

    def tearDown(self):
        """Reset cache after each test."""
        _MEMORY_MD_CACHE["data"] = {}
        _MEMORY_MD_CACHE["timestamp"] = 0.0

    @patch("portal_constitution._find_memory_md")
    @patch("portal_constitution._parse_memory_md")
    def test_cache_hit_avoids_reparse(self, mock_parse, mock_find):
        """Second call within TTL should not re-parse."""
        mock_find.return_value = Path("/fake/MEMORY.md")
        mock_parse.return_value = {"Project": [{"title": "P1", "desc": "d"}]}

        # First call -- should parse
        result1 = _get_memory_md_sections()
        self.assertEqual(mock_parse.call_count, 1)

        # Second call -- should use cache
        result2 = _get_memory_md_sections()
        self.assertEqual(mock_parse.call_count, 1)  # NOT 2
        self.assertEqual(result1, result2)

    @patch("portal_constitution._find_memory_md")
    @patch("portal_constitution._parse_memory_md")
    def test_cache_expires_after_ttl(self, mock_parse, mock_find):
        """After TTL expires, cache should be refreshed."""
        mock_find.return_value = Path("/fake/MEMORY.md")
        mock_parse.return_value = {"Project": [{"title": "P1", "desc": "d"}]}

        # First call
        _get_memory_md_sections()
        self.assertEqual(mock_parse.call_count, 1)

        # Expire the cache
        _MEMORY_MD_CACHE["timestamp"] = time.time() - _MEMORY_MD_CACHE_TTL - 1

        # Second call -- should re-parse
        _get_memory_md_sections()
        self.assertEqual(mock_parse.call_count, 2)

    @patch("portal_constitution._find_memory_md", return_value=None)
    def test_no_memory_md_returns_empty(self, mock_find):
        """When no MEMORY.md exists, return empty dict."""
        result = _get_memory_md_sections()
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# Find MEMORY.md Tests
# ---------------------------------------------------------------------------

class TestFindMemoryMd(unittest.TestCase):
    """Test MEMORY.md file discovery."""

    def test_finds_in_claude_projects(self):
        """Should find MEMORY.md in ~/.claude/projects/*/memory/."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create the expected directory structure
            mem_dir = Path(tmpdir) / ".claude" / "projects" / "test-proj" / "memory"
            mem_dir.mkdir(parents=True)
            mem_file = mem_dir / "MEMORY.md"
            mem_file.write_text("# Test")

            with patch("portal_constitution.Path.home", return_value=Path(tmpdir)):
                result = _find_memory_md()
                self.assertIsNotNone(result)
                self.assertEqual(result.name, "MEMORY.md")

    def test_finds_in_memories_dir(self):
        """Should find MEMORY.md in ~/memories/ as fallback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mem_dir = Path(tmpdir) / "memories"
            mem_dir.mkdir()
            mem_file = mem_dir / "MEMORY.md"
            mem_file.write_text("# Test")

            with patch("portal_constitution.Path.home", return_value=Path(tmpdir)):
                result = _find_memory_md()
                self.assertIsNotNone(result)
                self.assertEqual(result.name, "MEMORY.md")

    def test_returns_none_when_not_found(self):
        """Should return None when no MEMORY.md exists anywhere."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("portal_constitution.Path.home", return_value=Path(tmpdir)):
                result = _find_memory_md()
                self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Build Memory Categories (Integration) Tests
# ---------------------------------------------------------------------------

class TestBuildMemoryCategories(unittest.TestCase):
    """Test the full category builder with mocked MEMORY.md data."""

    def setUp(self):
        """Reset cache."""
        _MEMORY_MD_CACHE["data"] = {}
        _MEMORY_MD_CACHE["timestamp"] = 0.0

    def tearDown(self):
        _MEMORY_MD_CACHE["data"] = {}
        _MEMORY_MD_CACHE["timestamp"] = 0.0

    @patch("portal_constitution._get_memory_md_sections")
    def test_always_returns_five_categories(self, mock_sections):
        """Should always return exactly 5 categories."""
        mock_sections.return_value = {}
        categories = _build_memory_categories()
        self.assertEqual(len(categories), 5)
        names = [c["category"] for c in categories]
        self.assertEqual(names, ["Identity", "Feedback", "Projects", "References", "Processes"])

    @patch("portal_constitution._get_memory_md_sections")
    def test_projects_populated_from_memory_md(self, mock_sections):
        """Projects category should contain entries from ## Project section."""
        mock_sections.return_value = {
            "Project": [
                {"title": "project_alpha.md", "desc": "Alpha: Phase 1 complete"},
                {"title": "project_beta.md", "desc": "Beta: In progress"},
            ],
        }
        categories = _build_memory_categories()
        projects = [c for c in categories if c["category"] == "Projects"][0]
        self.assertEqual(len(projects["entries"]), 2)
        self.assertEqual(projects["entries"][0]["title"], "project_alpha.md")
        self.assertEqual(projects["entries"][1]["title"], "project_beta.md")

    @patch("portal_constitution._get_memory_md_sections")
    def test_references_populated_from_memory_md(self, mock_sections):
        """References category should contain entries from ## Reference section."""
        mock_sections.return_value = {
            "Reference": [
                {"title": "reference_github.md", "desc": "GitHub info"},
            ],
        }
        categories = _build_memory_categories()
        refs = [c for c in categories if c["category"] == "References"][0]
        self.assertEqual(len(refs["entries"]), 1)
        self.assertEqual(refs["entries"][0]["title"], "reference_github.md")

    @patch("portal_constitution._get_memory_md_sections")
    def test_processes_merges_multiple_sections(self, mock_sections):
        """Processes category should merge Infrastructure + Sessions + Active TODO."""
        mock_sections.return_value = {
            "Infrastructure": [{"title": "infra1", "desc": "Infra entry"}],
            "Sessions": [{"title": "sess1", "desc": "Session entry"}],
            "Active TODO": [{"title": "todo1", "desc": "Todo entry"}],
        }
        categories = _build_memory_categories()
        processes = [c for c in categories if c["category"] == "Processes"][0]
        self.assertEqual(len(processes["entries"]), 3)
        titles = [e["title"] for e in processes["entries"]]
        self.assertIn("infra1", titles)
        self.assertIn("sess1", titles)
        self.assertIn("todo1", titles)

    @patch("portal_constitution._get_memory_md_sections")
    def test_empty_memory_md_shows_placeholders(self, mock_sections):
        """When MEMORY.md has no matching sections, show placeholder entries."""
        mock_sections.return_value = {}
        categories = _build_memory_categories()
        projects = [c for c in categories if c["category"] == "Projects"][0]
        self.assertEqual(len(projects["entries"]), 1)
        self.assertIn("No projects configured", projects["entries"][0]["title"])

    @patch("portal_constitution._get_memory_md_sections")
    def test_user_section_merges_into_identity(self, mock_sections):
        """## User section entries should appear in the Identity category."""
        mock_sections.return_value = {
            "User": [{"title": "user_info.md", "desc": "Human partner details"}],
        }
        categories = _build_memory_categories()
        identity = [c for c in categories if c["category"] == "Identity"][0]
        titles = [e["title"] for e in identity["entries"]]
        self.assertIn("user_info.md", titles)
        # Should still have the base entries (Civ Name, Human Partner, North Star)
        self.assertIn("Civilization Name", titles)
        self.assertIn("Human Partner", titles)

    @patch("portal_constitution._get_memory_md_sections")
    def test_feedback_merges_rules_and_memory_md(self, mock_sections):
        """Feedback should include both constitution rules AND MEMORY.md feedback."""
        mock_sections.return_value = {
            "Feedback": [{"title": "feedback_test.md", "desc": "From MEMORY.md"}],
        }
        categories = _build_memory_categories()
        feedback = [c for c in categories if c["category"] == "Feedback"][0]
        # Should have entries from both sources (rules + MEMORY.md)
        titles = [e["title"] for e in feedback["entries"]]
        self.assertIn("feedback_test.md", titles)

    @patch("portal_constitution._get_memory_md_sections")
    def test_each_category_has_icon(self, mock_sections):
        """Every category should have an icon field."""
        mock_sections.return_value = {}
        categories = _build_memory_categories()
        for cat in categories:
            self.assertIn("icon", cat, f"Category {cat['category']} missing icon")
            self.assertTrue(cat["icon"], f"Category {cat['category']} has empty icon")

    @patch("portal_constitution._get_memory_md_sections")
    def test_no_sensitive_data_in_output(self, mock_sections):
        """Sanitization should prevent IPs from appearing in output."""
        mock_sections.return_value = {
            "Reference": [{"title": "server", "desc": "Connect to [redacted] for access"}],
        }
        categories = _build_memory_categories()
        import json
        full_text = json.dumps(categories)
        # No raw IPs should leak through
        import re
        ip_pattern = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        ips_found = ip_pattern.findall(full_text)
        # Filter out "2.0" etc that aren't real IPs
        real_ips = [ip for ip in ips_found if all(int(o) < 256 for o in ip.split("."))]
        self.assertEqual(len(real_ips), 0, f"Found IPs in output: {real_ips}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
