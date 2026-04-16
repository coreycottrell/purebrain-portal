"""
Tests for Brevo (email marketing) sync module.

Tests mock urllib.request.urlopen to avoid real API calls.
Also tests integration with tracking.py — Brevo failures never block tracking.
"""

import base64
import io
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Add portal root to path
PORTAL_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PORTAL_DIR))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_encoded_key(api_key: str = "xkeysib-test123") -> str:
    """Create a base64-encoded JSON key like the real BREVO_API_KEY env var."""
    payload = json.dumps({"api_key": api_key})
    return base64.b64encode(payload.encode()).decode()


def _mock_response(status: int = 200, body: dict = None):
    """Create a mock HTTP response object."""
    resp = MagicMock()
    resp.status = status
    resp_body = json.dumps(body or {}).encode("utf-8")
    resp.read.return_value = resp_body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _create_test_db(path: str) -> None:
    """Create a fresh clients.db with tracking columns for testing."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            name                  TEXT NOT NULL,
            email                 TEXT NOT NULL UNIQUE COLLATE NOCASE,
            goes_by               TEXT NOT NULL DEFAULT '',
            ai_name               TEXT NOT NULL DEFAULT '',
            company               TEXT NOT NULL DEFAULT '',
            role                  TEXT NOT NULL DEFAULT '',
            goal                  TEXT NOT NULL DEFAULT '',
            tier                  TEXT NOT NULL DEFAULT 'unknown',
            status                TEXT NOT NULL DEFAULT 'active',
            payment_status        TEXT NOT NULL DEFAULT 'none',
            paypal_subscription_id TEXT NOT NULL DEFAULT '',
            total_paid            REAL NOT NULL DEFAULT 0,
            payment_count         INTEGER NOT NULL DEFAULT 0,
            referral_code         TEXT NOT NULL DEFAULT '',
            first_seen_at         TEXT NOT NULL,
            last_active_at        TEXT NOT NULL DEFAULT '',
            onboarded_at          TEXT NOT NULL DEFAULT '',
            notes                 TEXT NOT NULL DEFAULT '',
            magic_link_token      TEXT NOT NULL DEFAULT '',
            created_at            TEXT NOT NULL DEFAULT '',
            updated_at            TEXT NOT NULL DEFAULT '',
            hidden                INTEGER NOT NULL DEFAULT 0,
            last_login_at         TEXT NOT NULL DEFAULT '',
            login_count           INTEGER NOT NULL DEFAULT 0,
            session_count         INTEGER NOT NULL DEFAULT 0,
            next_billing_date     TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paypal_webhook_log (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id       TEXT NOT NULL DEFAULT '',
            event_type     TEXT NOT NULL,
            resource_id    TEXT NOT NULL DEFAULT '',
            payload        TEXT NOT NULL,
            received_at    TEXT NOT NULL,
            processed      INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def _insert_test_client(db_path: str, email: str = "test@example.com",
                        name: str = "Test User", **kwargs) -> int:
    """Insert a test client and return the id."""
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    defaults = {
        "goes_by": "", "ai_name": "", "company": "", "role": "", "goal": "",
        "tier": "Awakened", "status": "active", "payment_status": "none",
        "paypal_subscription_id": "", "total_paid": 0, "payment_count": 0,
        "referral_code": "", "first_seen_at": now, "last_active_at": "",
        "onboarded_at": "", "notes": "", "magic_link_token": "",
        "created_at": now, "updated_at": now, "hidden": 0,
        "last_login_at": "", "login_count": 0, "session_count": 0,
        "next_billing_date": "",
    }
    defaults.update(kwargs)
    cur = conn.execute(
        """INSERT INTO clients (name, email, goes_by, ai_name, company, role, goal,
           tier, status, payment_status, paypal_subscription_id, total_paid,
           payment_count, referral_code, first_seen_at, last_active_at, onboarded_at,
           notes, magic_link_token, created_at, updated_at, hidden,
           last_login_at, login_count, session_count, next_billing_date)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (name, email, defaults["goes_by"], defaults["ai_name"],
         defaults["company"], defaults["role"], defaults["goal"],
         defaults["tier"], defaults["status"], defaults["payment_status"],
         defaults["paypal_subscription_id"], defaults["total_paid"],
         defaults["payment_count"], defaults["referral_code"],
         defaults["first_seen_at"], defaults["last_active_at"],
         defaults["onboarded_at"], defaults["notes"], defaults["magic_link_token"],
         defaults["created_at"], defaults["updated_at"], defaults["hidden"],
         defaults["last_login_at"], defaults["login_count"],
         defaults["session_count"], defaults["next_billing_date"]),
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary clients.db for testing."""
    db_path = str(tmp_path / "clients.db")
    _create_test_db(db_path)
    return db_path


@pytest.fixture
def populated_db(tmp_db):
    """Create a DB with a test client already inserted."""
    _insert_test_client(tmp_db, email="alice@example.com", name="Alice Smith")
    return tmp_db


@pytest.fixture(autouse=True)
def clear_brevo_cache():
    """Clear the Brevo key cache between tests."""
    import brevo_sync
    brevo_sync._brevo_key_cache = None
    yield
    brevo_sync._brevo_key_cache = None


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE 1: Key Decoding
# ═══════════════════════════════════════════════════════════════════════════

class TestBrevoKeyDecoding:
    """Test base64-encoded JSON API key decoding."""

    def test_decode_base64_key(self):
        """Should decode base64 JSON and extract api_key."""
        from brevo_sync import _get_brevo_key
        encoded = _make_encoded_key("xkeysib-real-key-123")
        with patch.dict(os.environ, {"BREVO_API_KEY": encoded}):
            key = _get_brevo_key()
        assert key == "xkeysib-real-key-123"

    @patch("brevo_sync._fetch_shared_key", return_value="xkeysib-from-cc")
    def test_empty_env_fetches_from_config_endpoint(self, mock_fetch):
        """No env var should fetch key from central config endpoint."""
        from brevo_sync import _get_brevo_key
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("BREVO_API_KEY", None)
            key = _get_brevo_key()
        mock_fetch.assert_called_once()
        assert key == "xkeysib-from-cc"

    @patch("brevo_sync._fetch_shared_key", return_value="xkeysib-from-cc")
    def test_invalid_base64_fetches_from_config_endpoint(self, mock_fetch):
        """Invalid base64 should fall back to central config endpoint."""
        from brevo_sync import _get_brevo_key
        with patch.dict(os.environ, {"BREVO_API_KEY": "not-valid-base64!!!"}):
            key = _get_brevo_key()
        mock_fetch.assert_called_once()
        assert key == "xkeysib-from-cc"

    @patch("brevo_sync._fetch_shared_key", return_value="")
    def test_no_env_no_endpoint_returns_empty(self, mock_fetch):
        """If both .env and config endpoint fail, return empty string."""
        from brevo_sync import _get_brevo_key
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("BREVO_API_KEY", None)
            key = _get_brevo_key()
        assert key == ""

    def test_key_is_cached(self):
        """Second call should use cached key, not re-decode."""
        from brevo_sync import _get_brevo_key
        encoded = _make_encoded_key("xkeysib-cached")
        with patch.dict(os.environ, {"BREVO_API_KEY": encoded}):
            key1 = _get_brevo_key()
            # Change env — should still return cached
            os.environ["BREVO_API_KEY"] = _make_encoded_key("xkeysib-different")
            key2 = _get_brevo_key()
        assert key1 == key2 == "xkeysib-cached"


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE 2: Low-level Request
# ═══════════════════════════════════════════════════════════════════════════

class TestBrevoRequest:
    """Test the _brevo_request helper."""

    @patch("brevo_sync.urllib.request.urlopen")
    def test_request_with_valid_key(self, mock_urlopen):
        """Should make request with correct api-key header."""
        from brevo_sync import _brevo_request
        mock_urlopen.return_value = _mock_response(200, {"id": 1})

        encoded = _make_encoded_key("xkeysib-test")
        with patch.dict(os.environ, {"BREVO_API_KEY": encoded}):
            result = _brevo_request("POST", "/events", {"test": True})

        assert result["ok"] is True
        assert result["status"] == 200

        # Verify the request was made with correct headers
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Api-key") == "xkeysib-test"
        assert req.get_header("Content-type") == "application/json"

    @patch("brevo_sync._fetch_shared_key", return_value="xkeysib-from-endpoint")
    @patch("brevo_sync.urllib.request.urlopen")
    def test_request_without_env_key_uses_config_endpoint(self, mock_urlopen, mock_fetch):
        """No env var should fetch key from config endpoint and use it."""
        from brevo_sync import _brevo_request
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_resp.read.return_value = b""
        mock_urlopen.return_value = mock_resp
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("BREVO_API_KEY", None)
            result = _brevo_request("POST", "/events", {"test": True})
        assert result["ok"] is True
        call_args = mock_urlopen.call_args[0][0]
        assert call_args.get_header("Api-key") == "xkeysib-from-endpoint"

    @patch("brevo_sync._fetch_shared_key", return_value="")
    def test_request_without_any_key_returns_error(self, mock_fetch):
        """No env var + failed config endpoint should return ok=False."""
        from brevo_sync import _brevo_request
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("BREVO_API_KEY", None)
            result = _brevo_request("POST", "/events", {"test": True})
        assert result["ok"] is False
        assert "no API key" in result["body"]

    @patch("brevo_sync.urllib.request.urlopen")
    def test_request_handles_http_error(self, mock_urlopen):
        """HTTP errors should return ok=False with status code."""
        from brevo_sync import _brevo_request
        import urllib.error

        error_resp = MagicMock()
        error_resp.read.return_value = b"Bad Request"
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.brevo.com/v3/events", 400, "Bad Request",
            {}, error_resp
        )

        encoded = _make_encoded_key("xkeysib-test")
        with patch.dict(os.environ, {"BREVO_API_KEY": encoded}):
            result = _brevo_request("POST", "/events", {"bad": True})

        assert result["ok"] is False
        assert result["status"] == 400

    @patch("brevo_sync.urllib.request.urlopen")
    def test_request_handles_timeout(self, mock_urlopen):
        """Timeout should return ok=False."""
        from brevo_sync import _brevo_request
        import socket

        mock_urlopen.side_effect = socket.timeout("timed out")

        encoded = _make_encoded_key("xkeysib-test")
        with patch.dict(os.environ, {"BREVO_API_KEY": encoded}):
            result = _brevo_request("POST", "/events", {"test": True})

        assert result["ok"] is False
        assert result["status"] == 0

    @patch("brevo_sync.urllib.request.urlopen")
    def test_request_url_construction(self, mock_urlopen):
        """Should construct URL from BREVO_API_URL + path."""
        from brevo_sync import _brevo_request
        mock_urlopen.return_value = _mock_response(200, {})

        encoded = _make_encoded_key("xkeysib-test")
        with patch.dict(os.environ, {"BREVO_API_KEY": encoded}):
            _brevo_request("POST", "/contacts", {"email": "x@y.com"})

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://api.brevo.com/v3/contacts"


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE 3: sync_contact_login
# ═══════════════════════════════════════════════════════════════════════════

class TestSyncContactLogin:
    """Test login event sync."""

    @patch("brevo_sync._brevo_request")
    def test_sends_correct_event(self, mock_req):
        """Should POST to /events with portal_login event_name."""
        from brevo_sync import sync_contact_login
        mock_req.return_value = {"ok": True, "status": 200, "body": {}}

        sync_contact_login("alice@example.com", 5)

        mock_req.assert_called_once()
        args = mock_req.call_args
        assert args[0][0] == "POST"
        assert args[0][1] == "/events"
        body = args[0][2]
        assert body["event_name"] == "portal_login"
        assert body["identifiers"]["email_id"] == "alice@example.com"
        assert body["contact_properties"]["LOGIN_COUNT"] == 5

    @patch("brevo_sync._brevo_request")
    def test_includes_login_date(self, mock_req):
        """Should include LAST_LOGIN_AT as today's date."""
        from brevo_sync import sync_contact_login
        mock_req.return_value = {"ok": True, "status": 200, "body": {}}

        sync_contact_login("alice@example.com", 1)

        body = mock_req.call_args[0][2]
        assert "LAST_LOGIN_AT" in body["contact_properties"]
        # Should be today's date in YYYY-MM-DD format
        today = time.strftime("%Y-%m-%d")
        assert body["contact_properties"]["LAST_LOGIN_AT"] == today


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE 4: sync_contact_session
# ═══════════════════════════════════════════════════════════════════════════

class TestSyncContactSession:
    """Test session event sync."""

    @patch("brevo_sync._brevo_request")
    def test_sends_session_start_event(self, mock_req):
        """Should POST to /events with session_start event_name."""
        from brevo_sync import sync_contact_session
        mock_req.return_value = {"ok": True, "status": 200, "body": {}}

        sync_contact_session("bob@example.com", 12)

        mock_req.assert_called_once()
        args = mock_req.call_args
        assert args[0][0] == "POST"
        assert args[0][1] == "/events"
        body = args[0][2]
        assert body["event_name"] == "session_start"
        assert body["identifiers"]["email_id"] == "bob@example.com"
        assert body["contact_properties"]["SESSION_COUNT"] == 12


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE 5: sync_contact_payment
# ═══════════════════════════════════════════════════════════════════════════

class TestSyncContactPayment:
    """Test payment event sync."""

    @patch("brevo_sync._brevo_request")
    def test_sends_payment_event(self, mock_req):
        """Should POST to /events with payment_completed event_name."""
        from brevo_sync import sync_contact_payment
        mock_req.return_value = {"ok": True, "status": 200, "body": {}}

        sync_contact_payment("alice@example.com", 149.0, "subscription_active", "I-ABC123")

        body = mock_req.call_args[0][2]
        assert body["event_name"] == "payment_completed"
        assert body["event_properties"]["amount"] == 149.0
        assert body["event_properties"]["subscription_id"] == "I-ABC123"
        assert body["contact_properties"]["PAYMENT_STATUS"] == "subscription_active"


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE 6: ensure_brevo_attributes
# ═══════════════════════════════════════════════════════════════════════════

class TestEnsureAttributes:
    """Test custom attribute creation."""

    @patch("brevo_sync._brevo_request")
    def test_creates_all_attributes(self, mock_req):
        """Should make POST requests for all custom attributes."""
        from brevo_sync import ensure_brevo_attributes
        mock_req.return_value = {"ok": True, "status": 201, "body": {}}

        results = ensure_brevo_attributes()

        assert len(results) == 7
        assert mock_req.call_count == 7

        # Verify attribute names
        attr_names = [r["name"] for r in results]
        assert "LAST_LOGIN_AT" in attr_names
        assert "LOGIN_COUNT" in attr_names
        assert "SESSION_COUNT" in attr_names
        assert "NEXT_BILLING_DATE" in attr_names
        assert "PAYMENT_STATUS" in attr_names
        assert "AI_NAME" in attr_names
        assert "MAGIC_PORTAL_LINK" in attr_names

    @patch("brevo_sync._brevo_request")
    def test_handles_already_exists(self, mock_req):
        """Should not crash if attribute already exists (400 from Brevo)."""
        from brevo_sync import ensure_brevo_attributes
        mock_req.return_value = {"ok": False, "status": 400, "body": "attribute already exists"}

        results = ensure_brevo_attributes()
        assert len(results) == 7  # All 7 attempted even if all fail


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE 7: upsert_contact
# ═══════════════════════════════════════════════════════════════════════════

class TestUpsertContact:
    """Test contact creation/update."""

    @patch("brevo_sync._brevo_request")
    def test_upsert_with_update_enabled(self, mock_req):
        """Should POST to /contacts with updateEnabled=true."""
        from brevo_sync import upsert_contact
        mock_req.return_value = {"ok": True, "status": 201, "body": {"id": 1}}

        upsert_contact("alice@example.com", first_name="Alice", last_name="Smith")

        args = mock_req.call_args
        assert args[0][0] == "POST"
        assert args[0][1] == "/contacts"
        body = args[0][2]
        assert body["email"] == "alice@example.com"
        assert body["updateEnabled"] is True
        assert body["attributes"]["FIRSTNAME"] == "Alice"
        assert body["attributes"]["LASTNAME"] == "Smith"

    @patch("brevo_sync._brevo_request")
    def test_upsert_with_list_ids(self, mock_req):
        """Should include listIds when provided."""
        from brevo_sync import upsert_contact
        mock_req.return_value = {"ok": True, "status": 201, "body": {"id": 1}}

        upsert_contact("bob@example.com", list_ids=[3, 7])

        body = mock_req.call_args[0][2]
        assert body["listIds"] == [3, 7]

    @patch("brevo_sync._brevo_request")
    def test_upsert_with_custom_attributes(self, mock_req):
        """Should merge custom attributes with name attributes."""
        from brevo_sync import upsert_contact
        mock_req.return_value = {"ok": True, "status": 201, "body": {"id": 1}}

        upsert_contact(
            "alice@example.com",
            first_name="Alice",
            attributes={"PAYMENT_STATUS": "active"},
        )

        body = mock_req.call_args[0][2]
        assert body["attributes"]["FIRSTNAME"] == "Alice"
        assert body["attributes"]["PAYMENT_STATUS"] == "active"

    @patch("brevo_sync._brevo_request")
    def test_upsert_minimal(self, mock_req):
        """Should work with just email, no optional fields."""
        from brevo_sync import upsert_contact
        mock_req.return_value = {"ok": True, "status": 201, "body": {"id": 1}}

        upsert_contact("minimal@example.com")

        body = mock_req.call_args[0][2]
        assert body["email"] == "minimal@example.com"
        assert body["updateEnabled"] is True
        assert "attributes" not in body
        assert "listIds" not in body


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE 8: sync_contact_status and sync_billing_date
# ═══════════════════════════════════════════════════════════════════════════

class TestSyncContactStatus:
    """Test contact status update."""

    @patch("brevo_sync._brevo_request")
    def test_sends_status_update(self, mock_req):
        """Should PUT to /contacts/{email} with payment status."""
        from brevo_sync import sync_contact_status
        mock_req.return_value = {"ok": True, "status": 204, "body": {}}

        sync_contact_status("alice@example.com", "subscription_cancelled")

        args = mock_req.call_args
        assert args[0][0] == "PUT"
        assert "alice%40example.com" in args[0][1]
        body = args[0][2]
        assert body["attributes"]["PAYMENT_STATUS"] == "subscription_cancelled"


class TestSyncBillingDate:
    """Test billing date sync."""

    @patch("brevo_sync._brevo_request")
    def test_sends_billing_date(self, mock_req):
        """Should PUT to /contacts/{email} with billing date."""
        from brevo_sync import sync_billing_date
        mock_req.return_value = {"ok": True, "status": 204, "body": {}}

        sync_billing_date("alice@example.com", "2026-05-15")

        args = mock_req.call_args
        assert args[0][0] == "PUT"
        body = args[0][2]
        assert body["attributes"]["NEXT_BILLING_DATE"] == "2026-05-15"


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE 9: Tracking Integration (fire-and-forget)
# ═══════════════════════════════════════════════════════════════════════════

class TestTrackingBrevoIntegration:
    """Test that tracking.py calls Brevo and handles failures gracefully."""

    @patch("brevo_sync._brevo_request")
    def test_record_login_calls_brevo(self, mock_req, populated_db):
        """record_login should call sync_contact_login on success."""
        mock_req.return_value = {"ok": True, "status": 200, "body": {}}

        from tracking import record_login
        result = record_login(populated_db, "alice@example.com")

        assert result is True
        # Brevo should have been called
        mock_req.assert_called()
        # Verify it was a portal_login event
        call_body = mock_req.call_args[0][2]
        assert call_body["event_name"] == "portal_login"
        assert call_body["contact_properties"]["LOGIN_COUNT"] == 1

    @patch("brevo_sync._brevo_request")
    def test_record_login_succeeds_when_brevo_fails(self, mock_req, populated_db):
        """record_login should still return True even if Brevo call fails."""
        mock_req.side_effect = Exception("Brevo is down")

        from tracking import record_login
        result = record_login(populated_db, "alice@example.com")

        assert result is True  # Tracking must succeed even if Brevo fails

        # Verify DB was still updated
        conn = sqlite3.connect(populated_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT login_count FROM clients WHERE email = ?", ("alice@example.com",)
        ).fetchone()
        conn.close()
        assert row["login_count"] == 1

    @patch("brevo_sync._brevo_request")
    def test_record_activity_new_session_calls_brevo(self, mock_req, populated_db):
        """New session should trigger sync_contact_session."""
        mock_req.return_value = {"ok": True, "status": 200, "body": {}}

        from tracking import record_activity
        result = record_activity(populated_db, "alice@example.com", force_new_session=True)

        assert result["new_session"] is True
        # Brevo should have been called with session_start
        mock_req.assert_called()
        call_body = mock_req.call_args[0][2]
        assert call_body["event_name"] == "session_start"
        assert call_body["contact_properties"]["SESSION_COUNT"] == 1

    @patch("brevo_sync._brevo_request")
    def test_record_activity_no_session_no_brevo(self, mock_req, populated_db):
        """Non-new-session activity should NOT call Brevo session sync."""
        mock_req.return_value = {"ok": True, "status": 200, "body": {}}

        # Set recent last_active_at so it's within session window
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(populated_db)
        conn.execute(
            "UPDATE clients SET last_active_at = ?, session_count = 1 WHERE email = ?",
            (now, "alice@example.com")
        )
        conn.commit()
        conn.close()

        from tracking import record_activity
        result = record_activity(populated_db, "alice@example.com")

        # Throttled or not a new session — no Brevo call
        mock_req.assert_not_called()

    @patch("brevo_sync._brevo_request")
    def test_record_activity_succeeds_when_brevo_fails(self, mock_req, populated_db):
        """record_activity should succeed even if Brevo call fails."""
        mock_req.side_effect = Exception("Brevo is down")

        from tracking import record_activity
        result = record_activity(populated_db, "alice@example.com", force_new_session=True)

        assert result["new_session"] is True
        assert result["updated"] is True

    @patch("brevo_sync._brevo_request")
    def test_webhook_cancelled_calls_brevo(self, mock_req, populated_db):
        """Webhook cancellation should sync status to Brevo."""
        mock_req.return_value = {"ok": True, "status": 204, "body": {}}

        # Set up subscription
        conn = sqlite3.connect(populated_db)
        conn.execute(
            "UPDATE clients SET payment_status = 'subscription_active', "
            "paypal_subscription_id = 'I-ABC123' WHERE email = ?",
            ("alice@example.com",)
        )
        conn.commit()
        conn.close()

        from tracking import process_webhook_event
        event = {
            "id": "WH-1",
            "event_type": "BILLING.SUBSCRIPTION.CANCELLED",
            "resource": {"id": "I-ABC123"},
        }
        result = process_webhook_event(populated_db, event)

        assert result["processed"] is True
        # Brevo should have been called
        mock_req.assert_called()
        call_args = mock_req.call_args[0]
        assert call_args[0] == "PUT"  # PUT to update contact
        assert call_args[2]["attributes"]["PAYMENT_STATUS"] == "subscription_cancelled"

    @patch("brevo_sync._brevo_request")
    def test_webhook_payment_completed_uses_billing_agreement_id(self, mock_req, populated_db):
        """PAYMENT.SALE.COMPLETED should look up email by billing_agreement_id, not resource_id."""
        mock_req.return_value = {"ok": True, "status": 204, "body": {}}

        conn = sqlite3.connect(populated_db)
        conn.execute(
            "UPDATE clients SET payment_status = 'subscription_active', "
            "paypal_subscription_id = 'I-SUB999' WHERE email = ?",
            ("alice@example.com",)
        )
        conn.commit()
        conn.close()

        from tracking import process_webhook_event
        event = {
            "id": "WH-PAY-1",
            "event_type": "PAYMENT.SALE.COMPLETED",
            "resource": {
                "id": "SALE-12345",  # This is the sale ID, NOT the subscription ID
                "billing_agreement_id": "I-SUB999",  # THIS is the subscription ID
                "amount": {"total": "49.00"},
            },
        }
        result = process_webhook_event(populated_db, event)

        assert result["processed"] is True
        # Brevo should have been called (found email via billing_agreement_id, not resource_id)
        mock_req.assert_called()
        # Find the status update call (PUT to /contacts/...)
        put_calls = [
            c for c in mock_req.call_args_list
            if c[0][0] == "PUT"
        ]
        assert len(put_calls) >= 1, "Should have at least one PUT call for status update"
        assert put_calls[0][0][2]["attributes"]["PAYMENT_STATUS"] == "subscription_active"

    @patch("brevo_sync._brevo_request")
    def test_webhook_succeeds_when_brevo_fails(self, mock_req, populated_db):
        """Webhook processing should succeed even if Brevo fails."""
        mock_req.side_effect = Exception("Brevo is down")

        conn = sqlite3.connect(populated_db)
        conn.execute(
            "UPDATE clients SET paypal_subscription_id = 'I-ABC123' WHERE email = ?",
            ("alice@example.com",)
        )
        conn.commit()
        conn.close()

        from tracking import process_webhook_event
        event = {
            "id": "WH-1",
            "event_type": "BILLING.SUBSCRIPTION.CANCELLED",
            "resource": {"id": "I-ABC123"},
        }
        result = process_webhook_event(populated_db, event)
        assert result["processed"] is True  # Must succeed regardless


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE 10: Enhancement 1 — List 8 auto-add on payment_completed
# ═══════════════════════════════════════════════════════════════════════════

class TestPaymentList8AutoAdd:
    """Test that sync_contact_payment upserts contact to List 8."""

    @patch("brevo_sync._brevo_request")
    def test_payment_completed_adds_to_list_8(self, mock_req):
        """sync_contact_payment should upsert contact to List 8 before firing event."""
        from brevo_sync import sync_contact_payment
        mock_req.return_value = {"ok": True, "status": 200, "body": {}}

        sync_contact_payment(
            "alice@example.com", 149.0, "subscription_active", "I-ABC123"
        )

        # Should have been called twice: first upsert (to List 8), then event
        assert mock_req.call_count == 2

        # First call: upsert to /contacts with listIds=[8]
        first_call = mock_req.call_args_list[0]
        assert first_call[0][0] == "POST"
        assert first_call[0][1] == "/contacts"
        body = first_call[0][2]
        assert body["listIds"] == [8]
        assert body["attributes"]["PAYMENT_STATUS"] == "subscription_active"

        # Second call: event to /events
        second_call = mock_req.call_args_list[1]
        assert second_call[0][1] == "/events"
        assert second_call[0][2]["event_name"] == "payment_completed"

    @patch("brevo_sync._brevo_request")
    def test_payment_completed_includes_ai_name(self, mock_req):
        """sync_contact_payment should include AI_NAME in attributes when provided."""
        from brevo_sync import sync_contact_payment
        mock_req.return_value = {"ok": True, "status": 200, "body": {}}

        sync_contact_payment(
            "alice@example.com", 149.0, "subscription_active",
            "I-ABC123", ai_name="Lyra"
        )

        # First call (upsert) should include AI_NAME
        first_call = mock_req.call_args_list[0]
        body = first_call[0][2]
        assert body["attributes"]["AI_NAME"] == "Lyra"

    @patch("brevo_sync._brevo_request")
    def test_payment_completed_includes_portal_url(self, mock_req):
        """sync_contact_payment should include MAGIC_PORTAL_LINK when provided."""
        from brevo_sync import sync_contact_payment
        mock_req.return_value = {"ok": True, "status": 200, "body": {}}

        sync_contact_payment(
            "alice@example.com", 149.0, "subscription_active",
            "I-ABC123", portal_url="https://my.purebrain.ai"
        )

        first_call = mock_req.call_args_list[0]
        body = first_call[0][2]
        assert body["attributes"]["MAGIC_PORTAL_LINK"] == "https://my.purebrain.ai"

    @patch("brevo_sync._brevo_request")
    def test_payment_completed_omits_empty_optionals(self, mock_req):
        """Empty ai_name/portal_url should not appear in attributes."""
        from brevo_sync import sync_contact_payment
        mock_req.return_value = {"ok": True, "status": 200, "body": {}}

        sync_contact_payment(
            "alice@example.com", 149.0, "subscription_active", "I-ABC123"
        )

        first_call = mock_req.call_args_list[0]
        body = first_call[0][2]
        assert "AI_NAME" not in body["attributes"]
        assert "MAGIC_PORTAL_LINK" not in body["attributes"]

    @patch("brevo_sync._brevo_request")
    def test_webhook_payment_calls_sync_contact_payment_with_ai_name(
        self, mock_req, populated_db
    ):
        """PAYMENT.SALE.COMPLETED webhook should pass ai_name to sync_contact_payment."""
        mock_req.return_value = {"ok": True, "status": 204, "body": {}}

        # Set up client with subscription and ai_name
        conn = sqlite3.connect(populated_db)
        conn.execute(
            "UPDATE clients SET payment_status = 'subscription_active', "
            "paypal_subscription_id = 'I-SUB999', ai_name = 'Lyra' "
            "WHERE email = ?",
            ("alice@example.com",),
        )
        conn.commit()
        conn.close()

        from tracking import process_webhook_event
        event = {
            "id": "WH-PAY-2",
            "event_type": "PAYMENT.SALE.COMPLETED",
            "resource": {
                "id": "SALE-99",
                "billing_agreement_id": "I-SUB999",
                "amount": {"total": "49.00"},
            },
        }
        result = process_webhook_event(populated_db, event)
        assert result["processed"] is True

        # Should have multiple Brevo calls: status update + payment sync
        # Find the upsert call (POST to /contacts with listIds)
        upsert_calls = [
            c for c in mock_req.call_args_list
            if c[0][1] == "/contacts" and c[0][0] == "POST"
        ]
        assert len(upsert_calls) >= 1, "Should upsert contact to List 8"
        upsert_body = upsert_calls[0][0][2]
        assert upsert_body["attributes"]["AI_NAME"] == "Lyra"
        assert upsert_body["listIds"] == [8]


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE 11: Enhancement 2 — AI_NAME + MAGIC_PORTAL_LINK attributes
# ═══════════════════════════════════════════════════════════════════════════

class TestAINamePortalLinkAttributes:
    """Test AI_NAME and MAGIC_PORTAL_LINK attribute sync."""

    @patch("brevo_sync._brevo_request")
    def test_ensure_attributes_includes_ai_name(self, mock_req):
        """ensure_brevo_attributes should create AI_NAME attribute."""
        from brevo_sync import ensure_brevo_attributes
        mock_req.return_value = {"ok": True, "status": 201, "body": {}}

        results = ensure_brevo_attributes()

        attr_names = [r["name"] for r in results]
        assert "AI_NAME" in attr_names

    @patch("brevo_sync._brevo_request")
    def test_ensure_attributes_includes_magic_portal_link(self, mock_req):
        """ensure_brevo_attributes should create MAGIC_PORTAL_LINK attribute."""
        from brevo_sync import ensure_brevo_attributes
        mock_req.return_value = {"ok": True, "status": 201, "body": {}}

        results = ensure_brevo_attributes()

        attr_names = [r["name"] for r in results]
        assert "MAGIC_PORTAL_LINK" in attr_names

    @patch("brevo_sync._brevo_request")
    def test_ensure_attributes_creates_seven_total(self, mock_req):
        """ensure_brevo_attributes should create 7 attributes (5 original + 2 new)."""
        from brevo_sync import ensure_brevo_attributes
        mock_req.return_value = {"ok": True, "status": 201, "body": {}}

        results = ensure_brevo_attributes()
        assert len(results) == 7
        assert mock_req.call_count == 7

    @patch("brevo_sync._brevo_request")
    def test_login_sync_includes_ai_name(self, mock_req):
        """sync_contact_login should include AI_NAME when provided."""
        from brevo_sync import sync_contact_login
        mock_req.return_value = {"ok": True, "status": 200, "body": {}}

        sync_contact_login("alice@example.com", 5, ai_name="Lyra")

        body = mock_req.call_args[0][2]
        assert body["contact_properties"]["AI_NAME"] == "Lyra"

    @patch("brevo_sync._brevo_request")
    def test_login_sync_includes_portal_url(self, mock_req):
        """sync_contact_login should include MAGIC_PORTAL_LINK when provided."""
        from brevo_sync import sync_contact_login
        mock_req.return_value = {"ok": True, "status": 200, "body": {}}

        sync_contact_login("alice@example.com", 5, portal_url="https://my.purebrain.ai")

        body = mock_req.call_args[0][2]
        assert body["contact_properties"]["MAGIC_PORTAL_LINK"] == "https://my.purebrain.ai"

    @patch("brevo_sync._brevo_request")
    def test_login_sync_omits_empty_optionals(self, mock_req):
        """sync_contact_login should not include AI_NAME/MAGIC_PORTAL_LINK when empty."""
        from brevo_sync import sync_contact_login
        mock_req.return_value = {"ok": True, "status": 200, "body": {}}

        sync_contact_login("alice@example.com", 5)

        body = mock_req.call_args[0][2]
        assert "AI_NAME" not in body["contact_properties"]
        assert "MAGIC_PORTAL_LINK" not in body["contact_properties"]

    @patch("brevo_sync._brevo_request")
    def test_record_login_passes_ai_name_to_brevo(self, mock_req, populated_db):
        """record_login should look up ai_name from DB and pass to Brevo."""
        mock_req.return_value = {"ok": True, "status": 200, "body": {}}

        # Set ai_name for the test client
        conn = sqlite3.connect(populated_db)
        conn.execute(
            "UPDATE clients SET ai_name = 'Meridian' WHERE email = ?",
            ("alice@example.com",),
        )
        conn.commit()
        conn.close()

        from tracking import record_login
        record_login(populated_db, "alice@example.com")

        body = mock_req.call_args[0][2]
        assert body["contact_properties"]["AI_NAME"] == "Meridian"

    @patch("brevo_sync._brevo_request")
    def test_record_login_passes_portal_url_to_brevo(self, mock_req, populated_db, tmp_path):
        """record_login should resolve portal URL and pass to Brevo."""
        mock_req.return_value = {"ok": True, "status": 200, "body": {}}

        # Create a fake .portal-cname file
        cname_file = tmp_path / ".portal-cname"
        cname_file.write_text("mike.purebrain.ai")

        from tracking import record_login
        with patch("tracking._get_portal_url", return_value="https://mike.purebrain.ai"):
            record_login(populated_db, "alice@example.com")

        body = mock_req.call_args[0][2]
        assert body["contact_properties"]["MAGIC_PORTAL_LINK"] == "https://mike.purebrain.ai"


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE 12: Enhancement 3 — Failure alerting via AgentMail
# ═══════════════════════════════════════════════════════════════════════════

class TestFailureAlerting:
    """Test Brevo failure alert emails via AgentMail."""

    @patch("brevo_sync.urllib.request.urlopen")
    def test_failure_alert_sends_email(self, mock_urlopen):
        """Brevo API failure should trigger alert email to Lyra + Aether."""
        from brevo_sync import _notify_sync_failure
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"id": "msg-1"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        with patch.dict(os.environ, {"AGENTMAIL_API_KEY": "test-key-123"}):
            _notify_sync_failure("alice@example.com", "portal_login", "HTTP 500: Internal")

        # Should have made a request to AgentMail
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert body["subject"] == "[Brevo Sync Failed] alice@example.com"
        assert "portal_login" in body["text"]
        assert "HTTP 500" in body["text"]
        # Should send to both Lyra and Aether
        to_emails = [r["email"] for r in body["to"]]
        assert "lyra-pmg@agentmail.to" in to_emails
        assert "aethergottaeat@agentmail.to" in to_emails

    def test_failure_alert_does_not_fire_on_no_key(self):
        """'No AGENTMAIL_API_KEY' should NOT trigger alert (silently skip)."""
        from brevo_sync import _notify_sync_failure
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("AGENTMAIL_API_KEY", None)
            # Should not raise
            _notify_sync_failure("alice@example.com", "portal_login", "HTTP 500")

    @patch("brevo_sync.urllib.request.urlopen")
    def test_failure_alert_does_not_block_on_agentmail_failure(self, mock_urlopen):
        """Alert email failure should be silently swallowed."""
        from brevo_sync import _notify_sync_failure
        mock_urlopen.side_effect = Exception("AgentMail is down")

        with patch.dict(os.environ, {"AGENTMAIL_API_KEY": "test-key-123"}):
            # Should not raise
            _notify_sync_failure("alice@example.com", "portal_login", "HTTP 500")

    @patch("brevo_sync.urllib.request.urlopen")
    def test_brevo_http_error_triggers_alert(self, mock_urlopen):
        """_brevo_request with alert_on_failure=True should call _notify on HTTP error."""
        from brevo_sync import _brevo_request
        import urllib.error

        # First call: Brevo returns HTTP error
        error_resp = MagicMock()
        error_resp.read.return_value = b"Bad Request"
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.brevo.com/v3/events", 400, "Bad Request",
            {}, error_resp
        )

        encoded = _make_encoded_key("xkeysib-test")
        with patch.dict(os.environ, {"BREVO_API_KEY": encoded, "AGENTMAIL_API_KEY": "ak-123"}):
            with patch("brevo_sync._notify_sync_failure") as mock_notify:
                result = _brevo_request(
                    "POST", "/events",
                    {"event_name": "portal_login", "identifiers": {"email_id": "bob@x.com"}},
                    alert_on_failure=True,
                )

        assert result["ok"] is False
        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        assert call_args[1]["email"] == "bob@x.com" or call_args[0][0] == "bob@x.com"

    @patch("brevo_sync.urllib.request.urlopen")
    def test_brevo_no_alert_by_default(self, mock_urlopen):
        """_brevo_request without alert_on_failure should NOT call _notify."""
        from brevo_sync import _brevo_request
        import urllib.error

        error_resp = MagicMock()
        error_resp.read.return_value = b"Bad Request"
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.brevo.com/v3/events", 400, "Bad Request",
            {}, error_resp
        )

        encoded = _make_encoded_key("xkeysib-test")
        with patch.dict(os.environ, {"BREVO_API_KEY": encoded}):
            with patch("brevo_sync._notify_sync_failure") as mock_notify:
                result = _brevo_request("POST", "/events", {"test": True})

        assert result["ok"] is False
        mock_notify.assert_not_called()
