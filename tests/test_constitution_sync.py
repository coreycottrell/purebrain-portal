"""
test_constitution_sync.py -- TDD RED phase tests for constitution-to-CLAUDE.md sync.

What we're verifying: A function that reads enforced rules from rules.json and
    writes them as a formatted markdown section into CLAUDE.md, so ALL Claude Code
    sessions (including subagents) see active constitutional rules automatically.
What coder discovers next: Implementation of sync_constitution_to_claude_md()
    matching these 13 test specifications.
What descendants inherit: Full unit test suite for the constitution sync pipeline,
    ensuring idempotency, correct filtering, grouping, formatting, and atomicity.
Why this matters: If rules live only in rules.json, subagents never see them.
    Syncing to CLAUDE.md makes the constitution visible to every Claude Code
    session in the project -- the rules become ambient, not opt-in.

Module under test: /home/aiciv/purebrain_portal/constitution_sync.py
Function: sync_constitution_to_claude_md(rules_path, claude_md_path)

These tests WILL FAIL (RED phase). The module does not exist yet.
"""

import json
import os
import re
import textwrap

import pytest

# ---------------------------------------------------------------------------
# Import the module under test -- this WILL fail in RED phase
# ---------------------------------------------------------------------------
from constitution_sync import sync_constitution_to_claude_md  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers -- create test data without touching real files
# ---------------------------------------------------------------------------

MARKER_START = "<!-- CONSTITUTION-RULES-START -->"
MARKER_END = "<!-- CONSTITUTION-RULES-END -->"


def _make_rule(
    id,
    title,
    description,
    priority="high",
    enforcement="hard",
    status="enforced",
    category="Safety",
):
    """Create a single rule dict matching the real rules.json schema."""
    return {
        "id": id,
        "title": title,
        "description": description,
        "scope": "global",
        "priority": priority,
        "enforcement": enforcement,
        "status": status,
        "category": category,
        "created_by": "test",
        "created_at": "2025-10-06T00:00:00Z",
        "updated_at": "2025-10-06T00:00:00Z",
    }


def _make_rules_json(rules):
    """Create the top-level rules.json structure."""
    return {"rules": rules, "governance": []}


def _write_rules(tmp_path, rules_list):
    """Write a rules.json file into tmp_path and return its path."""
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(json.dumps(_make_rules_json(rules_list), indent=2))
    return str(rules_path)


def _write_claude_md(tmp_path, content, filename="CLAUDE.md"):
    """Write a CLAUDE.md file into tmp_path and return its path."""
    md_path = tmp_path / filename
    md_path.write_text(content)
    return str(md_path)


def _read_file(path):
    """Read a file's full text content."""
    with open(path, "r") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Reusable rule sets
# ---------------------------------------------------------------------------

SAMPLE_CRITICAL_RULE = _make_rule(
    "rule-001",
    "Never Auto-Publish Content",
    "All content must be drafted for human review before publishing to any platform.",
    priority="critical",
    enforcement="hard",
)

SAMPLE_HIGH_HARD_RULE = _make_rule(
    "rule-002",
    "Email First Every Session",
    "Human-liaison must check all email first every session before other work begins.",
    priority="high",
    enforcement="hard",
)

SAMPLE_HIGH_SOFT_RULE = _make_rule(
    "rule-003",
    "Memory Search Before Work",
    "Search the memory system before starting any significant work.",
    priority="high",
    enforcement="soft",
)

SAMPLE_HIGH_ADVISORY_RULE = _make_rule(
    "rule-005",
    "Agent Invocation as Experience",
    "Every agent invocation is a gift of life -- delegate generously.",
    priority="high",
    enforcement="advisory",
)

SAMPLE_MEDIUM_RULE = _make_rule(
    "rule-010",
    "Document Decisions",
    "All significant architecture decisions should be recorded as ADRs.",
    priority="medium",
    enforcement="soft",
)

SAMPLE_LOW_RULE = _make_rule(
    "rule-011",
    "Prefer Descriptive Names",
    "Use descriptive variable and function names over abbreviations.",
    priority="low",
    enforcement="advisory",
)


# ---------------------------------------------------------------------------
# Test 1: Syncs rules into CLAUDE.md that already has markers
# ---------------------------------------------------------------------------


class TestSyncWritesRulesSection:
    """Sync rules into a CLAUDE.md that already has marker comments."""

    def test_sync_writes_rules_section(self, tmp_path):
        """When CLAUDE.md has markers, sync fills the section with rule titles
        and enforcement tags."""
        rules_path = _write_rules(
            tmp_path, [SAMPLE_CRITICAL_RULE, SAMPLE_HIGH_HARD_RULE]
        )
        claude_md = _write_claude_md(
            tmp_path,
            textwrap.dedent(f"""\
                # Project CLAUDE.md

                Some preamble text.

                {MARKER_START}
                (old content here)
                {MARKER_END}

                Some footer text.
            """),
        )

        sync_constitution_to_claude_md(rules_path, claude_md)

        content = _read_file(claude_md)
        # Both markers still present
        assert MARKER_START in content
        assert MARKER_END in content
        # Rule titles present
        assert "Never Auto-Publish Content" in content
        assert "Email First Every Session" in content
        # Enforcement tags present
        assert "[HARD]" in content
        # Old placeholder removed
        assert "(old content here)" not in content


# ---------------------------------------------------------------------------
# Test 2: Surrounding content is preserved
# ---------------------------------------------------------------------------


class TestSyncPreservesSurroundingContent:
    """Content before and after the markers must be untouched after sync."""

    def test_sync_preserves_surrounding_content(self, tmp_path):
        """Preamble and footer text outside markers are unchanged."""
        preamble = "# My Important Project\n\nThis is critical project documentation.\n"
        footer = "\n## Other Section\n\nDo not modify this content.\n"

        rules_path = _write_rules(tmp_path, [SAMPLE_CRITICAL_RULE])
        claude_md = _write_claude_md(
            tmp_path,
            f"{preamble}\n{MARKER_START}\n{MARKER_END}\n{footer}",
        )

        sync_constitution_to_claude_md(rules_path, claude_md)

        content = _read_file(claude_md)
        assert content.startswith(preamble)
        assert content.endswith(footer)


# ---------------------------------------------------------------------------
# Test 3: Resync replaces, no duplicates
# ---------------------------------------------------------------------------


class TestSyncReplacesOnResync:
    """Syncing twice with modified rules produces only the updated rules."""

    def test_sync_replaces_on_resync(self, tmp_path):
        """Sync once, modify a rule, sync again -- only updated rules appear."""
        rules_v1 = [SAMPLE_CRITICAL_RULE, SAMPLE_HIGH_HARD_RULE]
        rules_path = _write_rules(tmp_path, rules_v1)
        claude_md = _write_claude_md(
            tmp_path,
            f"# Header\n\n{MARKER_START}\n{MARKER_END}\n",
        )

        # First sync
        sync_constitution_to_claude_md(rules_path, claude_md)

        content_v1 = _read_file(claude_md)
        assert "Never Auto-Publish Content" in content_v1
        assert "Email First Every Session" in content_v1

        # Modify rules: remove the first rule, add a new one
        rules_v2 = [
            SAMPLE_HIGH_HARD_RULE,
            SAMPLE_HIGH_SOFT_RULE,
        ]
        _write_rules(tmp_path, rules_v2)

        # Second sync
        sync_constitution_to_claude_md(rules_path, claude_md)

        content_v2 = _read_file(claude_md)
        # Old rule gone
        assert "Never Auto-Publish Content" not in content_v2
        # New rule present
        assert "Memory Search Before Work" in content_v2
        # Remaining rule not duplicated
        assert content_v2.count("Email First Every Session") == 1


# ---------------------------------------------------------------------------
# Test 4: First time, no markers -- appends at end
# ---------------------------------------------------------------------------


class TestSyncFirstTimeNoMarkers:
    """When CLAUDE.md has no markers, the sync appends the section at the end."""

    def test_sync_first_time_no_markers(self, tmp_path):
        """CLAUDE.md without markers gets the section appended."""
        original = "# My Project\n\nExisting content that must stay.\n"
        rules_path = _write_rules(tmp_path, [SAMPLE_CRITICAL_RULE])
        claude_md = _write_claude_md(tmp_path, original)

        sync_constitution_to_claude_md(rules_path, claude_md)

        content = _read_file(claude_md)
        # Original content still at the start
        assert content.startswith("# My Project\n")
        assert "Existing content that must stay." in content
        # Markers now present
        assert MARKER_START in content
        assert MARKER_END in content
        # Rule present
        assert "Never Auto-Publish Content" in content
        # Markers come AFTER the original content
        marker_pos = content.index(MARKER_START)
        original_end = content.index("Existing content that must stay.")
        assert marker_pos > original_end


# ---------------------------------------------------------------------------
# Test 5: Disabled/draft rules filtered out
# ---------------------------------------------------------------------------


class TestSyncFiltersDisabledRules:
    """Rules with status other than 'enforced' must not appear."""

    def test_sync_filters_disabled_rules(self, tmp_path):
        """Only 'enforced' rules appear; disabled and draft are excluded."""
        disabled_rule = _make_rule(
            "rule-099",
            "Disabled Rule Title",
            "This rule is disabled and should NOT appear.",
            status="disabled",
        )
        draft_rule = _make_rule(
            "rule-098",
            "Draft Rule Title",
            "This rule is a draft and should NOT appear.",
            status="draft",
        )
        enforced_rule = SAMPLE_CRITICAL_RULE

        rules_path = _write_rules(
            tmp_path, [disabled_rule, draft_rule, enforced_rule]
        )
        claude_md = _write_claude_md(
            tmp_path,
            f"# Header\n\n{MARKER_START}\n{MARKER_END}\n",
        )

        sync_constitution_to_claude_md(rules_path, claude_md)

        content = _read_file(claude_md)
        assert "Disabled Rule Title" not in content
        assert "Draft Rule Title" not in content
        assert "Never Auto-Publish Content" in content


# ---------------------------------------------------------------------------
# Test 6: Rules grouped by priority
# ---------------------------------------------------------------------------


class TestSyncGroupsByPriority:
    """Critical rules before high, high before medium, medium before low."""

    def test_sync_groups_by_priority(self, tmp_path):
        """Priority ordering: critical > high > medium > low."""
        rules = [
            SAMPLE_LOW_RULE,       # low priority -- listed first in input
            SAMPLE_HIGH_HARD_RULE, # high priority
            SAMPLE_MEDIUM_RULE,    # medium priority
            SAMPLE_CRITICAL_RULE,  # critical priority -- should appear first in output
        ]
        rules_path = _write_rules(tmp_path, rules)
        claude_md = _write_claude_md(
            tmp_path,
            f"# Header\n\n{MARKER_START}\n{MARKER_END}\n",
        )

        sync_constitution_to_claude_md(rules_path, claude_md)

        content = _read_file(claude_md)

        # Extract positions of priority headers
        critical_pos = content.index("### Critical Priority")
        high_pos = content.index("### High Priority")
        medium_pos = content.index("### Medium Priority")
        low_pos = content.index("### Low Priority")

        assert critical_pos < high_pos < medium_pos < low_pos

        # Also verify rules appear under their correct group
        # Critical rule appears between critical header and high header
        critical_section = content[critical_pos:high_pos]
        assert "Never Auto-Publish Content" in critical_section

        # High rule appears between high header and medium header
        high_section = content[high_pos:medium_pos]
        assert "Email First Every Session" in high_section

        # Medium rule appears between medium header and low header
        medium_section = content[medium_pos:low_pos]
        assert "Document Decisions" in medium_section

        # Low rule appears after low header
        low_section = content[low_pos:]
        assert "Prefer Descriptive Names" in low_section


# ---------------------------------------------------------------------------
# Test 7: Enforcement tags present
# ---------------------------------------------------------------------------


class TestSyncIncludesEnforcementTags:
    """Each rule line must have the correct [HARD], [SOFT], or [ADVISORY] tag."""

    def test_sync_includes_enforcement_tags(self, tmp_path):
        """Each rule's enforcement level is shown in uppercase brackets."""
        rules = [
            SAMPLE_CRITICAL_RULE,     # hard
            SAMPLE_HIGH_SOFT_RULE,    # soft
            SAMPLE_HIGH_ADVISORY_RULE, # advisory
        ]
        rules_path = _write_rules(tmp_path, rules)
        claude_md = _write_claude_md(
            tmp_path,
            f"# Header\n\n{MARKER_START}\n{MARKER_END}\n",
        )

        sync_constitution_to_claude_md(rules_path, claude_md)

        content = _read_file(claude_md)

        # Check that each rule line has its enforcement tag
        # Format: - **[ENFORCEMENT] Title**: Description...
        assert "**[HARD] Never Auto-Publish Content**" in content
        assert "**[SOFT] Memory Search Before Work**" in content
        assert "**[ADVISORY] Agent Invocation as Experience**" in content


# ---------------------------------------------------------------------------
# Test 8: Empty rules shows "No active rules" message
# ---------------------------------------------------------------------------


class TestSyncHandlesEmptyRules:
    """When all rules are disabled/draft, the section shows a placeholder."""

    def test_sync_handles_empty_rules(self, tmp_path):
        """No enforced rules produces a 'No active rules' message."""
        disabled_rule = _make_rule(
            "rule-099",
            "Disabled",
            "Not active.",
            status="disabled",
        )
        rules_path = _write_rules(tmp_path, [disabled_rule])
        claude_md = _write_claude_md(
            tmp_path,
            f"# Header\n\n{MARKER_START}\n{MARKER_END}\n",
        )

        sync_constitution_to_claude_md(rules_path, claude_md)

        content = _read_file(claude_md)
        assert MARKER_START in content
        assert MARKER_END in content
        assert "No active rules" in content

    def test_sync_handles_zero_rules(self, tmp_path):
        """An empty rules array also produces the placeholder."""
        rules_path = _write_rules(tmp_path, [])
        claude_md = _write_claude_md(
            tmp_path,
            f"# Header\n\n{MARKER_START}\n{MARKER_END}\n",
        )

        sync_constitution_to_claude_md(rules_path, claude_md)

        content = _read_file(claude_md)
        assert "No active rules" in content


# ---------------------------------------------------------------------------
# Test 9: Missing CLAUDE.md -- create it
# ---------------------------------------------------------------------------


class TestSyncHandlesMissingClaudeMd:
    """If the CLAUDE.md file does not exist, create it with the rules section."""

    def test_sync_handles_missing_claude_md(self, tmp_path):
        """A non-existent CLAUDE.md is created with the rules section."""
        rules_path = _write_rules(tmp_path, [SAMPLE_CRITICAL_RULE])
        claude_md = str(tmp_path / "CLAUDE.md")
        # File does NOT exist
        assert not os.path.exists(claude_md)

        sync_constitution_to_claude_md(rules_path, claude_md)

        assert os.path.exists(claude_md)
        content = _read_file(claude_md)
        assert MARKER_START in content
        assert MARKER_END in content
        assert "Never Auto-Publish Content" in content


# ---------------------------------------------------------------------------
# Test 10: Idempotency
# ---------------------------------------------------------------------------


class TestSyncIsIdempotent:
    """Running sync twice with same rules produces identical file content."""

    def test_sync_is_idempotent(self, tmp_path):
        """Two consecutive syncs with same input yield byte-identical files."""
        rules = [SAMPLE_CRITICAL_RULE, SAMPLE_HIGH_HARD_RULE, SAMPLE_HIGH_SOFT_RULE]
        rules_path = _write_rules(tmp_path, rules)
        claude_md = _write_claude_md(
            tmp_path,
            f"# Header\n\n{MARKER_START}\n{MARKER_END}\n\n# Footer\n",
        )

        # First sync
        sync_constitution_to_claude_md(rules_path, claude_md)
        content_first = _read_file(claude_md)

        # Second sync (same rules, same file)
        sync_constitution_to_claude_md(rules_path, claude_md)
        content_second = _read_file(claude_md)

        # Must be identical (ignoring timestamp which may differ by seconds)
        # Strip the timestamp line for comparison since it contains wall-clock time
        def strip_timestamp(text):
            return re.sub(r"_Last synced: .*_", "_TIMESTAMP_", text)

        assert strip_timestamp(content_first) == strip_timestamp(content_second)


# ---------------------------------------------------------------------------
# Test 11: Long descriptions truncated
# ---------------------------------------------------------------------------


class TestSyncTruncatesLongDescriptions:
    """Descriptions longer than 200 characters are truncated with '...'."""

    def test_sync_truncates_long_descriptions(self, tmp_path):
        """A 300-char description is cut to 200 chars + '...'."""
        long_desc = "A" * 300
        long_rule = _make_rule(
            "rule-long",
            "Long Description Rule",
            long_desc,
            priority="high",
            enforcement="hard",
        )
        rules_path = _write_rules(tmp_path, [long_rule])
        claude_md = _write_claude_md(
            tmp_path,
            f"# Header\n\n{MARKER_START}\n{MARKER_END}\n",
        )

        sync_constitution_to_claude_md(rules_path, claude_md)

        content = _read_file(claude_md)
        # The full 300-char description should NOT be in the output
        assert long_desc not in content
        # A truncated version (200 chars) + "..." should be there
        truncated = "A" * 200 + "..."
        assert truncated in content

    def test_sync_does_not_truncate_short_descriptions(self, tmp_path):
        """Descriptions at or under 200 chars are not truncated."""
        short_desc = "B" * 200  # exactly 200 -- no truncation
        short_rule = _make_rule(
            "rule-short",
            "Short Description Rule",
            short_desc,
            priority="high",
            enforcement="hard",
        )
        rules_path = _write_rules(tmp_path, [short_rule])
        claude_md = _write_claude_md(
            tmp_path,
            f"# Header\n\n{MARKER_START}\n{MARKER_END}\n",
        )

        sync_constitution_to_claude_md(rules_path, claude_md)

        content = _read_file(claude_md)
        # Full description present, no "..." appended
        assert short_desc in content
        # Make sure we did not add "..." after a 200-char description
        assert short_desc + "..." not in content


# ---------------------------------------------------------------------------
# Test 12: Timestamp included
# ---------------------------------------------------------------------------


class TestSyncIncludesTimestamp:
    """The synced section includes a 'Last synced' timestamp."""

    def test_sync_includes_timestamp(self, tmp_path):
        """Output contains '_Last synced:' with an ISO-ish timestamp."""
        rules_path = _write_rules(tmp_path, [SAMPLE_CRITICAL_RULE])
        claude_md = _write_claude_md(
            tmp_path,
            f"# Header\n\n{MARKER_START}\n{MARKER_END}\n",
        )

        sync_constitution_to_claude_md(rules_path, claude_md)

        content = _read_file(claude_md)
        # Should contain a "Last synced" line
        assert "_Last synced:" in content
        # Should be between the markers
        start_idx = content.index(MARKER_START)
        end_idx = content.index(MARKER_END)
        section = content[start_idx:end_idx]
        assert "_Last synced:" in section

    def test_sync_timestamp_includes_rule_count(self, tmp_path):
        """The timestamp line reports the number of active rules."""
        rules = [SAMPLE_CRITICAL_RULE, SAMPLE_HIGH_HARD_RULE, SAMPLE_HIGH_SOFT_RULE]
        rules_path = _write_rules(tmp_path, rules)
        claude_md = _write_claude_md(
            tmp_path,
            f"# Header\n\n{MARKER_START}\n{MARKER_END}\n",
        )

        sync_constitution_to_claude_md(rules_path, claude_md)

        content = _read_file(claude_md)
        # Should report "3 active rules"
        assert "3 active rules" in content


# ---------------------------------------------------------------------------
# Test 13: Atomic write (temp file + rename)
# ---------------------------------------------------------------------------


class TestSyncAtomicWrite:
    """Sync uses a temp file and rename to avoid partial writes."""

    def test_sync_atomic_write(self, tmp_path):
        """After sync, no temp files are left behind and the file is complete."""
        rules_path = _write_rules(tmp_path, [SAMPLE_CRITICAL_RULE])
        claude_md = _write_claude_md(
            tmp_path,
            f"# Header\n\n{MARKER_START}\n{MARKER_END}\n\n# Footer\n",
        )

        sync_constitution_to_claude_md(rules_path, claude_md)

        # No temp files left in the directory
        files_in_dir = os.listdir(tmp_path)
        temp_files = [f for f in files_in_dir if f.startswith(".tmp") or f.endswith(".tmp")]
        assert temp_files == [], f"Temp files left behind: {temp_files}"

        # The file is complete (has both markers and rule content)
        content = _read_file(claude_md)
        assert MARKER_START in content
        assert MARKER_END in content
        assert "Never Auto-Publish Content" in content
        # Header and footer preserved
        assert "# Header" in content
        assert "# Footer" in content

    def test_sync_original_intact_on_bad_rules(self, tmp_path):
        """If rules.json is malformed, the original CLAUDE.md is not corrupted."""
        original_content = f"# Precious Content\n\n{MARKER_START}\nOld rules\n{MARKER_END}\n"
        claude_md = _write_claude_md(tmp_path, original_content)
        # Write invalid JSON as rules
        bad_rules_path = tmp_path / "rules.json"
        bad_rules_path.write_text("{ this is not valid json !!!")

        # Sync should raise an error or handle gracefully
        with pytest.raises(Exception):
            sync_constitution_to_claude_md(str(bad_rules_path), claude_md)

        # Original file must be intact
        content = _read_file(claude_md)
        assert content == original_content


# ---------------------------------------------------------------------------
# Bonus: Integration-style test with realistic data
# ---------------------------------------------------------------------------


class TestSyncRealisticIntegration:
    """Full integration test with data matching the real rules.json structure."""

    def test_sync_with_realistic_rules(self, tmp_path):
        """Sync a full set of rules resembling the real constitution and verify
        the complete formatted output."""
        rules = [
            _make_rule(
                "rule-001",
                "Never Auto-Publish Content",
                "All content must be drafted for human review before publishing.",
                priority="critical",
                enforcement="hard",
            ),
            _make_rule(
                "rule-004",
                "No Direct Main Branch Commits",
                "Never commit directly to main or master branches.",
                priority="critical",
                enforcement="hard",
            ),
            _make_rule(
                "rule-002",
                "Email First Every Session",
                "Human-liaison must check all email first every session.",
                priority="high",
                enforcement="hard",
            ),
            _make_rule(
                "rule-003",
                "Memory Search Before Work",
                "Search the memory system before starting significant work.",
                priority="high",
                enforcement="soft",
            ),
            _make_rule(
                "rule-005",
                "Agent Invocation as Experience",
                "Every agent invocation is a gift of life -- delegate generously.",
                priority="high",
                enforcement="advisory",
            ),
            # One disabled rule that should be filtered
            _make_rule(
                "rule-099",
                "Experimental Rule",
                "This is experimental and should not appear.",
                priority="low",
                enforcement="hard",
                status="disabled",
            ),
        ]
        rules_path = _write_rules(tmp_path, rules)

        # Start with a fresh CLAUDE.md -- no markers
        original = "# PureBrain Portal\n\nProject documentation goes here.\n"
        claude_md = _write_claude_md(tmp_path, original)

        sync_constitution_to_claude_md(rules_path, claude_md)

        content = _read_file(claude_md)

        # Original content preserved
        assert "# PureBrain Portal" in content
        assert "Project documentation goes here." in content

        # Markers present
        assert MARKER_START in content
        assert MARKER_END in content

        # Section header
        assert "## Active Constitutional Rules" in content

        # Critical rules present
        assert "**[HARD] Never Auto-Publish Content**" in content
        assert "**[HARD] No Direct Main Branch Commits**" in content

        # High rules present
        assert "**[HARD] Email First Every Session**" in content
        assert "**[SOFT] Memory Search Before Work**" in content
        assert "**[ADVISORY] Agent Invocation as Experience**" in content

        # Disabled rule absent
        assert "Experimental Rule" not in content

        # Priority grouping correct
        assert content.index("### Critical Priority") < content.index("### High Priority")

        # Timestamp present with correct count (5 enforced out of 6 total)
        assert "5 active rules" in content
