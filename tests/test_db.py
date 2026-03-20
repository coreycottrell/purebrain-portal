"""
test_db.py -- Database integrity verification for PureBrain portal

Verifies the three SQLite databases have expected tables, required columns,
valid data values, and no structural corruption.

Skips gracefully if database files don't exist (e.g., fresh clone without data).
"""

import unittest
import sqlite3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import AGENTS_DB, REFERRALS_DB, CLIENTS_DB

ALL_DBS = [AGENTS_DB, REFERRALS_DB, CLIENTS_DB]


class TestDatabasesReadable(unittest.TestCase):
    """Verify database files exist and are readable."""

    def test_databases_are_valid_sqlite(self):
        """All existing database files should be openable as SQLite."""
        for db_path in ALL_DBS:
            if not os.path.exists(db_path):
                continue
            with self.subTest(db=os.path.basename(db_path)):
                conn = sqlite3.connect(db_path)
                try:
                    cur = conn.execute("SELECT 1")
                    self.assertEqual(cur.fetchone()[0], 1)
                finally:
                    conn.close()

    def test_databases_have_tables(self):
        """Each existing database should contain at least one user table."""
        for db_path in ALL_DBS:
            if not os.path.exists(db_path):
                continue
            with self.subTest(db=os.path.basename(db_path)):
                conn = sqlite3.connect(db_path)
                try:
                    cur = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'")
                    tables = [r[0] for r in cur.fetchall()]
                    self.assertGreater(len(tables), 0,
                        f"{os.path.basename(db_path)} has no user tables")
                finally:
                    conn.close()


class TestAgentsDB(unittest.TestCase):
    """Verify agents.db schema and data integrity."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(AGENTS_DB):
            raise unittest.SkipTest("agents.db not found")
        cls.conn = sqlite3.connect(AGENTS_DB)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "conn"):
            cls.conn.close()

    def test_agents_table_exists(self):
        cur = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        self.assertIn("agents", tables)

    def test_agents_has_required_columns(self):
        """agents table must have key columns: id, name, status."""
        cur = self.conn.execute("PRAGMA table_info(agents)")
        columns = [r[1] for r in cur.fetchall()]
        for col in ["id", "name", "status"]:
            with self.subTest(column=col):
                self.assertIn(col, columns)

    def test_agents_has_expected_columns(self):
        cur = self.conn.execute("PRAGMA table_info(agents)")
        columns = [r[1] for r in cur.fetchall()]
        for col in ["id", "name", "description", "type", "status",
                     "capabilities", "department", "is_lead", "last_active", "created_at"]:
            with self.subTest(column=col):
                self.assertIn(col, columns)

    def test_no_duplicate_agent_names(self):
        cur = self.conn.execute(
            "SELECT name, COUNT(*) as cnt FROM agents GROUP BY name HAVING cnt > 1")
        duplicates = cur.fetchall()
        self.assertEqual(len(duplicates), 0, f"Duplicate agent names: {duplicates}")

    def test_agent_status_values_valid(self):
        cur = self.conn.execute("SELECT DISTINCT status FROM agents WHERE status IS NOT NULL")
        valid = {"idle", "active", "working", "boop", "error", "offline", "unknown", ""}
        for (status,) in cur.fetchall():
            with self.subTest(status=status):
                self.assertIn(status.lower(), valid, f"Unexpected status: '{status}'")

    def test_no_null_agent_ids(self):
        cur = self.conn.execute("SELECT COUNT(*) FROM agents WHERE id IS NULL")
        self.assertEqual(cur.fetchone()[0], 0)

    def test_no_null_agent_names(self):
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM agents WHERE name IS NULL OR TRIM(name) = ''")
        self.assertEqual(cur.fetchone()[0], 0)


class TestReferralsDB(unittest.TestCase):
    """Verify referrals.db schema."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(REFERRALS_DB):
            raise unittest.SkipTest("referrals.db not found")
        cls.conn = sqlite3.connect(REFERRALS_DB)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "conn"):
            cls.conn.close()

    def test_referrers_table_exists(self):
        cur = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        self.assertIn("referrers", [r[0] for r in cur.fetchall()])

    def test_referrals_table_exists(self):
        cur = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        self.assertIn("referrals", [r[0] for r in cur.fetchall()])

    def test_referrers_has_required_columns(self):
        cur = self.conn.execute("PRAGMA table_info(referrers)")
        columns = [r[1] for r in cur.fetchall()]
        for col in ["id", "user_name", "user_email", "referral_code"]:
            with self.subTest(column=col):
                self.assertIn(col, columns)

    def test_referrals_has_required_columns(self):
        cur = self.conn.execute("PRAGMA table_info(referrals)")
        columns = [r[1] for r in cur.fetchall()]
        for col in ["id", "referrer_id", "referred_email", "status"]:
            with self.subTest(column=col):
                self.assertIn(col, columns)


class TestClientsDB(unittest.TestCase):
    """Verify clients.db schema."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(CLIENTS_DB):
            raise unittest.SkipTest("clients.db not found")
        cls.conn = sqlite3.connect(CLIENTS_DB)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "conn"):
            cls.conn.close()

    def test_clients_table_exists(self):
        cur = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        self.assertIn("clients", [r[0] for r in cur.fetchall()])

    def test_clients_has_required_columns(self):
        cur = self.conn.execute("PRAGMA table_info(clients)")
        columns = [r[1] for r in cur.fetchall()]
        for col in ["id", "name", "email", "tier", "status"]:
            with self.subTest(column=col):
                self.assertIn(col, columns)

    def test_clients_has_payment_columns(self):
        cur = self.conn.execute("PRAGMA table_info(clients)")
        columns = [r[1] for r in cur.fetchall()]
        for col in ["payment_status", "total_paid", "payment_count"]:
            with self.subTest(column=col):
                self.assertIn(col, columns)


if __name__ == "__main__":
    unittest.main(verbosity=2)
