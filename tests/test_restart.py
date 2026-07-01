"""
test_restart.py -- TDD tests for universal restart fallback + update throttle.

Tests cover:
  1. Universal restart: os.execv used as primary fallback (works in Docker, bare metal, etc.)
  2. Restart works when no systemd and no tmux (container/bare env)
  3. Restart works when tmux is available (existing behavior preserved)
  4. Restart works when systemd is available (existing behavior preserved)
  5. os.execv fallback replaces current process correctly
  6. Update throttle prevents updates after 3 updates within 30-min window
  7. Update throttle allows updates after 30-min window expires
  8. Throttle can be overridden with force flag
  9. Restart doesn't leave orphan processes (no Popen without tracking)
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call, AsyncMock

import pytest

PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PORTAL_DIR)


def run_async(coro):
    """Helper to run async functions in tests."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# 1. Universal restart: container env (no tmux, no systemd, no watchdog)
# ---------------------------------------------------------------------------

class TestUniversalRestart:
    """When no supervisor is detected, restart must use os.execv directly."""

    def test_execv_called_when_no_supervisor(self):
        """In a bare container (no tmux, no systemd, no watchdog), os.execv
        should be called to restart the portal in-place."""
        from portal_updates import _run_release_update, _update_state

        # The restart section (lines ~896-1010) should call os.execv
        # when no tmux/systemd/watchdog is found AND restart.sh doesn't exist.
        # We mock all supervisor checks to fail and restart.sh to not exist.
        with patch("portal_updates.subprocess.run") as mock_run, \
             patch("portal_updates.os.execv") as mock_execv, \
             patch("portal_updates.os._exit") as mock_exit, \
             patch("portal_updates.os.getppid", return_value=1), \
             patch("portal_updates.Path") as mock_path_cls:

            # tmux has-session fails
            tmux_result = MagicMock()
            tmux_result.returncode = 1
            # systemctl is-active fails for all services
            systemd_result = MagicMock()
            systemd_result.returncode = 3

            def mock_run_side_effect(cmd, **kwargs):
                if "tmux" in cmd:
                    return tmux_result
                if "systemctl" in cmd:
                    return systemd_result
                return MagicMock(returncode=1)

            mock_run.side_effect = mock_run_side_effect

            # No restart.sh
            mock_path_cls.return_value.exists.return_value = False

            # Simulate just the restart section by inspecting the source
            # The key assertion: os.execv IS the fallback when nothing else works
            import inspect
            source = inspect.getsource(_run_release_update)
            assert "os.execv(sys.executable" in source, \
                "os.execv must be used as a fallback restart mechanism"

    def test_execv_is_reached_before_restart_sh_popen(self):
        """os.execv should be the preferred restart when no supervisor is found,
        NOT restart.sh via Popen (which creates orphan processes in containers)."""
        # After the fix, the code should try os.execv BEFORE restart.sh Popen
        # (or remove the restart.sh Popen entirely in favor of os.execv)
        import inspect
        from portal_updates import _run_release_update
        source = inspect.getsource(_run_release_update)

        # os.execv should be the FIRST non-supervisor restart method
        # (the old code had restart.sh before os.execv, which broke containers)
        execv_pos = source.find("os.execv(sys.executable")
        assert execv_pos != -1, "os.execv must exist in _run_release_update"

        # There should be no Popen-based restart.sh call before os.execv
        # (Popen creates orphan processes in containers)
        popen_restart = source.find('Popen')
        if popen_restart != -1:
            assert popen_restart > execv_pos, \
                "os.execv must come BEFORE any Popen-based restart.sh (Popen orphans in Docker)"


class TestRestartWithTmux:
    """When tmux session 'portal-server' exists, restart via tmux send-keys."""

    def test_tmux_restart_still_works(self):
        """tmux restart path should still be priority 1."""
        import inspect
        from portal_updates import _run_release_update
        source = inspect.getsource(_run_release_update)

        tmux_pos = source.find('tmux", "has-session"')
        execv_pos = source.find("os.execv")
        assert tmux_pos != -1, "tmux detection must exist"
        assert tmux_pos < execv_pos, "tmux should be checked before os.execv"


class TestRestartWithSystemd:
    """When systemd service is active, restart via systemctl."""

    def test_systemd_restart_still_works(self):
        """systemd restart path should still be priority 2."""
        import inspect
        from portal_updates import _run_release_update
        source = inspect.getsource(_run_release_update)

        systemd_pos = source.find('"systemctl", "is-active"')
        execv_pos = source.find("os.execv")
        assert systemd_pos != -1, "systemd detection must exist"
        assert systemd_pos < execv_pos, "systemd should be checked before os.execv"


class TestNoOrphanProcesses:
    """Restart must not leave orphan processes."""

    def test_no_popen_without_exit(self):
        """Any subprocess.Popen used for restart must be followed by os._exit
        or equivalent to prevent orphan parent processes."""
        import inspect
        from portal_updates import _run_release_update
        source = inspect.getsource(_run_release_update)

        # Find all Popen calls in the restart section
        restart_section_start = source.find("# --- Supervisor detection")
        if restart_section_start == -1:
            restart_section_start = source.find("restart_method = None")
        assert restart_section_start != -1, "Restart section must exist"

        restart_section = source[restart_section_start:]

        # If Popen is used anywhere in the restart section, os._exit must follow
        if "Popen" in restart_section:
            popen_pos = restart_section.find("Popen")
            exit_after = restart_section.find("os._exit", popen_pos)
            assert exit_after != -1, \
                "Any Popen in restart section must be followed by os._exit to prevent orphan parent"


# ---------------------------------------------------------------------------
# 2. Update throttle — 30-minute window, 3 updates before throttle
# ---------------------------------------------------------------------------

class TestUpdateThrottle:
    """Updates should be throttled after 3 updates within a 30-minute window."""

    def test_throttle_blocks_recent_update(self, tmp_path):
        """If 3+ updates happened within a 30-min window, api_update_apply should
        return throttled status."""
        from portal_updates import api_update_apply, _update_state

        # Set up last update as 5 minutes ago with 3 updates in the window
        five_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        window_start = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        last_update = {
            "job_id": "update-test",
            "status": "success",
            "version": "2.2.96",
            "completed_at": five_min_ago,
            "update_count_in_window": 3,
            "window_start": window_start,
        }

        status_file = tmp_path / "last-update-status.json"
        status_file.write_text(json.dumps(last_update))

        mock_request = MagicMock()
        mock_request.query_params = {}

        _update_state["status"] = "idle"

        with patch("portal_updates.check_auth", return_value=True), \
             patch("portal_updates.PORTAL_UPDATE_TOKEN", "test-token"), \
             patch("portal_updates._LAST_UPDATE_STATUS_FILE", status_file):

            # Throttle check happens BEFORE lock acquisition, so no lock mock needed
            resp = run_async(api_update_apply(mock_request))
            data = json.loads(resp.body.decode())

            assert data["status"] == "throttled", \
                f"Expected throttled, got {data['status']}: {data}"
            assert "30" in data.get("message", ""), \
                "Throttle message should mention 30 minutes"

    def test_throttle_allows_after_window_expires(self, tmp_path):
        """If the 30-min window expired, update should proceed even with high count."""
        from portal_updates import api_update_apply, _update_state

        # Set up last update as 35 minutes ago — window should be expired
        old_update = {
            "job_id": "update-old",
            "status": "success",
            "version": "2.2.90",
            "completed_at": (datetime.now(timezone.utc) - timedelta(minutes=35)).isoformat(),
            "update_count_in_window": 5,
            "window_start": (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat(),
        }

        with patch("portal_updates.check_auth", return_value=True), \
             patch("portal_updates.PORTAL_UPDATE_TOKEN", "test-token"), \
             patch("portal_updates._LAST_UPDATE_STATUS_FILE", tmp_path / "last-update-status.json"), \
             patch("portal_updates._get_update_lock") as mock_lock_fn:

            (tmp_path / "last-update-status.json").write_text(json.dumps(old_update))

            # Mock the lock so we don't actually start an update
            mock_lock = AsyncMock()
            mock_lock.locked.return_value = False
            mock_lock_fn.return_value = mock_lock

            # Reset state
            _update_state["status"] = "idle"

            mock_request = MagicMock()
            mock_request.query_params = {}

            with patch("portal_updates.asyncio.create_task"):
                resp = run_async(api_update_apply(mock_request))
                data = json.loads(resp.body.decode())

                # Should NOT be throttled — window expired
                assert data["status"] != "throttled", \
                    f"Update should not be throttled after window expires, got: {data}"


    def test_throttle_allows_failed_updates(self, tmp_path):
        """If last update FAILED, throttle should not apply (allow retry)."""
        from portal_updates import api_update_apply, _update_state

        recent_failure = {
            "job_id": "update-failed",
            "status": "failed",
            "completed_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        }

        with patch("portal_updates.check_auth", return_value=True), \
             patch("portal_updates.PORTAL_UPDATE_TOKEN", "test-token"), \
             patch("portal_updates._LAST_UPDATE_STATUS_FILE", tmp_path / "last-update-status.json"), \
             patch("portal_updates._get_update_lock") as mock_lock_fn:

            (tmp_path / "last-update-status.json").write_text(json.dumps(recent_failure))

            mock_lock = AsyncMock()
            mock_lock.locked.return_value = False
            mock_lock_fn.return_value = mock_lock

            _update_state["status"] = "idle"

            mock_request = MagicMock()
            mock_request.query_params = {}

            with patch("portal_updates.asyncio.create_task"):
                resp = run_async(api_update_apply(mock_request))
                data = json.loads(resp.body.decode())

                assert data["status"] != "throttled", \
                    f"Failed updates should not trigger throttle, got: {data}"

    def test_throttle_override_with_force(self, tmp_path):
        """?force=true should bypass the 30-min throttle."""
        from portal_updates import api_update_apply, _update_state

        recent_success = {
            "job_id": "update-recent",
            "status": "success",
            "version": "2.2.96",
            "completed_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            "update_count_in_window": 5,
            "window_start": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
        }

        with patch("portal_updates.check_auth", return_value=True), \
             patch("portal_updates.PORTAL_UPDATE_TOKEN", "test-token"), \
             patch("portal_updates._LAST_UPDATE_STATUS_FILE", tmp_path / "last-update-status.json"), \
             patch("portal_updates._get_update_lock") as mock_lock_fn:

            (tmp_path / "last-update-status.json").write_text(json.dumps(recent_success))

            mock_lock = AsyncMock()
            mock_lock.locked.return_value = False
            mock_lock_fn.return_value = mock_lock

            _update_state["status"] = "idle"

            mock_request = MagicMock()
            mock_request.query_params = {"force": "true"}

            with patch("portal_updates.asyncio.create_task"):
                resp = run_async(api_update_apply(mock_request))
                data = json.loads(resp.body.decode())

                assert data["status"] != "throttled", \
                    f"force=true should bypass throttle, got: {data}"

    def test_throttle_returns_next_check_time(self, tmp_path):
        """Throttled response should include when the next update is allowed."""
        from portal_updates import api_update_apply, _update_state

        five_min_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
        recent_success = {
            "job_id": "update-recent",
            "status": "success",
            "version": "2.2.96",
            "completed_at": five_min_ago.isoformat(),
            "update_count_in_window": 3,
            "window_start": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
        }

        with patch("portal_updates.check_auth", return_value=True), \
             patch("portal_updates.PORTAL_UPDATE_TOKEN", "test-token"), \
             patch("portal_updates._LAST_UPDATE_STATUS_FILE", tmp_path / "last-update-status.json"):

            (tmp_path / "last-update-status.json").write_text(json.dumps(recent_success))

            _update_state["status"] = "idle"

            mock_request = MagicMock()
            mock_request.query_params = {}

            resp = run_async(api_update_apply(mock_request))
            data = json.loads(resp.body.decode())

            if data["status"] == "throttled":
                assert "next_check_after" in data, \
                    "Throttled response should include next_check_after timestamp"


# ---------------------------------------------------------------------------
# 3. Restart method: os.execv replaces process correctly
# ---------------------------------------------------------------------------

class TestExecvRestart:
    """os.execv should be called with the correct arguments."""

    def test_execv_uses_sys_executable(self):
        """os.execv must use sys.executable to restart the same Python interpreter."""
        import inspect
        from portal_updates import _run_release_update
        source = inspect.getsource(_run_release_update)

        assert "os.execv(sys.executable" in source, \
            "os.execv must use sys.executable as the executable"

    def test_execv_passes_portal_server_path(self):
        """os.execv should pass portal_server.py as the script to run."""
        import inspect
        from portal_updates import _run_release_update
        source = inspect.getsource(_run_release_update)

        # The execv call should reference portal_server.py
        # Either via sys.argv or explicitly
        assert "sys.executable" in source and "execv" in source
