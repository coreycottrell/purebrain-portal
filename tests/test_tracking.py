"""
Tests for Priority 1 user tracking features:
- Login timestamp tracking (last_login_at, login_count)
- Session count + last_active_at throttling
- PayPal webhook endpoint
- Subscription renewal date tracking
"""

import asyncio
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Add portal root to path
PORTAL_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PORTAL_DIR))


# ── Helpers ──────────────────────────────────────────────────────────────────

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


def _get_client(db_path: str, email: str) -> dict:
    """Fetch a client row as dict."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT * FROM clients WHERE email = ? COLLATE NOCASE", (email,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else {}


# ── Import the tracking module ───────────────────────────────────────────────

# We'll import the tracking module after creating it
# For now, define the expected interface so tests serve as spec

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


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE 1: Login Tracking
# ═══════════════════════════════════════════════════════════════════════════

class TestLoginTracking:
    """Test that login timestamps and counts are recorded correctly."""

    def test_record_login_updates_last_login_at(self, populated_db):
        """First login should set last_login_at."""
        from tracking import record_login
        record_login(populated_db, "alice@example.com")

        client = _get_client(populated_db, "alice@example.com")
        assert client["last_login_at"] != "", "last_login_at should be set after login"
        assert client["login_count"] == 1

    def test_record_login_increments_count(self, populated_db):
        """Multiple logins should increment login_count."""
        from tracking import record_login
        record_login(populated_db, "alice@example.com")
        record_login(populated_db, "alice@example.com")
        record_login(populated_db, "alice@example.com")

        client = _get_client(populated_db, "alice@example.com")
        assert client["login_count"] == 3

    def test_record_login_unknown_email_is_noop(self, populated_db):
        """Login for unknown email should not crash."""
        from tracking import record_login
        # Should not raise
        record_login(populated_db, "nobody@example.com")

    def test_record_login_updates_timestamp_each_time(self, populated_db):
        """Each login should update the timestamp."""
        from tracking import record_login
        record_login(populated_db, "alice@example.com")
        first = _get_client(populated_db, "alice@example.com")["last_login_at"]

        time.sleep(0.01)  # Ensure different timestamp
        record_login(populated_db, "alice@example.com")
        second = _get_client(populated_db, "alice@example.com")["last_login_at"]

        assert second >= first, "Timestamp should advance on each login"


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE 2: Session Tracking
# ═══════════════════════════════════════════════════════════════════════════

class TestSessionTracking:
    """Test session counting and last_active_at throttling."""

    def test_new_session_increments_session_count(self, populated_db):
        """A new session (>5 min gap) should increment session_count."""
        from tracking import record_activity

        # First activity = new session
        record_activity(populated_db, "alice@example.com", force_new_session=True)
        client = _get_client(populated_db, "alice@example.com")
        assert client["session_count"] == 1

    def test_activity_within_session_does_not_increment(self, populated_db):
        """Activity within same session should NOT increment session_count."""
        from tracking import record_activity

        now = datetime.now(timezone.utc).isoformat()
        # Set last_active_at to just now (within 5 min window)
        conn = sqlite3.connect(populated_db)
        conn.execute(
            "UPDATE clients SET last_active_at = ?, session_count = 1 WHERE email = ?",
            (now, "alice@example.com")
        )
        conn.commit()
        conn.close()

        record_activity(populated_db, "alice@example.com")
        client = _get_client(populated_db, "alice@example.com")
        assert client["session_count"] == 1, "Should not increment within same session"

    def test_activity_updates_last_active_at(self, populated_db):
        """Activity should update last_active_at."""
        from tracking import record_activity

        record_activity(populated_db, "alice@example.com", force_new_session=True)
        client = _get_client(populated_db, "alice@example.com")
        assert client["last_active_at"] != "", "last_active_at should be set"

    def test_last_active_at_throttle(self, populated_db):
        """last_active_at should not update more than once per minute."""
        from tracking import record_activity

        record_activity(populated_db, "alice@example.com", force_new_session=True)
        first = _get_client(populated_db, "alice@example.com")["last_active_at"]

        # Call again immediately — should be throttled
        record_activity(populated_db, "alice@example.com")
        second = _get_client(populated_db, "alice@example.com")["last_active_at"]

        assert second == first, "Should be throttled — same timestamp within 1 minute"

    def test_last_active_at_updates_after_throttle_window(self, populated_db):
        """After throttle window, last_active_at should update."""
        from tracking import record_activity

        # Set last_active_at to 2 minutes ago
        old_time = "2020-01-01T00:00:00+00:00"
        conn = sqlite3.connect(populated_db)
        conn.execute(
            "UPDATE clients SET last_active_at = ?, session_count = 1 WHERE email = ?",
            (old_time, "alice@example.com")
        )
        conn.commit()
        conn.close()

        record_activity(populated_db, "alice@example.com")
        client = _get_client(populated_db, "alice@example.com")
        assert client["last_active_at"] != old_time, "Should update after throttle window"


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE 3: PayPal Webhook
# ═══════════════════════════════════════════════════════════════════════════

class TestPayPalWebhook:
    """Test PayPal webhook receiving and processing."""

    def test_log_webhook_event(self, tmp_db):
        """Webhook events should be logged to the database."""
        from tracking import log_webhook_event

        event = {
            "id": "WH-12345",
            "event_type": "PAYMENT.SALE.COMPLETED",
            "resource": {"id": "PAY-67890"},
        }
        log_webhook_event(tmp_db, event)

        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM paypal_webhook_log")
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()

        assert len(rows) == 1
        assert rows[0]["event_type"] == "PAYMENT.SALE.COMPLETED"
        assert rows[0]["event_id"] == "WH-12345"

    def test_process_subscription_cancelled(self, populated_db):
        """BILLING.SUBSCRIPTION.CANCELLED should update client status."""
        from tracking import process_webhook_event

        # Set up client with active subscription
        conn = sqlite3.connect(populated_db)
        conn.execute(
            "UPDATE clients SET payment_status = 'subscription_active', "
            "paypal_subscription_id = 'I-ABC123' WHERE email = ?",
            ("alice@example.com",)
        )
        conn.commit()
        conn.close()

        event = {
            "id": "WH-CANCEL-1",
            "event_type": "BILLING.SUBSCRIPTION.CANCELLED",
            "resource": {"id": "I-ABC123"},
        }
        result = process_webhook_event(populated_db, event)

        client = _get_client(populated_db, "alice@example.com")
        assert client["payment_status"] == "subscription_cancelled"

    def test_process_payment_completed(self, populated_db):
        """PAYMENT.SALE.COMPLETED should update last payment info."""
        from tracking import process_webhook_event

        conn = sqlite3.connect(populated_db)
        conn.execute(
            "UPDATE clients SET paypal_subscription_id = 'I-ABC123', "
            "payment_status = 'subscription_active' WHERE email = ?",
            ("alice@example.com",)
        )
        conn.commit()
        conn.close()

        event = {
            "id": "WH-PAY-1",
            "event_type": "PAYMENT.SALE.COMPLETED",
            "resource": {
                "id": "SALE-123",
                "billing_agreement_id": "I-ABC123",
                "amount": {"total": "149.00", "currency": "USD"},
            },
        }
        result = process_webhook_event(populated_db, event)
        assert result["processed"] is True

    def test_process_payment_failed(self, populated_db):
        """BILLING.SUBSCRIPTION.PAYMENT.FAILED should flag client."""
        from tracking import process_webhook_event

        conn = sqlite3.connect(populated_db)
        conn.execute(
            "UPDATE clients SET paypal_subscription_id = 'I-ABC123', "
            "payment_status = 'subscription_active' WHERE email = ?",
            ("alice@example.com",)
        )
        conn.commit()
        conn.close()

        event = {
            "id": "WH-FAIL-1",
            "event_type": "BILLING.SUBSCRIPTION.PAYMENT.FAILED",
            "resource": {"id": "I-ABC123"},
        }
        result = process_webhook_event(populated_db, event)

        client = _get_client(populated_db, "alice@example.com")
        assert client["payment_status"] == "payment_failed"

    def test_unknown_event_type_is_logged_not_processed(self, tmp_db):
        """Unknown event types should be logged but not processed."""
        from tracking import log_webhook_event, process_webhook_event

        event = {
            "id": "WH-UNKNOWN",
            "event_type": "SOME.UNKNOWN.EVENT",
            "resource": {"id": "RES-1"},
        }
        log_webhook_event(tmp_db, event)
        result = process_webhook_event(tmp_db, event)
        assert result["processed"] is False


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE 4: Renewal Date Tracking
# ═══════════════════════════════════════════════════════════════════════════

class TestRenewalDateTracking:
    """Test that next_billing_date is stored correctly."""

    def test_store_next_billing_date(self, populated_db):
        """Should store next_billing_date for a client."""
        from tracking import update_next_billing_date

        update_next_billing_date(
            populated_db, "alice@example.com", "2026-05-15T00:00:00Z"
        )
        client = _get_client(populated_db, "alice@example.com")
        assert client["next_billing_date"] == "2026-05-15T00:00:00Z"

    def test_clear_next_billing_date(self, populated_db):
        """Should clear next_billing_date when set to empty."""
        from tracking import update_next_billing_date

        update_next_billing_date(
            populated_db, "alice@example.com", "2026-05-15T00:00:00Z"
        )
        update_next_billing_date(populated_db, "alice@example.com", "")

        client = _get_client(populated_db, "alice@example.com")
        assert client["next_billing_date"] == ""


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE 5: DB Migration Safety
# ═══════════════════════════════════════════════════════════════════════════

class TestDBMigration:
    """Test that new columns are added safely to existing databases."""

    def test_ensure_tracking_columns_on_old_db(self, tmp_path):
        """ensure_tracking_columns should add missing columns to old schema."""
        db_path = str(tmp_path / "old_clients.db")
        # Create DB without tracking columns
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                first_seen_at TEXT NOT NULL,
                last_active_at TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paypal_webhook_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL,
                resource_id TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL,
                received_at TEXT NOT NULL,
                processed INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()

        from tracking import ensure_tracking_columns
        ensure_tracking_columns(db_path)

        # Verify columns exist
        conn = sqlite3.connect(db_path)
        cur = conn.execute("PRAGMA table_info(clients)")
        columns = {row[1] for row in cur.fetchall()}
        conn.close()

        assert "last_login_at" in columns
        assert "login_count" in columns
        assert "session_count" in columns
        assert "next_billing_date" in columns

    def test_ensure_tracking_columns_idempotent(self, tmp_db):
        """Running ensure_tracking_columns twice should not crash."""
        from tracking import ensure_tracking_columns
        ensure_tracking_columns(tmp_db)
        ensure_tracking_columns(tmp_db)  # Should not raise


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE 6: Computed Fields
# ═══════════════════════════════════════════════════════════════════════════

class TestComputedFields:
    """Test computed fields like days_since_login."""

    def test_days_since_login(self, populated_db):
        """days_since_login should compute from last_login_at."""
        from tracking import get_tracking_stats

        # Set last_login_at to 3 days ago
        from datetime import timedelta
        three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        conn = sqlite3.connect(populated_db)
        conn.execute(
            "UPDATE clients SET last_login_at = ? WHERE email = ?",
            (three_days_ago, "alice@example.com")
        )
        conn.commit()
        conn.close()

        stats = get_tracking_stats(populated_db, "alice@example.com")
        assert stats["days_since_login"] >= 2  # Allow for timezone edge cases
        assert stats["days_since_login"] <= 4

    def test_days_since_login_never_logged_in(self, populated_db):
        """days_since_login should be -1 if never logged in."""
        from tracking import get_tracking_stats

        stats = get_tracking_stats(populated_db, "alice@example.com")
        assert stats["days_since_login"] == -1
