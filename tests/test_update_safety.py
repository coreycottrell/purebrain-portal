"""
test_update_safety.py -- Tests for the release-server update mechanism.

Tests cover:
  A. Release Server Configuration (URL default, token reading, auth)
  B. Update Check (version fetching, comparison, response statuses)
  C. Update Apply Safety (download, SHA256 verify, backup/restore, background thread)
  D. Update State Management (status tracking, step tracking, failure handling, lock)
  E. Update Status Endpoint (state return)
  F. Functional Tests (mocked HTTP calls to update check/apply)
  G. Panel Injection Validation (missing marker warnings) -- unchanged from v1
  H. Panel Injection Warnings -- unchanged from v1

These are UNIT tests that use static source analysis and mocks -- no running server needed.
"""

import asyncio
import json
import os
import re
import signal
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# Add the portal root to sys.path so we can import from portal_server
PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PORTAL_DIR)


def run_async(coro):
    """Helper to run async functions in tests."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _read_portal_source() -> str:
    """Read portal_server.py source for static analysis tests."""
    server_path = os.path.join(PORTAL_DIR, "portal_server.py")
    with open(server_path) as f:
        return f.read()


def _read_updates_source() -> str:
    """Read portal_updates.py source for update-related static analysis tests."""
    updates_path = os.path.join(PORTAL_DIR, "portal_updates.py")
    with open(updates_path) as f:
        return f.read()


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

    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i]
        if (stripped.startswith('def ') or stripped.startswith('async def ') or
                stripped.startswith('class ')):
            end = i
            break

    return '\n'.join(lines[start:end])


# ===========================================================================
# A. Release Server Configuration Tests
# ===========================================================================

class TestReleaseServerConfiguration:
    """Tests for release server config variables."""

    def test_release_server_url_has_default(self):
        """RELEASE_SERVER_URL must default to cc.purebrain.ai."""
        source = _read_updates_source()
        # Should have a line like: RELEASE_SERVER_URL = os.getenv("RELEASE_SERVER_URL", "https://cc.purebrain.ai")
        assert 'RELEASE_SERVER_URL' in source, "RELEASE_SERVER_URL not found in portal_updates.py"
        match = re.search(
            r'RELEASE_SERVER_URL\s*=\s*os\.getenv\([^)]*"(https://[^"]+)"',
            source
        )
        assert match, "RELEASE_SERVER_URL must use os.getenv with a default URL"
        assert "cc.purebrain.ai" in match.group(1), (
            f"RELEASE_SERVER_URL default must point to cc.purebrain.ai, got: {match.group(1)}"
        )

    def test_portal_update_token_built_in(self):
        """PORTAL_UPDATE_TOKEN must have a built-in shared key for zero-config updates."""
        source = _read_updates_source()
        assert 'PORTAL_UPDATE_TOKEN' in source, "PORTAL_UPDATE_TOKEN not found"
        # Must have a hardcoded built-in key so all portals work without .env config
        assert 'Hq-Of6ktPmQ' in source, (
            "PORTAL_UPDATE_TOKEN must have built-in shared key for zero-config updates"
        )

    def test_update_check_requires_auth(self):
        """api_update_check must call check_auth to verify the request."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_check")
        assert func_src, "api_update_check function not found"
        assert "check_auth" in func_src, (
            "api_update_check must call check_auth for authorization"
        )

    def test_update_apply_requires_auth(self):
        """api_update_apply must call check_auth to verify the request."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_apply")
        assert func_src, "api_update_apply function not found"
        assert "check_auth" in func_src, (
            "api_update_apply must call check_auth for authorization"
        )

    def test_update_status_requires_auth(self):
        """api_update_status must call check_auth to verify the request."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_status")
        assert func_src, "api_update_status function not found"
        assert "check_auth" in func_src, (
            "api_update_status must call check_auth for authorization"
        )


# ===========================================================================
# B. Update Check Tests (source analysis)
# ===========================================================================

class TestUpdateCheck:
    """Tests that api_update_check correctly fetches and compares versions."""

    def test_update_check_fetches_remote_version(self):
        """api_update_check must fetch from the release server /api/releases/portal/version."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_check")
        assert func_src, "api_update_check function not found"
        assert "/api/releases/portal/version" in func_src, (
            "api_update_check must fetch from /api/releases/portal/version"
        )

    def test_update_check_sends_portal_token_header(self):
        """api_update_check must send X-Portal-Token header for auth."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_check")
        assert func_src, "api_update_check function not found"
        assert "X-Portal-Token" in func_src, (
            "api_update_check must send X-Portal-Token header"
        )

    def test_update_check_compares_versions(self):
        """api_update_check must compare current_version against remote_version."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_check")
        assert func_src, "api_update_check function not found"
        assert "current_version" in func_src, "Must reference current_version"
        assert "remote_version" in func_src, "Must reference remote_version"
        # Must have a comparison between the two
        assert "==" in func_src, "Must compare versions with =="

    def test_update_check_returns_available_or_up_to_date(self):
        """api_update_check must return 'available' or 'up_to_date' status."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_check")
        assert func_src, "api_update_check function not found"
        assert '"available"' in func_src, "Must return 'available' when update exists"
        assert '"up_to_date"' in func_src, "Must return 'up_to_date' when version matches"

    def test_update_check_handles_missing_token(self):
        """api_update_check must return error when PORTAL_UPDATE_TOKEN is empty."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_check")
        assert func_src, "api_update_check function not found"
        assert "PORTAL_UPDATE_TOKEN" in func_src, (
            "api_update_check must check for PORTAL_UPDATE_TOKEN"
        )
        assert '"error"' in func_src, "Must return error status when token is missing"

    def test_update_check_handles_server_error(self):
        """api_update_check must catch exceptions from the release server."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_check")
        assert func_src, "api_update_check function not found"
        assert "except" in func_src, (
            "api_update_check must have exception handling for server errors"
        )


# ===========================================================================
# C. Update Apply Safety Tests (source analysis)
# ===========================================================================

class TestUpdateApplySafety:
    """Tests that the update apply mechanism is safe."""

    def test_update_apply_downloads_tarball(self):
        """_run_release_update must download from /api/releases/portal/latest."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"
        assert "/api/releases/portal/latest" in func_src, (
            "_run_release_update must download from /api/releases/portal/latest"
        )

    def test_update_apply_verifies_sha256(self):
        """_run_release_update must verify SHA256 checksum of downloaded tarball."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"
        assert "sha256" in func_src.lower(), "Must reference SHA256"
        assert "hashlib.sha256" in func_src, (
            "_run_release_update must use hashlib.sha256 for checksum verification"
        )

    def test_update_apply_compares_sha256_hashes(self):
        """_run_release_update must compare actual vs expected SHA256."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"
        assert "expected_sha256" in func_src, "Must have expected_sha256 from server"
        assert "actual_sha256" in func_src, "Must compute actual_sha256 from file"
        assert "actual_sha256 != expected_sha256" in func_src, (
            "Must compare actual vs expected SHA256"
        )

    def test_update_apply_backs_up_preserved_files(self):
        """_run_release_update must back up preserved files before extraction."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"
        assert "_PRESERVED_FILES" in func_src, (
            "_run_release_update must reference _PRESERVED_FILES for backup"
        )
        assert "backup" in func_src.lower(), "Must have backup logic"
        assert "shutil.copy2" in func_src, (
            "_run_release_update must use shutil.copy2 to backup preserved files"
        )

    def test_update_apply_restores_preserved_files(self):
        """_run_release_update must restore preserved files after extraction."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"

        # Must have a restore step AFTER extract step
        extract_pos = func_src.find('"extract"')
        restore_pos = func_src.find('"restore_preserved"')
        assert extract_pos != -1, "extract step not found"
        assert restore_pos != -1, "restore_preserved step not found"
        assert extract_pos < restore_pos, (
            "restore_preserved must come AFTER extract"
        )

    def test_update_apply_uses_background_task(self):
        """api_update_apply must launch _run_release_update as a background task."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_apply")
        assert func_src, "api_update_apply function not found"
        assert "asyncio.create_task" in func_src, (
            "api_update_apply must use asyncio.create_task for background execution"
        )
        assert "_run_release_update" in func_src, (
            "api_update_apply must launch _run_release_update"
        )

    def test_preserved_files_list_includes_env(self):
        """_PRESERVED_FILES must include .env."""
        source = _read_updates_source()
        # Find the _PRESERVED_FILES list
        match = re.search(r'_PRESERVED_FILES\s*=\s*\[([^\]]+)\]', source, re.DOTALL)
        assert match, "_PRESERVED_FILES list not found"
        files_str = match.group(1)
        assert '".env"' in files_str, ".env must be in _PRESERVED_FILES"

    def test_preserved_files_list_includes_portal_token(self):
        """_PRESERVED_FILES must include .portal-token."""
        source = _read_updates_source()
        match = re.search(r'_PRESERVED_FILES\s*=\s*\[([^\]]+)\]', source, re.DOTALL)
        assert match, "_PRESERVED_FILES list not found"
        files_str = match.group(1)
        assert '".portal-token"' in files_str, ".portal-token must be in _PRESERVED_FILES"

    def test_preserved_files_list_includes_chat_log(self):
        """_PRESERVED_FILES must include portal-chat.jsonl."""
        source = _read_updates_source()
        match = re.search(r'_PRESERVED_FILES\s*=\s*\[([^\]]+)\]', source, re.DOTALL)
        assert match, "_PRESERVED_FILES list not found"
        files_str = match.group(1)
        assert '"portal-chat.jsonl"' in files_str, "portal-chat.jsonl must be in _PRESERVED_FILES"

    def test_preserved_files_list_includes_databases(self):
        """_PRESERVED_FILES must include agents.db, referrals.db, clients.db."""
        source = _read_updates_source()
        match = re.search(r'_PRESERVED_FILES\s*=\s*\[([^\]]+)\]', source, re.DOTALL)
        assert match, "_PRESERVED_FILES list not found"
        files_str = match.group(1)
        for db in ["agents.db", "referrals.db", "clients.db"]:
            assert f'"{db}"' in files_str, f"{db} must be in _PRESERVED_FILES"

    def test_preserved_dirs_exist(self):
        """_PRESERVED_DIRS must be defined with key directories."""
        source = _read_updates_source()
        match = re.search(r'_PRESERVED_DIRS\s*=\s*\[([^\]]+)\]', source, re.DOTALL)
        assert match, "_PRESERVED_DIRS list not found"
        dirs_str = match.group(1)
        for d in ["memories", ".claude", "custom", "logs"]:
            assert f'"{d}"' in dirs_str, f"{d} must be in _PRESERVED_DIRS"

    def test_tarball_extraction_has_path_traversal_protection(self):
        """_run_release_update must check for path traversal in tarball members."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"
        # Must check for ".." or "/" in member names
        assert '".."' in func_src, "Must check for '..' in tarball member names"
        assert 'startswith("/")' in func_src, "Must check for absolute paths in tarball members"

    def test_sha256_mismatch_raises_error(self):
        """_run_release_update must raise RuntimeError on SHA256 mismatch."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"

        # Find the verify_checksum section
        checksum_start = func_src.find('"verify_checksum"')
        # Find the next step
        next_step = func_src.find('"backup"', checksum_start)
        assert checksum_start != -1, "verify_checksum step not found"
        assert next_step != -1, "backup step not found after verify_checksum"
        checksum_section = func_src[checksum_start:next_step]
        assert "raise RuntimeError" in checksum_section, (
            "SHA256 mismatch must raise RuntimeError"
        )


# ===========================================================================
# D. Update State Management Tests (source analysis)
# ===========================================================================

class TestUpdateStateManagement:
    """Tests for proper state management during updates."""

    def test_update_state_has_status_field(self):
        """_update_state dict must have 'status' key."""
        source = _read_updates_source()
        assert '"status"' in source, "_update_state must have 'status' field"
        # Check the initial value is 'idle'
        assert '"status": "idle"' in source, "Initial status must be 'idle'"

    def test_update_state_tracks_step(self):
        """_update_state must track current step during update."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_update_step")
        assert func_src, "_update_step function not found"
        assert '"step"' in func_src, "_update_step must update 'step' field"
        assert '"steps_remaining"' in func_src, "_update_step must update steps_remaining"
        assert '"steps_completed"' in func_src, "_update_step must update steps_completed"

    def test_update_state_set_to_failed_on_error(self):
        """On error, _run_release_update must set status to 'failed'."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"

        except_block = func_src[func_src.find("except Exception"):]
        assert '"failed"' in except_block, (
            "except block must set status to 'failed'"
        )

    def test_update_state_records_step_failed_on_error(self):
        """On error, _run_release_update must record which step failed."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"

        except_block = func_src[func_src.find("except Exception"):]
        assert '"step_failed"' in except_block, (
            "except block must record step_failed"
        )

    def test_update_lock_prevents_concurrent_updates(self):
        """api_update_apply must check lock.locked() to reject concurrent updates."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_apply")
        assert func_src, "api_update_apply function not found"
        assert "lock.locked()" in func_src, (
            "api_update_apply must check lock.locked() to reject concurrent requests"
        )
        assert "Update already in progress" in func_src, (
            "api_update_apply must return 'Update already in progress' when lock is held"
        )

    def test_lock_is_asyncio_lock(self):
        """The update lock must be asyncio.Lock for async-safe concurrency."""
        source = _read_updates_source()
        assert "asyncio.Lock" in source, (
            "Update lock must be asyncio.Lock for async-safe concurrency"
        )

    def test_lock_acquired_before_background_task(self):
        """lock.acquire() must happen BEFORE asyncio.create_task in api_update_apply."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_apply")
        assert func_src, "api_update_apply function not found"
        acquire_pos = func_src.find("lock.acquire()")
        create_task_pos = func_src.find("asyncio.create_task")
        assert acquire_pos != -1, "lock.acquire() not found in api_update_apply"
        assert create_task_pos != -1, "asyncio.create_task not found in api_update_apply"
        assert acquire_pos < create_task_pos, (
            "lock.acquire() must come BEFORE asyncio.create_task"
        )

    def test_lock_released_in_finally_block(self):
        """_run_release_update must release the lock in a finally block."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"
        assert "finally:" in func_src, "_run_release_update must have a finally block"

        # Find the finally block and ensure lock.release() is inside it
        lines = func_src.split('\n')
        in_finally = False
        found_release_in_finally = False
        for line in lines:
            stripped = line.strip()
            if stripped == "finally:":
                in_finally = True
            elif in_finally and "lock.release()" in stripped:
                found_release_in_finally = True
                break

        assert found_release_in_finally, (
            "lock.release() must be inside the finally block of _run_release_update"
        )

    def test_state_reset_before_new_update(self):
        """_update_state must be fully reset before starting a new update."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_apply")
        assert func_src, "api_update_apply not found"
        assert "_update_state.update(" in func_src, (
            "api_update_apply must reset _update_state before starting update"
        )

    def test_initial_state_has_required_fields(self):
        """The _update_state dict must have all required tracking fields."""
        source = _read_updates_source()
        required_fields = [
            "status", "job_id", "step", "steps_completed", "steps_remaining",
            "started_at", "completed_at", "error", "step_failed", "message",
        ]
        for field in required_fields:
            assert f'"{field}"' in source, (
                f"_update_state must have '{field}' field"
            )


# ===========================================================================
# E. Update Status Endpoint Tests (source analysis)
# ===========================================================================

class TestUpdateStatusEndpoint:
    """Tests for api_update_status endpoint."""

    def test_update_status_returns_current_state(self):
        """api_update_status must read and return _update_state."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_status")
        assert func_src, "api_update_status function not found"
        assert "_update_state" in func_src, (
            "api_update_status must reference _update_state"
        )

    def test_update_status_handles_in_progress(self):
        """api_update_status must return step info for in_progress status."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_status")
        assert func_src, "api_update_status function not found"
        assert '"in_progress"' in func_src, "Must handle in_progress status"
        assert '"step"' in func_src, "Must return current step"

    def test_update_status_handles_success(self):
        """api_update_status must return version info for success status."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_status")
        assert func_src, "api_update_status function not found"
        assert '"success"' in func_src, "Must handle success status"

    def test_update_status_handles_failed(self):
        """api_update_status must return error info for failed status."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_status")
        assert func_src, "api_update_status function not found"
        assert '"failed"' in func_src, "Must handle failed status"
        assert '"step_failed"' in func_src, "Must return step_failed on failure"

    def test_update_status_handles_idle(self):
        """api_update_status must return idle when no update is running."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_status")
        assert func_src, "api_update_status function not found"
        assert '"idle"' in func_src, "Must handle idle status"


# ===========================================================================
# F. Update Pipeline Steps Ordering (source analysis)
# ===========================================================================

class TestUpdatePipelineSteps:
    """Tests that verify the update pipeline step ordering."""

    def test_steps_remaining_list_is_complete(self):
        """api_update_apply must define all 7 steps in steps_remaining."""
        source = _read_updates_source()
        func_src = _extract_function(source, "api_update_apply")
        assert func_src, "api_update_apply not found"

        expected_steps = [
            "download", "verify_checksum", "backup",
            "extract", "restore_preserved", "update_version", "restart",
        ]
        for step in expected_steps:
            assert f'"{step}"' in func_src, (
                f"Step '{step}' must be in steps_remaining list"
            )

    def test_verify_checksum_before_extract(self):
        """verify_checksum step must execute BEFORE extract in _run_release_update."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"

        checksum_pos = func_src.find('"verify_checksum"')
        extract_pos = func_src.find('"extract"')
        assert checksum_pos != -1, "verify_checksum step not found"
        assert extract_pos != -1, "extract step not found"
        assert checksum_pos < extract_pos, (
            "verify_checksum must execute BEFORE extract"
        )

    def test_backup_before_extract(self):
        """backup step must execute BEFORE extract in _run_release_update."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"

        backup_pos = func_src.find('"backup"')
        extract_pos = func_src.find('"extract"')
        assert backup_pos != -1, "backup step not found"
        assert extract_pos != -1, "extract step not found"
        assert backup_pos < extract_pos, (
            "backup must execute BEFORE extract to save preserved files"
        )

    def test_restore_after_extract(self):
        """restore_preserved step must execute AFTER extract in _run_release_update."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"

        extract_pos = func_src.find('"extract"')
        restore_pos = func_src.find('"restore_preserved"')
        assert extract_pos != -1, "extract step not found"
        assert restore_pos != -1, "restore_preserved step not found"
        assert extract_pos < restore_pos, (
            "restore_preserved must execute AFTER extract"
        )

    def test_update_version_before_restart(self):
        """update_version step must execute BEFORE restart in _run_release_update."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"

        version_pos = func_src.find('"update_version"')
        restart_pos = func_src.find('"restart"')
        assert version_pos != -1, "update_version step not found"
        assert restart_pos != -1, "restart step not found"
        assert version_pos < restart_pos, (
            "update_version must execute BEFORE restart"
        )

    def test_success_status_set_before_restart(self):
        """Status must be set to 'success' before SIGTERM/restart."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"

        success_pos = func_src.find('"success"')
        sigterm_pos = func_src.find("signal.SIGTERM")
        assert success_pos != -1, "success status assignment not found"
        assert sigterm_pos != -1, "SIGTERM not found"
        assert success_pos < sigterm_pos, (
            "Status must be set to 'success' BEFORE sending SIGTERM"
        )

    def test_sleep_before_sigterm(self):
        """There must be a delay before SIGTERM so success status can be polled."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"

        sleep_pos = func_src.find("asyncio.sleep")
        sigterm_pos = func_src.find("signal.SIGTERM")
        assert sleep_pos != -1, "asyncio.sleep not found before SIGTERM"
        assert sigterm_pos != -1, "SIGTERM not found"
        assert sleep_pos < sigterm_pos, (
            "asyncio.sleep must come before SIGTERM to allow status polling"
        )


# ===========================================================================
# G. Watchdog Detection Tests
# ===========================================================================

class TestWatchdogDetection:
    """Tests for process manager detection before self-restart."""

    def test_sigterm_sent_for_watchdog_restart(self):
        """_run_release_update must send SIGTERM when a process manager is detected."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"
        assert "signal.SIGTERM" in func_src, (
            "_run_release_update must send SIGTERM for clean restart"
        )
        assert "os.kill(os.getpid()" in func_src, (
            "_run_release_update must use os.kill(os.getpid(), signal.SIGTERM)"
        )

    def test_exec_restart_fallback(self):
        """_run_release_update must have os.execv fallback when no watchdog detected."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"
        assert "os.execv" in func_src, (
            "_run_release_update must have os.execv fallback for restart without watchdog"
        )

    def test_message_field_set_on_restart(self):
        """_run_release_update must set a 'message' field for user feedback."""
        source = _read_updates_source()
        func_src = _extract_function(source, "_run_release_update")
        assert func_src, "_run_release_update function not found"
        assert '"message"' in func_src, (
            "_run_release_update must set a 'message' field in _update_state"
        )


# ===========================================================================
# H. Functional Tests (with mocks)
# ===========================================================================

class TestUpdateCheckFunctional:
    """Functional tests that actually call api_update_check with mocked HTTP."""

    def test_update_check_returns_available_when_newer(self):
        """Mock server returns newer version -- api_update_check returns 'available'."""
        import portal_updates

        class FakeRequest:
            headers = {"Authorization": "Bearer test-token"}

        fake_resp_data = json.dumps({
            "version": "99.0.0",
            "sha256": "abc123",
            "size_bytes": 1024000,
        }).encode()

        class FakeHTTPResponse:
            def read(self):
                return fake_resp_data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        with patch.object(portal_updates, "check_auth", return_value=True), \
             patch.object(portal_updates, "_get_current_version", new=AsyncMock(return_value="1.0.0")), \
             patch.object(portal_updates, "PORTAL_UPDATE_TOKEN", "test-token"), \
             patch("urllib.request.urlopen", return_value=FakeHTTPResponse()):
            resp = run_async(portal_updates.api_update_check(FakeRequest()))

        body = json.loads(resp.body)
        assert body["status"] == "available", f"Expected 'available', got: {body}"
        assert body["remote_version"] == "99.0.0"

    def test_update_check_returns_up_to_date_when_same(self):
        """Mock server returns same version -- api_update_check returns 'up_to_date'."""
        import portal_updates

        class FakeRequest:
            headers = {"Authorization": "Bearer test-token"}

        fake_resp_data = json.dumps({
            "version": "1.0.0",
            "sha256": "abc123",
            "size_bytes": 1024000,
        }).encode()

        class FakeHTTPResponse:
            def read(self):
                return fake_resp_data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        with patch.object(portal_updates, "check_auth", return_value=True), \
             patch.object(portal_updates, "_get_current_version", new=AsyncMock(return_value="1.0.0")), \
             patch.object(portal_updates, "PORTAL_UPDATE_TOKEN", "test-token"), \
             patch("urllib.request.urlopen", return_value=FakeHTTPResponse()):
            resp = run_async(portal_updates.api_update_check(FakeRequest()))

        body = json.loads(resp.body)
        assert body["status"] == "up_to_date", f"Expected 'up_to_date', got: {body}"

    def test_update_check_returns_error_when_no_token(self):
        """When PORTAL_UPDATE_TOKEN is empty, api_update_check returns error."""
        import portal_updates

        class FakeRequest:
            headers = {"Authorization": "Bearer test-token"}

        with patch.object(portal_updates, "check_auth", return_value=True), \
             patch.object(portal_updates, "_get_current_version", new=AsyncMock(return_value="1.0.0")), \
             patch.object(portal_updates, "PORTAL_UPDATE_TOKEN", ""):
            resp = run_async(portal_updates.api_update_check(FakeRequest()))

        body = json.loads(resp.body)
        assert body["status"] == "error", f"Expected 'error', got: {body}"
        assert "PORTAL_UPDATE_TOKEN" in body.get("error", ""), (
            "Error message must mention PORTAL_UPDATE_TOKEN"
        )

    def test_update_check_returns_error_when_server_unreachable(self):
        """When release server is unreachable, api_update_check returns error."""
        import portal_updates

        class FakeRequest:
            headers = {"Authorization": "Bearer test-token"}

        with patch.object(portal_updates, "check_auth", return_value=True), \
             patch.object(portal_updates, "_get_current_version", new=AsyncMock(return_value="1.0.0")), \
             patch.object(portal_updates, "PORTAL_UPDATE_TOKEN", "test-token"), \
             patch("urllib.request.urlopen", side_effect=ConnectionError("Connection refused")):
            resp = run_async(portal_updates.api_update_check(FakeRequest()))

        body = json.loads(resp.body)
        assert body["status"] == "error", f"Expected 'error', got: {body}"


class TestUpdateApplyFunctional:
    """Functional tests for api_update_apply with mocks."""

    def test_apply_rejects_concurrent_updates(self):
        """When lock is already held, api_update_apply returns error."""
        import portal_updates

        class FakeRequest:
            headers = {"Authorization": "Bearer test-token"}
            query_params = {}

        fake_lock = asyncio.Lock()

        async def run():
            await fake_lock.acquire()  # Lock is held
            with patch.object(portal_updates, "check_auth", return_value=True), \
                 patch.object(portal_updates, "_get_update_lock", new=AsyncMock(return_value=fake_lock)), \
                 patch.object(portal_updates, "PORTAL_UPDATE_TOKEN", "test-token"), \
                 patch.object(portal_updates, "_LAST_UPDATE_STATUS_FILE", Path("/nonexistent")):
                resp = await portal_updates.api_update_apply(FakeRequest())
            return resp

        resp = run_async(run())
        body = json.loads(resp.body)
        assert body["status"] == "error"
        assert "already in progress" in body.get("error", "").lower()

    def test_apply_rejects_when_no_token(self):
        """When PORTAL_UPDATE_TOKEN is empty, api_update_apply returns error."""
        import portal_updates

        class FakeRequest:
            headers = {"Authorization": "Bearer test-token"}
            query_params = {}

        fake_lock = asyncio.Lock()

        async def run():
            with patch.object(portal_updates, "check_auth", return_value=True), \
                 patch.object(portal_updates, "_get_update_lock", new=AsyncMock(return_value=fake_lock)), \
                 patch.object(portal_updates, "PORTAL_UPDATE_TOKEN", ""), \
                 patch.object(portal_updates, "_update_state", {"status": "idle"}):
                resp = await portal_updates.api_update_apply(FakeRequest())
            return resp

        resp = run_async(run())
        body = json.loads(resp.body)
        assert body["status"] == "error"
        assert "PORTAL_UPDATE_TOKEN" in body.get("error", "")


# ===========================================================================
# I. Lock Behavioral Tests
# ===========================================================================

class TestLockBehavioral:
    """Behavioral tests verifying asyncio.Lock works as expected for our use case."""

    def test_lock_released_after_successful_operation(self):
        """Lock must be released in finally block regardless of outcome."""
        async def check_lock_release():
            lock = asyncio.Lock()
            await lock.acquire()
            assert lock.locked(), "Lock should be locked after acquire"
            try:
                pass  # Simulate successful update
            finally:
                if lock.locked():
                    lock.release()
            assert not lock.locked(), "Lock should be released after finally"

        run_async(check_lock_release())

    def test_lock_released_after_failed_operation(self):
        """Lock must be released even when an exception occurs."""
        async def check_lock_release_on_error():
            lock = asyncio.Lock()
            await lock.acquire()
            assert lock.locked()
            try:
                raise RuntimeError("Download failed: simulated")
            except Exception:
                pass
            finally:
                if lock.locked():
                    lock.release()
            assert not lock.locked(), "Lock must NOT be locked after exception + finally"

        run_async(check_lock_release_on_error())


# ===========================================================================
# J. Panel Injection Validation Tests (unchanged -- not update-related)
# ===========================================================================

class TestPanelInjectionValidation:
    """Tests that missing injection markers produce warnings, not silent failures."""

    def _run_injection_with_html(self, html: str, panel_html: str) -> tuple:
        """Run _inject_custom_panels with custom HTML and capture prints.

        Returns (result_html, printed_lines).
        """
        source = _read_portal_source()
        parse_src = _extract_function(source, "_parse_panel_meta")
        inject_src = _extract_function(source, "_inject_custom_panels")
        if not parse_src or not inject_src:
            pytest.skip("Could not extract injection functions")

        with tempfile.TemporaryDirectory() as tmpdir:
            panels_dir = Path(tmpdir) / "custom" / "panels"
            panels_dir.mkdir(parents=True)
            (panels_dir / "test-panel.html").write_text(panel_html)

            inject_src = inject_src.replace(
                'SCRIPT_DIR / "custom" / "panels"',
                f'Path("{panels_dir}")'
            )

            from html import escape as html_escape
            printed = []

            def mock_print(*args, **kwargs):
                printed.append(' '.join(str(a) for a in args))

            ns = {
                "re": __import__("re"),
                "Path": Path,
                "sorted": sorted,
                "print": mock_print,
                "escape": html_escape,
            }
            exec(parse_src, ns)
            exec(inject_src, ns)

            result = ns["_inject_custom_panels"](html)
            return result, printed

    @pytest.fixture
    def sample_panel(self):
        return (
            '<!-- panel-id: test -->\n'
            '<!-- panel-label: Test Panel -->\n'
            '<!-- panel-icon: &#x2726; -->\n'
            '<div>Content</div>\n'
        )

    def test_missing_nav_marker_no_crash(self, sample_panel):
        """If <!-- /nav-panels --> is missing, injection should not crash."""
        html_no_nav_marker = (
            '<div class="main">\n'
            '  <nav class="sidebar">\n'
            '    <div class="nav-item active" data-panel="chat">Chat</div>\n'
            '  </nav>\n'
            '  <div class="content">\n'
            '    <div class="panel active" id="panel-chat">Chat</div>\n'
            '  <!-- /panels -->\n'
            '  </div>\n'
            '</div>\n'
            '<div id="mobile-more-menu">\n'
            '    <!-- /mobile-menu-items -->\n'
            '</div>\n'
        )
        result, printed = self._run_injection_with_html(html_no_nav_marker, sample_panel)
        assert isinstance(result, str)

    def test_missing_panels_marker_no_crash(self, sample_panel):
        """If <!-- /panels --> is missing, injection should not crash."""
        html_no_panels_marker = (
            '<div class="main">\n'
            '  <nav class="sidebar">\n'
            '    <div class="nav-item" data-panel="chat">Chat</div>\n'
            '    <!-- /nav-panels -->\n'
            '  </nav>\n'
            '  <div class="content">\n'
            '    <div class="panel active" id="panel-chat">Chat</div>\n'
            '  </div>\n'
            '</div>\n'
            '<div id="mobile-more-menu">\n'
            '    <!-- /mobile-menu-items -->\n'
            '</div>\n'
        )
        result, printed = self._run_injection_with_html(html_no_panels_marker, sample_panel)
        assert isinstance(result, str)

    def test_missing_mobile_marker_no_crash(self, sample_panel):
        """If <!-- /mobile-menu-items --> is missing, injection should not crash."""
        html_no_mobile_marker = (
            '<div class="main">\n'
            '  <nav class="sidebar">\n'
            '    <div class="nav-item" data-panel="chat">Chat</div>\n'
            '    <!-- /nav-panels -->\n'
            '  </nav>\n'
            '  <div class="content">\n'
            '    <div class="panel active" id="panel-chat">Chat</div>\n'
            '  <!-- /panels -->\n'
            '  </div>\n'
            '</div>\n'
        )
        result, printed = self._run_injection_with_html(html_no_mobile_marker, sample_panel)
        assert isinstance(result, str)

    def test_partial_markers_still_injects_available(self, sample_panel):
        """If only some markers exist, inject into those that are found."""
        html_panels_only = (
            '<div class="content">\n'
            '    <div class="panel active" id="panel-chat">Chat</div>\n'
            '  <!-- /panels -->\n'
            '</div>\n'
        )
        result, printed = self._run_injection_with_html(html_panels_only, sample_panel)
        assert 'id="panel-test"' in result, (
            "Panel div should be injected when <!-- /panels --> marker is present"
        )

    def test_all_markers_present_succeeds(self, sample_panel):
        """When all markers present, all injections succeed."""
        html_all_markers = (
            '<div class="main">\n'
            '  <nav class="sidebar">\n'
            '    <div class="nav-item active" data-panel="chat">Chat</div>\n'
            '    <!-- /nav-panels -->\n'
            '  </nav>\n'
            '  <div class="content">\n'
            '    <div class="panel active" id="panel-chat">Chat</div>\n'
            '  <!-- /panels -->\n'
            '  </div>\n'
            '</div>\n'
            '<div id="mobile-more-menu">\n'
            '    <!-- /mobile-menu-items -->\n'
            '</div>\n'
        )
        result, printed = self._run_injection_with_html(html_all_markers, sample_panel)

        assert 'data-panel="test"' in result, "Nav item should be injected"
        assert 'id="panel-test"' in result, "Panel div should be injected"
        assert "selectMobileMenuItem('test')" in result, "Mobile item should be injected"

    def test_all_three_markers_exist_in_injection_code(self):
        """The injection function must reference all three markers."""
        source = _read_portal_source()
        func_src = _extract_function(source, "_inject_custom_panels")
        assert func_src, "_inject_custom_panels not found"

        assert "<!-- /nav-panels -->" in func_src, "Must handle nav-panels marker"
        assert "<!-- /panels -->" in func_src, "Must handle panels marker"
        assert "<!-- /mobile-menu-items -->" in func_src, "Must handle mobile-menu-items marker"


# ===========================================================================
# K. Panel Injection Warning Tests (unchanged -- not update-related)
# ===========================================================================

class TestPanelInjectionWarnings:
    """Tests that missing injection markers produce appropriate WARNING messages."""

    def _run_injection_with_html(self, html: str, panel_html: str) -> tuple:
        """Run _inject_custom_panels with custom HTML and capture prints.

        Returns (result_html, printed_lines).
        """
        source = _read_portal_source()
        parse_src = _extract_function(source, "_parse_panel_meta")
        inject_src = _extract_function(source, "_inject_custom_panels")
        if not parse_src or not inject_src:
            pytest.skip("Could not extract injection functions")

        with tempfile.TemporaryDirectory() as tmpdir:
            panels_dir = Path(tmpdir) / "custom" / "panels"
            panels_dir.mkdir(parents=True)
            (panels_dir / "test-panel.html").write_text(panel_html)

            inject_src = inject_src.replace(
                'SCRIPT_DIR / "custom" / "panels"',
                f'Path("{panels_dir}")'
            )

            from html import escape as html_escape
            printed = []

            def mock_print(*args, **kwargs):
                printed.append(' '.join(str(a) for a in args))

            ns = {
                "re": __import__("re"),
                "Path": Path,
                "sorted": sorted,
                "print": mock_print,
                "escape": html_escape,
            }
            exec(parse_src, ns)
            exec(inject_src, ns)

            result = ns["_inject_custom_panels"](html)
            return result, printed

    @pytest.fixture
    def sample_panel(self):
        return (
            '<!-- panel-id: test -->\n'
            '<!-- panel-label: Test Panel -->\n'
            '<!-- panel-icon: &#x2726; -->\n'
            '<div>Content</div>\n'
        )

    def test_missing_nav_marker_logs_warning(self, sample_panel):
        """If <!-- /nav-panels --> is missing, a WARNING must be printed."""
        html = (
            '<div class="main">\n'
            '  <nav class="sidebar">\n'
            '    <div class="nav-item active" data-panel="chat">Chat</div>\n'
            '  </nav>\n'
            '  <div class="content">\n'
            '    <div class="panel active" id="panel-chat">Chat</div>\n'
            '  <!-- /panels -->\n'
            '  </div>\n'
            '</div>\n'
            '<div id="mobile-more-menu">\n'
            '    <!-- /mobile-menu-items -->\n'
            '</div>\n'
        )
        result, printed = self._run_injection_with_html(html, sample_panel)
        warnings = [p for p in printed if 'WARNING' in p and 'nav-panels' in p]
        assert len(warnings) > 0, (
            "Missing <!-- /nav-panels --> must produce a WARNING mentioning 'nav-panels'"
        )

    def test_missing_panels_marker_logs_warning(self, sample_panel):
        """If <!-- /panels --> is missing, a WARNING must be printed."""
        html = (
            '<div class="main">\n'
            '  <nav class="sidebar">\n'
            '    <div class="nav-item" data-panel="chat">Chat</div>\n'
            '    <!-- /nav-panels -->\n'
            '  </nav>\n'
            '  <div class="content">\n'
            '    <div class="panel active" id="panel-chat">Chat</div>\n'
            '  </div>\n'
            '</div>\n'
            '<div id="mobile-more-menu">\n'
            '    <!-- /mobile-menu-items -->\n'
            '</div>\n'
        )
        result, printed = self._run_injection_with_html(html, sample_panel)
        warnings = [p for p in printed if 'WARNING' in p and '<!-- /panels -->' in p]
        assert len(warnings) > 0, (
            "Missing <!-- /panels --> must produce a WARNING mentioning the marker"
        )

    def test_missing_mobile_marker_logs_warning(self, sample_panel):
        """If <!-- /mobile-menu-items --> is missing, a WARNING must be printed."""
        html = (
            '<div class="main">\n'
            '  <nav class="sidebar">\n'
            '    <div class="nav-item" data-panel="chat">Chat</div>\n'
            '    <!-- /nav-panels -->\n'
            '  </nav>\n'
            '  <div class="content">\n'
            '    <div class="panel active" id="panel-chat">Chat</div>\n'
            '  <!-- /panels -->\n'
            '  </div>\n'
            '</div>\n'
        )
        result, printed = self._run_injection_with_html(html, sample_panel)
        warnings = [p for p in printed if 'WARNING' in p and 'mobile-menu-items' in p]
        assert len(warnings) > 0, (
            "Missing <!-- /mobile-menu-items --> must produce a WARNING mentioning the marker"
        )

    def test_all_markers_missing_logs_partial_count_warning(self, sample_panel):
        """When all 3 markers are missing, a partial-count WARNING must be printed."""
        html = (
            '<div class="main">\n'
            '  <nav class="sidebar">\n'
            '    <div class="nav-item" data-panel="chat">Chat</div>\n'
            '  </nav>\n'
            '  <div class="content">\n'
            '    <div class="panel active" id="panel-chat">Chat</div>\n'
            '  </div>\n'
            '</div>\n'
        )
        result, printed = self._run_injection_with_html(html, sample_panel)
        count_warnings = [p for p in printed if 'WARNING' in p and '/3' in p]
        assert len(count_warnings) > 0, (
            "When all markers are missing, a WARNING with '0/3' or similar count must be printed"
        )

    def test_all_markers_present_no_warnings(self, sample_panel):
        """When all markers are present, no WARNING messages should be printed."""
        html = (
            '<div class="main">\n'
            '  <nav class="sidebar">\n'
            '    <div class="nav-item active" data-panel="chat">Chat</div>\n'
            '    <!-- /nav-panels -->\n'
            '  </nav>\n'
            '  <div class="content">\n'
            '    <div class="panel active" id="panel-chat">Chat</div>\n'
            '  <!-- /panels -->\n'
            '  </div>\n'
            '</div>\n'
            '<div id="mobile-more-menu">\n'
            '    <!-- /mobile-menu-items -->\n'
            '</div>\n'
        )
        result, printed = self._run_injection_with_html(html, sample_panel)
        warnings = [p for p in printed if 'WARNING' in p and 'marker' in p.lower()]
        assert len(warnings) == 0, (
            f"When all markers are present, no marker-related WARNINGs should be printed. "
            f"Got: {warnings}"
        )

    def test_one_marker_missing_logs_partial_count(self, sample_panel):
        """When exactly 1 marker is missing, the partial-count warning must show 2/3."""
        html = (
            '<div class="main">\n'
            '  <nav class="sidebar">\n'
            '    <div class="nav-item active" data-panel="chat">Chat</div>\n'
            '    <!-- /nav-panels -->\n'
            '  </nav>\n'
            '  <div class="content">\n'
            '    <div class="panel active" id="panel-chat">Chat</div>\n'
            '  <!-- /panels -->\n'
            '  </div>\n'
            '</div>\n'
            # No mobile-more-menu section at all
        )
        result, printed = self._run_injection_with_html(html, sample_panel)
        count_warnings = [p for p in printed if 'WARNING' in p and '2/3' in p]
        assert len(count_warnings) > 0, (
            "When 1 marker is missing, WARNING should report '2/3 injection markers found'"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
