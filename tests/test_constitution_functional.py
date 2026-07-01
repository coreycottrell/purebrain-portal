"""
test_constitution_functional.py -- Functional tests for the Constitution Tab.

What we're verifying: Full CRUD lifecycle, validation edge cases, audit trail,
    governance policies, sync endpoint, and frontend HTML structure against the
    LIVE portal server at localhost:8097.
What coder discovered: Update endpoint lacks enum validation for priority/status/enforcement.
    Create endpoint enforces it. This asymmetry is documented here.
What descendants inherit: Complete functional coverage of the Constitution tab API
    with cleanup, so tests are safe to run repeatedly.
Why this matters: The Constitution tab is the governance backbone of the civilization.
    Every rule change, every policy update must work correctly and leave an audit trail.

Run:  python3 -m pytest tests/test_constitution_functional.py -v
"""

import re
import os
import sys
import unittest
import uuid

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import BASE_URL, HTML_FILE, load_token

TOKEN = load_token()
AUTH = {"Authorization": f"Bearer {TOKEN}"}
RULES_URL = f"{BASE_URL}/api/constitution/rules"
GOV_URL = f"{BASE_URL}/api/constitution/governance"
AUDIT_URL = f"{BASE_URL}/api/constitution/audit-log"
SYNC_URL = f"{BASE_URL}/api/constitution/sync"
TIMEOUT = 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unique(prefix="functest"):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _create_rule(title=None, description=None, **overrides):
    """Create a rule via POST. Returns response object."""
    payload = {
        "title": title or f"Functional Test Rule {_unique()}",
        "description": description or "Auto-generated for functional testing",
        "scope": "global",
        "priority": "medium",
        "enforcement": "soft",
        "status": "proposed",
        "category": "Testing",
        "created_by": "tester-agent-functional",
    }
    payload.update(overrides)
    return requests.post(RULES_URL, json=payload, headers=AUTH, timeout=TIMEOUT)


def _delete_rule(rule_id):
    """Best-effort cleanup."""
    try:
        requests.delete(f"{RULES_URL}/{rule_id}", headers=AUTH, timeout=TIMEOUT)
    except Exception:
        pass


class PortalLiveTest(unittest.TestCase):
    """Base class: skip if no token or portal unreachable."""

    def setUp(self):
        if not TOKEN:
            self.skipTest("No portal token (.portal-token)")
        try:
            requests.get(f"{BASE_URL}/health", timeout=3)
        except requests.exceptions.ConnectionError:
            self.skipTest("Portal server not running at " + BASE_URL)


# ===========================================================================
# 1. GET /api/constitution/rules -- response shape and field completeness
# ===========================================================================

class TestRulesListShape(PortalLiveTest):
    """GET /api/constitution/rules returns well-formed data."""

    def test_returns_rules_array(self):
        """Response has 'rules' key containing a list."""
        resp = requests.get(RULES_URL, headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("rules", data)
        self.assertIsInstance(data["rules"], list)

    def test_each_rule_has_required_fields(self):
        """Every rule object contains id, title, description, priority, status."""
        resp = requests.get(RULES_URL, headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 200)
        rules = resp.json()["rules"]
        self.assertGreater(len(rules), 0, "Expected at least one rule in the system")
        required = {"id", "title", "description", "priority", "status"}
        for rule in rules:
            for field in required:
                self.assertIn(field, rule,
                              f"Rule {rule.get('id', '?')} missing field '{field}'")

    def test_each_rule_has_full_field_set(self):
        """Every rule has the complete field set: scope, enforcement, category, timestamps."""
        resp = requests.get(RULES_URL, headers=AUTH, timeout=TIMEOUT)
        rules = resp.json()["rules"]
        full_fields = {"id", "title", "description", "scope", "priority",
                       "enforcement", "status", "category", "created_by",
                       "created_at", "updated_at"}
        for rule in rules:
            for field in full_fields:
                self.assertIn(field, rule,
                              f"Rule {rule.get('id', '?')} missing field '{field}'")


# ===========================================================================
# 2. POST /api/constitution/rules -- create, then verify in GET
# ===========================================================================

class TestRulesCreateAndVerify(PortalLiveTest):
    """Create a test rule, verify it appears in GET, then clean up."""

    def setUp(self):
        super().setUp()
        self._created_ids = []

    def tearDown(self):
        for rid in self._created_ids:
            _delete_rule(rid)

    def test_create_rule_appears_in_listing(self):
        """POST creates a rule; GET confirms it exists."""
        title = f"Lifecycle Create {_unique()}"
        resp = _create_rule(title=title)
        self.assertEqual(resp.status_code, 201)
        created = resp.json()
        self._created_ids.append(created["id"])

        # Verify in listing
        list_resp = requests.get(RULES_URL, headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(list_resp.status_code, 200)
        ids = [r["id"] for r in list_resp.json()["rules"]]
        self.assertIn(created["id"], ids)

    def test_create_returns_all_fields(self):
        """POST response includes all expected fields with correct values."""
        title = f"Field Check {_unique()}"
        resp = _create_rule(title=title, priority="high", enforcement="hard",
                            status="enforced", category="Safety")
        self.assertEqual(resp.status_code, 201)
        rule = resp.json()
        self._created_ids.append(rule["id"])

        self.assertEqual(rule["title"], title)
        self.assertEqual(rule["priority"], "high")
        self.assertEqual(rule["enforcement"], "hard")
        self.assertEqual(rule["status"], "enforced")
        self.assertEqual(rule["category"], "Safety")
        self.assertEqual(rule["scope"], "global")
        self.assertIn("created_at", rule)
        self.assertIn("updated_at", rule)
        self.assertTrue(rule["id"].startswith("rule-"))


# ===========================================================================
# 3. PUT /api/constitution/rules/{id} -- update, then verify persistence
# ===========================================================================

class TestRulesUpdateAndVerify(PortalLiveTest):
    """Update a test rule and verify changes persist."""

    def setUp(self):
        super().setUp()
        resp = _create_rule(title=f"Update Target {_unique()}")
        if resp.status_code != 201:
            self.skipTest("Cannot create setup rule")
        self._rule = resp.json()
        self._id = self._rule["id"]

    def tearDown(self):
        _delete_rule(self._id)

    def test_update_title_persists(self):
        """PUT title change is reflected in subsequent GET."""
        new_title = f"Updated Title {_unique()}"
        resp = requests.put(f"{RULES_URL}/{self._id}",
                            json={"title": new_title},
                            headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["title"], new_title)

        # Confirm persistence via GET
        list_resp = requests.get(RULES_URL, headers=AUTH, timeout=TIMEOUT)
        found = [r for r in list_resp.json()["rules"] if r["id"] == self._id]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["title"], new_title)

    def test_update_multiple_fields(self):
        """PUT can update description, priority, and category simultaneously."""
        updates = {
            "description": "Multi-field update test",
            "priority": "critical",
            "category": "Security",
        }
        resp = requests.put(f"{RULES_URL}/{self._id}",
                            json=updates, headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 200)
        updated = resp.json()
        self.assertEqual(updated["description"], "Multi-field update test")
        self.assertEqual(updated["priority"], "critical")
        self.assertEqual(updated["category"], "Security")

    def test_update_changes_updated_at(self):
        """PUT bumps the updated_at timestamp."""
        original_ts = self._rule["updated_at"]
        resp = requests.put(f"{RULES_URL}/{self._id}",
                            json={"description": "timestamp bump"},
                            headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 200)
        self.assertNotEqual(resp.json()["updated_at"], original_ts)


# ===========================================================================
# 4. DELETE /api/constitution/rules/{id} -- delete, verify gone
# ===========================================================================

class TestRulesDeleteAndVerify(PortalLiveTest):
    """Delete a test rule, verify it disappears."""

    def test_delete_then_verify_absent(self):
        """DELETE removes the rule; it no longer appears in GET."""
        resp = _create_rule(title=f"Delete Target {_unique()}")
        self.assertEqual(resp.status_code, 201)
        rule_id = resp.json()["id"]

        # Delete
        del_resp = requests.delete(f"{RULES_URL}/{rule_id}",
                                   headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(del_resp.status_code, 200)
        self.assertEqual(del_resp.json()["deleted"], rule_id)

        # Verify absent
        list_resp = requests.get(RULES_URL, headers=AUTH, timeout=TIMEOUT)
        ids = [r["id"] for r in list_resp.json()["rules"]]
        self.assertNotIn(rule_id, ids)

    def test_delete_nonexistent_returns_404(self):
        """DELETE on a fake rule ID returns 404."""
        fake_id = f"rule-{_unique()}"
        resp = requests.delete(f"{RULES_URL}/{fake_id}",
                               headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 404)


# ===========================================================================
# 5. GET /api/constitution/governance -- policies shape
# ===========================================================================

class TestGovernanceList(PortalLiveTest):
    """GET /api/constitution/governance returns well-formed policies."""

    def test_returns_policies_array(self):
        """Response has 'policies' key containing a list."""
        resp = requests.get(GOV_URL, headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("policies", data)
        self.assertIsInstance(data["policies"], list)

    def test_each_policy_has_required_fields(self):
        """Each policy has id, title, description, type, status."""
        resp = requests.get(GOV_URL, headers=AUTH, timeout=TIMEOUT)
        policies = resp.json()["policies"]
        self.assertGreater(len(policies), 0, "Expected at least one governance policy")
        required = {"id", "title", "description", "type", "status"}
        for policy in policies:
            for field in required:
                self.assertIn(field, policy,
                              f"Policy {policy.get('id', '?')} missing field '{field}'")


# ===========================================================================
# 6. PUT /api/constitution/governance/{id} -- update a policy
# ===========================================================================

class TestGovernanceUpdate(PortalLiveTest):
    """Update a governance policy and verify."""

    def test_update_policy_description(self):
        """PUT description on an existing policy returns 200 with change."""
        # Get a real policy ID
        list_resp = requests.get(GOV_URL, headers=AUTH, timeout=TIMEOUT)
        policies = list_resp.json().get("policies", [])
        if not policies:
            self.skipTest("No governance policies to update")
        policy_id = policies[0]["id"]
        original_desc = policies[0].get("description", "")

        new_desc = f"Functional test update {_unique()}"
        resp = requests.put(f"{GOV_URL}/{policy_id}",
                            json={"description": new_desc},
                            headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["description"], new_desc)

        # Restore original
        requests.put(f"{GOV_URL}/{policy_id}",
                     json={"description": original_desc},
                     headers=AUTH, timeout=TIMEOUT)

    def test_update_nonexistent_policy_returns_404(self):
        """PUT on a fake policy ID returns 404."""
        resp = requests.put(f"{GOV_URL}/gov-{_unique()}",
                            json={"description": "ghost"},
                            headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 404)


# ===========================================================================
# 7. GET /api/constitution/audit-log -- audit trail
# ===========================================================================

class TestAuditLog(PortalLiveTest):
    """GET /api/constitution/audit-log returns log entries."""

    def test_returns_entries_array(self):
        """Response has 'entries' key and 'total' count."""
        resp = requests.get(AUDIT_URL, headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("entries", data)
        self.assertIsInstance(data["entries"], list)
        self.assertIn("total", data)
        self.assertIsInstance(data["total"], int)

    def test_create_generates_audit_entry(self):
        """Creating a rule leaves a 'create' entry in the audit log."""
        # Create a rule
        title = f"Audit Trail Test {_unique()}"
        resp = _create_rule(title=title)
        self.assertEqual(resp.status_code, 201)
        rule_id = resp.json()["id"]

        # Check audit log
        log_resp = requests.get(AUDIT_URL, params={"limit": 10},
                                headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(log_resp.status_code, 200)
        entries = log_resp.json()["entries"]
        create_entries = [e for e in entries
                         if e.get("action") == "create" and e.get("rule_id") == rule_id]
        self.assertGreater(len(create_entries), 0,
                           f"No 'create' audit entry found for rule {rule_id}")

        # Audit entry should have timestamp and actor
        entry = create_entries[0]
        self.assertIn("timestamp", entry)
        self.assertIn("actor", entry)
        self.assertIn("rule_title", entry)

        # Cleanup
        _delete_rule(rule_id)

    def test_audit_log_pagination(self):
        """Audit log respects limit and offset parameters."""
        resp = requests.get(AUDIT_URL, params={"limit": 2, "offset": 0},
                            headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertLessEqual(len(data["entries"]), 2)


# ===========================================================================
# 7b. GET /api/constitution/memory -- Dynamic memory categories
# ===========================================================================

MEMORY_URL = f"{BASE_URL}/api/constitution/memory"
OVERRIDES_URL = f"{BASE_URL}/api/constitution/overrides"


class TestConstitutionMemory(PortalLiveTest):
    """GET /api/constitution/memory returns dynamic memory categories."""

    def test_returns_categories_array(self):
        """Response has 'categories' key with a list value."""
        resp = requests.get(MEMORY_URL, headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("categories", data)
        self.assertIsInstance(data["categories"], list)

    def test_has_five_category_names(self):
        """Response includes all 5 expected category names."""
        resp = requests.get(MEMORY_URL, headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 200)
        cats = [c["category"] for c in resp.json()["categories"]]
        for expected in ["Identity", "Feedback", "Projects", "References", "Processes"]:
            self.assertIn(expected, cats, f"Missing category: {expected}")

    def test_identity_has_civ_name(self):
        """Identity category includes a Civilization Name entry."""
        resp = requests.get(MEMORY_URL, headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 200)
        identity = [c for c in resp.json()["categories"] if c["category"] == "Identity"]
        self.assertEqual(len(identity), 1)
        titles = [e["title"] for e in identity[0]["entries"]]
        self.assertIn("Civilization Name", titles)

    def test_no_ssh_credentials_in_response(self):
        """Memory endpoint must never return SSH credentials or IPs."""
        resp = requests.get(MEMORY_URL, headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        for forbidden in ["37.27.237.109", "157.180.69.225", "89.167.19.20",
                          "pushdabutton", "flux_access"]:
            self.assertNotIn(forbidden, body,
                             f"Leaked credential/IP: {forbidden}")

    def test_requires_auth(self):
        """Memory endpoint rejects unauthenticated requests."""
        resp = requests.get(MEMORY_URL, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 401)


class TestConstitutionOverrides(PortalLiveTest):
    """GET /api/constitution/overrides returns override decisions."""

    def test_returns_overrides_array(self):
        """Response has 'overrides' key with a list value."""
        resp = requests.get(OVERRIDES_URL, headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("overrides", data)
        self.assertIsInstance(data["overrides"], list)

    def test_requires_auth(self):
        """Overrides endpoint rejects unauthenticated requests."""
        resp = requests.get(OVERRIDES_URL, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 401)


# ===========================================================================
# 8. POST /api/constitution/sync -- CLAUDE.md sync
# ===========================================================================

class TestSync(PortalLiveTest):
    """POST /api/constitution/sync triggers constitution sync."""

    def test_sync_returns_200_with_counts(self):
        """Sync returns status, rules_count, enforced_count, governance_count, timestamp."""
        resp = requests.post(SYNC_URL, headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "synced")
        self.assertIn("rules_count", data)
        self.assertIn("enforced_count", data)
        self.assertIn("governance_count", data)
        self.assertIn("timestamp", data)
        self.assertIsInstance(data["rules_count"], int)
        self.assertIsInstance(data["enforced_count"], int)
        self.assertGreater(data["rules_count"], 0)


# ===========================================================================
# 9-12. Validation edge cases
# ===========================================================================

class TestValidationEdgeCases(PortalLiveTest):
    """Validation: empty title, XSS, invalid enum values."""

    def setUp(self):
        super().setUp()
        self._created_ids = []

    def tearDown(self):
        for rid in self._created_ids:
            _delete_rule(rid)

    # --- 9. Empty title ---

    def test_create_with_empty_title_returns_400(self):
        """POST with empty string title returns 400."""
        resp = requests.post(RULES_URL,
                             json={"title": "", "description": "has desc"},
                             headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 400)

    def test_create_with_missing_title_returns_400(self):
        """POST with no title key returns 400."""
        resp = requests.post(RULES_URL,
                             json={"description": "no title field"},
                             headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 400)

    # --- 10. XSS in title: stored as text, not executed ---

    def test_xss_in_title_stored_as_plain_text(self):
        """XSS payload in title is stored literally (no transformation/stripping)."""
        xss_title = '<script>alert(1)</script>'
        resp = _create_rule(title=xss_title,
                            description="XSS safety test")
        self.assertEqual(resp.status_code, 201)
        created = resp.json()
        self._created_ids.append(created["id"])

        # The title should be stored exactly as sent (plain text)
        self.assertEqual(created["title"], xss_title,
                         "XSS payload should be stored as literal text in the API response")

        # Verify it persists correctly in listing
        list_resp = requests.get(RULES_URL, headers=AUTH, timeout=TIMEOUT)
        found = [r for r in list_resp.json()["rules"] if r["id"] == created["id"]]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["title"], xss_title)

    def test_xss_in_description_stored_as_plain_text(self):
        """XSS payload in description is stored literally."""
        xss_desc = '<img src=x onerror=alert(1)>'
        resp = _create_rule(description=xss_desc)
        self.assertEqual(resp.status_code, 201)
        self._created_ids.append(resp.json()["id"])
        self.assertEqual(resp.json()["description"], xss_desc)

    # --- 11. Invalid priority on create ---

    def test_create_with_invalid_priority_returns_400(self):
        """POST with a non-allowed priority value returns 400."""
        resp = requests.post(RULES_URL,
                             json={"title": "Bad Priority",
                                   "description": "testing invalid priority",
                                   "priority": "ultra-mega-critical"},
                             headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.json())

    def test_create_with_invalid_status_returns_400(self):
        """POST with a non-allowed status value returns 400."""
        resp = requests.post(RULES_URL,
                             json={"title": "Bad Status",
                                   "description": "testing invalid status",
                                   "status": "yolo"},
                             headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 400)

    def test_create_with_invalid_enforcement_returns_400(self):
        """POST with a non-allowed enforcement value returns 400."""
        resp = requests.post(RULES_URL,
                             json={"title": "Bad Enforcement",
                                   "description": "testing invalid enforcement",
                                   "enforcement": "nuclear"},
                             headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 400)

    def test_create_with_invalid_scope_returns_400(self):
        """POST with scope that is neither 'global' nor 'agent:{name}' returns 400."""
        resp = requests.post(RULES_URL,
                             json={"title": "Bad Scope",
                                   "description": "testing invalid scope",
                                   "scope": "everywhere"},
                             headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 400)

    def test_create_with_agent_scope_succeeds(self):
        """POST with scope 'agent:tester' is accepted."""
        resp = _create_rule(scope="agent:tester")
        self.assertEqual(resp.status_code, 201)
        self._created_ids.append(resp.json()["id"])
        self.assertEqual(resp.json()["scope"], "agent:tester")

    # --- 12. Delete non-existent rule ---

    def test_delete_nonexistent_rule_returns_404(self):
        """DELETE on a non-existent rule returns 404 with error message."""
        fake_id = f"rule-nonexistent-{_unique()}"
        resp = requests.delete(f"{RULES_URL}/{fake_id}",
                               headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 404)
        self.assertIn("error", resp.json())

    # --- Title length limit ---

    def test_create_with_title_over_200_chars_returns_400(self):
        """POST with title exceeding 200 characters returns 400."""
        long_title = "A" * 201
        resp = requests.post(RULES_URL,
                             json={"title": long_title,
                                   "description": "too long title"},
                             headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 400)


# ===========================================================================
# Full CRUD Lifecycle
# ===========================================================================

class TestFullCRUDLifecycle(PortalLiveTest):
    """End-to-end: create -> read -> update -> delete in one flow."""

    def test_complete_lifecycle(self):
        """Create a rule, verify, update it, verify again, delete it, confirm gone."""
        # CREATE
        title = f"Lifecycle {_unique()}"
        create_resp = _create_rule(title=title, priority="low",
                                   status="draft", enforcement="advisory")
        self.assertEqual(create_resp.status_code, 201)
        rule = create_resp.json()
        rule_id = rule["id"]
        self.assertEqual(rule["title"], title)
        self.assertEqual(rule["priority"], "low")
        self.assertEqual(rule["status"], "draft")

        try:
            # READ -- confirm it exists in listing
            list_resp = requests.get(RULES_URL, headers=AUTH, timeout=TIMEOUT)
            self.assertEqual(list_resp.status_code, 200)
            ids = [r["id"] for r in list_resp.json()["rules"]]
            self.assertIn(rule_id, ids, "Created rule should appear in listing")

            # UPDATE -- change title and status
            new_title = f"Updated Lifecycle {_unique()}"
            update_resp = requests.put(
                f"{RULES_URL}/{rule_id}",
                json={"title": new_title, "status": "enforced", "priority": "critical"},
                headers=AUTH, timeout=TIMEOUT)
            self.assertEqual(update_resp.status_code, 200)
            updated = update_resp.json()
            self.assertEqual(updated["title"], new_title)
            self.assertEqual(updated["status"], "enforced")
            self.assertEqual(updated["priority"], "critical")

            # READ again -- confirm update persisted
            list_resp2 = requests.get(RULES_URL, headers=AUTH, timeout=TIMEOUT)
            found = [r for r in list_resp2.json()["rules"] if r["id"] == rule_id]
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["title"], new_title)
            self.assertEqual(found[0]["status"], "enforced")

            # DELETE
            del_resp = requests.delete(f"{RULES_URL}/{rule_id}",
                                       headers=AUTH, timeout=TIMEOUT)
            self.assertEqual(del_resp.status_code, 200)

            # VERIFY GONE
            list_resp3 = requests.get(RULES_URL, headers=AUTH, timeout=TIMEOUT)
            ids_after = [r["id"] for r in list_resp3.json()["rules"]]
            self.assertNotIn(rule_id, ids_after, "Deleted rule should not appear in listing")

        except Exception:
            # Cleanup on failure
            _delete_rule(rule_id)
            raise


# ===========================================================================
# 13-16. Frontend HTML structure tests
# ===========================================================================

class TestFrontendHTMLStructure(unittest.TestCase):
    """Verify DOM structure of the Constitution tab in portal HTML."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(HTML_FILE):
            raise unittest.SkipTest(f"HTML file not found: {HTML_FILE}")
        with open(HTML_FILE, "r") as f:
            cls.html = f.read()

    # --- 13. Seven subtab panes exist ---

    def test_global_rules_pane_exists(self):
        """The globalRules pane exists in the HTML."""
        self.assertIn('id="globalRules"', self.html)

    def test_agent_rules_pane_exists(self):
        """The agentRules pane exists in the HTML."""
        self.assertIn('id="agentRules"', self.html)

    def test_governance_pane_exists(self):
        """The govPane pane exists in the HTML."""
        self.assertIn('id="govPane"', self.html)

    def test_dashboard_pane_exists(self):
        """The constDashboard pane exists in the HTML."""
        self.assertIn('id="constDashboard"', self.html)

    def test_action_log_pane_exists(self):
        """The constActionLog pane exists in the HTML."""
        self.assertIn('id="constActionLog"', self.html)

    def test_memory_pane_exists(self):
        """The constMemory pane exists in the HTML."""
        self.assertIn('id="constMemory"', self.html)

    def test_overrides_pane_exists(self):
        """The constOverrides pane exists in the HTML."""
        self.assertIn('id="constOverrides"', self.html)

    def test_all_seven_panes_have_const_pane_class(self):
        """All 7 Constitution panes use the 'const-pane' class."""
        pane_ids = ["globalRules", "agentRules", "govPane", "constDashboard",
                    "constActionLog", "constMemory", "constOverrides"]
        for pane_id in pane_ids:
            # globalRules has class="const-pane active" (default visible)
            pattern = rf'class="const-pane[^"]*"\s+id="{pane_id}"'
            self.assertRegex(self.html, pattern,
                             f"Pane {pane_id} missing or not using const-pane class")

    # --- 14. Subtab click handlers registered ---

    def test_subtab_buttons_have_onclick_handlers(self):
        """Each subtab button has a switchConstTab onclick handler."""
        expected_targets = ["globalRules", "agentRules", "govPane",
                            "constDashboard", "constActionLog",
                            "constMemory", "constOverrides"]
        for target in expected_targets:
            self.assertIn(f"switchConstTab(this,'{target}')", self.html,
                          f"Missing switchConstTab handler for '{target}'")

    def test_switchConstTab_function_defined(self):
        """The switchConstTab function is defined in the HTML/JS."""
        self.assertIn("function switchConstTab", self.html)

    # --- 15. Dashboard has 6 stat cards ---

    def test_dashboard_renders_six_stat_cards(self):
        """The loadConstDashboard function defines 6 stat card labels."""
        expected_labels = ["Total Rules", "Enforced", "Critical Priority",
                           "Disabled", "Governance Policies", "Recent Actions"]
        for label in expected_labels:
            self.assertIn(f"label:'{label}'", self.html,
                          f"Dashboard stat card '{label}' not found in JS")

    def test_dashboard_cards_container_exists(self):
        """The constDashCards container div exists."""
        self.assertIn('id="constDashCards"', self.html)

    def test_dashboard_range_selector_exists(self):
        """The constDashRange time range selector exists."""
        self.assertIn('id="constDashRange"', self.html)

    # --- 16. Memory pane (API-backed) ---

    def test_memory_fetches_from_api(self):
        """The loadConstMemory function fetches from /api/constitution/memory."""
        self.assertIn("/api/constitution/memory", self.html)

    def test_memory_pills_container_exists(self):
        """The constMemCategoryPills container exists."""
        self.assertIn('id="constMemCategoryPills"', self.html)

    def test_memory_list_container_exists(self):
        """The constMemList container exists."""
        self.assertIn('id="constMemList"', self.html)

    def test_loadConstMemory_function_defined(self):
        """The loadConstMemory function is defined in the HTML/JS."""
        self.assertIn("function loadConstMemory", self.html)

    def test_renderConstMemory_function_defined(self):
        """The _renderConstMemory function is defined for rendering fetched data."""
        self.assertIn("function _renderConstMemory", self.html)

    # --- 16b. Overrides pane (API-backed) ---

    def test_overrides_fetches_from_api(self):
        """The loadConstOverrides function fetches from /api/constitution/overrides."""
        self.assertIn("/api/constitution/overrides", self.html)

    def test_renderConstOverrides_function_defined(self):
        """The _renderConstOverrides function is defined for rendering fetched data."""
        self.assertIn("function _renderConstOverrides", self.html)

    def test_no_hardcoded_flux_data_in_html(self):
        """No Flux-specific personal data remains in the Constitution tab HTML."""
        forbidden = [
            "37.27.237.109",      # Container IP
            "157.180.69.225",     # PureSurf IP
            "89.167.19.20",       # CC IP
            "pushdabutton",       # GitHub account
            "flux_access",        # SSH key name
        ]
        for pattern in forbidden:
            self.assertNotIn(pattern, self.html,
                             f"Hardcoded personal data '{pattern}' still in HTML")


# ===========================================================================
# Update endpoint: invalid enum on update (documents current behavior)
# ===========================================================================

class TestUpdateValidationBehavior(PortalLiveTest):
    """Document how the update endpoint handles invalid enum values.

    NOTE: Unlike create, the update endpoint does NOT validate enum values
    for priority/status/enforcement. This test documents that behavior.
    """

    def setUp(self):
        super().setUp()
        resp = _create_rule(title=f"Enum Update Test {_unique()}")
        if resp.status_code != 201:
            self.skipTest("Cannot create setup rule")
        self._rule = resp.json()
        self._id = self._rule["id"]

    def tearDown(self):
        _delete_rule(self._id)

    def test_update_with_invalid_priority_rejected(self):
        """PUT with invalid priority value is rejected with 400.

        Enum validation is shared between create and update endpoints via
        _validate_rule_enums() helper.
        """
        resp = requests.put(f"{RULES_URL}/{self._id}",
                            json={"priority": "ultra-mega-critical"},
                            headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 400,
                         "Update endpoint should reject invalid priority values")
        self.assertIn("invalid priority", resp.json().get("error", ""))

    def test_update_ignores_non_updatable_fields(self):
        """PUT cannot change id, created_at, or created_by."""
        original_id = self._id
        resp = requests.put(f"{RULES_URL}/{self._id}",
                            json={"id": "rule-hacked", "created_by": "evil"},
                            headers=AUTH, timeout=TIMEOUT)
        self.assertEqual(resp.status_code, 200)
        updated = resp.json()
        # id should NOT change
        self.assertEqual(updated["id"], original_id)
        # created_by should NOT change
        self.assertEqual(updated["created_by"], "tester-agent-functional")


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    unittest.main()
