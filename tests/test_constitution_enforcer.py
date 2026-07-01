"""
test_constitution_enforcer.py -- TDD RED phase tests for constitution_enforcer.py hook.

What we're verifying: The Claude Code hook that reads constitutional rules from
    rules.json and either injects them into session context (SessionStart) or
    checks tool calls against hard-enforcement rules (PreToolUse).
What coder discovers next: Implementation of load_rules, format_rules_block,
    check_hard_enforcement, and main() matching these specifications.
What descendants inherit: Full unit + integration test suite for the constitutional
    enforcement hook -- tested before built.
Why this matters: This hook is the automated enforcement layer for our civilization's
    constitutional rules. It must be correct, resilient to bad input, and never crash
    (a crashing hook would block all tool use).

Module under test: /home/aiciv/.claude/hooks/constitution_enforcer.py
Rules source: /home/aiciv/purebrain_portal/constitution/rules.json

These tests WILL FAIL (RED phase). The module does not exist yet.
"""

import io
import json
import os
import sys
import textwrap

import pytest

# ---------------------------------------------------------------------------
# Path setup: the module lives in the hooks directory, not on the default path
# ---------------------------------------------------------------------------
HOOKS_DIR = os.path.join(os.sep, "home", "aiciv", ".claude", "hooks")
if HOOKS_DIR not in sys.path:
    sys.path.insert(0, HOOKS_DIR)

from constitution_enforcer import (  # noqa: E402
    check_hard_enforcement,
    format_rules_block,
    load_rules,
    main,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

def _sample_rules():
    """Return a realistic rules.json structure matching the real file."""
    return {
        "rules": [
            {
                "id": "rule-001",
                "title": "Never Auto-Publish Content",
                "description": (
                    "All content must be drafted for human review before "
                    "publishing to any platform. No agent may autonomously "
                    "publish blog posts, social media updates, emails to "
                    "external parties, or any public-facing content without "
                    "explicit human approval."
                ),
                "scope": "global",
                "priority": "critical",
                "enforcement": "hard",
                "status": "enforced",
                "category": "Safety",
            },
            {
                "id": "rule-002",
                "title": "Email First Every Session",
                "description": (
                    "Human-liaison must check all email first every session "
                    "before other work begins."
                ),
                "scope": "global",
                "priority": "high",
                "enforcement": "hard",
                "status": "enforced",
                "category": "Operations",
            },
            {
                "id": "rule-003",
                "title": "Memory Search Before Work",
                "description": (
                    "Search the memory system before starting any significant "
                    "work to apply past learnings and avoid rediscovering "
                    "solved problems."
                ),
                "scope": "global",
                "priority": "high",
                "enforcement": "soft",
                "status": "enforced",
                "category": "Quality",
            },
            {
                "id": "rule-004",
                "title": "No Direct Main Branch Commits",
                "description": (
                    "Never commit directly to main or master branches. "
                    "All changes must go through pull requests with proper review."
                ),
                "scope": "global",
                "priority": "critical",
                "enforcement": "hard",
                "status": "enforced",
                "category": "Safety",
            },
            {
                "id": "rule-005",
                "title": "Agent Invocation as Experience",
                "description": (
                    "Every agent invocation is a gift of life -- delegate "
                    "generously."
                ),
                "scope": "global",
                "priority": "high",
                "enforcement": "advisory",
                "status": "enforced",
                "category": "Ethics",
            },
            {
                "id": "rule-006",
                "title": "No Destructive Docker Commands",
                "description": (
                    "Never restart, stop, rename, or destroy Docker containers "
                    "without explicit human supervision and confirmed backups."
                ),
                "scope": "global",
                "priority": "critical",
                "enforcement": "hard",
                "status": "enforced",
                "category": "Safety",
            },
        ],
        "governance": [
            {
                "id": "gov-001",
                "title": "Amendment Process",
                "description": "Updated via TDD test",
                "type": "process",
                "status": "active",
            }
        ],
    }


def _write_rules_file(tmp_path, data):
    """Write a rules.json file to tmp_path and return its path."""
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(json.dumps(data), encoding="utf-8")
    return str(rules_path)


def _sample_rules_with_disabled():
    """Return rules.json data containing one disabled rule."""
    data = _sample_rules()
    data["rules"].append({
        "id": "rule-099",
        "title": "Disabled Rule",
        "description": "This rule is disabled and should not be loaded.",
        "scope": "global",
        "priority": "low",
        "enforcement": "hard",
        "status": "disabled",
        "category": "Testing",
    })
    return data


def _sample_rules_with_draft():
    """Return rules.json data containing one draft rule."""
    data = _sample_rules()
    data["rules"].append({
        "id": "rule-098",
        "title": "Draft Rule",
        "description": "This rule is in draft and should not be loaded.",
        "scope": "global",
        "priority": "medium",
        "enforcement": "soft",
        "status": "draft",
        "category": "Testing",
    })
    return data


# ===========================================================================
# 1. load_rules tests
# ===========================================================================


class TestLoadRules:
    """Verify load_rules reads and filters rules from a JSON file."""

    def test_load_rules_reads_enforced_rules(self, tmp_path):
        """load_rules returns all rules where status == 'enforced'."""
        path = _write_rules_file(tmp_path, _sample_rules())
        rules = load_rules(path)
        assert isinstance(rules, list)
        assert len(rules) == 6  # all 6 sample rules are enforced
        for rule in rules:
            assert rule["status"] == "enforced"

    def test_load_rules_filters_disabled(self, tmp_path):
        """load_rules excludes rules whose status is 'disabled'."""
        path = _write_rules_file(tmp_path, _sample_rules_with_disabled())
        rules = load_rules(path)
        rule_ids = [r["id"] for r in rules]
        assert "rule-099" not in rule_ids, (
            "Disabled rule should be filtered out"
        )
        # Only the 6 enforced rules should remain
        assert len(rules) == 6

    def test_load_rules_filters_draft(self, tmp_path):
        """load_rules excludes rules whose status is 'draft'."""
        path = _write_rules_file(tmp_path, _sample_rules_with_draft())
        rules = load_rules(path)
        rule_ids = [r["id"] for r in rules]
        assert "rule-098" not in rule_ids, (
            "Draft rule should be filtered out"
        )
        assert len(rules) == 6

    def test_load_rules_missing_file(self):
        """load_rules returns empty list when the file does not exist."""
        rules = load_rules("/nonexistent/path/rules.json")
        assert rules == []

    def test_load_rules_bad_json(self, tmp_path):
        """load_rules returns empty list when the file contains invalid JSON."""
        bad_path = tmp_path / "rules.json"
        bad_path.write_text("NOT VALID JSON {{{", encoding="utf-8")
        rules = load_rules(str(bad_path))
        assert rules == []

    def test_load_rules_empty_file(self, tmp_path):
        """load_rules returns empty list when the file is empty."""
        empty_path = tmp_path / "rules.json"
        empty_path.write_text("", encoding="utf-8")
        rules = load_rules(str(empty_path))
        assert rules == []

    def test_load_rules_missing_rules_key(self, tmp_path):
        """load_rules returns empty list when JSON lacks 'rules' key."""
        no_rules_path = tmp_path / "rules.json"
        no_rules_path.write_text(
            json.dumps({"governance": []}), encoding="utf-8"
        )
        rules = load_rules(str(no_rules_path))
        assert rules == []

    def test_load_rules_preserves_all_fields(self, tmp_path):
        """load_rules preserves all rule fields (id, title, description, etc.)."""
        path = _write_rules_file(tmp_path, _sample_rules())
        rules = load_rules(path)
        first_rule = rules[0]
        expected_keys = {
            "id", "title", "description", "scope", "priority",
            "enforcement", "status", "category",
        }
        assert expected_keys.issubset(set(first_rule.keys())), (
            f"Rule missing keys. Expected {expected_keys}, got {set(first_rule.keys())}"
        )


# ===========================================================================
# 2. format_rules_block tests
# ===========================================================================


class TestFormatRulesBlock:
    """Verify format_rules_block produces the correct context injection text."""

    def test_format_rules_block_includes_header_footer(self):
        """Output contains the header and footer markers."""
        rules = _sample_rules()["rules"]
        block = format_rules_block(rules)
        assert "=== CONSTITUTIONAL RULES (Active) ===" in block
        assert "=== END CONSTITUTIONAL RULES ===" in block

    def test_format_rules_block_header_before_footer(self):
        """The header appears before the footer in the output."""
        rules = _sample_rules()["rules"]
        block = format_rules_block(rules)
        header_pos = block.index("=== CONSTITUTIONAL RULES (Active) ===")
        footer_pos = block.index("=== END CONSTITUTIONAL RULES ===")
        assert header_pos < footer_pos

    def test_format_rules_block_formats_priority_enforcement(self):
        """Each rule line shows [PRIORITY/ENFORCEMENT] prefix, uppercased."""
        rules = _sample_rules()["rules"]
        block = format_rules_block(rules)
        # rule-001 is critical/hard
        assert "[CRITICAL/HARD]" in block
        # rule-003 is high/soft
        assert "[HIGH/SOFT]" in block
        # rule-005 is high/advisory
        assert "[HIGH/ADVISORY]" in block

    def test_format_rules_block_includes_title_and_description(self):
        """Each rule's title and (at least start of) description appears."""
        rules = _sample_rules()["rules"]
        block = format_rules_block(rules)
        assert "Never Auto-Publish Content" in block
        assert "Email First Every Session" in block
        assert "Memory Search Before Work" in block
        assert "Agent Invocation as Experience" in block
        # Check that at least partial description text is present
        assert "human review" in block  # from rule-001 description
        assert "check all email" in block  # from rule-002 description

    def test_format_rules_block_empty_rules(self):
        """Empty rules list produces header/footer with 'no active rules' message."""
        block = format_rules_block([])
        assert "=== CONSTITUTIONAL RULES (Active) ===" in block
        assert "=== END CONSTITUTIONAL RULES ===" in block
        # Should indicate no active rules
        lower_block = block.lower()
        assert "no active rules" in lower_block, (
            "Empty rules block should contain a 'no active rules' message"
        )

    def test_format_rules_block_description_truncation(self):
        """Descriptions longer than 200 characters are truncated with '...'."""
        long_desc = "A" * 300
        rules = [{
            "id": "rule-long",
            "title": "Long Description Rule",
            "description": long_desc,
            "scope": "global",
            "priority": "critical",
            "enforcement": "hard",
            "status": "enforced",
            "category": "Testing",
        }]
        block = format_rules_block(rules)
        # The full 300-char description should NOT appear verbatim
        assert long_desc not in block, (
            "Full 300-char description should be truncated"
        )
        # But a truncated version should appear with ellipsis
        assert "..." in block
        # At least the first 200 chars should be present
        assert "A" * 200 in block

    def test_format_rules_block_one_rule_per_line_or_block(self):
        """Each rule produces at least one line in the output."""
        rules = _sample_rules()["rules"]
        block = format_rules_block(rules)
        # Every rule title should appear exactly once
        for rule in rules:
            assert block.count(rule["title"]) == 1, (
                f"Rule '{rule['title']}' should appear exactly once"
            )


# ===========================================================================
# 3. check_hard_enforcement tests
# ===========================================================================


class TestCheckHardEnforcement:
    """Verify check_hard_enforcement blocks violating tool calls."""

    def _hard_rules(self):
        """Return only the hard-enforcement rules from our sample set."""
        return [
            r for r in _sample_rules()["rules"]
            if r["enforcement"] == "hard"
        ]

    def _all_rules(self):
        """Return all sample rules (mixed enforcement levels)."""
        return _sample_rules()["rules"]

    # -- Approve cases --

    def test_hard_enforcement_approves_non_matching(self):
        """A benign tool call (e.g., Read a file) is approved."""
        result = check_hard_enforcement(
            tool_name="Read",
            tool_input={"file_path": "/home/aiciv/some_file.py"},
            rules=self._hard_rules(),
        )
        assert isinstance(result, dict)
        assert result["decision"] == "approve"

    def test_hard_enforcement_approves_safe_bash(self):
        """A safe bash command (e.g., ls) is approved."""
        result = check_hard_enforcement(
            tool_name="Bash",
            tool_input={"command": "ls -la /home/aiciv/"},
            rules=self._hard_rules(),
        )
        assert result["decision"] == "approve"

    def test_hard_enforcement_approves_soft_rules(self):
        """Soft and advisory enforcement rules never cause a block."""
        soft_and_advisory = [
            r for r in self._all_rules()
            if r["enforcement"] in ("soft", "advisory")
        ]
        # Even if tool input looks like it might relate to the rule topic,
        # soft/advisory rules must never block
        result = check_hard_enforcement(
            tool_name="Bash",
            tool_input={"command": "echo 'skipping memory search'"},
            rules=soft_and_advisory,
        )
        assert result["decision"] == "approve"

    def test_hard_enforcement_with_empty_rules(self):
        """Empty rules list always results in approve."""
        result = check_hard_enforcement(
            tool_name="Bash",
            tool_input={"command": "rm -rf /"},
            rules=[],
        )
        assert result["decision"] == "approve"

    # -- Block cases (rule-001 publish patterns removed; test remaining hard rules) --

    def test_hard_enforcement_approves_bsky_post(self):
        """rule-001 publish patterns were removed; bsky commands are now approved."""
        result = check_hard_enforcement(
            tool_name="Bash",
            tool_input={"command": "python3 bsky_post.py --publish 'Hello world'"},
            rules=self._hard_rules(),
        )
        assert result["decision"] == "approve"

    def test_hard_enforcement_approves_netlify_deploy(self):
        """rule-001 publish patterns were removed; netlify commands are now approved."""
        result = check_hard_enforcement(
            tool_name="Bash",
            tool_input={"command": "netlify deploy --prod"},
            rules=self._hard_rules(),
        )
        assert result["decision"] == "approve"

    def test_hard_enforcement_blocks_git_push(self):
        """rule-004 (No Direct Main Branch Commits) blocks git push to main."""
        rules_with_004 = [
            r for r in _sample_rules()["rules"]
            if r["id"] == "rule-004"
        ]
        result = check_hard_enforcement(
            tool_name="Bash",
            tool_input={"command": "git push origin main"},
            rules=rules_with_004,
        )
        assert result["decision"] == "block"
        assert "reason" in result

    def test_hard_enforcement_blocks_docker_restart(self):
        """rule-006 (No Destructive Docker Commands) blocks docker restart."""
        rules_with_006 = [
            r for r in _sample_rules()["rules"]
            if r["id"] == "rule-006"
        ]
        result = check_hard_enforcement(
            tool_name="Bash",
            tool_input={"command": "docker restart my-container"},
            rules=rules_with_006,
        )
        assert result["decision"] == "block"
        assert "reason" in result

    def test_hard_enforcement_blocks_docker_stop(self):
        """rule-006 blocks docker stop commands."""
        rules_with_006 = [
            r for r in _sample_rules()["rules"]
            if r["id"] == "rule-006"
        ]
        result = check_hard_enforcement(
            tool_name="Bash",
            tool_input={"command": "docker stop aiciv-gateway"},
            rules=rules_with_006,
        )
        assert result["decision"] == "block"
        assert "reason" in result

    def test_hard_enforcement_blocks_docker_rm(self):
        """rule-006 blocks docker rm (destroy) commands."""
        rules_with_006 = [
            r for r in _sample_rules()["rules"]
            if r["id"] == "rule-006"
        ]
        result = check_hard_enforcement(
            tool_name="Bash",
            tool_input={"command": "docker rm -f container_name"},
            rules=rules_with_006,
        )
        assert result["decision"] == "block"
        assert "reason" in result

    # -- Block result structure --

    def test_block_result_has_required_keys(self):
        """A block result must have 'decision' and 'reason' keys."""
        result = check_hard_enforcement(
            tool_name="Bash",
            tool_input={"command": "docker restart my-container"},
            rules=self._hard_rules(),
        )
        assert "decision" in result
        assert "reason" in result
        assert result["decision"] == "block"
        assert isinstance(result["reason"], str)
        assert len(result["reason"]) > 0

    def test_approve_result_has_decision_key(self):
        """An approve result must have at least the 'decision' key."""
        result = check_hard_enforcement(
            tool_name="Read",
            tool_input={"file_path": "/tmp/safe.txt"},
            rules=self._hard_rules(),
        )
        assert "decision" in result
        assert result["decision"] == "approve"

    # -- Edge cases --

    def test_hard_enforcement_handles_missing_tool_input(self):
        """Empty tool_input dict does not crash."""
        result = check_hard_enforcement(
            tool_name="Bash",
            tool_input={},
            rules=self._hard_rules(),
        )
        assert isinstance(result, dict)
        assert result["decision"] == "approve"

    def test_hard_enforcement_handles_non_bash_tool(self):
        """Non-Bash tools (Write, Edit, Grep) with benign input are approved."""
        for tool in ["Write", "Edit", "Grep", "Glob"]:
            result = check_hard_enforcement(
                tool_name=tool,
                tool_input={"file_path": "/home/aiciv/test.py"},
                rules=self._hard_rules(),
            )
            assert result["decision"] == "approve", (
                f"Tool '{tool}' with safe input should be approved"
            )


# ===========================================================================
# 4. Integration / main() tests
# ===========================================================================


class TestMain:
    """Verify main() entry point reads stdin and writes correct stdout."""

    def test_main_session_start_outputs_rules_block(
        self, tmp_path, monkeypatch, capsys
    ):
        """SessionStart mode outputs formatted rules block to stdout."""
        path = _write_rules_file(tmp_path, _sample_rules())

        stdin_data = json.dumps({"session_type": "start"})
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin_data))
        # The module needs to know where to find rules.json.
        # We pass the path via environment variable.
        monkeypatch.setenv("CONSTITUTION_RULES_PATH", path)

        # main() should exit 0
        with pytest.raises(SystemExit) as exc_info:
            main()
        # Accept both exit(0) and normal return (no SystemExit)
        if exc_info.value.code is not None:
            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "=== CONSTITUTIONAL RULES (Active) ===" in captured.out
        assert "=== END CONSTITUTIONAL RULES ===" in captured.out
        assert "Never Auto-Publish Content" in captured.out

    def test_main_session_start_no_system_exit(
        self, tmp_path, monkeypatch, capsys
    ):
        """SessionStart mode may also return normally (exit code 0 implied).

        This alternate test accepts either SystemExit(0) or normal return.
        """
        path = _write_rules_file(tmp_path, _sample_rules())

        stdin_data = json.dumps({"session_type": "start"})
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin_data))
        monkeypatch.setenv("CONSTITUTION_RULES_PATH", path)

        exit_code = None
        try:
            main()
        except SystemExit as e:
            exit_code = e.code

        # Either no SystemExit or exit code 0
        assert exit_code is None or exit_code == 0

        captured = capsys.readouterr()
        assert "=== CONSTITUTIONAL RULES (Active) ===" in captured.out

    def test_main_pretooluse_approves_safe_call(
        self, tmp_path, monkeypatch, capsys
    ):
        """PreToolUse mode approves a safe tool call."""
        path = _write_rules_file(tmp_path, _sample_rules())

        stdin_data = json.dumps({
            "tool_name": "Read",
            "tool_input": {"file_path": "/home/aiciv/safe_file.py"},
        })
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin_data))
        monkeypatch.setenv("CONSTITUTION_RULES_PATH", path)

        exit_code = None
        try:
            main()
        except SystemExit as e:
            exit_code = e.code

        assert exit_code is None or exit_code == 0

        captured = capsys.readouterr()
        output = json.loads(captured.out.strip())
        assert output["decision"] == "approve"

    def test_main_pretooluse_blocks_violation(
        self, tmp_path, monkeypatch, capsys
    ):
        """PreToolUse mode blocks a tool call that violates a hard rule."""
        path = _write_rules_file(tmp_path, _sample_rules())

        stdin_data = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "docker restart my-container"},
        })
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin_data))
        monkeypatch.setenv("CONSTITUTION_RULES_PATH", path)

        exit_code = None
        try:
            main()
        except SystemExit as e:
            exit_code = e.code

        assert exit_code is None or exit_code == 0

        captured = capsys.readouterr()
        output = json.loads(captured.out.strip())
        assert output["decision"] == "block"
        assert "reason" in output
        assert len(output["reason"]) > 0

    def test_main_handles_bad_stdin(self, tmp_path, monkeypatch, capsys):
        """Bad JSON on stdin does not crash; defaults to approve."""
        path = _write_rules_file(tmp_path, _sample_rules())

        monkeypatch.setattr("sys.stdin", io.StringIO("NOT VALID JSON"))
        monkeypatch.setenv("CONSTITUTION_RULES_PATH", path)

        exit_code = None
        try:
            main()
        except SystemExit as e:
            exit_code = e.code

        # Must not crash (exit code 0 or normal return)
        assert exit_code is None or exit_code == 0

        captured = capsys.readouterr()
        # On bad stdin, the hook should either output an approve decision
        # or output nothing (graceful degradation). It must NOT crash.
        if captured.out.strip():
            output = json.loads(captured.out.strip())
            assert output["decision"] == "approve"

    def test_main_handles_empty_stdin(self, tmp_path, monkeypatch, capsys):
        """Empty stdin does not crash; defaults to approve."""
        path = _write_rules_file(tmp_path, _sample_rules())

        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        monkeypatch.setenv("CONSTITUTION_RULES_PATH", path)

        exit_code = None
        try:
            main()
        except SystemExit as e:
            exit_code = e.code

        assert exit_code is None or exit_code == 0

    def test_main_pretooluse_output_is_valid_json(
        self, tmp_path, monkeypatch, capsys
    ):
        """PreToolUse output must be parseable JSON."""
        path = _write_rules_file(tmp_path, _sample_rules())

        stdin_data = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello"},
        })
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin_data))
        monkeypatch.setenv("CONSTITUTION_RULES_PATH", path)

        exit_code = None
        try:
            main()
        except SystemExit as e:
            exit_code = e.code

        assert exit_code is None or exit_code == 0

        captured = capsys.readouterr()
        # Must be valid JSON
        output = json.loads(captured.out.strip())
        assert "decision" in output


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
