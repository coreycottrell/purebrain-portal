"""Tests for the portal release version-detection logic.

The deploy script (tools/deploy-release.sh) must auto-detect PORTAL_VERSION
from portal_config.py when no explicit version arg is given. A historical
bug used a broken nested-quote regex inside a `python3 -c "..."` string that
raised a SyntaxError, was swallowed by `2>/dev/null`, and silently fell back
to a git-derived version (e.g. "v2.0.51-8024ebb"). Mislabeled releases cause
fleet CIVs to skip updates.

These tests exercise tools/detect-portal-version.sh in ISOLATION — no upload,
no scp, no curl. They verify:
  1. Bare detection returns the real PORTAL_VERSION from a config file.
  2. A missing config file / missing PORTAL_VERSION aborts with non-zero exit
     (fail-loud, never a silent fallback).
"""
import os
import subprocess
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETECT_SCRIPT = os.path.join(REPO_ROOT, "tools", "detect-portal-version.sh")


def _run_detect(portal_dir):
    """Run the detection script against portal_dir, return (rc, stdout)."""
    proc = subprocess.run(
        ["bash", DETECT_SCRIPT, portal_dir],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout.strip()


def test_detect_script_exists():
    assert os.path.isfile(DETECT_SCRIPT), (
        "detect-portal-version.sh must exist for testable, isolated detection"
    )


def test_detects_real_version(tmp_path):
    cfg = tmp_path / "portal_config.py"
    cfg.write_text('PORTAL_VERSION = "2.3.4"\nFOO = 1\n')
    rc, out = _run_detect(str(tmp_path))
    assert rc == 0
    assert out == "2.3.4"


def test_detects_single_quoted_version(tmp_path):
    cfg = tmp_path / "portal_config.py"
    cfg.write_text("PORTAL_VERSION = '9.10.11'\n")
    rc, out = _run_detect(str(tmp_path))
    assert rc == 0
    assert out == "9.10.11"


def test_detects_version_with_extra_whitespace(tmp_path):
    cfg = tmp_path / "portal_config.py"
    cfg.write_text('PORTAL_VERSION   =    "1.2.3"\n')
    rc, out = _run_detect(str(tmp_path))
    assert rc == 0
    assert out == "1.2.3"


def test_aborts_when_config_missing(tmp_path):
    # No portal_config.py in tmp_path.
    rc, out = _run_detect(str(tmp_path / "does-not-exist"))
    assert rc != 0
    assert out == ""


def test_aborts_when_version_absent(tmp_path):
    cfg = tmp_path / "portal_config.py"
    cfg.write_text("FOO = 1\nBAR = 2\n")
    rc, out = _run_detect(str(tmp_path))
    assert rc != 0
    assert out == ""


def test_detects_against_real_repo_config():
    """Sanity check: the real portal_config.py in this repo is detected."""
    rc, out = _run_detect(REPO_ROOT)
    assert rc == 0
    # Must be a dotted version, never a git-derived fallback like "2.0.51-abc".
    assert out and "-" not in out, f"got non-clean version: {out!r}"
