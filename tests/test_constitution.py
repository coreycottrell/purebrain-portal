"""
test_constitution.py -- TDD RED phase tests for Constitution Tab API.

What we're verifying: Constitutional rules CRUD, governance policies, and sync endpoint.
What coder discovers next: Implementation of 7 endpoints matching these specifications.
What descendants inherit: Full API contract for the Constitution tab, tested before built.
Why this matters: The Constitution tab enables civilizations to manage their own rules
    programmatically. These tests define the exact contract before any code exists.

Endpoints under test:
  GET    /api/constitution/rules           - List rules with optional filters
  POST   /api/constitution/rules           - Create a new rule
  PUT    /api/constitution/rules/{id}      - Update an existing rule
  DELETE /api/constitution/rules/{id}      - Delete a rule
  GET    /api/constitution/governance      - List governance policies
  PUT    /api/constitution/governance/{id} - Update a governance policy
  POST   /api/constitution/sync            - Force sync rules to enforcement files

These tests WILL FAIL (RED phase). The endpoints do not exist yet.
"""

import unittest
import requests
import uuid
import os
import sys

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
        "title": title or f"Test Rule {suffix}",
        "description": description or f"Auto-generated test rule {suffix}",
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


# ===========================================================================
# 1. GET /api/constitution/rules
# ===========================================================================

class TestConstitutionRulesList(unittest.TestCase):
    """Verify listing and filtering of constitutional rules."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

    # -- Auth --

    def test_list_rules_requires_auth(self):
        """GET /api/constitution/rules without token returns 401."""
        resp = requests.get(f"{BASE_URL}/api/constitution/rules", timeout=5)
        self.assertEqual(resp.status_code, 401)

    # -- Basic response shape --

    def test_list_rules_returns_200_with_rules_key(self):
        """GET /api/constitution/rules with auth returns 200 and a dict with 'rules' list."""
        resp = requests.get(
            f"{BASE_URL}/api/constitution/rules",
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsInstance(data, dict)
        self.assertIn("rules", data)
        self.assertIsInstance(data["rules"], list)

    # -- Filters --

    def test_filter_by_scope_global(self):
        """GET /api/constitution/rules?scope=global returns only global-scoped rules."""
        resp = requests.get(
            f"{BASE_URL}/api/constitution/rules",
            params={"scope": "global"},
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(resp.status_code, 200)
        rules = resp.json()["rules"]
        self.assertGreater(len(rules), 0, "Expected at least one global-scoped rule in test data")
        for rule in rules:
            self.assertEqual(rule["scope"], "global",
                             f"Rule {rule.get('id')} has scope '{rule['scope']}', expected 'global'")

    def test_filter_by_status_enforced(self):
        """GET /api/constitution/rules?status=enforced returns only enforced rules."""
        resp = requests.get(
            f"{BASE_URL}/api/constitution/rules",
            params={"status": "enforced"},
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(resp.status_code, 200)
        rules = resp.json()["rules"]
        self.assertGreater(len(rules), 0, "Expected at least one enforced rule in test data")
        for rule in rules:
            self.assertEqual(rule["status"], "enforced",
                             f"Rule {rule.get('id')} has status '{rule['status']}', expected 'enforced'")

    def test_filter_by_priority_critical(self):
        """GET /api/constitution/rules?priority=critical returns only critical rules."""
        resp = requests.get(
            f"{BASE_URL}/api/constitution/rules",
            params={"priority": "critical"},
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(resp.status_code, 200)
        rules = resp.json()["rules"]
        self.assertGreater(len(rules), 0, "Expected at least one critical-priority rule in test data")
        for rule in rules:
            self.assertEqual(rule["priority"], "critical",
                             f"Rule {rule.get('id')} has priority '{rule['priority']}', expected 'critical'")

    def test_filter_by_category_safety(self):
        """GET /api/constitution/rules?category=Safety returns only Safety category rules."""
        resp = requests.get(
            f"{BASE_URL}/api/constitution/rules",
            params={"category": "Safety"},
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(resp.status_code, 200)
        rules = resp.json()["rules"]
        self.assertGreater(len(rules), 0, "Expected at least one Safety category rule in test data")
        for rule in rules:
            self.assertEqual(rule["category"], "Safety",
                             f"Rule {rule.get('id')} has category '{rule['category']}', expected 'Safety'")


# ===========================================================================
# 2. POST /api/constitution/rules
# ===========================================================================

class TestConstitutionRulesCreate(unittest.TestCase):
    """Verify creation of constitutional rules."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")
        # Track rules created during this test for cleanup
        self._created_rule_ids = []

    def tearDown(self):
        for rule_id in self._created_rule_ids:
            _delete_rule(rule_id)

    # -- Auth --

    def test_create_rule_requires_auth(self):
        """POST /api/constitution/rules without token returns 401."""
        payload = _make_rule_payload()
        resp = requests.post(
            f"{BASE_URL}/api/constitution/rules",
            json=payload,
            timeout=5,
        )
        self.assertEqual(resp.status_code, 401)

    # -- Successful creation --

    def test_create_rule_returns_201_with_generated_fields(self):
        """POST with valid data returns 201 and the created rule with id + timestamps."""
        payload = _make_rule_payload(title="TDD Create Test Rule")
        resp = _create_rule(payload)
        self.assertEqual(resp.status_code, 201)

        created = resp.json()
        self.assertIn("id", created)
        self.assertIn("created_at", created)
        self.assertIn("updated_at", created)
        self.assertEqual(created["title"], "TDD Create Test Rule")
        self.assertEqual(created["scope"], payload["scope"])
        self.assertEqual(created["priority"], payload["priority"])
        self.assertEqual(created["category"], payload["category"])

        self._created_rule_ids.append(created["id"])

    # -- Validation --

    def test_create_rule_missing_title_returns_400(self):
        """POST without required 'title' field returns 400."""
        payload = _make_rule_payload()
        del payload["title"]
        resp = requests.post(
            f"{BASE_URL}/api/constitution/rules",
            json=payload,
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(resp.status_code, 400)

    def test_create_rule_missing_description_returns_400(self):
        """POST without required 'description' field returns 400."""
        payload = _make_rule_payload()
        del payload["description"]
        resp = requests.post(
            f"{BASE_URL}/api/constitution/rules",
            json=payload,
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(resp.status_code, 400)

    # -- Persistence --

    def test_created_rule_appears_in_list(self):
        """A newly created rule appears in subsequent GET /api/constitution/rules."""
        payload = _make_rule_payload(title="Persistence Verification Rule")
        create_resp = _create_rule(payload)
        self.assertEqual(create_resp.status_code, 201)
        created = create_resp.json()
        rule_id = created["id"]
        self._created_rule_ids.append(rule_id)

        list_resp = requests.get(
            f"{BASE_URL}/api/constitution/rules",
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(list_resp.status_code, 200)
        rule_ids = [r["id"] for r in list_resp.json()["rules"]]
        self.assertIn(rule_id, rule_ids,
                      f"Created rule {rule_id} not found in rules listing")


# ===========================================================================
# 3. PUT /api/constitution/rules/{id}
# ===========================================================================

class TestConstitutionRulesUpdate(unittest.TestCase):
    """Verify updating of constitutional rules."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

        # Create a rule to update during the test
        payload = _make_rule_payload(title="Rule To Update")
        resp = _create_rule(payload)
        if resp.status_code != 201:
            self.skipTest("Could not create setup rule (endpoint may not exist yet)")
        self._rule = resp.json()
        self._rule_id = self._rule["id"]

    def tearDown(self):
        if hasattr(self, "_rule_id"):
            _delete_rule(self._rule_id)

    # -- Auth --

    def test_update_rule_requires_auth(self):
        """PUT /api/constitution/rules/{id} without token returns 401."""
        resp = requests.put(
            f"{BASE_URL}/api/constitution/rules/{self._rule_id}",
            json={"title": "Updated Without Auth"},
            timeout=5,
        )
        self.assertEqual(resp.status_code, 401)

    # -- Successful update --

    def test_update_rule_title_returns_200(self):
        """PUT with new title returns 200 and reflects the change."""
        new_title = "Updated Rule Title"
        resp = requests.put(
            f"{BASE_URL}/api/constitution/rules/{self._rule_id}",
            json={"title": new_title},
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(resp.status_code, 200)
        updated = resp.json()
        self.assertEqual(updated["title"], new_title)

    # -- Not found --

    def test_update_nonexistent_rule_returns_404(self):
        """PUT /api/constitution/rules/nonexistent-id returns 404."""
        resp = requests.put(
            f"{BASE_URL}/api/constitution/rules/nonexistent-rule-{uuid.uuid4().hex[:8]}",
            json={"title": "Ghost Rule"},
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(resp.status_code, 404)

    # -- Timestamp change --

    def test_update_changes_updated_at_timestamp(self):
        """After PUT, updated_at should differ from the original value."""
        original_updated_at = self._rule.get("updated_at")

        resp = requests.put(
            f"{BASE_URL}/api/constitution/rules/{self._rule_id}",
            json={"description": "Modified description to trigger timestamp change"},
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(resp.status_code, 200)
        new_updated_at = resp.json().get("updated_at")

        self.assertIsNotNone(new_updated_at)
        self.assertNotEqual(original_updated_at, new_updated_at,
                            "updated_at should change after a PUT update")


# ===========================================================================
# 4. DELETE /api/constitution/rules/{id}
# ===========================================================================

class TestConstitutionRulesDelete(unittest.TestCase):
    """Verify deletion of constitutional rules."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

        # Create a disposable rule for deletion tests
        payload = _make_rule_payload(title="Rule To Delete")
        resp = _create_rule(payload)
        if resp.status_code != 201:
            self.skipTest("Could not create setup rule (endpoint may not exist yet)")
        self._rule = resp.json()
        self._rule_id = self._rule["id"]

    # -- Auth --

    def test_delete_rule_requires_auth(self):
        """DELETE /api/constitution/rules/{id} without token returns 401."""
        resp = requests.delete(
            f"{BASE_URL}/api/constitution/rules/{self._rule_id}",
            timeout=5,
        )
        self.assertEqual(resp.status_code, 401)
        # Cleanup: delete with auth so tearDown isn't needed
        _delete_rule(self._rule_id)

    # -- Successful deletion --

    def test_delete_existing_rule_returns_200(self):
        """DELETE /api/constitution/rules/{id} for existing rule returns 200."""
        resp = requests.delete(
            f"{BASE_URL}/api/constitution/rules/{self._rule_id}",
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(resp.status_code, 200)

    # -- Not found --

    def test_delete_nonexistent_rule_returns_404(self):
        """DELETE /api/constitution/rules/nonexistent-id returns 404."""
        fake_id = f"nonexistent-rule-{uuid.uuid4().hex[:8]}"
        resp = requests.delete(
            f"{BASE_URL}/api/constitution/rules/{fake_id}",
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(resp.status_code, 404)
        # Cleanup the real rule
        _delete_rule(self._rule_id)

    # -- Persistence --

    def test_deleted_rule_absent_from_list(self):
        """After DELETE, the rule no longer appears in GET /api/constitution/rules."""
        # Delete it
        del_resp = requests.delete(
            f"{BASE_URL}/api/constitution/rules/{self._rule_id}",
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(del_resp.status_code, 200)

        # Verify absence
        list_resp = requests.get(
            f"{BASE_URL}/api/constitution/rules",
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(list_resp.status_code, 200)
        rule_ids = [r["id"] for r in list_resp.json()["rules"]]
        self.assertNotIn(self._rule_id, rule_ids,
                         f"Deleted rule {self._rule_id} still appears in listing")


# ===========================================================================
# 5. GET /api/constitution/governance
# ===========================================================================

class TestConstitutionGovernanceList(unittest.TestCase):
    """Verify listing of governance policies."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

    # -- Auth --

    def test_governance_requires_auth(self):
        """GET /api/constitution/governance without token returns 401."""
        resp = requests.get(f"{BASE_URL}/api/constitution/governance", timeout=5)
        self.assertEqual(resp.status_code, 401)

    # -- Basic response shape --

    def test_governance_returns_200_with_policies_key(self):
        """GET /api/constitution/governance with auth returns 200 and 'policies' list."""
        resp = requests.get(
            f"{BASE_URL}/api/constitution/governance",
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsInstance(data, dict)
        self.assertIn("policies", data)
        self.assertIsInstance(data["policies"], list)


# ===========================================================================
# 6. PUT /api/constitution/governance/{id}
# ===========================================================================

class TestConstitutionGovernanceUpdate(unittest.TestCase):
    """Verify updating of governance policies."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

    # -- Auth --

    def test_governance_update_requires_auth(self):
        """PUT /api/constitution/governance/{id} without token returns 401."""
        resp = requests.put(
            f"{BASE_URL}/api/constitution/governance/gov-001",
            json={"title": "Updated Policy"},
            timeout=5,
        )
        self.assertEqual(resp.status_code, 401)

    # -- Successful update --

    def test_governance_update_returns_200(self):
        """PUT /api/constitution/governance/{id} with valid data returns 200."""
        # First, fetch existing policies to get a real ID
        list_resp = requests.get(
            f"{BASE_URL}/api/constitution/governance",
            headers=AUTH_HEADERS,
            timeout=5,
        )
        if list_resp.status_code != 200:
            self.skipTest("Cannot list governance policies (endpoint may not exist yet)")

        policies = list_resp.json().get("policies", [])
        if not policies:
            self.skipTest("No governance policies exist to update")

        policy_id = policies[0]["id"]
        resp = requests.put(
            f"{BASE_URL}/api/constitution/governance/{policy_id}",
            json={"description": "Updated via TDD test"},
            headers=AUTH_HEADERS,
            timeout=5,
        )
        self.assertEqual(resp.status_code, 200)
        updated = resp.json()
        self.assertEqual(updated.get("description"), "Updated via TDD test",
                         "Governance update should reflect the new description")


# ===========================================================================
# 7. POST /api/constitution/sync
# ===========================================================================

class TestConstitutionSync(unittest.TestCase):
    """Verify sync endpoint pushes rules to enforcement files."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token found (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running")

    # -- Auth --

    def test_sync_requires_auth(self):
        """POST /api/constitution/sync without token returns 401."""
        resp = requests.post(f"{BASE_URL}/api/constitution/sync", timeout=5)
        self.assertEqual(resp.status_code, 401)

    # -- Successful sync --

    def test_sync_returns_200_with_status(self):
        """POST /api/constitution/sync with auth returns 200 with sync status."""
        resp = requests.post(
            f"{BASE_URL}/api/constitution/sync",
            headers=AUTH_HEADERS,
            timeout=10,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsInstance(data, dict)
        self.assertIn("status", data, "Sync response should contain 'status' key")
        self.assertEqual(data["status"], "synced")
        self.assertIn("rules_count", data, "Sync response should contain 'rules_count' key")
        self.assertIsInstance(data["rules_count"], int)
        self.assertIn("timestamp", data, "Sync response should contain 'timestamp' key")


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    unittest.main()
