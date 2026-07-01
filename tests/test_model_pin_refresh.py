"""Tests for the portal upgrade model-pin refresh logic.

The upgrade script (tools/upgrade-portal.sh) refreshes ~/.claude_session_model
during an upgrade. A regression let an existing, valid model pin get DOWNGRADED:

  - If no live model was detected from the session transcript AND the model file
    was older than 7 days (`find "$MODEL_FILE" -mtime +7`), the script overwrote
    the pin with a hardcoded "claude-opus-4-6[1m]".

Two problems:
  1. A model pin does NOT expire. Staleness (file age) must never trigger an
     overwrite — a CIV correctly pinned to claude-opus-4-8 would be downgraded
     to 4.6 on every upgrade once the pin was >7 days old.
  2. The hardcoded default (4.6) is below the current fleet floor (4.8).

Correct behavior:
  - Live model detected  -> write it.
  - File exists (any age) -> PRESERVE it untouched.
  - File genuinely missing -> write the current default "claude-opus-4-8[1m]".
  - Never downgrade an existing pin based on age.

These tests exercise tools/refresh-model-pin.sh in ISOLATION — no upgrade,
no restart, no pip, no scp. They run the model-pin block against a sandboxed
HOME so the real ~/.claude_session_model is never touched.
"""
import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFRESH_SCRIPT = os.path.join(REPO_ROOT, "tools", "refresh-model-pin.sh")

DEFAULT_MODEL = "claude-opus-4-8[1m]"


def _run_refresh(home, detected_model=""):
    """Run the refresh script with a sandboxed HOME.

    detected_model is passed as $1; empty string means "no live model detected".
    Returns (rc, contents_of_model_file or None if absent).
    """
    env = dict(os.environ)
    env["HOME"] = str(home)
    proc = subprocess.run(
        ["bash", REFRESH_SCRIPT, detected_model],
        capture_output=True,
        text=True,
        env=env,
    )
    model_file = os.path.join(str(home), ".claude_session_model")
    contents = None
    if os.path.isfile(model_file):
        with open(model_file) as f:
            contents = f.read().strip()
    return proc.returncode, contents


def test_refresh_script_exists():
    assert os.path.isfile(REFRESH_SCRIPT), (
        "refresh-model-pin.sh must exist for testable, isolated pin logic"
    )


def test_missing_file_no_session_writes_current_default(tmp_path):
    # No model file, no detected model -> writes the current floor (4.8), not 4.6.
    rc, contents = _run_refresh(tmp_path, detected_model="")
    assert rc == 0
    assert contents == DEFAULT_MODEL
    assert "4-6" not in contents


def test_existing_stale_pin_is_preserved_not_downgraded(tmp_path):
    # Existing 4.8 pin, file mtime > 7 days old, no detected model.
    # The old bug overwrote this with 4.6. It must now be PRESERVED.
    model_file = tmp_path / ".claude_session_model"
    model_file.write_text("claude-opus-4-8[1m]\n")
    stale = __import__("time").time() - (30 * 86400)  # 30 days ago
    os.utime(model_file, (stale, stale))

    rc, contents = _run_refresh(tmp_path, detected_model="")
    assert rc == 0
    assert contents == "claude-opus-4-8[1m]"
    assert "4-6" not in contents


def test_existing_recent_pin_is_preserved(tmp_path):
    # Existing pin, recent file, no detected model -> preserved untouched.
    model_file = tmp_path / ".claude_session_model"
    model_file.write_text("claude-opus-4-8[1m]\n")

    rc, contents = _run_refresh(tmp_path, detected_model="")
    assert rc == 0
    assert contents == "claude-opus-4-8[1m]"


def test_detected_live_model_is_written(tmp_path):
    # A live model detected from the session -> written.
    rc, contents = _run_refresh(tmp_path, detected_model="claude-opus-4-8[1m]")
    assert rc == 0
    assert contents == "claude-opus-4-8[1m]"


def test_detected_model_overwrites_missing_file(tmp_path):
    # No existing file, live model detected -> written (not the default).
    rc, contents = _run_refresh(tmp_path, detected_model="claude-sonnet-4-6")
    assert rc == 0
    assert contents == "claude-sonnet-4-6"


def test_never_writes_outdated_default(tmp_path):
    # Belt-and-suspenders: the script must not contain/emit the old 4.6 default
    # for the missing-file path.
    rc, contents = _run_refresh(tmp_path, detected_model="")
    assert contents == DEFAULT_MODEL
    assert contents != "claude-opus-4-6[1m]"
