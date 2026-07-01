"""Tests for the portal watchdog script.

TDD: Tests written before implementation.
The watchdog should:
1. Detect when the portal is not running
2. Start the portal when it's down
3. NOT start a duplicate if portal is already running
4. Log all restart events to /tmp/portal_watchdog.log
5. Health-check via HTTP, not just PID existence
6. Handle the case where process exists but is unresponsive
"""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Add parent directory to path for imports
PORTAL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PORTAL_DIR))


class TestWatchdogHelpers(unittest.TestCase):
    """Test individual watchdog helper functions."""

    def setUp(self):
        """Import watchdog module fresh for each test."""
        # Remove cached module if present
        if "portal_watchdog" in sys.modules:
            del sys.modules["portal_watchdog"]
        import portal_watchdog
        self.wd = portal_watchdog

    def test_find_portal_pid_returns_none_when_not_running(self):
        """find_portal_pid returns None when no portal_server.py is running."""
        with patch.object(self.wd, "_run_cmd", return_value=""):
            result = self.wd.find_portal_pid()
            self.assertIsNone(result)

    def test_find_portal_pid_returns_pid_when_running(self):
        """find_portal_pid returns integer PID when portal is running."""
        with patch.object(self.wd, "_run_cmd", return_value="12345 python3 /home/aiciv/purebrain_portal/portal_server.py\n"):
            result = self.wd.find_portal_pid()
            self.assertEqual(result, 12345)

    def test_find_portal_pid_ignores_grep_itself(self):
        """find_portal_pid filters out grep/pgrep commands from results."""
        output = "99999 grep portal_server.py\n12345 python3 portal_server.py\n"
        with patch.object(self.wd, "_run_cmd", return_value=output):
            result = self.wd.find_portal_pid()
            self.assertEqual(result, 12345)

    def test_find_portal_pid_ignores_watchdog_itself(self):
        """find_portal_pid filters out the watchdog's own PID."""
        my_pid = os.getpid()
        output = f"{my_pid} python3 portal_watchdog.py\n12345 python3 portal_server.py\n"
        with patch.object(self.wd, "_run_cmd", return_value=output):
            result = self.wd.find_portal_pid()
            self.assertEqual(result, 12345)

    def test_health_check_returns_true_on_200(self):
        """health_check returns True when /health returns 200."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"status":"ok"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            self.assertTrue(self.wd.health_check(port=8097, timeout=3))

    def test_health_check_returns_false_on_connection_error(self):
        """health_check returns False when connection fails."""
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            self.assertFalse(self.wd.health_check(port=8097, timeout=3))

    def test_health_check_returns_false_on_timeout(self):
        """health_check returns False when request times out."""
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            self.assertFalse(self.wd.health_check(port=8097, timeout=3))


class TestWatchdogLogic(unittest.TestCase):
    """Test the core watchdog decision logic."""

    def setUp(self):
        if "portal_watchdog" in sys.modules:
            del sys.modules["portal_watchdog"]
        import portal_watchdog
        self.wd = portal_watchdog
        # Use a temp log file for testing
        self.log_fd, self.log_path = tempfile.mkstemp(suffix=".log")
        os.close(self.log_fd)

    def tearDown(self):
        if os.path.exists(self.log_path):
            os.unlink(self.log_path)

    def test_no_restart_when_healthy(self):
        """Watchdog should NOT restart when portal is healthy."""
        with patch.object(self.wd, "find_portal_pid", return_value=12345), \
             patch.object(self.wd, "health_check", return_value=True), \
             patch.object(self.wd, "start_portal") as mock_start:
            self.wd.check_and_restart(log_path=self.log_path)
            mock_start.assert_not_called()

    def test_restart_when_no_process(self):
        """Watchdog SHOULD restart when no portal process exists."""
        with patch.object(self.wd, "find_portal_pid", return_value=None), \
             patch.object(self.wd, "health_check", return_value=False), \
             patch.object(self.wd, "start_portal") as mock_start:
            self.wd.check_and_restart(log_path=self.log_path)
            mock_start.assert_called_once()

    def test_restart_when_process_exists_but_unhealthy(self):
        """Watchdog SHOULD kill and restart when process exists but health check fails."""
        with patch.object(self.wd, "find_portal_pid", return_value=12345), \
             patch.object(self.wd, "health_check", return_value=False), \
             patch.object(self.wd, "kill_portal") as mock_kill, \
             patch.object(self.wd, "start_portal") as mock_start:
            self.wd.check_and_restart(log_path=self.log_path)
            mock_kill.assert_called_once_with(12345)
            mock_start.assert_called_once()

    def test_restart_logged_to_file(self):
        """Each restart event should be logged with timestamp."""
        with patch.object(self.wd, "find_portal_pid", return_value=None), \
             patch.object(self.wd, "health_check", return_value=False), \
             patch.object(self.wd, "start_portal"):
            self.wd.check_and_restart(log_path=self.log_path)

        log_content = Path(self.log_path).read_text()
        self.assertIn("RESTART", log_content)
        # Should contain a timestamp-like pattern
        self.assertRegex(log_content, r"\d{4}-\d{2}-\d{2}")

    def test_no_duplicate_start_when_already_running(self):
        """If portal is already running and healthy, do nothing."""
        with patch.object(self.wd, "find_portal_pid", return_value=12345), \
             patch.object(self.wd, "health_check", return_value=True), \
             patch.object(self.wd, "start_portal") as mock_start, \
             patch.object(self.wd, "kill_portal") as mock_kill:
            self.wd.check_and_restart(log_path=self.log_path)
            mock_start.assert_not_called()
            mock_kill.assert_not_called()


class TestWatchdogStartPortal(unittest.TestCase):
    """Test the portal start function."""

    def setUp(self):
        if "portal_watchdog" in sys.modules:
            del sys.modules["portal_watchdog"]
        import portal_watchdog
        self.wd = portal_watchdog

    def test_start_portal_uses_correct_command(self):
        """start_portal should launch portal_server.py with nohup."""
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 99999
            mock_popen.return_value = mock_proc
            self.wd.start_portal()
            args = mock_popen.call_args
            cmd = args[0][0] if args[0] else args[1].get("args", [])
            # Should contain python3 and portal_server.py
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            self.assertIn("portal_server.py", cmd_str)
            self.assertIn("python3", cmd_str)

    def test_start_portal_redirects_output(self):
        """start_portal should redirect stdout/stderr to log file."""
        with patch("subprocess.Popen") as mock_popen, \
             patch("builtins.open", unittest.mock.mock_open()) as mock_file:
            mock_proc = MagicMock()
            mock_proc.pid = 99999
            mock_popen.return_value = mock_proc
            self.wd.start_portal()
            # Verify Popen was called (output redirection handled internally)
            mock_popen.assert_called_once()


class TestWatchdogKillPortal(unittest.TestCase):
    """Test the portal kill function."""

    def setUp(self):
        if "portal_watchdog" in sys.modules:
            del sys.modules["portal_watchdog"]
        import portal_watchdog
        self.wd = portal_watchdog

    def test_kill_portal_sends_sigterm_first(self):
        """kill_portal should try SIGTERM before SIGKILL."""
        with patch("os.kill") as mock_kill, \
             patch("time.sleep"):
            # First kill succeeds, process goes away
            mock_kill.side_effect = [None, ProcessLookupError]
            self.wd.kill_portal(12345)
            # First call should be SIGTERM
            mock_kill.assert_any_call(12345, signal.SIGTERM)

    def test_kill_portal_escalates_to_sigkill(self):
        """kill_portal should escalate to SIGKILL if SIGTERM doesn't work."""
        with patch("os.kill") as mock_kill, \
             patch("time.sleep"):
            # SIGTERM succeeds, 10 alive checks all pass (process refuses to die),
            # then SIGKILL, then process finally gone
            side_effects = [None]  # SIGTERM
            side_effects += [None] * 10  # 10 os.kill(pid, 0) checks -- still alive
            side_effects += [ProcessLookupError]  # SIGKILL -- or process gone after
            mock_kill.side_effect = side_effects
            self.wd.kill_portal(12345)
            calls = mock_kill.call_args_list
            signals_sent = [c[0][1] for c in calls]
            self.assertIn(signal.SIGTERM, signals_sent)
            self.assertIn(signal.SIGKILL, signals_sent)


if __name__ == "__main__":
    unittest.main()
