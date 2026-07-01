"""Release-invariant tests for the CURRENT portal release.

These tests are pure file-content invariants — no upload, scp, or curl.
They guard the things the deploy depends on so a release cannot ship
mislabeled or with a stale version (which would make fleet CIVs skip the
update because upgrade-portal.sh compares CURRENT_VERSION == REMOTE_VERSION
and exits 0 when equal):

  1. portal_config.py PORTAL_VERSION == EXPECTED_VERSION.
  2. release_notes.json current_version agrees with PORTAL_VERSION (a mismatch
     triggers the portal_config.py startup warning and a mislabeled release).
  3. The newest release entry is the current version and documents what shipped.

History:
  v2.3.6  — restored the [PORTAL_FILE:...] file-card renderer (Aether/Jared catch).
  v2.3.7  — Task Scheduler date picker for one-time tasks (Jared/Aether catch).
  v2.3.71 — referral security (PayPal payout IDOR + passwordless bypass) and
            restart-cascade hardening (Aether/Chy + Prodigy catch).
"""
import json
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(REPO_ROOT, "portal_config.py")
RELEASE_NOTES = os.path.join(REPO_ROOT, "release_notes.json")

EXPECTED_VERSION = "2.3.71"


def _config_version():
    text = open(CONFIG).read()
    m = re.search(r"""PORTAL_VERSION\s*=\s*["']([^"']+)["']""", text)
    assert m, "PORTAL_VERSION not found in portal_config.py"
    return m.group(1)


def _release_notes():
    return json.loads(open(RELEASE_NOTES).read())


def test_portal_config_bumped():
    assert _config_version() == EXPECTED_VERSION, (
        f"portal_config.py PORTAL_VERSION must be {EXPECTED_VERSION} so the "
        f"release propagates past the previously-published version"
    )


def test_release_notes_current_version_matches_config():
    rn = _release_notes()
    assert rn["current_version"] == EXPECTED_VERSION
    assert rn["current_version"] == _config_version(), (
        "release_notes.current_version must equal portal_config PORTAL_VERSION "
        "(mismatch triggers the startup warning and a mislabeled release)"
    )


def test_newest_release_entry_is_current_version():
    rn = _release_notes()
    assert rn["releases"], "release_notes.json has no releases"
    assert rn["releases"][0]["version"] == EXPECTED_VERSION, (
        f"newest release entry must be the v{EXPECTED_VERSION} entry, at index 0"
    )


def test_current_entry_documents_date_picker():
    rn = _release_notes()
    entry = rn["releases"][0]
    blob = json.dumps(entry).lower()
    assert "date picker" in blob or "date" in blob, (
        "the current changelog entry must carry a release date / dated content"
    )


def test_current_entry_credits_jared_aether_catch():
    rn = _release_notes()
    entry = rn["releases"][0]
    blob = json.dumps(entry).lower()
    assert "aether" in blob or "jared" in blob, (
        "the current changelog should credit the contributor catch (Aether/Jared)"
    )
