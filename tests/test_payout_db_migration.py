"""
Tests for payout system migration from JSONL flat file to SQLite.

TDD: These tests are written FIRST, before implementation.
Tests the new _read_payout_requests_db, _write_payout_request_db,
_update_payout_status_db functions and the JSONL migration logic.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

# Add portal root to path so we can import portal_server
PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PORTAL_DIR)

import aiosqlite


def _run(coro):
    """Helper to run async code in sync tests."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestPayoutDBHelpers(unittest.TestCase):
    """Test the new SQLite-backed payout helper functions."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def _init_db(self):
        with patch("portal_server.REFERRALS_DB", Path(self.db_path)):
            from portal_server import _init_referral_db
            _run(_init_referral_db())

    def test_read_returns_empty_for_fresh_db(self):
        """Fresh DB should return empty list."""
        self._init_db()
        with patch("portal_server.REFERRALS_DB", Path(self.db_path)):
            from portal_server import _read_payout_requests_db
            result = _run(_read_payout_requests_db())
        self.assertEqual(result, [])

    def test_write_and_read_payout_request(self):
        """Write a payout request to DB, read it back."""
        self._init_db()
        entry = {
            "request_id": "payout-PB-TEST-1234",
            "referral_code": "PB-TEST",
            "paypal_email": "test@example.com",
            "amount": 50.0,
            "status": "pending",
            "batch_id": "",
            "notes": "",
            "created_at": "2026-04-16T12:00:00+00:00",
            "created_at_ts": 1776528000.0,
            "paid_at": None,
        }
        with patch("portal_server.REFERRALS_DB", Path(self.db_path)):
            from portal_server import _write_payout_request_db, _read_payout_requests_db
            _run(_write_payout_request_db(entry))
            result = _run(_read_payout_requests_db())

        self.assertEqual(len(result), 1)
        row = result[0]
        self.assertEqual(row["request_id"], "payout-PB-TEST-1234")
        self.assertEqual(row["referral_code"], "PB-TEST")
        self.assertEqual(row["paypal_email"], "test@example.com")
        self.assertAlmostEqual(row["amount"], 50.0)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["created_at"], "2026-04-16T12:00:00+00:00")
        # created_at_ts should be synthesized from created_at ISO string
        self.assertIn("created_at_ts", row)
        self.assertIsInstance(row["created_at_ts"], float)

    def test_update_payout_status_changes_status(self):
        """Updating status should change it in DB."""
        self._init_db()
        entry = {
            "request_id": "payout-PB-UPD-1000",
            "referral_code": "PB-UPD",
            "paypal_email": "upd@example.com",
            "amount": 75.0,
            "status": "pending",
            "created_at": "2026-04-16T12:00:00+00:00",
            "created_at_ts": 1776528000.0,
            "paid_at": None,
        }
        with patch("portal_server.REFERRALS_DB", Path(self.db_path)):
            from portal_server import (
                _write_payout_request_db,
                _read_payout_requests_db,
                _update_payout_status_db,
            )
            _run(_write_payout_request_db(entry))
            ok = _run(_update_payout_status_db("payout-PB-UPD-1000", "processing"))
            self.assertTrue(ok)
            result = _run(_read_payout_requests_db())

        self.assertEqual(result[0]["status"], "processing")

    def test_update_payout_status_sets_paid_at(self):
        """Updating to 'completed' should set paid_at."""
        self._init_db()
        entry = {
            "request_id": "payout-PB-PAID-2000",
            "referral_code": "PB-PAID",
            "paypal_email": "paid@example.com",
            "amount": 100.0,
            "status": "pending",
            "created_at": "2026-04-16T12:00:00+00:00",
            "created_at_ts": 1776528000.0,
            "paid_at": None,
        }
        with patch("portal_server.REFERRALS_DB", Path(self.db_path)):
            from portal_server import (
                _write_payout_request_db,
                _read_payout_requests_db,
                _update_payout_status_db,
            )
            _run(_write_payout_request_db(entry))
            _run(_update_payout_status_db("payout-PB-PAID-2000", "completed", batch_id="BATCH-123"))
            result = _run(_read_payout_requests_db())

        row = result[0]
        self.assertEqual(row["status"], "completed")
        self.assertIsNotNone(row["paid_at"])
        self.assertIn("T", row["paid_at"])  # ISO format
        self.assertEqual(row["batch_id"], "BATCH-123")

    def test_update_nonexistent_returns_false(self):
        """Updating a nonexistent request_id returns False."""
        self._init_db()
        with patch("portal_server.REFERRALS_DB", Path(self.db_path)):
            from portal_server import _update_payout_status_db
            ok = _run(_update_payout_status_db("nonexistent-id", "completed"))
        self.assertFalse(ok)

    def test_duplicate_request_id_raises(self):
        """Inserting a duplicate request_id should raise IntegrityError."""
        self._init_db()
        entry = {
            "request_id": "payout-DUP-001",
            "referral_code": "PB-DUP",
            "paypal_email": "dup@example.com",
            "amount": 25.0,
            "status": "pending",
            "created_at": "2026-04-16T12:00:00+00:00",
            "created_at_ts": 1776528000.0,
            "paid_at": None,
        }

        with patch("portal_server.REFERRALS_DB", Path(self.db_path)):
            from portal_server import _write_payout_request_db
            _run(_write_payout_request_db(entry))
            with self.assertRaises(aiosqlite.IntegrityError):
                _run(_write_payout_request_db(entry))

    def test_cooldown_check_atomic(self):
        """Cooldown + insert should be atomic (single transaction).

        After inserting a pending request, reading back should show it
        immediately (no window where another request could slip in).
        """
        self._init_db()
        entry = {
            "request_id": "payout-PB-COOL-3000",
            "referral_code": "PB-COOL",
            "paypal_email": "cool@example.com",
            "amount": 30.0,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_at_ts": time.time(),
            "paid_at": None,
        }
        with patch("portal_server.REFERRALS_DB", Path(self.db_path)):
            from portal_server import _write_payout_request_db, _read_payout_requests_db
            _run(_write_payout_request_db(entry))
            # Immediately read back - should see the pending request
            result = _run(_read_payout_requests_db())

        pending = [r for r in result if r["referral_code"] == "PB-COOL" and r["status"] == "pending"]
        self.assertEqual(len(pending), 1, "Pending request should be visible immediately after write")

    def test_balance_deduction_uses_db_payouts(self):
        """Balance check should query payout_requests table for previously paid amounts.

        Insert two completed payouts, verify sum is correct via direct SQL
        (the same query the C4 balance fix will use).
        """
        self._init_db()
        entries = [
            {
                "request_id": "payout-PB-BAL-1",
                "referral_code": "PB-BAL",
                "paypal_email": "bal@example.com",
                "amount": 25.0,
                "status": "completed",
                "created_at": "2026-04-15T10:00:00+00:00",
                "created_at_ts": 1776441600.0,
                "paid_at": "2026-04-15T10:01:00+00:00",
            },
            {
                "request_id": "payout-PB-BAL-2",
                "referral_code": "PB-BAL",
                "paypal_email": "bal@example.com",
                "amount": 30.0,
                "status": "paid",
                "created_at": "2026-04-16T10:00:00+00:00",
                "created_at_ts": 1776528000.0,
                "paid_at": "2026-04-16T10:01:00+00:00",
            },
            {
                "request_id": "payout-PB-BAL-3",
                "referral_code": "PB-BAL",
                "paypal_email": "bal@example.com",
                "amount": 15.0,
                "status": "pending",  # should NOT count
                "created_at": "2026-04-16T11:00:00+00:00",
                "created_at_ts": 1776531600.0,
                "paid_at": None,
            },
        ]
        with patch("portal_server.REFERRALS_DB", Path(self.db_path)):
            from portal_server import _write_payout_request_db
            for e in entries:
                _run(_write_payout_request_db(e))

        # Query directly via SQL (same query the C4 fix uses)
        async def _check_sum():
            async with aiosqlite.connect(self.db_path) as db:
                cur = await db.execute(
                    """SELECT COALESCE(SUM(amount), 0) FROM payout_requests
                       WHERE referral_code = ? COLLATE NOCASE
                       AND status IN ('completed', 'paid')""",
                    ("PB-BAL",)
                )
                row = await cur.fetchone()
                return float(row[0]) if row else 0.0

        paid_total = _run(_check_sum())
        self.assertAlmostEqual(paid_total, 55.0)  # 25 + 30, NOT 25 + 30 + 15


class TestPayoutDBUpdateNotes(unittest.TestCase):
    """Test that _update_payout_status_db handles notes correctly."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def _init_db(self):
        with patch("portal_server.REFERRALS_DB", Path(self.db_path)):
            from portal_server import _init_referral_db
            _run(_init_referral_db())

    def test_update_with_notes(self):
        """Updating with notes should store them."""
        self._init_db()
        entry = {
            "request_id": "payout-PB-NOTES-1",
            "referral_code": "PB-NOTES",
            "paypal_email": "notes@example.com",
            "amount": 40.0,
            "status": "pending",
            "created_at": "2026-04-16T12:00:00+00:00",
            "created_at_ts": 1776528000.0,
            "paid_at": None,
        }
        with patch("portal_server.REFERRALS_DB", Path(self.db_path)):
            from portal_server import (
                _write_payout_request_db,
                _read_payout_requests_db,
                _update_payout_status_db,
            )
            _run(_write_payout_request_db(entry))
            _run(_update_payout_status_db(
                "payout-PB-NOTES-1", "paid", notes="Manual PayPal transfer"
            ))
            result = _run(_read_payout_requests_db())

        self.assertEqual(result[0]["notes"], "Manual PayPal transfer")
        self.assertEqual(result[0]["status"], "paid")
        self.assertIsNotNone(result[0]["paid_at"])


class TestJSONLMigration(unittest.TestCase):
    """Test one-time JSONL to SQLite migration logic."""

    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp_db.name
        self.tmp_db.close()

        self.tmp_dir = tempfile.mkdtemp()
        self.jsonl_path = Path(self.tmp_dir) / "payout-requests.jsonl"

    def tearDown(self):
        os.unlink(self.db_path)
        # Clean up tmp_dir
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_migration_imports_jsonl_entries(self):
        """Existing JSONL entries should be imported into SQLite on init."""
        # Create a JSONL file with two entries
        entries = [
            {
                "request_id": "payout-PB-MIG-1",
                "referral_code": "PB-MIG",
                "paypal_email": "mig1@example.com",
                "amount": 60.0,
                "status": "completed",
                "batch_id": "BATCH-M1",
                "notes": "migrated entry 1",
                "created_at": "2026-04-10T10:00:00+00:00",
                "paid_at": "2026-04-10T10:05:00+00:00",
            },
            {
                "request_id": "payout-PB-MIG-2",
                "referral_code": "PB-MIG",
                "paypal_email": "mig2@example.com",
                "amount": 40.0,
                "status": "pending",
                "batch_id": "",
                "notes": "",
                "created_at": "2026-04-15T10:00:00+00:00",
                "paid_at": None,
            },
        ]
        with self.jsonl_path.open("w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        with patch("portal_server.REFERRALS_DB", Path(self.db_path)), \
             patch("portal_server.PAYOUT_REQUESTS_FILE", self.jsonl_path):
            from portal_server import _init_referral_db
            _run(_init_referral_db())

        # Verify entries are in SQLite
        async def _check():
            async with aiosqlite.connect(self.db_path) as db:
                cur = await db.execute("SELECT request_id, amount, status FROM payout_requests ORDER BY request_id")
                return await cur.fetchall()

        rows = _run(_check())
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "payout-PB-MIG-1")
        self.assertAlmostEqual(rows[0][1], 60.0)
        self.assertEqual(rows[0][2], "completed")
        self.assertEqual(rows[1][0], "payout-PB-MIG-2")

    def test_migration_renames_jsonl_to_migrated(self):
        """After migration, JSONL file should be renamed to .jsonl.migrated."""
        with self.jsonl_path.open("w") as f:
            f.write(json.dumps({
                "request_id": "payout-REN-1",
                "referral_code": "PB-REN",
                "amount": 10.0,
                "status": "pending",
                "created_at": "2026-04-16T00:00:00+00:00",
            }) + "\n")

        with patch("portal_server.REFERRALS_DB", Path(self.db_path)), \
             patch("portal_server.PAYOUT_REQUESTS_FILE", self.jsonl_path):
            from portal_server import _init_referral_db
            _run(_init_referral_db())

        self.assertFalse(self.jsonl_path.exists(), "Original JSONL should be gone")
        migrated = self.jsonl_path.with_suffix(".jsonl.migrated")
        self.assertTrue(migrated.exists(), "Renamed .jsonl.migrated file should exist")

    def test_migration_skips_when_no_jsonl(self):
        """When no JSONL file exists, migration should be a no-op."""
        # Don't create any JSONL file
        with patch("portal_server.REFERRALS_DB", Path(self.db_path)), \
             patch("portal_server.PAYOUT_REQUESTS_FILE", self.jsonl_path):
            from portal_server import _init_referral_db
            _run(_init_referral_db())

        # DB should init fine with zero payout_requests
        async def _check():
            async with aiosqlite.connect(self.db_path) as db:
                cur = await db.execute("SELECT COUNT(*) FROM payout_requests")
                row = await cur.fetchone()
                return row[0]

        count = _run(_check())
        self.assertEqual(count, 0)

    def test_migration_ignores_duplicates(self):
        """INSERT OR IGNORE means re-running migration with same data is safe."""
        entry_json = json.dumps({
            "request_id": "payout-DUP-MIG-1",
            "referral_code": "PB-DUP",
            "amount": 20.0,
            "status": "pending",
            "created_at": "2026-04-16T00:00:00+00:00",
        })

        # Write the JSONL file
        with self.jsonl_path.open("w") as f:
            f.write(entry_json + "\n")

        with patch("portal_server.REFERRALS_DB", Path(self.db_path)), \
             patch("portal_server.PAYOUT_REQUESTS_FILE", self.jsonl_path):
            from portal_server import _init_referral_db
            _run(_init_referral_db())

        # Recreate JSONL file and run init again (simulating non-rename edge case)
        migrated = self.jsonl_path.with_suffix(".jsonl.migrated")
        if migrated.exists():
            migrated.rename(self.jsonl_path)

        with patch("portal_server.REFERRALS_DB", Path(self.db_path)), \
             patch("portal_server.PAYOUT_REQUESTS_FILE", self.jsonl_path):
            from portal_server import _init_referral_db
            # Should not raise
            _run(_init_referral_db())

        async def _check():
            async with aiosqlite.connect(self.db_path) as db:
                cur = await db.execute("SELECT COUNT(*) FROM payout_requests")
                row = await cur.fetchone()
                return row[0]

        count = _run(_check())
        self.assertEqual(count, 1, "Duplicate should be ignored")


if __name__ == "__main__":
    unittest.main()
