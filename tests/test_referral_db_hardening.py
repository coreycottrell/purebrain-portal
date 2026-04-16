"""
Tests for referral DB hardening: indexes, payout_requests table,
financial_audit_log table, and _log_financial_event helper.

TDD: These tests are written FIRST, before implementation.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

# Add portal root to path so we can import portal_server
PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PORTAL_DIR)

import aiosqlite


def _run(coro):
    """Helper to run async code in sync tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


class TestIndexesExistAfterInit(unittest.TestCase):
    """All performance indexes should be created by _init_referral_db."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_indexes_exist_after_init(self):
        """_init_referral_db should create all performance indexes."""
        from pathlib import Path

        with patch("portal_server.REFERRALS_DB", Path(self.db_path)):
            from portal_server import _init_referral_db
            _run(_init_referral_db())

        async def _check():
            async with aiosqlite.connect(self.db_path) as db:
                cur = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
                rows = await cur.fetchall()
                index_names = {r[0] for r in rows}
                return index_names

        index_names = _run(_check())

        expected = {
            "idx_referrals_referrer_status",
            "idx_referrals_referred_email",
            "idx_rewards_referrer",
            "idx_clicks_code",
            "idx_commissions_referrer",
            "idx_commissions_order",
        }
        for idx in expected:
            self.assertIn(idx, index_names, f"Missing index: {idx}")


class TestPayoutRequestsTable(unittest.TestCase):
    """payout_requests table should be created by _init_referral_db."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def _init_db(self):
        from pathlib import Path
        with patch("portal_server.REFERRALS_DB", Path(self.db_path)):
            from portal_server import _init_referral_db
            _run(_init_referral_db())

    def test_payout_requests_table_exists(self):
        """payout_requests table should be created by _init_referral_db."""
        self._init_db()

        async def _check():
            async with aiosqlite.connect(self.db_path) as db:
                cur = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='payout_requests'"
                )
                return await cur.fetchone()

        row = _run(_check())
        self.assertIsNotNone(row, "payout_requests table should exist")

    def test_payout_requests_has_unique_request_id(self):
        """Inserting duplicate request_id should raise IntegrityError."""
        self._init_db()

        async def _insert_duplicate():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO payout_requests (request_id, referral_code, created_at) "
                    "VALUES ('REQ-001', 'PB-AAAA', '2026-04-16T00:00:00Z')"
                )
                await db.commit()
                # Second insert with same request_id should fail
                await db.execute(
                    "INSERT INTO payout_requests (request_id, referral_code, created_at) "
                    "VALUES ('REQ-001', 'PB-BBBB', '2026-04-16T00:00:00Z')"
                )
                await db.commit()

        with self.assertRaises(aiosqlite.IntegrityError):
            _run(_insert_duplicate())

    def test_payout_requests_indexes_exist(self):
        """payout_requests should have indexes on referral_code and status."""
        self._init_db()

        async def _check():
            async with aiosqlite.connect(self.db_path) as db:
                cur = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
                rows = await cur.fetchall()
                return {r[0] for r in rows}

        index_names = _run(_check())
        self.assertIn("idx_payouts_code", index_names)
        self.assertIn("idx_payouts_status", index_names)


class TestFinancialAuditLogTable(unittest.TestCase):
    """financial_audit_log table should exist after init."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def _init_db(self):
        from pathlib import Path
        with patch("portal_server.REFERRALS_DB", Path(self.db_path)):
            from portal_server import _init_referral_db
            _run(_init_referral_db())

    def test_audit_log_table_exists(self):
        """financial_audit_log table should exist after init."""
        self._init_db()

        async def _check():
            async with aiosqlite.connect(self.db_path) as db:
                cur = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='financial_audit_log'"
                )
                return await cur.fetchone()

        row = _run(_check())
        self.assertIsNotNone(row, "financial_audit_log table should exist")

    def test_audit_log_records_events(self):
        """Should be able to insert and read audit log entries."""
        self._init_db()

        async def _insert_and_read():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """INSERT INTO financial_audit_log
                       (event_type, actor, referral_code, amount, details, ip_address, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    ("payout_requested", "admin@test.com", "PB-AAAA", 50.0,
                     "Test payout", "127.0.0.1", "2026-04-16T00:00:00Z")
                )
                await db.commit()
                cur = await db.execute("SELECT * FROM financial_audit_log")
                return await cur.fetchall()

        rows = _run(_insert_and_read())
        self.assertEqual(len(rows), 1)
        # id, event_type, actor, referral_code, amount, details, ip_address, created_at
        self.assertEqual(rows[0][1], "payout_requested")
        self.assertEqual(rows[0][2], "admin@test.com")
        self.assertEqual(rows[0][3], "PB-AAAA")
        self.assertAlmostEqual(rows[0][4], 50.0)


class TestLogFinancialEvent(unittest.TestCase):
    """_log_financial_event should insert a row into financial_audit_log."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_log_financial_event_writes_to_db(self):
        """_log_financial_event should insert a row into financial_audit_log."""
        from pathlib import Path

        with patch("portal_server.REFERRALS_DB", Path(self.db_path)):
            from portal_server import _init_referral_db, _log_financial_event
            _run(_init_referral_db())
            _run(_log_financial_event(
                event_type="commission_paid",
                referral_code="PB-TEST",
                amount=25.50,
                actor="system",
                details="Monthly commission",
                ip_address="10.0.0.1",
            ))

        async def _read():
            async with aiosqlite.connect(self.db_path) as db:
                cur = await db.execute("SELECT * FROM financial_audit_log")
                return await cur.fetchall()

        rows = _run(_read())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "commission_paid")
        self.assertEqual(rows[0][2], "system")
        self.assertEqual(rows[0][3], "PB-TEST")
        self.assertAlmostEqual(rows[0][4], 25.50)
        self.assertEqual(rows[0][5], "Monthly commission")
        self.assertEqual(rows[0][6], "10.0.0.1")
        # created_at should be an ISO timestamp
        self.assertIn("T", rows[0][7])

    def test_log_financial_event_handles_errors_gracefully(self):
        """_log_financial_event should not raise on DB errors (prints instead)."""
        from pathlib import Path

        # Point to a non-existent directory so DB creation fails
        bad_path = Path("/nonexistent/dir/bad.db")
        with patch("portal_server.REFERRALS_DB", bad_path):
            from portal_server import _log_financial_event
            # Should NOT raise - just prints error
            _run(_log_financial_event(
                event_type="test_event",
                referral_code="PB-FAIL",
            ))


if __name__ == "__main__":
    unittest.main()
