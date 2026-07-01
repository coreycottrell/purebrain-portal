"""
test_restart_method_selection.py -- TDD RED tests for the restart-cascade fix.

CONTEXT (the bug):
  In portal_updates.py, _run_release_update's post-update restart cascade detects
  a supervisor and picks restart_method in priority order:
      tmux (P1) -> systemd (P2) -> watchdog (P3) -> os.execv (P4) -> restart.sh (P5)
  Portals run INSIDE a tmux session named 'portal-server', so they hit the tmux
  path FIRST. The tmux path does `tmux send-keys C-c` then blindly re-runs
  `python3 portal_server.py` then `os._exit(0)` with NO verification the new
  process bound the port -> the restart countdown loops / never cleanly relaunches.

THE INTENDED FIX:
  Extract a PURE decision helper:
      _select_restart_method(tmux_present, systemd_svc, watchdog_name) -> str
  with this mapping (AFTER the fix):
      - systemd service present      -> "systemd:<svc>"   (supervisor owns lifecycle)
      - watchdog parent (no systemd) -> "watchdog:<name>" (supervisor owns lifecycle)
      - tmux present (no sysd/wdog)   -> "execv"           <-- THE KEY CHANGE (was "tmux")
      - nothing present              -> "execv"
  Under tmux, os.execv replaces the process in-place inside the same pane: no C-c
  dance, no double-bind, no unverified relaunch. systemd/watchdog must stay
  supervisor-driven because execv would fight the supervisor's own restart.

RED STATUS (read carefully):
  - The decision-function tests below import/call `_select_restart_method`, which
    does NOT exist yet. They will FAIL with AttributeError. That AttributeError IS
    the RED contract -- it defines the function the coder must add. (Documented
    per-test in docstrings.)
  - test_tmux_branch_does_not_blind_relaunch_ahead_of_execv is a STATIC-SOURCE
    assertion that FAILS today on a REAL assertion (not AttributeError) and will
    PASS after the fix. This is the meaningful RED proof of the behavior change.

Conventions mirror tests/test_restart.py: sys.path insert, inspect.getsource for
static-source analysis, class-based grouping. No real process forking is exercised.
"""

import inspect
import os
import sys

import pytest

PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PORTAL_DIR)


# ---------------------------------------------------------------------------
# Decision-function contract tests.
#
# These define the contract for the extracted pure helper
# `_select_restart_method`. Until the coder adds that function, importing it
# raises AttributeError -> these ERROR/FAIL. That is the intended RED.
# ---------------------------------------------------------------------------

class TestSelectRestartMethod:
    """Pure decision helper: tmux -> execv (the key change), supervisors preserved."""

    def test_tmux_environment_selects_execv(self):
        """tmux present, NO systemd, NO watchdog -> 'execv' (NOT 'tmux').

        THE KEY CHANGE. Under tmux, execv replaces the process in-place in the
        same pane -- no C-c send-keys dance, no unverified blind relaunch.

        RED: `_select_restart_method` does not exist yet -> AttributeError until
        the coder extracts it. After extraction, this asserts the new mapping.
        Currently the inline logic sets restart_method='tmux', so this behavior
        does not exist today.
        """
        import portal_updates
        result = portal_updates._select_restart_method(
            tmux_present=True, systemd_svc=None, watchdog_name=None
        )
        assert result == "execv", (
            f"Under tmux (no systemd/watchdog) the chosen method must be 'execv', "
            f"got {result!r}. execv replaces the process in-place -- no blind "
            f"send-keys relaunch."
        )

    def test_systemd_environment_selects_systemd(self):
        """systemd service present -> 'systemd:<svc>' (supervisor owns lifecycle).

        Must keep working: execv would fight a systemd-managed restart.
        RED: AttributeError until helper exists; then asserts mapping preserved.
        """
        import portal_updates
        result = portal_updates._select_restart_method(
            tmux_present=False, systemd_svc="portal-server", watchdog_name=None
        )
        assert result == "systemd:portal-server", (
            f"systemd service present must map to 'systemd:portal-server', "
            f"got {result!r}."
        )

    def test_systemd_takes_priority_over_tmux(self):
        """systemd present AND tmux present -> systemd wins (supervisor priority).

        Even inside a tmux pane, if systemd manages the service, the supervisor
        must own the restart -- execv/tmux would double-restart.
        RED: AttributeError until helper exists.
        """
        import portal_updates
        result = portal_updates._select_restart_method(
            tmux_present=True, systemd_svc="purebrain-portal", watchdog_name=None
        )
        assert result == "systemd:purebrain-portal", (
            f"systemd must take priority over tmux, got {result!r}."
        )

    def test_watchdog_environment_selects_watchdog(self):
        """watchdog parent present, NO systemd -> 'watchdog:<name>'.

        Process-manager (supervisord/s6/etc.) must own the restart.
        RED: AttributeError until helper exists.
        """
        import portal_updates
        result = portal_updates._select_restart_method(
            tmux_present=False, systemd_svc=None, watchdog_name="supervisord"
        )
        assert result == "watchdog:supervisord", (
            f"watchdog parent (no systemd) must map to 'watchdog:supervisord', "
            f"got {result!r}."
        )

    def test_watchdog_takes_priority_over_tmux(self):
        """watchdog parent present AND tmux present (no systemd) -> watchdog wins.

        RED: AttributeError until helper exists.
        """
        import portal_updates
        result = portal_updates._select_restart_method(
            tmux_present=True, systemd_svc=None, watchdog_name="s6-supervise"
        )
        assert result == "watchdog:s6-supervise", (
            f"watchdog must take priority over tmux, got {result!r}."
        )

    def test_bare_environment_selects_execv(self):
        """Nothing present (Docker/bare metal) -> 'execv'.

        RED: AttributeError until helper exists.
        """
        import portal_updates
        result = portal_updates._select_restart_method(
            tmux_present=False, systemd_svc=None, watchdog_name=None
        )
        assert result == "execv", (
            f"With no supervisor at all the method must be 'execv', got {result!r}."
        )

    def test_function_exists_and_is_callable(self):
        """The extracted helper `_select_restart_method` must exist and be callable.

        RED: this fails today because the decision logic is still inline in
        _run_release_update (lines ~971-1011) and has not been extracted.
        Defines the API surface the coder must create.
        """
        import portal_updates
        assert hasattr(portal_updates, "_select_restart_method"), (
            "portal_updates must expose a pure decision helper "
            "`_select_restart_method(tmux_present, systemd_svc, watchdog_name)`."
        )
        assert callable(portal_updates._select_restart_method)


# ---------------------------------------------------------------------------
# Static-source regression: the tmux blind-relaunch must NOT be the primary
# path ahead of execv.
#
# This test FAILS TODAY on a REAL assertion (not AttributeError) and will PASS
# after the fix. It is the meaningful RED proof that behavior changed, and it
# does not depend on the helper being extracted.
# ---------------------------------------------------------------------------

class TestTmuxBlindRelaunchRemoved:
    """Under tmux, the C-c send-keys + blind re-run + os._exit dance must no
    longer be the chosen primary restart path ahead of os.execv."""

    def test_tmux_branch_does_not_blind_relaunch_ahead_of_execv(self):
        """The `if restart_method == 'tmux'` execution branch that does
        `tmux send-keys ... C-c` followed by a blind `python3 portal_server.py`
        relaunch and `os._exit(0)` must NOT execute before os.execv is reached.

        CURRENT CODE (RED): _run_release_update has::
            if restart_method == "tmux":
                ... tmux send-keys ... "C-c" ...
                ... "cd {SCRIPT_DIR} && python3 portal_server.py", "Enter" ...
                os._exit(0)
        which runs BEFORE the os.execv branch. After the fix, tmux maps to
        'execv', so this dedicated blind-relaunch dispatch branch must be gone
        (or no longer reached as the primary path).

        REAL assertion failure today (string IS present in source); PASSES after
        fix when the blind-relaunch send-keys dispatch is removed.
        """
        from portal_updates import _run_release_update
        source = inspect.getsource(_run_release_update)

        execv_pos = source.find("os.execv(sys.executable")
        assert execv_pos != -1, "os.execv(sys.executable ...) must exist as the restart mechanism"

        # The blind-relaunch signature: a send-keys that re-runs portal_server.py.
        blind_relaunch_pos = source.find('python3 portal_server.py", "Enter"')

        # After the fix this dedicated blind tmux relaunch dispatch must be gone.
        assert blind_relaunch_pos == -1, (
            "The tmux blind-relaunch dispatch (send-keys "
            "'cd ... && python3 portal_server.py' + os._exit) must be removed -- "
            "under tmux the restart must go through os.execv (in-place replace), "
            "not a C-c + unverified re-run. Found it still present in source."
        )

    def test_tmux_method_string_not_assigned_as_chosen_method(self):
        """The chosen restart_method must never be the literal 'tmux' anymore.

        CURRENT CODE (RED): line ~981 does `restart_method = "tmux"` when a
        tmux session is detected. After the fix, tmux detection should map to
        'execv' (via the helper), so no `restart_method = "tmux"` assignment
        should remain.

        REAL assertion failure today; PASSES after fix.
        """
        from portal_updates import _run_release_update
        source = inspect.getsource(_run_release_update)

        # Normalize whitespace around '=' to catch `="tmux"` and `= "tmux"`.
        assigns_tmux = (
            'restart_method = "tmux"' in source
            or "restart_method = 'tmux'" in source
            or 'restart_method="tmux"' in source
        )
        assert not assigns_tmux, (
            "restart_method must no longer be assigned the literal 'tmux' -- "
            "under tmux the method must resolve to 'execv'."
        )
