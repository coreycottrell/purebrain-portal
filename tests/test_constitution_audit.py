"""
test_constitution_audit.py -- TDD RED phase tests for Constitution Audit Trail.

What we're verifying: An append-only audit trail that logs every rule mutation
    (create, update, delete, toggle) to constitution/audit-log.jsonl, and a new
    GET /api/constitution/audit-log endpoint that returns paginated entries in
    reverse chronological order.
What coder discovers next: Implementation of audit-log appending on every mutation
    and the /api/constitution/audit-log endpoint with pagination support.
What descendants inherit: Full API contract for audit trail verification -- the
    mechanism that makes every constitutional change traceable and accountable.
Why this matters: An append-only audit trail is the foundation of constitutional
    accountability. Every rule change must be witnessed, recorded, and queryable.
    Without this, governance has no memory.

Endpoint under test:
  GET /api/constitution/audit-log  - List audit trail entries (paginated)

Mutation events under test (side-effects of existing CRUD endpoints):
  POST   /api/constitution/rules       -> appends action="create" entry
  PUT    /api/constitution/rules/{id}  -> appends action="update" entry
  DELETE /api/constitution/rules/{id}  -> appends action="delete" entry
  PUT    /api/constitution/rules/{id}  -> appends action="toggle" when status changes

These tests WILL FAIL (RED phase). The endpoint and logging do not exist yet.
"""

import unittest
import requests
import uuid
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import BASE_URL, load_token

TOKEN = load_token()
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rule_payload(*, title=None, description=None, scope="global",
                       priority="critical", enforcement="hard",
                       status="enforced", category="Safety"):
    """Build a valid rule creation payload with sensible defaults.

    Uses a UUID suffix so every test run produces unique titles, preventing
    collisions across parallel or repeated runs.
    """
    suffix = uuid.uuid4().hex[:8]
    return {
        "title": title or f"Audit Test Rule {suffix}",
        "description": description or f"Auto-generated audit test rule {suffix}",
        "scope": scope,
        "priority": priority,
        "enforcement": enforcement,
        "status": status,
        "category": category,
        "created_by": "tester-agent",
    }


def _create_rule(payload=None):
    """POST a new rule and return the response. Caller checks status."""
    payload = payload or _make_rule_payload()
    return requests.post(
        f"{BASE_URL}/api/constitution/rules",
        json=payload,
        headers=AUTH_HEADERS,
        timeout=10,
    )


def _delete_rule(rule_id):
    """DELETE a rule by id. Best-effort cleanup; ignores errors."""
    try:
        requests.delete(
            f"{BASE_URL}/api/constitution/rules/{rule_id}",
            headers=AUTH_HEADERS,
            timeout=10,
        )
    except Exception:
        pass


def _get_audit_log(params=None):
    """GET /api/constitution/audit-log with optional query params."""
    return requests.get(
        f"{BASE_URL}/api/constitution/audit-log",
        params=params,
        headers=AUTH_HEADERS,
        timeout=10,
    )


def _find_audit_entry(entries, *, rule_id=None, action=None):
    """Find the first audit entry matching the given rule_id and/or action.

    Searches from the beginning of entries (which should already be in
    reverse chronological order, so index 0 = newest).
    """
    for entry in entries:
        match = True
        if rule_id is not None and entry.get("rule_id") != rule_id:
            match = False
        if action is not None and entry.get("action") != action:
            match = False
        if match:
            return entry
    return None


# ===========================================================================
# 1. GET /api/constitution/audit-log -- Endpoint basics
# ===========================================================================

class TestAuditLogEndpoint(unittest.TestCase):
    """Verify the audit-log listing endpoint basics: auth, shape, pagination."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

    # -- Auth --

    def test_audit_log_requires_auth(self):
        """GET /api/constitution/audit-log without token returns 401."""
        resp = requests.get(
            f"{BASE_URL}/api/constitution/audit-log",
            timeout=5,
        )
        self.assertEqual(resp.status_code, 401)

    # -- Basic response shape --

    def test_audit_log_returns_200_with_entries_key(self):
        """GET /api/constitution/audit-log with auth returns 200 and
        a dict with 'entries' list and 'total' integer."""
        resp = _get_audit_log()
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsInstance(data, dict)
        self.assertIn("entries", data)
        self.assertIsInstance(data["entries"], list)
        self.assertIn("total", data)
        self.assertIsInstance(data["total"], int)

    # -- Default pagination --

    def test_audit_log_default_limit_50(self):
        """Without params, returns at most 50 entries (the default limit)."""
        resp = _get_audit_log()
        self.assertEqual(resp.status_code, 200)
        entries = resp.json()["entries"]
        self.assertLessEqual(
            len(entries), 50,
            "Default limit should cap entries at 50",
        )

    # -- Custom limit --

    def test_audit_log_custom_limit(self):
        """?limit=5 returns at most 5 entries."""
        resp = _get_audit_log(params={"limit": 5})
        self.assertEqual(resp.status_code, 200)
        entries = resp.json()["entries"]
        self.assertLessEqual(
            len(entries), 5,
            "Custom limit=5 should cap entries at 5",
        )

    # -- Offset pagination --

    def test_audit_log_offset_pagination(self):
        """?offset=N skips the first N entries.

        Create 3 rules so there are at least 3 audit entries, then verify
        offset=1 returns one fewer entry at the start.
        """
        created_ids = []
        for _ in range(3):
            resp = _create_rule()
            if resp.status_code == 201:
                created_ids.append(resp.json()["id"])

        try:
            # Get a small number of entries to ensure limit isn't saturated
            resp_all = _get_audit_log(params={"limit": 5})
            self.assertEqual(resp_all.status_code, 200)
            all_entries = resp_all.json()["entries"]

            if len(all_entries) < 2:
                self.skipTest("Need at least 2 audit entries to test offset")

            # Get with offset=1 and same limit
            resp_offset = _get_audit_log(params={"limit": 5, "offset": 1})
            self.assertEqual(resp_offset.status_code, 200)
            offset_entries = resp_offset.json()["entries"]

            # The first entry with offset=1 should match the second entry
            # from the un-offset response (offset shifts the window)
            # Note: if total entries > limit, both may return `limit` entries
            # so we only check content alignment, not count
            # The first entry with offset=1 should match the second entry
            # from the un-offset response
            if len(all_entries) >= 2 and len(offset_entries) >= 1:
                self.assertEqual(
                    offset_entries[0].get("timestamp"),
                    all_entries[1].get("timestamp"),
                    "First entry at offset=1 should match second entry at offset=0",
                )
        finally:
            for rule_id in created_ids:
                _delete_rule(rule_id)

    # -- Ordering --

    def test_audit_log_reverse_chronological_order(self):
        """Entries are returned newest-first (reverse chronological).

        Create two rules with a small gap, verify the first entry in the
        response is the more recent one.
        """
        created_ids = []
        resp1 = _create_rule(_make_rule_payload(title="Chrono Test First"))
        if resp1.status_code == 201:
            created_ids.append(resp1.json()["id"])

        # Small delay to ensure distinct timestamps
        time.sleep(0.1)

        resp2 = _create_rule(_make_rule_payload(title="Chrono Test Second"))
        if resp2.status_code == 201:
            created_ids.append(resp2.json()["id"])

        try:
            resp = _get_audit_log(params={"limit": 10})
            self.assertEqual(resp.status_code, 200)
            entries = resp.json()["entries"]

            if len(entries) < 2:
                self.skipTest("Need at least 2 audit entries to verify ordering")

            # Timestamps should be descending (newest first)
            for i in range(len(entries) - 1):
                self.assertGreaterEqual(
                    entries[i]["timestamp"],
                    entries[i + 1]["timestamp"],
                    f"Entry {i} timestamp ({entries[i]['timestamp']}) should be "
                    f">= entry {i+1} timestamp ({entries[i+1]['timestamp']})",
                )
        finally:
            for rule_id in created_ids:
                _delete_rule(rule_id)


# ===========================================================================
# 2. Audit trail on CREATE
# ===========================================================================

class TestAuditTrailOnCreate(unittest.TestCase):
    """Verify that creating a rule appends a properly structured audit entry."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")
        self._created_rule_ids = []

    def tearDown(self):
        for rule_id in self._created_rule_ids:
            _delete_rule(rule_id)

    def test_create_rule_appends_audit_entry(self):
        """POST a new rule, then GET audit-log; latest entry should have
        action='create' and match the created rule's id and title."""
        payload = _make_rule_payload(title="Audit Create Witness")
        resp = _create_rule(payload)
        self.assertEqual(resp.status_code, 201, "Prerequisite: rule creation must work")
        created = resp.json()
        rule_id = created["id"]
        self._created_rule_ids.append(rule_id)

        # Fetch audit log and find the entry
        audit_resp = _get_audit_log(params={"limit": 10})
        self.assertEqual(audit_resp.status_code, 200)
        entries = audit_resp.json()["entries"]

        entry = _find_audit_entry(entries, rule_id=rule_id, action="create")
        self.assertIsNotNone(
            entry,
            f"No audit entry with action='create' and rule_id='{rule_id}' found "
            f"in the latest 10 entries",
        )
        self.assertEqual(entry["rule_title"], "Audit Create Witness")

    def test_create_audit_entry_has_required_fields(self):
        """A create audit entry must contain: timestamp (ISO8601), action,
        rule_id, rule_title, actor, and changes (with the full new rule)."""
        payload = _make_rule_payload(title="Audit Fields Check")
        resp = _create_rule(payload)
        self.assertEqual(resp.status_code, 201, "Prerequisite: rule creation must work")
        created = resp.json()
        rule_id = created["id"]
        self._created_rule_ids.append(rule_id)

        audit_resp = _get_audit_log(params={"limit": 10})
        self.assertEqual(audit_resp.status_code, 200)
        entries = audit_resp.json()["entries"]

        entry = _find_audit_entry(entries, rule_id=rule_id, action="create")
        self.assertIsNotNone(entry, "Create audit entry not found")

        # Required top-level fields
        required_fields = ["timestamp", "action", "rule_id", "rule_title",
                           "actor", "changes"]
        for field in required_fields:
            self.assertIn(
                field, entry,
                f"Audit entry missing required field '{field}'",
            )

        # Timestamp should look like ISO8601 (starts with YYYY-)
        self.assertRegex(
            entry["timestamp"],
            r"^\d{4}-\d{2}-\d{2}T",
            "Timestamp should be ISO8601 format",
        )

        # Action should be 'create'
        self.assertEqual(entry["action"], "create")

        # Changes should be an object containing the created rule data
        self.assertIsInstance(
            entry["changes"], dict,
            "changes should be an object for create entries",
        )


# ===========================================================================
# 3. Audit trail on UPDATE
# ===========================================================================

class TestAuditTrailOnUpdate(unittest.TestCase):
    """Verify that updating a rule appends a properly structured audit entry."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

        # Create a rule to update during the test
        payload = _make_rule_payload(title="Audit Update Target")
        resp = _create_rule(payload)
        if resp.status_code != 201:
            self.skipTest("Could not create setup rule (endpoint may not exist yet)")
        self._rule = resp.json()
        self._rule_id = self._rule["id"]

    def tearDown(self):
        if hasattr(self, "_rule_id"):
            _delete_rule(self._rule_id)

    def test_update_rule_appends_audit_entry(self):
        """PUT a rule update, then GET audit-log; latest entry should have
        action='update' and match the updated rule's id."""
        new_title = f"Updated Title {uuid.uuid4().hex[:6]}"
        resp = requests.put(
            f"{BASE_URL}/api/constitution/rules/{self._rule_id}",
            json={"title": new_title},
            headers=AUTH_HEADERS,
            timeout=10,
        )
        self.assertEqual(resp.status_code, 200, "Prerequisite: rule update must work")

        audit_resp = _get_audit_log(params={"limit": 10})
        self.assertEqual(audit_resp.status_code, 200)
        entries = audit_resp.json()["entries"]

        entry = _find_audit_entry(entries, rule_id=self._rule_id, action="update")
        self.assertIsNotNone(
            entry,
            f"No audit entry with action='update' and rule_id='{self._rule_id}' "
            f"found in the latest 10 entries",
        )

    def test_update_audit_entry_captures_changes(self):
        """The changes object for an update should contain old and new values
        for the modified fields."""
        original_title = self._rule.get("title", "Audit Update Target")
        new_title = f"Changed Title {uuid.uuid4().hex[:6]}"

        resp = requests.put(
            f"{BASE_URL}/api/constitution/rules/{self._rule_id}",
            json={"title": new_title},
            headers=AUTH_HEADERS,
            timeout=10,
        )
        self.assertEqual(resp.status_code, 200, "Prerequisite: rule update must work")

        audit_resp = _get_audit_log(params={"limit": 10})
        self.assertEqual(audit_resp.status_code, 200)
        entries = audit_resp.json()["entries"]

        entry = _find_audit_entry(entries, rule_id=self._rule_id, action="update")
        self.assertIsNotNone(entry, "Update audit entry not found")

        changes = entry.get("changes", {})
        self.assertIsInstance(changes, dict, "changes should be an object")

        # The changes object should capture what changed with old -> new values
        self.assertIn("old", changes, "changes should have 'old' key for updates")
        self.assertIn("new", changes, "changes should have 'new' key for updates")

        # The new values should reflect our update
        new_changes = changes["new"]
        self.assertIsInstance(new_changes, dict)
        self.assertEqual(
            new_changes.get("title"), new_title,
            f"changes.new.title should be '{new_title}'",
        )


# ===========================================================================
# 4. Audit trail on DELETE
# ===========================================================================

class TestAuditTrailOnDelete(unittest.TestCase):
    """Verify that deleting a rule appends a properly structured audit entry."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

        # Create a disposable rule for deletion
        payload = _make_rule_payload(title="Audit Delete Target")
        resp = _create_rule(payload)
        if resp.status_code != 201:
            self.skipTest("Could not create setup rule (endpoint may not exist yet)")
        self._rule = resp.json()
        self._rule_id = self._rule["id"]

    def test_delete_rule_appends_audit_entry(self):
        """DELETE a rule, then GET audit-log; latest entry should have
        action='delete' and match the deleted rule's id and title."""
        resp = requests.delete(
            f"{BASE_URL}/api/constitution/rules/{self._rule_id}",
            headers=AUTH_HEADERS,
            timeout=10,
        )
        self.assertEqual(resp.status_code, 200, "Prerequisite: rule deletion must work")

        audit_resp = _get_audit_log(params={"limit": 10})
        self.assertEqual(audit_resp.status_code, 200)
        entries = audit_resp.json()["entries"]

        entry = _find_audit_entry(entries, rule_id=self._rule_id, action="delete")
        self.assertIsNotNone(
            entry,
            f"No audit entry with action='delete' and rule_id='{self._rule_id}' "
            f"found in the latest 10 entries",
        )
        self.assertEqual(entry["rule_title"], "Audit Delete Target")

        # Changes should contain the deleted rule's data
        changes = entry.get("changes", {})
        self.assertIsInstance(changes, dict, "changes should contain the deleted rule")


# ===========================================================================
# 5. Audit trail on TOGGLE (status change)
# ===========================================================================

class TestAuditTrailOnToggle(unittest.TestCase):
    """Verify that toggling a rule's status appends an action='toggle' entry."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

        # Create a rule in "enforced" status to toggle
        payload = _make_rule_payload(
            title="Audit Toggle Target",
            status="enforced",
        )
        resp = _create_rule(payload)
        if resp.status_code != 201:
            self.skipTest("Could not create setup rule (endpoint may not exist yet)")
        self._rule = resp.json()
        self._rule_id = self._rule["id"]

    def tearDown(self):
        if hasattr(self, "_rule_id"):
            _delete_rule(self._rule_id)

    def test_toggle_rule_appends_audit_entry(self):
        """PUT with a status change (enforced -> disabled) should produce an
        audit entry with action='toggle' (distinct from a general 'update')."""
        resp = requests.put(
            f"{BASE_URL}/api/constitution/rules/{self._rule_id}",
            json={"status": "disabled"},
            headers=AUTH_HEADERS,
            timeout=10,
        )
        self.assertEqual(resp.status_code, 200, "Prerequisite: rule update must work")

        audit_resp = _get_audit_log(params={"limit": 10})
        self.assertEqual(audit_resp.status_code, 200)
        entries = audit_resp.json()["entries"]

        entry = _find_audit_entry(entries, rule_id=self._rule_id, action="toggle")
        self.assertIsNotNone(
            entry,
            f"No audit entry with action='toggle' and rule_id='{self._rule_id}' "
            f"found in the latest 10 entries. Status changes should produce "
            f"action='toggle', not action='update'.",
        )

        # Verify the changes capture the status transition
        changes = entry.get("changes", {})
        self.assertIn("old", changes, "Toggle changes should have 'old' key")
        self.assertIn("new", changes, "Toggle changes should have 'new' key")

        old_status = changes["old"].get("status")
        new_status = changes["new"].get("status")
        self.assertEqual(
            old_status, "enforced",
            f"Toggle old status should be 'enforced', got '{old_status}'",
        )
        self.assertEqual(
            new_status, "disabled",
            f"Toggle new status should be 'disabled', got '{new_status}'",
        )


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    unittest.main()
