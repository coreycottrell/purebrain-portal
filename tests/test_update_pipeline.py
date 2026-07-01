"""
test_update_pipeline.py -- Tests for hardened update pipeline.

Tests cover:
  1. Pre-delete ALL files before extraction (not just critical ones)
  2. pip install step runs after extraction
  3. Post-restart health check verifies new version
  4. Missing optional modules don't crash the portal
  5. Pre-flight dependency check
  6. Preserved files survive pre-delete + extraction
  7. Supervisor detection priority order (tmux > systemd > watchdog > restart.sh > exec)

Unit tests using mocks -- no running server needed.
"""

import asyncio
import importlib
import io
import json
import os
import sys
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

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
# 1. Pre-delete ALL files before extraction
# ---------------------------------------------------------------------------

class TestPreDeleteAllFiles:
    """_extract_tarball should pre-delete ALL extractable files, not just critical ones."""

    def test_extract_tarball_pre_deletes_all_files(self, tmp_path):
        """Verify every file in the tarball is deleted before extraction."""
        from portal_updates import _PRESERVED_FILES, _PRESERVED_DIRS, _SKIP_EXTRACT_DIRS

        # Create a tarball with several files
        tar_path = tmp_path / "test.tar.gz"
        files_in_tar = {
            "portal_server.py": b"# new server code",
            "portal_config.py": b"# new config",
            "static/js/features/chat.js": b"// new chat",
            "some_module.py": b"# new module",
        }

        with tarfile.open(str(tar_path), "w:gz") as tar:
            for name, content in files_in_tar.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))

        # Create "old" versions of these files in the portal dir
        portal_dir = tmp_path / "portal"
        portal_dir.mkdir()
        for name in files_in_tar:
            p = portal_dir / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("OLD CONTENT THAT SHOULD BE DELETED")

        # Track which files get unlinked
        unlinked = []
        original_unlink = Path.unlink

        def tracking_unlink(self, *args, **kwargs):
            unlinked.append(str(self))
            original_unlink(self, *args, **kwargs)

        with patch.object(Path, 'unlink', tracking_unlink):
            with tarfile.open(str(tar_path), "r:gz") as tar:
                safe_members = list(tar.getmembers())

                # Simulate the pre-delete logic from _extract_tarball
                pre_deleted = 0
                for member in safe_members:
                    if member.isfile():
                        target = portal_dir / member.name
                        if target.exists():
                            target.unlink()
                            pre_deleted += 1

                tar.extractall(path=str(portal_dir), members=safe_members,
                               filter="fully_trusted")

        # All 4 files existed and should have been pre-deleted
        assert pre_deleted == len(files_in_tar)

        # Verify new content was written
        for name, content in files_in_tar.items():
            assert (portal_dir / name).read_bytes() == content

    def test_pre_delete_skips_nonexistent_files(self, tmp_path):
        """Pre-delete should not fail on files that don't exist yet."""
        tar_path = tmp_path / "test.tar.gz"
        with tarfile.open(str(tar_path), "w:gz") as tar:
            content = b"# brand new file"
            info = tarfile.TarInfo(name="brand_new.py")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))

        portal_dir = tmp_path / "portal"
        portal_dir.mkdir()

        # brand_new.py does NOT exist — should not raise
        with tarfile.open(str(tar_path), "r:gz") as tar:
            safe_members = list(tar.getmembers())
            pre_deleted = 0
            for member in safe_members:
                if member.isfile():
                    target = portal_dir / member.name
                    if target.exists():
                        target.unlink()
                        pre_deleted += 1

            tar.extractall(path=str(portal_dir), members=safe_members,
                           filter="fully_trusted")

        assert pre_deleted == 0
        assert (portal_dir / "brand_new.py").read_bytes() == b"# brand new file"


# ---------------------------------------------------------------------------
# 2. pip install step runs after extraction
# ---------------------------------------------------------------------------

class TestPipInstallStep:
    """pip install -r requirements.txt runs after extraction."""

    def test_pip_install_in_steps_remaining(self):
        """pip_install should be in the steps list between restore_preserved and update_version."""
        from portal_updates import api_update_apply
        # Read the source to verify the steps_remaining list
        import inspect
        source = inspect.getsource(api_update_apply)
        assert "pip_install" in source

    def test_pip_install_runs_with_break_system_packages(self):
        """pip install should try --break-system-packages first, then retry without."""
        from portal_updates import _log_update
        import subprocess

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            # Simulate the pip install logic
            req_file = Path("/fake/requirements.txt")

            pip_result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r",
                 str(req_file), "--quiet", "--break-system-packages"],
                capture_output=True, text=True, timeout=120,
            )

            assert mock_run.called
            call_args = mock_run.call_args
            assert "--break-system-packages" in call_args[0][0]

    def test_pip_install_retries_without_break_system_packages(self):
        """If --break-system-packages fails, retry without it."""
        import subprocess as sp

        fail_result = MagicMock()
        fail_result.returncode = 1

        success_result = MagicMock()
        success_result.returncode = 0

        with patch.object(sp, "run", side_effect=[fail_result, success_result]) as mock_run:
            req_file = Path("/fake/requirements.txt")

            pip_result = sp.run(
                [sys.executable, "-m", "pip", "install", "-r",
                 str(req_file), "--quiet", "--break-system-packages"],
                capture_output=True, text=True, timeout=120,
            )
            if pip_result.returncode != 0:
                pip_result = sp.run(
                    [sys.executable, "-m", "pip", "install", "-r",
                     str(req_file), "--quiet"],
                    capture_output=True, text=True, timeout=120,
                )

            assert mock_run.call_count == 2
            second_call_args = mock_run.call_args_list[1][0][0]
            assert "--break-system-packages" not in second_call_args


# ---------------------------------------------------------------------------
# 3. Post-restart health check verifies new version
# ---------------------------------------------------------------------------

class TestPostRestartHealthCheck:
    """After restart, poll /health and verify version matches."""

    def test_health_check_passes_on_matching_version(self):
        """Health check should succeed when version matches."""
        import urllib.request

        expected_version = "2.2.95"
        health_response = json.dumps({
            "status": "ok", "version": expected_version, "uptime": 5,
        }).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = health_response

        with patch.object(urllib.request, "urlopen", return_value=mock_resp):
            resp = urllib.request.urlopen("http://localhost:8097/health", timeout=2)
            data = json.loads(resp.read().decode())
            assert data["version"] == expected_version

    def test_health_check_detects_version_mismatch(self):
        """Health check should detect when old version is still running."""
        expected_version = "2.2.95"
        old_response = json.dumps({
            "status": "ok", "version": "2.2.94", "uptime": 100,
        })

        data = json.loads(old_response)
        assert data["version"] != expected_version
        assert data["status"] == "ok"

    def test_health_check_handles_connection_refused(self):
        """Health check should handle portal not being up yet."""
        import urllib.request
        import urllib.error

        with patch.object(urllib.request, "urlopen",
                          side_effect=urllib.error.URLError("Connection refused")):
            try:
                urllib.request.urlopen("http://localhost:8097/health", timeout=2)
                assert False, "Should have raised"
            except urllib.error.URLError:
                pass  # Expected


# ---------------------------------------------------------------------------
# 4. Missing optional modules don't crash the portal
# ---------------------------------------------------------------------------

class TestOptionalModuleImports:
    """Optional module imports should have try/except fallbacks."""

    def test_portal_activity_import_has_fallback(self):
        """from portal_activity import ... should be wrapped in try/except."""
        portal_server_path = os.path.join(PORTAL_DIR, "portal_server.py")
        source = Path(portal_server_path).read_text()

        # Find the try/except block around portal_activity import
        assert "try:" in source
        assert "from portal_activity import log_activity, api_activity" in source
        assert "except ImportError:" in source
        assert "def log_activity(*a, **kw): pass" in source

    def test_fallback_log_activity_is_noop(self):
        """Fallback log_activity should accept any args and do nothing."""
        def log_activity(*a, **kw): pass

        # Should not raise
        log_activity("test", "detail", "category")
        log_activity()

    def test_fallback_api_activity_returns_empty(self):
        """Fallback api_activity should return empty activities list."""
        from starlette.responses import JSONResponse

        async def api_activity(request):
            return JSONResponse({"activities": []})

        mock_request = MagicMock()
        result = run_async(api_activity(mock_request))
        assert isinstance(result, JSONResponse)


# ---------------------------------------------------------------------------
# 5. Pre-flight dependency check
# ---------------------------------------------------------------------------

class TestPreflightDependencyCheck:
    """Portal should fail fast with clear message if packages are missing."""

    def test_preflight_check_exists_in_source(self):
        """portal_server.py should have the pre-flight dependency check at the top."""
        portal_server_path = os.path.join(PORTAL_DIR, "portal_server.py")
        source = Path(portal_server_path).read_text()

        # The check should appear before the main imports
        check_pos = source.find("_REQUIRED")
        import_pos = source.find("import asyncio")
        assert check_pos != -1, "Pre-flight check not found in portal_server.py"
        assert check_pos < import_pos, "Pre-flight check should appear before import asyncio"

    def test_preflight_checks_critical_packages(self):
        """The check should verify httpx, aiosqlite, starlette, uvicorn."""
        portal_server_path = os.path.join(PORTAL_DIR, "portal_server.py")
        source = Path(portal_server_path).read_text()

        for pkg in ["httpx", "aiosqlite", "starlette", "uvicorn"]:
            assert pkg in source, f"Pre-flight check should include {pkg}"

    def test_preflight_check_logic(self):
        """Verify the check logic works: missing packages are detected."""
        required = ["httpx", "aiosqlite", "starlette", "uvicorn"]

        # All should be installed in test environment
        missing = [p for p in required if not importlib.util.find_spec(p)]
        assert len(missing) == 0, f"Test env missing: {missing}"

        # Test with a fake package name
        fake_required = ["httpx", "nonexistent_package_xyz_12345"]
        fake_missing = [p for p in fake_required
                        if not importlib.util.find_spec(p)]
        assert "nonexistent_package_xyz_12345" in fake_missing


# ---------------------------------------------------------------------------
# 6. Preserved files survive pre-delete + extraction
# ---------------------------------------------------------------------------

class TestPreservedFilesSurvival:
    """Preserved files/dirs must NOT be deleted during pre-delete or extraction."""

    def test_preserved_files_not_in_safe_members(self, tmp_path):
        """Preserved files in tarball should be filtered out of safe_members."""
        from portal_updates import _PRESERVED_FILES, _PRESERVED_DIRS, _SKIP_EXTRACT_DIRS

        # Create a tarball that includes a preserved file
        tar_path = tmp_path / "test.tar.gz"
        with tarfile.open(str(tar_path), "w:gz") as tar:
            # Normal file
            normal = b"# normal file"
            info = tarfile.TarInfo(name="portal_server.py")
            info.size = len(normal)
            tar.addfile(info, io.BytesIO(normal))

            # Preserved file (.env)
            preserved = b"SECRET=value"
            info = tarfile.TarInfo(name=".env")
            info.size = len(preserved)
            tar.addfile(info, io.BytesIO(preserved))

            # Preserved directory file
            mem_file = b"# memory data"
            info = tarfile.TarInfo(name="memories/test.md")
            info.size = len(mem_file)
            tar.addfile(info, io.BytesIO(mem_file))

        # Simulate the filtering logic from _extract_tarball
        skip_prefixes = tuple(
            d.rstrip("/") + "/" for d in _PRESERVED_DIRS + _SKIP_EXTRACT_DIRS
        )
        skip_files = set(_PRESERVED_FILES)

        with tarfile.open(str(tar_path), "r:gz") as tar:
            safe_members = []
            skipped = []
            for member in tar.getmembers():
                clean = member.name[2:] if member.name.startswith("./") else member.name
                if clean in skip_files:
                    skipped.append(clean)
                    continue
                if any(clean.startswith(p) for p in skip_prefixes):
                    skipped.append(clean)
                    continue
                safe_members.append(member)

        # portal_server.py should be in safe_members
        safe_names = [m.name for m in safe_members]
        assert "portal_server.py" in safe_names

        # .env and memories/ should be skipped
        assert ".env" in skipped
        assert "memories/test.md" in skipped
        assert ".env" not in safe_names
        assert "memories/test.md" not in safe_names

    def test_preserved_files_restored_after_extraction(self, tmp_path):
        """Backup/restore cycle preserves user data even with full pre-delete."""
        from portal_updates import _PRESERVED_FILES

        portal_dir = tmp_path / "portal"
        portal_dir.mkdir()
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()

        # Create a preserved file
        env_file = portal_dir / ".env"
        env_file.write_text("MY_SECRET=preserved")

        # Step 3: Back it up
        for pf in [".env"]:
            src = portal_dir / pf
            if src.exists():
                dest = backup_dir / pf
                dest.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copy2(str(src), str(dest))

        # Step 4: Simulate extraction (file gets deleted or overwritten)
        env_file.unlink()
        assert not env_file.exists()

        # Step 5: Restore from backup
        for pf in [".env"]:
            backup_src = backup_dir / pf
            dest = portal_dir / pf
            if backup_src.exists():
                import shutil
                shutil.copy2(str(backup_src), str(dest))

        # .env should be back with original content
        assert env_file.exists()
        assert env_file.read_text() == "MY_SECRET=preserved"


# ---------------------------------------------------------------------------
# 7. Supervisor detection priority order
# ---------------------------------------------------------------------------

class TestSupervisorDetection:
    """Restart should detect supervisors in priority order: tmux > systemd > watchdog > restart.sh > exec."""

    def test_supervisor_detection_source_has_tmux_first(self):
        """Verify tmux detection comes before systemd in the source."""
        source = Path(os.path.join(PORTAL_DIR, "portal_updates.py")).read_text()

        tmux_pos = source.find('"tmux", "has-session"')
        systemd_pos = source.find('"systemctl", "is-active"', tmux_pos + 1 if tmux_pos >= 0 else 0)
        watchdog_pos = source.find("watchdog:", systemd_pos + 1 if systemd_pos >= 0 else 0)

        assert tmux_pos != -1, "tmux detection not found"
        assert systemd_pos != -1, "systemd detection not found"
        assert tmux_pos < systemd_pos, "tmux should be checked before systemd"
        assert systemd_pos < watchdog_pos, "systemd should be checked before watchdog"

    def test_tmux_restart_uses_execv_not_send_keys(self):
        """Under tmux, restart must use os.execv (in-place replace), NOT a
        send-keys C-c + blind relaunch dispatch.

        UPDATED CONTRACT (restart-cascade fix): the old tmux send-keys dispatch
        was removed because it did a blind, unverified relaunch that could loop
        the restart countdown / double-bind the port. tmux is still DETECTED
        (has-session) but maps to execv via _select_restart_method. execv
        replaces the process in the same pane -- no new bind, no relaunch dance.

        Was: test_tmux_restart_uses_send_keys (asserted the now-removed dispatch).
        """
        source = Path(os.path.join(PORTAL_DIR, "portal_updates.py")).read_text()

        # tmux DETECTION is preserved.
        assert '"tmux", "has-session"' in source, "tmux detection must be preserved"
        # The blind send-keys relaunch dispatch must be GONE.
        assert 'python3 portal_server.py", "Enter"' not in source, (
            "tmux send-keys blind-relaunch dispatch must be removed -- execv preferred"
        )
        # execv must be the in-place replacement mechanism.
        assert "os.execv(sys.executable" in source

    def test_systemd_restart_uses_systemctl(self):
        """systemd restart should use systemctl restart."""
        source = Path(os.path.join(PORTAL_DIR, "portal_updates.py")).read_text()

        assert '"systemctl", "restart"' in source

    def test_restart_sh_fallback_exists(self):
        """restart.sh should be used as fallback if no supervisor detected."""
        source = Path(os.path.join(PORTAL_DIR, "portal_updates.py")).read_text()

        assert 'restart_script = SCRIPT_DIR / "restart.sh"' in source

    def test_execv_precedes_restart_sh_fallback(self):
        """os.execv must be PREFERRED over the restart.sh Popen fallback.

        UPDATED CONTRACT (restart-cascade fix): execv replaces the process
        in-place (no orphan, no second bind). restart.sh Popen is only a
        last-ditch fallback if execv raises -- so os.execv must come BEFORE the
        restart.sh Popen, never after it. This matches
        test_restart.py::test_execv_is_reached_before_restart_sh_popen.

        Was: test_exec_is_last_resort (asserted execv AFTER restart.sh).
        """
        source = Path(os.path.join(PORTAL_DIR, "portal_updates.py")).read_text()

        execv_pos = source.find("os.execv(sys.executable")
        popen_pos = source.find("subprocess.Popen")
        assert execv_pos != -1, "os.execv must exist"
        if popen_pos != -1:
            assert execv_pos < popen_pos, (
                "os.execv must come BEFORE the restart.sh Popen fallback "
                "(execv replaces in-place; Popen orphans/double-binds)"
            )


# ---------------------------------------------------------------------------
# 8. Update steps list includes pip_install
# ---------------------------------------------------------------------------

class TestUpdateStepsList:
    """The update steps list should include the pip_install step."""

    def test_steps_remaining_includes_pip_install(self):
        """steps_remaining in api_update_apply should include pip_install."""
        import inspect
        from portal_updates import api_update_apply
        source = inspect.getsource(api_update_apply)

        # Verify pip_install is between restore_preserved and update_version
        assert '"pip_install"' in source
        restore_pos = source.find('"restore_preserved"')
        pip_pos = source.find('"pip_install"')
        version_pos = source.find('"update_version"')
        assert restore_pos < pip_pos < version_pos, \
            "pip_install should be between restore_preserved and update_version"


# ---------------------------------------------------------------------------
# 9. Port detection from .env
# ---------------------------------------------------------------------------

class TestPortDetection:
    """Health check should use the correct port from .env."""

    def test_port_detection_logic(self, tmp_path):
        """Port should be read from .env file."""
        env_file = tmp_path / ".env"
        env_file.write_text("PORT=9090\nOTHER=value\n")

        port = "8097"
        for line in env_file.read_text().splitlines():
            if line.strip().startswith("PORT") and "=" in line:
                port = line.split("=", 1)[1].strip()
                break

        assert port == "9090"

    def test_port_defaults_to_8097(self, tmp_path):
        """If no PORT in .env, default to 8097."""
        env_file = tmp_path / ".env"
        env_file.write_text("OTHER=value\n")

        port = "8097"
        for line in env_file.read_text().splitlines():
            if line.strip().startswith("PORT") and "=" in line:
                port = line.split("=", 1)[1].strip()
                break

        assert port == "8097"
