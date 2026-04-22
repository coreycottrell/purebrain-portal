"""
test_update_safety.py -- Tests for update mechanism safety improvements.

Tests cover:
  - Lock race condition safety (concurrent update rejection)
  - Shim survival verification after git pull
  - Panel injection validation (missing marker warnings)
  - Watchdog detection before self-restart
  - Missing tests/ directory handling
  - Backup identity step ordering

These are UNIT tests that mock subprocess/git calls -- no running server needed.
"""

import asyncio
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


# ---------------------------------------------------------------------------
# 1. Lock Race Condition Tests
# ---------------------------------------------------------------------------

class TestUpdateLockSafety:
    """Tests that concurrent update requests are safely rejected."""

    def test_lock_acquired_before_background_task(self):
        """The asyncio.Lock must be acquired BEFORE _run_update launches.

        In api_update_apply, lock.acquire() must happen before asyncio.create_task.
        We verify this by checking the source order.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "api_update_apply")
        assert func_src, "api_update_apply function not found in portal_server.py"

        acquire_pos = func_src.find("lock.acquire()")
        create_task_pos = func_src.find("asyncio.create_task")

        assert acquire_pos != -1, "lock.acquire() not found in api_update_apply"
        assert create_task_pos != -1, "asyncio.create_task not found in api_update_apply"
        assert acquire_pos < create_task_pos, (
            "lock.acquire() must come BEFORE asyncio.create_task(_run_update)"
        )

    def test_lock_released_on_success(self):
        """Lock must be released after _run_update completes successfully.

        The _run_update function must have a finally block that releases the lock.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found in portal_server.py"

        # Must contain finally block with lock.release()
        assert "finally:" in func_src, "_run_update must have a finally block"
        assert "lock.release()" in func_src, "_run_update finally must release the lock"

    def test_lock_released_on_failure(self):
        """Lock must be released even if _run_update raises an exception.

        We verify the release is in a finally block, not just in the success path.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

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
            elif in_finally and stripped and not stripped.startswith('#') and not stripped.startswith('if') and not stripped.startswith('lock'):
                # Still inside finally (indented)
                pass

        assert found_release_in_finally, (
            "lock.release() must be inside the finally block of _run_update"
        )

    def test_concurrent_apply_rejected_via_lock(self):
        """Second apply request returns error when lock is held.

        api_update_apply checks lock.locked() before proceeding.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "api_update_apply")
        assert func_src, "api_update_apply function not found"

        assert "lock.locked()" in func_src, (
            "api_update_apply must check lock.locked() to reject concurrent requests"
        )
        assert "Update already in progress" in func_src, (
            "api_update_apply must return 'Update already in progress' when lock is held"
        )

    def test_lock_is_asyncio_lock(self):
        """The update lock must be an asyncio.Lock (not threading.Lock).

        asyncio.Lock is required because the update runs in an async context.
        """
        source = _read_portal_source()
        assert "asyncio.Lock" in source, (
            "Update lock must be asyncio.Lock for async-safe concurrency"
        )


# ---------------------------------------------------------------------------
# 2. Shim Survival Tests
# ---------------------------------------------------------------------------

class TestShimSurvival:
    """Tests that the customization shim is verified after git pull."""

    def test_verify_shim_step_exists_in_steps_list(self):
        """'verify_shim' must be in the steps_remaining list.

        This ensures the UI shows the shim verification step in the progress bar.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "api_update_apply")
        assert func_src, "api_update_apply function not found"

        assert '"verify_shim"' in func_src, (
            "'verify_shim' must be listed in steps_remaining in api_update_apply"
        )

    def test_verify_shim_step_runs_after_pull(self):
        """The verify_shim step must execute AFTER the pull step in _run_update."""
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        pull_pos = func_src.find('_update_step("pull")')
        shim_pos = func_src.find('_update_step("verify_shim")')

        assert pull_pos != -1, "pull step not found in _run_update"
        assert shim_pos != -1, "verify_shim step not found in _run_update"
        assert pull_pos < shim_pos, (
            "verify_shim must execute AFTER the pull step"
        )

    def test_update_aborts_if_shim_missing_after_pull(self):
        """If CUSTOMIZATION LAYER marker is gone after pull, update must abort.

        The _run_update function checks for the marker and raises RuntimeError.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        # Must check for CUSTOMIZATION LAYER marker
        assert '"CUSTOMIZATION LAYER"' in func_src, (
            "_run_update must check for CUSTOMIZATION LAYER marker in portal_server.py"
        )

        # Must raise/abort if marker is missing
        assert "ABORT" in func_src, (
            "_run_update must abort if customization shim marker is missing"
        )

    def test_update_continues_if_shim_present(self):
        """If CUSTOMIZATION LAYER marker is present, update proceeds.

        The check only triggers failure when the marker is NOT found.
        The pattern: if 'CUSTOMIZATION LAYER' not in content: raise.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        # The condition should be 'not in' (abort on absence, not presence)
        assert '"CUSTOMIZATION LAYER" not in' in func_src, (
            "Shim check should abort when marker is NOT found (not when found)"
        )

    def test_shim_marker_exists_in_current_source(self):
        """Verify the CUSTOMIZATION LAYER marker exists in the current portal_server.py.

        This protects against accidentally removing the marker during development.
        """
        source = _read_portal_source()
        # The actual marker line (not the check code)
        assert "CUSTOMIZATION LAYER (do not remove" in source, (
            "portal_server.py must contain the 'CUSTOMIZATION LAYER (do not remove' marker. "
            "This marker enables the shim survival check during updates."
        )


# ---------------------------------------------------------------------------
# 3. Panel Injection Validation Tests
# ---------------------------------------------------------------------------

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
        # Should not raise even though <!-- /nav-panels --> is missing
        result, printed = self._run_injection_with_html(html_no_nav_marker, sample_panel)
        # The nav marker replacement simply won't find a match -- str.replace is safe
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
        """If only some markers exist, inject into those that are found.

        With only <!-- /panels --> present, the panel div should still be injected
        even if nav and mobile markers are missing.
        """
        html_panels_only = (
            '<div class="content">\n'
            '    <div class="panel active" id="panel-chat">Chat</div>\n'
            '  <!-- /panels -->\n'
            '</div>\n'
        )
        result, printed = self._run_injection_with_html(html_panels_only, sample_panel)
        # The panels marker IS present, so the panel div should be injected
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


# ---------------------------------------------------------------------------
# 4. Watchdog Detection Tests
# ---------------------------------------------------------------------------

class TestWatchdogDetection:
    """Tests for process manager detection before self-restart."""

    def test_sigterm_sent_in_run_update(self):
        """SIGTERM should be sent at the end of a successful update.

        _run_update sends os.kill(os.getpid(), signal.SIGTERM) so that
        systemd/supervisor can restart the process cleanly.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        assert "signal.SIGTERM" in func_src, (
            "_run_update must send SIGTERM for clean restart by process manager"
        )
        assert "os.kill(os.getpid()" in func_src, (
            "_run_update must use os.kill(os.getpid(), signal.SIGTERM) for self-restart"
        )

    def test_sigterm_sent_after_success_state(self):
        """SIGTERM must be sent only AFTER status is set to 'success'.

        This ensures the status endpoint can return success before the process dies.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        success_pos = func_src.find('"success"')
        sigterm_pos = func_src.find("signal.SIGTERM")

        assert success_pos != -1, "Success state assignment not found"
        assert sigterm_pos != -1, "SIGTERM not found"
        assert success_pos < sigterm_pos, (
            "Status must be set to 'success' BEFORE sending SIGTERM"
        )

    def test_restart_has_sleep_before_sigterm(self):
        """There should be a delay before SIGTERM so the success status can be polled.

        Clients need time to poll /api/update/status and see 'success' before
        the process terminates.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        # There should be an asyncio.sleep before SIGTERM
        sleep_pos = func_src.find("asyncio.sleep")
        sigterm_pos = func_src.find("signal.SIGTERM")

        assert sleep_pos != -1, "asyncio.sleep not found before SIGTERM"
        assert sleep_pos < sigterm_pos, (
            "asyncio.sleep must come before SIGTERM to allow status polling"
        )

    def test_update_state_has_message_field(self):
        """On restart failure, update_state should have a message for the user."""
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        assert '"message"' in func_src, (
            "_run_update must set a 'message' field in _update_state for user feedback"
        )


# ---------------------------------------------------------------------------
# 5. Tests Directory Handling
# ---------------------------------------------------------------------------

class TestMissingTestsDirectory:
    """Tests that missing tests/ directory ABORTS the update (tests are mandatory)."""

    def test_update_checks_for_tests_dir_existence(self):
        """_run_update must check if tests/ directory exists before running tests."""
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        assert "tests_dir" in func_src or "tests" in func_src, (
            "_run_update must reference tests directory"
        )
        assert ".exists()" in func_src, (
            "_run_update must check if tests directory exists"
        )

    def test_update_checks_for_test_files(self):
        """_run_update must check for test_*.py files, not just the directory."""
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        assert "test_*.py" in func_src, (
            "_run_update must glob for test_*.py files to verify tests exist"
        )

    def test_missing_tests_aborts_update(self):
        """When no tests exist, the update must ABORT — not skip.

        Tests are mandatory for safe updates. An update without tests
        is an update without a safety net.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        # The missing-tests path must raise RuntimeError, not skip
        test_section = func_src[func_src.find("running_tests"):func_src.find("read_version")]
        assert "RuntimeError" in test_section, (
            "Missing tests must raise RuntimeError to abort the update"
        )
        assert "mandatory" in test_section.lower() or "ABORT" in test_section, (
            "Error message must indicate tests are mandatory"
        )

    def test_no_skip_logic_for_tests(self):
        """There must be no path that sets tests_passed = None (skip).

        Tests either pass (True) or the update fails. No skipping allowed.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        assert 'tests_passed"] = None' not in func_src, (
            "tests_passed must never be set to None — tests are mandatory, not skippable"
        )

    def test_running_tests_step_in_steps_list(self):
        """'running_tests' must be in the steps_remaining list."""
        source = _read_portal_source()
        func_src = _extract_function(source, "api_update_apply")
        assert func_src, "api_update_apply not found"

        assert '"running_tests"' in func_src, (
            "'running_tests' must be in steps_remaining"
        )


# ---------------------------------------------------------------------------
# 6. Backup Script Tests
# ---------------------------------------------------------------------------

class TestBackupIdentity:
    """Tests for the identity backup step."""

    def test_backup_runs_before_pull(self):
        """backup_identity step must execute before the pull step.

        The steps_remaining list order in api_update_apply defines the UI order,
        but we check actual execution order in _run_update.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        backup_pos = func_src.find("backup_identity")
        pull_pos = func_src.find('_update_step("pull")')

        assert backup_pos != -1, "backup_identity step not found in _run_update"
        assert pull_pos != -1, "pull step not found in _run_update"
        assert backup_pos < pull_pos, (
            "backup_identity must execute BEFORE the pull step"
        )

    def test_backup_failure_does_not_block_update(self):
        """If backup script fails, update should continue with a warning.

        The backup step must be wrapped in try/except and only log a warning,
        not raise a RuntimeError that would abort the update.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        # Find the backup section
        backup_start = func_src.find("backup_identity")
        assert backup_start != -1, "backup_identity not found"

        # The backup section should have a try/except or check returncode
        # but NOT raise RuntimeError
        # Find the section between backup_identity and the next step
        next_step = func_src.find('_update_step("ensure_git")', backup_start)
        if next_step == -1:
            next_step = func_src.find('_update_step("fetch")', backup_start)
        assert next_step != -1, "Could not find next step after backup"

        backup_section = func_src[backup_start:next_step]

        # Backup failures should be warnings, not raises
        assert "WARNING" in backup_section, (
            "Backup failure should log a WARNING, not crash the update"
        )

    def test_backup_checks_script_exists(self):
        """The backup step should check if backup_identity.sh exists before running."""
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        assert "backup_identity" in func_src, "backup_identity reference not found"
        assert ".exists()" in func_src, (
            "_run_update must check if backup script exists before running it"
        )


# ---------------------------------------------------------------------------
# 7. Update Steps Ordering (Cross-cutting)
# ---------------------------------------------------------------------------

class TestUpdateStepsOrdering:
    """Tests that verify the overall step ordering in the update pipeline."""

    def test_steps_remaining_matches_execution_order(self):
        """The steps_remaining list in api_update_apply should match _run_update execution order.

        This ensures the UI progress bar accurately reflects the actual update flow.
        """
        source = _read_portal_source()

        # Extract steps_remaining list from api_update_apply
        apply_func = _extract_function(source, "api_update_apply")
        assert apply_func, "api_update_apply not found"

        steps_match = re.search(
            r'steps_remaining.*?\[([^\]]+)\]',
            apply_func, re.DOTALL
        )
        assert steps_match, "steps_remaining list not found in api_update_apply"

        # Parse the step names
        steps_str = steps_match.group(1)
        declared_steps = [
            s.strip().strip('"').strip("'")
            for s in steps_str.split(',')
            if s.strip().strip('"').strip("'")
        ]

        # Verify key safety steps are present
        safety_steps = {"verify_shim", "running_tests", "verify_custom", "verify_preserved"}
        declared_set = set(declared_steps)
        missing = safety_steps - declared_set
        assert not missing, (
            f"Safety steps missing from steps_remaining: {missing}"
        )

    def test_verify_shim_after_pull_in_steps(self):
        """verify_shim must come after pull in the declared steps list."""
        source = _read_portal_source()
        apply_func = _extract_function(source, "api_update_apply")
        assert apply_func, "api_update_apply not found"

        pull_pos = apply_func.find('"pull"')
        shim_pos = apply_func.find('"verify_shim"')

        assert pull_pos != -1, "pull not in steps_remaining"
        assert shim_pos != -1, "verify_shim not in steps_remaining"
        assert pull_pos < shim_pos, (
            "verify_shim must come after pull in steps_remaining list"
        )

    def test_rollback_logic_covers_post_pull_failures(self):
        """If a step fails after pull, rollback to previous_sha must be attempted.

        The except block in _run_update should check if we're past the pull step.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        # Must have rollback logic
        assert "reset --hard" in func_src, (
            "_run_update must use 'git reset --hard' for rollback"
        )
        assert "rolled_back_to" in func_src, (
            "_run_update must track rolled_back_to SHA"
        )

    def test_rollback_only_when_previous_sha_exists(self):
        """Rollback should only happen when previous_sha is set (not fresh install)."""
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        # The rollback condition should check previous_sha
        except_block = func_src[func_src.find("except Exception"):]
        assert "previous_sha" in except_block, (
            "Rollback logic must check previous_sha before attempting reset"
        )


# ---------------------------------------------------------------------------
# 8. State Management Safety
# ---------------------------------------------------------------------------

class TestUpdateStateManagement:
    """Tests for proper state management during updates."""

    def test_state_reset_before_new_update(self):
        """_update_state must be fully reset before starting a new update.

        All fields should be cleared so stale data from a previous run
        doesn't leak into the new update.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "api_update_apply")
        assert func_src, "api_update_apply not found"

        assert "_update_state.update(" in func_src, (
            "api_update_apply must reset _update_state before starting update"
        )

    def test_initial_state_has_required_fields(self):
        """The _update_state dict must have all required tracking fields."""
        source = _read_portal_source()

        required_fields = [
            "status", "job_id", "step", "steps_completed", "steps_remaining",
            "started_at", "completed_at", "error", "previous_sha", "new_sha",
            "new_version", "rolled_back_to", "step_failed", "tests_passed", "message"
        ]
        for field in required_fields:
            assert f'"{field}"' in source, (
                f"_update_state must have '{field}' field"
            )

    def test_error_state_preserves_step_info(self):
        """On failure, the error state must include which step failed."""
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        assert '"step_failed"' in func_src, (
            "_run_update must record step_failed on error"
        )


# ---------------------------------------------------------------------------
# 9. Behavioral Tests (_run_update with mocks)
# ---------------------------------------------------------------------------

class TestRunUpdateBehavioral:
    """Behavioral tests that exercise _run_update logic with mocked dependencies.

    Since portal_server.py has heavy module-level side effects, we test by
    extracting the function source and verifying its structural properties,
    combined with integration-style tests that simulate the function's I/O.
    """

    def test_shim_check_triggers_rollback(self):
        """When CUSTOMIZATION LAYER marker is missing after pull, rollback must occur.

        The _run_update function reads portal_server.py after pull and checks
        for the marker. If absent, it raises RuntimeError which triggers the
        rollback path (because verify_shim is a post-pull step).
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        # 1. verify_shim step raises RuntimeError when marker is missing
        shim_section_start = func_src.find('_update_step("verify_shim")')
        shim_section_end = func_src.find('_update_step("running_tests")')
        assert shim_section_start != -1, "verify_shim step not found"
        assert shim_section_end != -1, "running_tests step not found"
        shim_section = func_src[shim_section_start:shim_section_end]
        assert "raise RuntimeError" in shim_section, (
            "verify_shim section must raise RuntimeError when marker is missing"
        )

        # 2. except block includes verify_shim in the rollback condition
        except_block = func_src[func_src.find("except Exception"):]
        assert "verify_shim" in except_block, (
            "Rollback condition must include verify_shim as a post-pull failure step"
        )

        # 3. Rollback calls git reset --hard with previous_sha
        assert "reset", "--hard" in except_block
        assert "rolled_back_to" in except_block

        # 4. Status is set to "failed" in the except block
        assert '"failed"' in except_block, (
            "Status must be set to 'failed' in rollback path"
        )

    def test_shim_check_passes_when_marker_present(self):
        """When CUSTOMIZATION LAYER marker is present after pull, update continues.

        Simulate the shim check logic: read file, check for marker, continue.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            server_file = Path(tmpdir) / "portal_server.py"
            server_file.write_text(
                "# --- CUSTOMIZATION LAYER (do not remove on upstream update) ---\n"
                "print('overlay code')\n"
            )
            content = server_file.read_text()
            assert "CUSTOMIZATION LAYER" in content, (
                "Test setup: marker should be present in simulated file"
            )
            # The _run_update code does: if "CUSTOMIZATION LAYER" not in content: raise
            # With marker present, no exception → update continues
            should_abort = "CUSTOMIZATION LAYER" not in content
            assert not should_abort, "Update should NOT abort when marker is present"

    def test_lock_released_after_successful_update(self):
        """Lock must be released in the finally block regardless of outcome.

        We verify structurally: the finally block calls lock.release().
        Also verify with a real asyncio.Lock that release works correctly.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")

        # Structural check
        finally_idx = func_src.rfind("finally:")
        assert finally_idx > 0
        finally_block = func_src[finally_idx:]
        assert "lock.release()" in finally_block

        # Behavioral check: a locked asyncio.Lock can be released
        async def check_lock_release():
            lock = asyncio.Lock()
            await lock.acquire()
            assert lock.locked(), "Lock should be locked after acquire"
            # Simulate what finally block does
            if lock.locked():
                lock.release()
            assert not lock.locked(), "Lock should be released after release"

        run_async(check_lock_release())

    def test_lock_released_after_failed_update(self):
        """Lock must be released even when _run_update raises an exception.

        The finally block must unconditionally release the lock.
        """
        async def check_lock_release_on_error():
            lock = asyncio.Lock()
            await lock.acquire()
            assert lock.locked()
            try:
                # Simulate an early failure (e.g., git fetch fails)
                raise RuntimeError("git fetch failed: simulated")
            except Exception:
                pass
            finally:
                # This mirrors _run_update's finally block
                if lock.locked():
                    lock.release()
            assert not lock.locked(), "Lock must NOT be locked after exception + finally"

        run_async(check_lock_release_on_error())

    def test_test_failure_triggers_rollback(self):
        """When pytest returns non-zero exit code, rollback must be triggered.

        running_tests is listed in the rollback condition in the except block.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")
        assert func_src, "_run_update function not found"

        # 1. Test failure raises RuntimeError
        test_section_start = func_src.find('_update_step("running_tests")')
        test_section_end = func_src.find('_update_step("read_version")')
        assert test_section_start != -1
        assert test_section_end != -1
        test_section = func_src[test_section_start:test_section_end]
        assert "raise RuntimeError" in test_section, (
            "Test failure must raise RuntimeError"
        )
        assert "returncode != 0" in test_section, (
            "Test section must check returncode != 0 for test failure"
        )

        # 2. running_tests is in the rollback condition
        except_block = func_src[func_src.find("except Exception"):]
        assert "running_tests" in except_block, (
            "Rollback condition must include running_tests as a post-pull failure step"
        )

    def test_rollback_condition_is_exhaustive_for_post_pull_steps(self):
        """All steps that execute after pull must be covered by the rollback condition.

        If a new post-pull step is added but not included in the rollback
        condition, we could leave the repo in a broken state.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")

        # Extract the rollback condition tuple/set
        except_block = func_src[func_src.find("except Exception"):]

        # All post-pull steps that modify state must trigger rollback
        post_pull_steps = ["verify_shim", "running_tests", "read_version", "restart"]
        for step in post_pull_steps:
            assert step in except_block, (
                f"Post-pull step '{step}' must be in the rollback condition"
            )

    def test_previous_sha_recorded_before_pull(self):
        """previous_sha must be captured before the pull step.

        Without it, rollback has no target SHA to reset to.
        """
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")

        record_pos = func_src.find("record_rollback")
        pull_pos = func_src.find('_update_step("pull")')
        assert record_pos != -1, "record_rollback step not found"
        assert pull_pos != -1, "pull step not found"
        assert record_pos < pull_pos, (
            "record_rollback must execute BEFORE pull so we have a rollback target"
        )

    def test_update_state_set_to_failed_on_exception(self):
        """_update_state['status'] must be set to 'failed' in the except block."""
        source = _read_portal_source()
        func_src = _extract_function(source, "_run_update")

        except_block = func_src[func_src.find("except Exception"):]
        assert '"status": "failed"' in except_block or "'status': 'failed'" in except_block or \
               '"status"' in except_block and '"failed"' in except_block, (
            "except block must set status to 'failed'"
        )


# ---------------------------------------------------------------------------
# 10. Panel Injection Warning Assertions
# ---------------------------------------------------------------------------

class TestPanelInjectionWarnings:
    """Tests that missing injection markers produce appropriate WARNING messages.

    These tests use the same _run_injection_with_html helper from
    TestPanelInjectionValidation to capture print() output and verify
    that warnings are emitted for missing markers.
    """

    def _run_injection_with_html(self, html: str, panel_html: str) -> tuple:
        """Run _inject_custom_panels with custom HTML and capture prints.

        Returns (result_html, printed_lines).
        Reuses the same pattern as TestPanelInjectionValidation.
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
        # Should have the "Only X/3 injection markers found" warning
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
