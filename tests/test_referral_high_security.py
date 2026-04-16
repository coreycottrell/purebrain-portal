"""
HIGH severity security tests for the PureBrain Portal referral system.

Tests cover:
  H1 - /api/referral/login must have rate limiting
  H2 - First-login password claim must send notification and log
  H3 - Bearer token and password hash must use constant-time comparison
  H4 - /api/referral/code/{email} must require authentication
  BONUS - Dead _check_admin_auth function must be removed

TDD: These tests are written FIRST (should FAIL), then fixes applied.
"""

import asyncio
import hmac
import inspect
import os
import re
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add portal root to path so we can import portal_server
PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PORTAL_DIR)

import aiosqlite


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _setup_referral_db(db_path: str):
    """Create referral tables and seed a test referrer."""
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
        # Referrer WITH a password (normal account)
        await db.execute(
            "INSERT INTO referrers (id, user_name, user_email, referral_code, password_hash, created_at) "
            "VALUES (1, 'Alice', 'alice@example.com', 'ALICE01', "
            "'$2b$12$fakehashfakehashfakehashfakehashfakehashfakehashfakeh', '2026-01-01')"
        )
        # Referrer WITHOUT a password (legacy account -- first-login claim scenario)
        await db.execute(
            "INSERT INTO referrers (id, user_name, user_email, referral_code, password_hash, created_at) "
            "VALUES (2, 'Legacy', 'legacy@example.com', 'LEGACY1', '', '2026-01-01')"
        )
        await db.commit()


def _run(coro):
    """Run async coroutine in sync test."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# H1: /api/referral/login must have rate limiting
# ---------------------------------------------------------------------------

class TestH1LoginRateLimiting(unittest.TestCase):
    """The /login endpoint should call _affiliate_login_rate_check and return 429 when rate-limited."""

    def test_login_endpoint_has_rate_limiting(self):
        """The /api/referral/login handler source must call _affiliate_login_rate_check."""
        import portal_server
        source = inspect.getsource(portal_server.api_referral_login)
        self.assertIn(
            "_affiliate_login_rate_check",
            source,
            "/api/referral/login does NOT call _affiliate_login_rate_check — brute-force is possible",
        )

    def test_login_rate_limit_returns_429(self):
        """When rate check fails, /login should return 429 status code."""
        import portal_server
        source = inspect.getsource(portal_server.api_referral_login)
        # Should mention 429 status code
        self.assertIn(
            "429",
            source,
            "/api/referral/login does not return 429 when rate-limited",
        )


# ---------------------------------------------------------------------------
# H3: Bearer token and legacy hash must use hmac.compare_digest
# ---------------------------------------------------------------------------

class TestH3ConstantTimeComparison(unittest.TestCase):
    """Token and password-hash comparisons must use hmac.compare_digest to prevent timing attacks."""

    def test_check_auth_uses_constant_time_comparison(self):
        """check_auth should use hmac.compare_digest, not == for token comparison."""
        import portal_server
        source = inspect.getsource(portal_server.check_auth)
        # Must not have bare == for token comparison
        # The pattern 'auth[7:] == BEARER_TOKEN' is the vulnerable line
        self.assertNotIn(
            "== BEARER_TOKEN",
            source,
            "check_auth uses == for token comparison (timing attack vulnerable)",
        )
        self.assertIn(
            "hmac.compare_digest",
            source,
            "check_auth should use hmac.compare_digest for constant-time comparison",
        )

    def test_legacy_sha256_verify_uses_constant_time(self):
        """_verify_affiliate_password (legacy SHA-256 path) must use hmac.compare_digest, not ==."""
        import portal_server
        func_name = "_verify_affiliate_password"
        self.assertTrue(
            hasattr(portal_server, func_name),
            f"Expected function {func_name} in portal_server",
        )
        source = inspect.getsource(getattr(portal_server, func_name))
        self.assertNotIn(
            "h == expected_hex",
            source,
            f"{func_name} uses == for hash comparison (timing attack vulnerable)",
        )
        self.assertIn(
            "hmac.compare_digest",
            source,
            f"{func_name} should use hmac.compare_digest for legacy SHA-256 comparison",
        )


# ---------------------------------------------------------------------------
# H4: /api/referral/code/{email} must require authentication
# ---------------------------------------------------------------------------

class TestH4CodeLookupAuth(unittest.TestCase):
    """The /code/{email} endpoint must require bearer token or affiliate session."""

    def test_code_lookup_requires_authentication(self):
        """The /code/{email} handler source must check auth before returning data."""
        import portal_server
        source = inspect.getsource(portal_server.api_referral_code_lookup)
        has_auth_check = (
            "check_auth" in source
            or "_verify_affiliate_session" in source
        )
        self.assertTrue(
            has_auth_check,
            "/api/referral/code/{email} does NOT check authentication — email enumeration possible",
        )

    def test_code_lookup_returns_401_on_no_auth(self):
        """Handler source should contain 401 status for unauthenticated requests."""
        import portal_server
        source = inspect.getsource(portal_server.api_referral_code_lookup)
        self.assertIn(
            "401",
            source,
            "/api/referral/code/{email} does not return 401 for unauthenticated requests",
        )


# ---------------------------------------------------------------------------
# H2: First-login password claim must log and notify
# ---------------------------------------------------------------------------

class TestH2FirstLoginClaimAudit(unittest.TestCase):
    """When a legacy account's password is first set via /login, it must be logged and notified."""

    def test_first_login_claim_sends_notification(self):
        """When a password is first set on a legacy account via /login, _send_telegram_notification should be called."""
        import portal_server
        source = inspect.getsource(portal_server.api_referral_login)
        self.assertIn(
            "_send_telegram_notification",
            source,
            "/api/referral/login does NOT send notification on first password claim — account takeover risk",
        )

    def test_first_login_claim_is_logged(self):
        """First password claim should be logged with IP address for audit."""
        import portal_server
        source = inspect.getsource(portal_server.api_referral_login)
        # Should contain a security log line mentioning IP
        has_security_log = (
            "[SECURITY]" in source or "[referral][SECURITY]" in source
        )
        self.assertTrue(
            has_security_log,
            "/api/referral/login does NOT log first password claim with IP — no audit trail",
        )


# ---------------------------------------------------------------------------
# BONUS: Dead _check_admin_auth function should be removed
# ---------------------------------------------------------------------------

class TestBonusDeadCodeRemoved(unittest.TestCase):
    """The _check_admin_auth function returns True for any non-empty token. It must be deleted."""

    def test_check_admin_auth_is_removed(self):
        """_check_admin_auth is a dangerous dead function and should not exist."""
        import portal_server
        self.assertFalse(
            hasattr(portal_server, "_check_admin_auth"),
            "_check_admin_auth still exists — returns True for ANY non-empty token (dangerous dead code)",
        )


class TestDashboardPasswordRemoved(unittest.TestCase):
    """Password-in-URL query param on dashboard endpoint must be removed (leaks to logs/history)."""

    def test_dashboard_no_password_query_param(self):
        """The dashboard endpoint source should not read a password query param."""
        import portal_server
        import inspect
        source = inspect.getsource(portal_server.api_referral_dashboard)
        # The old pattern was: password_param = request.query_params.get("password", "")
        self.assertNotIn(
            'query_params.get("password"',
            source,
            "Dashboard still reads ?password= query param — passwords in URLs leak to logs/history",
        )

    def test_dashboard_docstring_no_password_reference(self):
        """Dashboard docstring should not mention password auth."""
        import portal_server
        doc = portal_server.api_referral_dashboard.__doc__ or ""
        self.assertNotIn(
            "?password=",
            doc,
            "Dashboard docstring still references ?password= — remove to avoid confusion",
        )


if __name__ == "__main__":
    unittest.main()
