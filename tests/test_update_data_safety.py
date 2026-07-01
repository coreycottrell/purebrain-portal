"""Tests for update system data safety — ensuring user data is never lost or leaked.

Covers:
- deploy-release.sh tarball exclusions (no user data shipped)
- _PRESERVED_FILES completeness (all user data files listed)
- _PRESERVED_DIRS completeness
- _SKIP_EXTRACT_DIRS prevents overwrite of self-referential dirs
- Extraction filtering (preserved files/dirs skipped)
- Rollback on failure
- No hardcoded user-specific values in distributable code
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PORTAL_DIR = Path(__file__).parent.parent
SRC_DIR = PORTAL_DIR


# ---------------------------------------------------------------------------
# 1. deploy-release.sh tarball exclusion tests
# ---------------------------------------------------------------------------

class TestDeployExclusions:
    """Verify deploy-release.sh excludes all user data from tarballs."""

    @pytest.fixture
    def deploy_script(self):
        return (PORTAL_DIR / "tools" / "deploy-release.sh").read_text()

    # Files that MUST be excluded from release tarballs
    MUST_EXCLUDE = [
        ".env", ".portal-token", ".gdrive-tokens.json",
        "*.db", "*.jsonl", "*.log",
        "memories", ".claude", "logs", "backups",
        "portal_uploads", "portal_owner.json",
        "kanban_tasks.json", "todo_tasks.json", "hub_tasks.json",
        "boop_config.json", "scheduled_tasks.json",
        "user-settings.json", "bookmarks.json",
        "telegram_config.json", "investor_config.json",
        "aether-infrastructure",
    ]

    @pytest.mark.parametrize("pattern", MUST_EXCLUDE)
    def test_tarball_excludes_user_data(self, deploy_script, pattern):
        """Each user-data file/dir must have a --exclude in deploy-release.sh."""
        assert f"--exclude='{pattern}'" in deploy_script, (
            f"deploy-release.sh missing --exclude='{pattern}' — "
            f"user data '{pattern}' would be shipped in release tarballs"
        )

    def test_deploy_vps_host_configurable(self, deploy_script):
        """VPS_HOST should be configurable via env var, not hardcoded."""
        assert "DEPLOY_VPS_HOST" in deploy_script, (
            "VPS_HOST should read from DEPLOY_VPS_HOST env var"
        )

    def test_deploy_releases_path_configurable(self, deploy_script):
        """RELEASES_PATH should be configurable via env var."""
        assert "DEPLOY_RELEASES_PATH" in deploy_script, (
            "RELEASES_PATH should read from DEPLOY_RELEASES_PATH env var"
        )


# ---------------------------------------------------------------------------
# 2. _PRESERVED_FILES completeness tests
# ---------------------------------------------------------------------------

class TestPreservedFiles:
    """Verify all user-data files are in _PRESERVED_FILES."""

    @pytest.fixture
    def preserved_files(self):
        # Import the list directly from portal_updates
        sys.path.insert(0, str(PORTAL_DIR))
        try:
            # Read the source to extract the list without importing (avoids deps)
            src = (PORTAL_DIR / "portal_updates.py").read_text()
            match = re.search(
                r'_PRESERVED_FILES\s*=\s*\[(.*?)\]', src, re.DOTALL
            )
            assert match, "_PRESERVED_FILES not found in portal_updates.py"
            items = re.findall(r'"([^"]+)"', match.group(1))
            return items
        finally:
            sys.path.pop(0)

    # Every user-data file that exists at runtime
    MUST_PRESERVE = [
        ".env", ".portal-token", "portal-chat.jsonl", "user-settings.json",
        "agents.db", "referrals.db", "clients.db", "portal_data.db",
        "boop_config.json", "scheduled_tasks.json",
        "portal_owner.json", "kanban_tasks.json", "todo_tasks.json",
        "hub_tasks.json", ".gdrive-tokens.json", "reaction-sentiment.jsonl",
        "telegram_config.json", "bookmarks.json", "investor_config.json",
    ]

    @pytest.mark.parametrize("filename", MUST_PRESERVE)
    def test_file_is_preserved(self, preserved_files, filename):
        """Each user-data file must be in _PRESERVED_FILES."""
        assert filename in preserved_files, (
            f"'{filename}' is missing from _PRESERVED_FILES in portal_updates.py — "
            f"this file would be DESTROYED on every portal update"
        )


# ---------------------------------------------------------------------------
# 2b. upgrade-portal.sh PRESERVED_FILES sync tests
# ---------------------------------------------------------------------------

class TestUpgradeScriptPreservedFiles:
    """Verify the bash upgrade-portal.sh PRESERVED_FILES array stays in sync."""

    @pytest.fixture
    def upgrade_preserved_files(self):
        # Read the bash script source and extract the PRESERVED_FILES=( ... ) array
        src = (PORTAL_DIR / "tools" / "upgrade-portal.sh").read_text()
        match = re.search(
            r'PRESERVED_FILES=\((.*?)\)', src, re.DOTALL
        )
        assert match, "PRESERVED_FILES=(...) not found in tools/upgrade-portal.sh"
        # Entries are bare tokens (no quotes), one per line
        return re.findall(r'[^\s()]+', match.group(1))

    def test_portal_data_db_in_upgrade_script(self, upgrade_preserved_files):
        """portal_data.db must be in upgrade-portal.sh's PRESERVED_FILES array."""
        assert "portal_data.db" in upgrade_preserved_files, (
            "'portal_data.db' is missing from PRESERVED_FILES in tools/upgrade-portal.sh — "
            "a CIV running portal_data.db would lose it on bash-driven upgrade"
        )


# ---------------------------------------------------------------------------
# 3. _PRESERVED_DIRS completeness tests
# ---------------------------------------------------------------------------

class TestPreservedDirs:
    """Verify all user-data directories are preserved."""

    @pytest.fixture
    def preserved_dirs(self):
        src = (PORTAL_DIR / "portal_updates.py").read_text()
        match = re.search(
            r'_PRESERVED_DIRS\s*=\s*\[(.*?)\]', src, re.DOTALL
        )
        assert match, "_PRESERVED_DIRS not found"
        return re.findall(r'"([^"]+)"', match.group(1))

    @pytest.fixture
    def skip_extract_dirs(self):
        src = (PORTAL_DIR / "portal_updates.py").read_text()
        match = re.search(
            r'_SKIP_EXTRACT_DIRS\s*=\s*\[(.*?)\]', src, re.DOTALL
        )
        assert match, "_SKIP_EXTRACT_DIRS not found"
        return re.findall(r'"([^"]+)"', match.group(1))

    MUST_PRESERVE_DIRS = ["memories", ".claude", "custom", "logs"]
    MUST_SKIP_EXTRACT = ["backups", ".module-backup", "portal_uploads"]

    @pytest.mark.parametrize("dirname", MUST_PRESERVE_DIRS)
    def test_dir_is_preserved(self, preserved_dirs, dirname):
        assert dirname in preserved_dirs, (
            f"'{dirname}' missing from _PRESERVED_DIRS — "
            f"directory would be overwritten on update"
        )

    @pytest.mark.parametrize("dirname", MUST_SKIP_EXTRACT)
    def test_dir_skipped_during_extract(self, skip_extract_dirs, dirname):
        assert dirname in skip_extract_dirs, (
            f"'{dirname}' missing from _SKIP_EXTRACT_DIRS — "
            f"directory could be overwritten by tarball extraction"
        )

    def test_backups_not_in_preserved_dirs(self, preserved_dirs):
        """backups must NOT be in _PRESERVED_DIRS (causes infinite recursion)."""
        assert "backups" not in preserved_dirs, (
            "backups in _PRESERVED_DIRS causes infinite recursion during backup step"
        )


# ---------------------------------------------------------------------------
# 4. Extraction safety tests
# ---------------------------------------------------------------------------

class TestExtractionSafety:
    """Verify the extraction step filters out preserved files/dirs and symlinks."""

    def test_extract_filters_preserved_files(self):
        """Extraction code must filter members against _PRESERVED_FILES."""
        src = (PORTAL_DIR / "portal_updates.py").read_text()
        assert "skip_files" in src or "skip_files = set(_PRESERVED_FILES)" in src, (
            "Extraction must filter tarball members against _PRESERVED_FILES"
        )
        assert "safe_members" in src, (
            "Extraction must build a filtered member list"
        )

    def test_extract_filters_preserved_dirs(self):
        """Extraction code must filter members against preserved dirs."""
        src = (PORTAL_DIR / "portal_updates.py").read_text()
        assert "skip_prefixes" in src, (
            "Extraction must filter tarball members against preserved dir prefixes"
        )

    def test_extract_skips_symlinks(self):
        """Extraction must skip symlinks for security."""
        src = (PORTAL_DIR / "portal_updates.py").read_text()
        assert "issym()" in src or "islnk()" in src, (
            "Extraction must skip symlinks to prevent symlink attacks"
        )

    def test_extract_uses_members_param(self):
        """extractall must use members= to only extract safe files."""
        src = (PORTAL_DIR / "portal_updates.py").read_text()
        assert "members=safe_members" in src, (
            "tar.extractall must use members=safe_members, not extract everything"
        )

    def test_no_lstrip_for_prefix_removal(self):
        """Must NOT use lstrip('./') — it strips character sets, not prefixes."""
        src = (PORTAL_DIR / "portal_updates.py").read_text()
        bad_pattern = 'lstrip("./")'
        assert bad_pattern not in src, (
            "lstrip('./') strips ALL leading dots and slashes (character set), "
            "not the prefix './'. This would turn '.env' into 'env', "
            "causing ALL dotfiles to bypass preservation. "
            "Use removeprefix('./') or startswith('./') slicing instead."
        )


# ---------------------------------------------------------------------------
# 5. Rollback logic tests
# ---------------------------------------------------------------------------

class TestRollbackLogic:
    """Verify rollback exists in the error handler."""

    def test_error_handler_has_rollback(self):
        """The except block must attempt rollback from backup_dir."""
        src = (PORTAL_DIR / "portal_updates.py").read_text()
        # Find the except block in _run_release_update
        assert "Attempting rollback" in src, (
            "Error handler must attempt rollback on failure"
        )

    def test_rollback_restores_preserved_files(self):
        src = (PORTAL_DIR / "portal_updates.py").read_text()
        # The rollback should iterate _PRESERVED_FILES
        assert "for pf in _PRESERVED_FILES" in src.split("Attempting rollback")[1], (
            "Rollback must restore _PRESERVED_FILES"
        )

    def test_rollback_restores_preserved_dirs(self):
        src = (PORTAL_DIR / "portal_updates.py").read_text()
        after_rollback = src.split("Attempting rollback")[1]
        assert "for pd in _PRESERVED_DIRS" in after_rollback, (
            "Rollback must restore _PRESERVED_DIRS"
        )

    def test_rollback_status_tracked(self):
        src = (PORTAL_DIR / "portal_updates.py").read_text()
        assert '"rolled_back"' in src, (
            "Update state must track whether rollback succeeded"
        )


# ---------------------------------------------------------------------------
# 5b. Single source of truth + version mismatch tests
# ---------------------------------------------------------------------------

class TestVersionSourceOfTruth:
    """Verify PORTAL_VERSION is the single source of truth."""

    def test_get_current_version_uses_portal_version(self):
        """_get_current_version must return PORTAL_VERSION, not read from file."""
        src = (PORTAL_DIR / "portal_updates.py").read_text()
        # Find the function
        match = re.search(
            r'async def _get_current_version.*?(?=\nasync def |\nclass |\ndef )',
            src, re.DOTALL
        )
        assert match, "_get_current_version function not found"
        func_body = match.group(0)
        assert "return PORTAL_VERSION" in func_body, (
            "_get_current_version must return PORTAL_VERSION directly"
        )
        assert "release_notes" not in func_body.lower(), (
            "_get_current_version should NOT read from release_notes.json"
        )

    def test_version_mismatch_detection_exists(self):
        """portal_config.py must detect and auto-fix version mismatches."""
        src = (PORTAL_DIR / "portal_config.py").read_text()
        assert "Version mismatch" in src or "version mismatch" in src, (
            "portal_config.py must detect version mismatch between PORTAL_VERSION and release_notes.json"
        )

    def test_version_mismatch_auto_fixes(self):
        """portal_config.py must auto-fix release_notes.json to match PORTAL_VERSION."""
        src = (PORTAL_DIR / "portal_config.py").read_text()
        assert "Auto-fix" in src or "auto-fix" in src or "Auto-fixed" in src, (
            "portal_config.py must auto-fix release_notes.json on mismatch"
        )


# ---------------------------------------------------------------------------
# 5c. Restart and health check tests
# ---------------------------------------------------------------------------

class TestRestartAndHealthCheck:
    """Verify restart fallback chain and health check support."""

    def test_restart_tries_restart_script(self):
        """Update should try restart.sh before exec fallback."""
        src = (PORTAL_DIR / "portal_updates.py").read_text()
        assert "restart.sh" in src, (
            "Update restart must try restart.sh as a clean restart method"
        )

    def test_restart_has_exec_fallback(self):
        """Update should have os.execv as final fallback."""
        src = (PORTAL_DIR / "portal_updates.py").read_text()
        assert "os.execv" in src, (
            "Update restart must have os.execv as exec fallback"
        )

    def test_frontend_has_health_check(self):
        """Frontend must poll for portal health after update restart."""
        src = (PORTAL_DIR / "portal-pb-styled.html").read_text()
        assert "_waitForRestart" in src, (
            "Frontend must have _waitForRestart health check function"
        )

    def test_frontend_has_periodic_check(self):
        """Frontend must check for updates periodically."""
        src = (PORTAL_DIR / "portal-pb-styled.html").read_text()
        assert "_silentUpdateCheck" in src, (
            "Frontend must have _silentUpdateCheck for periodic background checks"
        )
        assert "setInterval" in src, (
            "Frontend must use setInterval for periodic update checks"
        )


# ---------------------------------------------------------------------------
# 6. .gitignore coverage tests
# ---------------------------------------------------------------------------

class TestGitignore:
    """Verify .gitignore covers all user data files."""

    @pytest.fixture
    def gitignore(self):
        return (PORTAL_DIR / ".gitignore").read_text()

    MUST_IGNORE = [
        "todo_tasks.json", "kanban_tasks.json", "hub_tasks.json",
        "bookmarks.json", "portal_owner.json", "investor_config.json",
        "*.jsonl", "*.db", ".env", ".portal-token",
        "user-settings.json", "boop_config.json",
        "memories/", ".claude/", "logs/", "backups/",
        "portal_uploads/", "telegram_config.json",
    ]

    @pytest.mark.parametrize("pattern", MUST_IGNORE)
    def test_pattern_in_gitignore(self, gitignore, pattern):
        assert pattern in gitignore, (
            f"'{pattern}' missing from .gitignore — "
            f"user data could be accidentally committed"
        )


# ---------------------------------------------------------------------------
# 7. User-agent-agnostic code tests (no hardcoded personal data)
# ---------------------------------------------------------------------------

class TestNoHardcodedPersonalData:
    """Runtime code must not contain hardcoded user-specific values."""

    # Files that are runtime code (deployed to every portal)
    RUNTIME_FILES = [
        "portal_config.py",
        "static/js/features/chat.js",
        "static/commands-shortcuts.js",
        "start.sh",
        "portal_send_file.sh",
        "portal_tgim.py",
        "cc_bridge.py",
    ]

    @pytest.mark.parametrize("filepath", RUNTIME_FILES)
    def test_no_hardcoded_witness_fallback(self, filepath):
        """No file should use 'witness' as a fallback CIV name."""
        content = (PORTAL_DIR / filepath).read_text()
        # Allow in comments/docs, but not as a default value
        lines = content.split('\n')
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith('#') or stripped.startswith('//'):
                continue
            if '= "witness"' in line or "= 'witness'" in line:
                pytest.fail(
                    f"{filepath}:{i} has hardcoded 'witness' fallback: {stripped}"
                )

    @pytest.mark.parametrize("filepath", RUNTIME_FILES)
    def test_no_hardcoded_aether_path(self, filepath):
        """No file should have hardcoded Aether directory paths."""
        content = (PORTAL_DIR / filepath).read_text()
        assert "AI-CIV/aether" not in content and "projects/AI-CIV" not in content, (
            f"{filepath} contains hardcoded Aether path"
        )

    def test_chat_js_generic_default(self):
        content = (PORTAL_DIR / "static/js/features/chat.js").read_text()
        assert "var civName = 'Flux'" not in content, (
            "chat.js still has hardcoded 'Flux' as default CIV name"
        )

    def test_commands_js_generic_fallback(self):
        content = (PORTAL_DIR / "static/commands-shortcuts.js").read_text()
        assert "'aether'" not in content, (
            "commands-shortcuts.js still has 'aether' as fallback"
        )
        assert "Warn Jared" not in content, (
            "commands-shortcuts.js still references 'Jared'"
        )

    def test_portal_config_generic_fallback(self):
        content = (PORTAL_DIR / "portal_config.py").read_text()
        assert 'CIV_NAME = "witness"' not in content, (
            "portal_config.py still uses 'witness' as fallback CIV name"
        )

    def test_tgim_no_hardcoded_email(self):
        content = (PORTAL_DIR / "portal_tgim.py").read_text()
        assert "alex@puretechnology.nyc" not in content, (
            "portal_tgim.py still has hardcoded email"
        )

    def test_cc_bridge_no_absolute_path(self):
        content = (PORTAL_DIR / "cc_bridge.py").read_text()
        assert '"/home/aiciv/' not in content, (
            "cc_bridge.py still has hardcoded /home/aiciv/ path"
        )

    def test_start_sh_generic(self):
        content = (PORTAL_DIR / "start.sh").read_text()
        assert "Witness" not in content, (
            "start.sh still references 'Witness'"
        )

    def test_release_notes_no_personal_names(self):
        content = (PORTAL_DIR / "release_notes.json").read_text()
        assert "Jared Sanborn" not in content, (
            "release_notes.json still contains personal name"
        )
        assert "Aether Portal" not in content, (
            "release_notes.json still references 'Aether Portal'"
        )


# ---------------------------------------------------------------------------
# 8. User-Agent header tests (prevents 403 from release server)
# ---------------------------------------------------------------------------

class TestUpdateHeaders:
    """Verify all urllib requests include User-Agent."""

    def test_all_requests_have_user_agent(self):
        src = (PORTAL_DIR / "portal_updates.py").read_text()
        # Find all urllib.request.Request calls
        requests = re.findall(
            r'urllib\.request\.Request\((.*?)\)', src, re.DOTALL
        )
        for i, req in enumerate(requests):
            assert "User-Agent" in req, (
                f"urllib.request.Request #{i+1} missing User-Agent header — "
                f"will get 403 from release server"
            )
