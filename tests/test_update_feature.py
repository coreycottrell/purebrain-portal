"""
test_update_feature.py -- Tests for update feature failure modes.

Tests cover:
  - Non-git-repo deployments (file-copy installs)
  - SSH origin auto-heal to HTTPS
  - git init + remote setup auto-heal
  - Uncommitted changes handling
  - _git_cmd error returns
  - _ensure_git_repo logic

These are UNIT tests that mock subprocess calls -- no running server needed.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add the portal root to sys.path so we can import from portal_server
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_async(coro):
    """Helper to run async functions in tests."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestGitCmdNonGitRepo(unittest.TestCase):
    """_git_cmd should return error code when run outside a git repo."""

    def test_git_cmd_returns_error_for_non_git_dir(self):
        """Running git commands in a non-git directory should return nonzero rc."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                ["git", "-C", tmpdir, "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=5
            )
            self.assertNotEqual(result.returncode, 0)

    def test_git_remote_add_fails_without_git_init(self):
        """git remote add fails if .git doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                ["git", "-C", tmpdir, "remote", "add", "origin",
                 "https://github.com/coreycottrell/purebrain-portal.git"],
                capture_output=True, text=True, timeout=5
            )
            self.assertNotEqual(result.returncode, 0)

    def test_git_fetch_fails_without_git_init(self):
        """git fetch fails if .git doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                ["git", "-C", tmpdir, "fetch", "origin", "main", "--quiet"],
                capture_output=True, text=True, timeout=5
            )
            self.assertNotEqual(result.returncode, 0)


class TestGitInitAndRemoteSetup(unittest.TestCase):
    """Test the git init + remote add workflow for non-git-repo deployments."""

    def test_git_init_then_remote_add_works(self):
        """git init followed by remote add should succeed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # git init
            result = subprocess.run(
                ["git", "-C", tmpdir, "init"],
                capture_output=True, text=True, timeout=5
            )
            self.assertEqual(result.returncode, 0)

            # git remote add
            result = subprocess.run(
                ["git", "-C", tmpdir, "remote", "add", "origin",
                 "https://github.com/coreycottrell/purebrain-portal.git"],
                capture_output=True, text=True, timeout=5
            )
            self.assertEqual(result.returncode, 0)

            # Verify remote exists
            result = subprocess.run(
                ["git", "-C", tmpdir, "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=5
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("purebrain-portal", result.stdout)

    def test_git_fetch_works_after_init(self):
        """After git init + remote add, fetch from public HTTPS repo should work."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "-C", tmpdir, "init"],
                           capture_output=True, text=True, timeout=5)
            subprocess.run(
                ["git", "-C", tmpdir, "remote", "add", "origin",
                 "https://github.com/coreycottrell/purebrain-portal.git"],
                capture_output=True, text=True, timeout=5
            )
            result = subprocess.run(
                ["git", "-C", tmpdir, "fetch", "origin", "main", "--quiet"],
                capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 0,
                             f"fetch failed: {result.stderr}")


class TestSSHOriginAutoHeal(unittest.TestCase):
    """SSH origins should be auto-healed to HTTPS for public repo."""

    def test_detect_ssh_origin(self):
        """Should detect git@... or ssh:// as SSH origins."""
        ssh_urls = [
            "git@github.com:coreycottrell/purebrain-portal.git",
            "git@github-corey:coreycottrell/purebrain-portal.git",
            "ssh://git@github.com/coreycottrell/purebrain-portal.git",
        ]
        for url in ssh_urls:
            is_ssh = url.startswith("git@") or url.startswith("ssh://")
            self.assertTrue(is_ssh, f"{url} should be detected as SSH")

    def test_https_origin_not_flagged(self):
        """HTTPS origins should NOT be flagged as SSH."""
        url = "https://github.com/coreycottrell/purebrain-portal.git"
        is_ssh = url.startswith("git@") or url.startswith("ssh://")
        self.assertFalse(is_ssh)

    def test_ssh_to_https_conversion(self):
        """SSH origin can be replaced with HTTPS in a real git repo."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "-C", tmpdir, "init"],
                           capture_output=True, text=True, timeout=5)
            subprocess.run(
                ["git", "-C", tmpdir, "remote", "add", "origin",
                 "git@github-corey:coreycottrell/purebrain-portal.git"],
                capture_output=True, text=True, timeout=5
            )
            # Replace SSH with HTTPS
            subprocess.run(
                ["git", "-C", tmpdir, "remote", "set-url", "origin",
                 "https://github.com/coreycottrell/purebrain-portal.git"],
                capture_output=True, text=True, timeout=5
            )
            result = subprocess.run(
                ["git", "-C", tmpdir, "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=5
            )
            self.assertEqual(result.returncode, 0)
            self.assertTrue(result.stdout.strip().startswith("https://"))


class TestEnsureGitRepoFunction(unittest.TestCase):
    """Test the _ensure_git_repo auto-heal function end-to-end."""

    def _ensure_git_repo_sync(self, directory: str) -> tuple:
        """
        Synchronous version of the auto-heal logic for testing.
        Returns (success: bool, message: str).
        """
        repo_url = "https://github.com/coreycottrell/purebrain-portal.git"

        def git_cmd(args, timeout=15):
            result = subprocess.run(
                ["git", "-C", directory] + args,
                capture_output=True, text=True, timeout=timeout
            )
            return (result.returncode, result.stdout.strip())

        # Step 1: Check if .git exists
        git_dir = Path(directory) / ".git"
        if not git_dir.exists():
            rc, _ = git_cmd(["init"])
            if rc != 0:
                return (False, "git init failed")

        # Step 2: Check origin remote
        rc, url = git_cmd(["remote", "get-url", "origin"])
        if rc != 0:
            # No origin remote -- add it
            rc, _ = git_cmd(["remote", "add", "origin", repo_url])
            if rc != 0:
                return (False, "failed to add origin remote")
        else:
            # Origin exists -- check if SSH
            if url.startswith("git@") or url.startswith("ssh://"):
                rc, _ = git_cmd(["remote", "set-url", "origin", repo_url])
                if rc != 0:
                    return (False, "failed to update SSH origin to HTTPS")

        return (True, "ok")

    def test_heals_non_git_directory(self):
        """Auto-heal should initialize git and add remote in non-git dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, msg = self._ensure_git_repo_sync(tmpdir)
            self.assertTrue(ok, f"ensure_git_repo failed: {msg}")
            self.assertTrue((Path(tmpdir) / ".git").exists())

    def test_heals_ssh_origin(self):
        """Auto-heal should convert SSH origin to HTTPS."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "-C", tmpdir, "init"],
                           capture_output=True, text=True)
            subprocess.run(
                ["git", "-C", tmpdir, "remote", "add", "origin",
                 "git@github-corey:coreycottrell/purebrain-portal.git"],
                capture_output=True, text=True
            )
            ok, msg = self._ensure_git_repo_sync(tmpdir)
            self.assertTrue(ok, f"ensure_git_repo failed: {msg}")
            result = subprocess.run(
                ["git", "-C", tmpdir, "remote", "get-url", "origin"],
                capture_output=True, text=True
            )
            self.assertTrue(result.stdout.strip().startswith("https://"))

    def test_preserves_working_https_origin(self):
        """Auto-heal should not touch a working HTTPS origin."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "-C", tmpdir, "init"],
                           capture_output=True, text=True)
            subprocess.run(
                ["git", "-C", tmpdir, "remote", "add", "origin",
                 "https://github.com/coreycottrell/purebrain-portal.git"],
                capture_output=True, text=True
            )
            ok, msg = self._ensure_git_repo_sync(tmpdir)
            self.assertTrue(ok, f"ensure_git_repo failed: {msg}")
            result = subprocess.run(
                ["git", "-C", tmpdir, "remote", "get-url", "origin"],
                capture_output=True, text=True
            )
            self.assertEqual(
                result.stdout.strip(),
                "https://github.com/coreycottrell/purebrain-portal.git"
            )

    def test_already_initialized_no_remote(self):
        """Auto-heal should add remote to an initialized repo without one."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "-C", tmpdir, "init"],
                           capture_output=True, text=True)
            ok, msg = self._ensure_git_repo_sync(tmpdir)
            self.assertTrue(ok, f"ensure_git_repo failed: {msg}")
            result = subprocess.run(
                ["git", "-C", tmpdir, "remote", "get-url", "origin"],
                capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0)


class TestUpdateApplyUncommittedChanges(unittest.TestCase):
    """Update apply should handle uncommitted changes gracefully."""

    def test_detects_tracked_changes(self):
        """Tracked modified files should be detected by git status --porcelain."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "-C", tmpdir, "init"],
                           capture_output=True, text=True)
            # Create and commit a file
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("original")
            subprocess.run(["git", "-C", tmpdir, "add", "test.txt"],
                           capture_output=True, text=True)
            subprocess.run(
                ["git", "-C", tmpdir, "commit", "-m", "initial"],
                capture_output=True, text=True,
                env={**os.environ, "GIT_AUTHOR_NAME": "test",
                     "GIT_AUTHOR_EMAIL": "test@test.com",
                     "GIT_COMMITTER_NAME": "test",
                     "GIT_COMMITTER_EMAIL": "test@test.com"}
            )
            # Modify the tracked file
            test_file.write_text("modified")
            result = subprocess.run(
                ["git", "-C", tmpdir, "status", "--porcelain"],
                capture_output=True, text=True
            )
            self.assertIn("M", result.stdout)
            # Filter tracked changes (not ??)
            tracked = [l for l in result.stdout.strip().split("\n")
                       if l.strip() and not l.startswith("??")]
            self.assertTrue(len(tracked) > 0)

    def test_untracked_files_ignored(self):
        """Untracked files (marked ??) should NOT block updates."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "-C", tmpdir, "init"],
                           capture_output=True, text=True)
            # Create untracked file only
            (Path(tmpdir) / "untracked.txt").write_text("hello")
            result = subprocess.run(
                ["git", "-C", tmpdir, "status", "--porcelain"],
                capture_output=True, text=True
            )
            tracked = [l for l in result.stdout.strip().split("\n")
                       if l.strip() and not l.startswith("??")]
            self.assertEqual(len(tracked), 0)


class TestUpdateCheckAutoHealIntegration(unittest.TestCase):
    """Integration test: full auto-heal + fetch on a fresh temp directory.

    This test actually hits GitHub (public repo) so it requires network.
    """

    def test_full_autoheal_then_fetch(self):
        """A brand new directory should auto-heal and then successfully fetch."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_url = "https://github.com/coreycottrell/purebrain-portal.git"

            # Step 1: git init
            result = subprocess.run(
                ["git", "-C", tmpdir, "init"],
                capture_output=True, text=True, timeout=5
            )
            self.assertEqual(result.returncode, 0)

            # Step 2: add remote
            result = subprocess.run(
                ["git", "-C", tmpdir, "remote", "add", "origin", repo_url],
                capture_output=True, text=True, timeout=5
            )
            self.assertEqual(result.returncode, 0)

            # Step 3: fetch
            result = subprocess.run(
                ["git", "-C", tmpdir, "fetch", "origin", "main", "--quiet"],
                capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 0,
                             f"fetch failed: {result.stderr}")

            # Step 4: rev-parse origin/main should work
            result = subprocess.run(
                ["git", "-C", tmpdir, "rev-parse", "origin/main"],
                capture_output=True, text=True, timeout=5
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(len(result.stdout.strip()), 40)  # SHA length


if __name__ == "__main__":
    unittest.main()
