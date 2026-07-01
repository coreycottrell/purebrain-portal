"""
Test for referral leaderboard cartesian product bug fix.

The old query double LEFT JOINed referrals and rewards on referrer_id,
producing N*M rows per referrer (e.g. 8 referrals * 8 rewards = 64 rows,
so completed_count showed 64 instead of 8).

The fix uses pre-aggregated subqueries to avoid the cartesian product.

This test creates a referrer with multiple completed referrals AND multiple
reward rows, then verifies the leaderboard returns the correct counts.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

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


async def _setup_test_db(db_path: str):
    """Create referral tables and insert test data with known counts."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA foreign_keys = ON")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrers (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name     TEXT NOT NULL DEFAULT '',
                user_email    TEXT NOT NULL UNIQUE COLLATE NOCASE,
                referral_code TEXT NOT NULL UNIQUE COLLATE NOCASE,
                paypal_email  TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id  INTEGER NOT NULL REFERENCES referrers(id),
                referred_email TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
                referred_name  TEXT NOT NULL DEFAULT '',
                status       TEXT NOT NULL DEFAULT 'pending',
                created_at   TEXT NOT NULL,
                completed_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rewards (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL REFERENCES referrers(id),
                referral_id INTEGER REFERENCES referrals(id),
                reward_type TEXT NOT NULL DEFAULT 'cash',
                reward_value REAL NOT NULL DEFAULT 0.0,
                issued_at   TEXT NOT NULL
            )
        """)

        # Insert a referrer
        await db.execute(
            "INSERT INTO referrers (id, user_name, user_email, referral_code, created_at) "
            "VALUES (1, 'TestUser', 'test@example.com', 'TEST123', '2026-01-01')"
        )

        # Insert 8 completed referrals
        for i in range(8):
            await db.execute(
                "INSERT INTO referrals (referrer_id, referred_email, status, created_at, completed_at) "
                "VALUES (1, ?, 'completed', '2026-01-01', '2026-01-02')",
                (f"referred{i}@example.com",)
            )

        # Insert 8 reward rows ($10 each = $80 total)
        for i in range(8):
            await db.execute(
                "INSERT INTO rewards (referrer_id, referral_id, reward_type, reward_value, issued_at) "
                "VALUES (1, ?, 'cash', 10.0, '2026-01-02')",
                (i + 1,)
            )

        # Insert a second referrer with fewer referrals (for ordering test)
        await db.execute(
            "INSERT INTO referrers (id, user_name, user_email, referral_code, created_at) "
            "VALUES (2, 'OtherUser', 'other@example.com', 'OTHER456', '2026-01-01')"
        )
        await db.execute(
            "INSERT INTO referrals (referrer_id, referred_email, status, created_at, completed_at) "
            "VALUES (2, 'someone@example.com', 'completed', '2026-01-01', '2026-01-02')"
        )
        await db.execute(
            "INSERT INTO rewards (referrer_id, referral_id, reward_type, reward_value, issued_at) "
            "VALUES (2, 9, 'cash', 10.0, '2026-01-02')"
        )

        await db.commit()


async def _run_leaderboard_query(db_path: str, limit: int = 10):
    """Run the fixed leaderboard query against the test DB."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA foreign_keys = ON")
        cur = await db.execute(
            """SELECT r.user_name, r.referral_code,
                      COALESCE(ref_counts.completed_count, 0) AS completed_count,
                      COALESCE(rw_totals.total_earned, 0) AS total_earned
               FROM referrers r
               LEFT JOIN (
                   SELECT referrer_id, COUNT(*) AS completed_count
                   FROM referrals
                   WHERE status = 'completed'
                   GROUP BY referrer_id
               ) ref_counts ON ref_counts.referrer_id = r.id
               LEFT JOIN (
                   SELECT referrer_id, SUM(reward_value) AS total_earned
                   FROM rewards
                   GROUP BY referrer_id
               ) rw_totals ON rw_totals.referrer_id = r.id
               ORDER BY completed_count DESC, total_earned DESC
               LIMIT ?""",
            (limit,)
        )
        return await cur.fetchall()


async def _run_old_buggy_query(db_path: str, limit: int = 10):
    """Run the OLD buggy query to prove it produces wrong results."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA foreign_keys = ON")
        cur = await db.execute(
            """SELECT r.user_name, r.referral_code,
                      COUNT(ref.id) AS completed_count,
                      COALESCE(SUM(rw.reward_value), 0) AS total_earned
               FROM referrers r
               LEFT JOIN referrals ref ON ref.referrer_id = r.id AND ref.status = 'completed'
               LEFT JOIN rewards rw ON rw.referrer_id = r.id
               GROUP BY r.id
               ORDER BY completed_count DESC, total_earned DESC
               LIMIT ?""",
            (limit,)
        )
        return await cur.fetchall()


class TestReferralLeaderboardCartesianFix(unittest.TestCase):
    """Test that the leaderboard query avoids cartesian product inflation."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        _run(_setup_test_db(self.db_path))

    def tearDown(self):
        os.unlink(self.db_path)

    def test_old_query_has_cartesian_bug(self):
        """Prove the old query inflates counts due to cartesian product."""
        rows = _run(
            _run_old_buggy_query(self.db_path)
        )
        # Old query: 8 referrals * 8 rewards = 64 for completed_count
        top = rows[0]
        self.assertEqual(top[0], "TestUser")
        # The bug: completed_count is 64 (8*8), not 8
        self.assertEqual(top[2], 64, "Old query should show inflated count of 64 (8 referrals * 8 rewards)")

    def test_fixed_query_returns_correct_counts(self):
        """Fixed query should return actual referral count, not N*M."""
        rows = _run(
            _run_leaderboard_query(self.db_path)
        )
        top = rows[0]
        self.assertEqual(top[0], "TestUser")
        # Fixed: completed_count should be 8 (actual referrals)
        self.assertEqual(top[2], 8, "Fixed query should show correct count of 8")
        # Fixed: total_earned should be 80.0 (8 * $10)
        self.assertAlmostEqual(top[3], 80.0, places=2)

    def test_fixed_query_correct_for_second_referrer(self):
        """Second referrer should also have correct counts."""
        rows = _run(
            _run_leaderboard_query(self.db_path)
        )
        second = rows[1]
        self.assertEqual(second[0], "OtherUser")
        self.assertEqual(second[2], 1)
        self.assertAlmostEqual(second[3], 10.0, places=2)

    def test_ordering_by_completed_count(self):
        """Leaderboard should be ordered by completed_count DESC."""
        rows = _run(
            _run_leaderboard_query(self.db_path)
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "TestUser")   # 8 completed
        self.assertEqual(rows[1][0], "OtherUser")   # 1 completed


if __name__ == "__main__":
    unittest.main()
