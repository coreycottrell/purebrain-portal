"""
Security tests for the PureBrain Portal referral system.

Tests cover four critical vulnerabilities:
  C1: Bearer token in query-param must NOT authenticate /api/referral/ routes
  C2: /api/referral/complete must require a shared secret when configured
  C3: Payout must be DENIED (not allowed) when DB query fails
  C4: Previously-paid amounts must be deducted from available balance
"""

import asyncio
import hmac
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add portal root to path so we can import portal_server
PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PORTAL_DIR)

import aiosqlite


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeHeaders:
    """Minimal headers-like object supporting .get() and .startswith() on values."""
    def __init__(self, d: dict):
        # Normalize keys to lowercase
        self._data = {k.lower(): v for k, v in d.items()}

    def get(self, key, default=""):
        return self._data.get(key.lower(), default)


class _FakeQueryParams:
    def __init__(self, d: dict):
        self._data = d

    def get(self, key, default=""):
        return self._data.get(key, default)


class _FakeURL:
    def __init__(self, path):
        self.path = path


class _FakeClient:
    def __init__(self, host="127.0.0.1"):
        self.host = host


class _FakeRequest:
    def __init__(self, path, headers=None, query_params=None, json_body=None):
        self.headers = _FakeHeaders(headers or {})
        self.query_params = _FakeQueryParams(query_params or {})
        self.url = _FakeURL(path)
        self._json_body = json_body or {}
        self.client = _FakeClient()

    async def json(self):
        return self._json_body


async def _setup_referral_db(db_path: str):
    """Create referral tables and insert a test referrer with rewards."""
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payout_requests (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id   TEXT NOT NULL UNIQUE,
                referral_code TEXT NOT NULL COLLATE NOCASE,
                paypal_email TEXT NOT NULL DEFAULT '',
                amount       REAL NOT NULL DEFAULT 0.0,
                status       TEXT NOT NULL DEFAULT 'pending',
                batch_id     TEXT NOT NULL DEFAULT '',
                notes        TEXT NOT NULL DEFAULT '',
                created_at   TEXT NOT NULL,
                paid_at      TEXT
            )
        """)

        # Referrer with $500 total earnings (10 completed referrals @ $50 each)
        await db.execute(
            "INSERT INTO referrers (id, user_name, user_email, referral_code, paypal_email, created_at) "
            "VALUES (1, 'SecurityTest', 'sec@test.com', 'SEC500', 'paypal@test.com', '2026-01-01')"
        )
        for i in range(10):
            await db.execute(
                "INSERT INTO referrals (referrer_id, referred_email, status, created_at, completed_at) "
                "VALUES (1, ?, 'completed', '2026-01-01', '2026-01-02')",
                (f"ref{i}@test.com",)
            )
            await db.execute(
                "INSERT INTO rewards (referrer_id, referral_id, reward_type, reward_value, issued_at) "
                "VALUES (1, ?, 'cash', 50.0, '2026-01-02')",
                (i + 1,)
            )

        await db.commit()


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ===========================================================================
# C1: Bearer token in query-param must NOT work for /api/referral/ routes
# ===========================================================================

class TestC1_QueryParamBearerToken(unittest.TestCase):
    """C1: /api/referral/ must NOT accept bearer token via ?token= query param."""

    def test_referral_endpoints_reject_query_param_bearer_token(self):
        """Bearer token in query param should NOT authenticate referral endpoints."""
        import portal_server
        saved_token = portal_server.BEARER_TOKEN

        req = _FakeRequest(
            "/api/referral/dashboard",
            query_params={"token": saved_token},
        )
        result = portal_server.check_auth(req)
        self.assertFalse(result, "check_auth must reject query-param token for /api/referral/ paths")

    def test_referral_endpoints_accept_authorization_header(self):
        """Bearer token via Authorization header should still authenticate referral endpoints."""
        import portal_server
        saved_token = portal_server.BEARER_TOKEN

        req = _FakeRequest(
            "/api/referral/dashboard",
            headers={"authorization": f"Bearer {saved_token}"},
        )
        result = portal_server.check_auth(req)
        self.assertTrue(result, "check_auth must accept Authorization header for /api/referral/ paths")

    def test_ws_still_accepts_query_param_token(self):
        """WebSocket paths should still accept query-param token (browsers can't set WS headers)."""
        import portal_server
        saved_token = portal_server.BEARER_TOKEN

        req = _FakeRequest(
            "/ws/chat",
            query_params={"token": saved_token},
        )
        result = portal_server.check_auth(req)
        self.assertTrue(result, "check_auth must still accept query-param token for /ws paths")

    def test_bearer_comparison_uses_constant_time(self):
        """Bearer token comparison must use hmac.compare_digest (timing-safe)."""
        import portal_server
        import inspect
        source = inspect.getsource(portal_server.check_auth)
        self.assertIn("hmac.compare_digest", source,
                       "check_auth must use hmac.compare_digest for timing-safe comparison")


# ===========================================================================
# C2: /api/referral/complete must require shared secret when configured
# ===========================================================================

class TestC2_CompleteEndpointAuth(unittest.TestCase):
    """C2: /api/referral/complete must check X-Referral-Secret when secret is configured."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        _run(_setup_referral_db(self.db_path))

    def tearDown(self):
        os.unlink(self.db_path)

    def test_complete_requires_valid_secret_when_configured(self):
        """When REFERRAL_COMPLETE_SECRET is set, requests without it should be rejected."""
        import portal_server

        secret = "test-secret-abc123"

        req = _FakeRequest(
            "/api/referral/complete",
            headers={"x-referral-secret": "wrong-secret"},
            json_body={
                "referral_code": "SEC500",
                "referred_email": "newuser@test.com",
                "order_id": "ORDER-123"
            },
        )

        with patch.dict(os.environ, {"REFERRAL_COMPLETE_SECRET": secret}):
            with patch.object(portal_server, 'REFERRALS_DB', Path(self.db_path)):
                resp = _run(portal_server.api_referral_complete(req))
                self.assertEqual(resp.status_code, 403,
                                 "Complete with wrong secret should return 403")

    def test_complete_accepts_valid_secret(self):
        """When REFERRAL_COMPLETE_SECRET is set, correct secret should be accepted."""
        import portal_server

        secret = "test-secret-abc123"

        req = _FakeRequest(
            "/api/referral/complete",
            headers={"x-referral-secret": secret},
            json_body={
                "referral_code": "SEC500",
                "referred_email": "newclient@test.com",
                "referred_name": "New Client",
                "order_id": "ORDER-456"
            },
        )

        with patch.dict(os.environ, {"REFERRAL_COMPLETE_SECRET": secret}):
            with patch.object(portal_server, 'REFERRALS_DB', Path(self.db_path)):
                resp = _run(portal_server.api_referral_complete(req))
                # Should NOT be 403 (secret matched)
                self.assertNotEqual(resp.status_code, 403,
                                    "Complete with correct secret should not return 403")

    def test_complete_works_without_secret_configured(self):
        """When REFERRAL_COMPLETE_SECRET is empty/unset, endpoint works as before (backward compat)."""
        import portal_server

        req = _FakeRequest(
            "/api/referral/complete",
            json_body={
                "referral_code": "SEC500",
                "referred_email": "compat@test.com",
                "referred_name": "Compat Test",
                "order_id": "ORDER-789"
            },
        )

        with patch.dict(os.environ, {"REFERRAL_COMPLETE_SECRET": ""}, clear=False):
            with patch.object(portal_server, 'REFERRALS_DB', Path(self.db_path)):
                resp = _run(portal_server.api_referral_complete(req))
                # Should NOT be 403 (no secret configured = open)
                self.assertNotEqual(resp.status_code, 403,
                                    "With no secret configured, endpoint should remain open")


# ===========================================================================
# C3: Payout must be DENIED when DB query fails
# ===========================================================================

class TestC3_PayoutDeniedOnDBError(unittest.TestCase):
    """C3: If DB query fails during balance check, payout should be DENIED, not allowed through."""

    def test_payout_denied_on_db_error(self):
        """If DB query fails during balance check, payout should be DENIED, not allowed through."""
        import portal_server

        req = _FakeRequest(
            "/api/referral/payout",
            headers={"authorization": f"Bearer {portal_server.BEARER_TOKEN}"},
            json_body={
                "paypal_email": "attacker@evil.com",
                "referral_code": "ANYCODE",
                "amount": 999.0,
            },
        )

        # Mock _read_payout_requests_db to return empty (no cooldown conflict)
        # Mock _referral_db to raise an exception (simulating DB error)
        with patch.object(portal_server, '_read_payout_requests_db', new_callable=AsyncMock, return_value=[]):
            # Create a context manager mock that raises on execute
            mock_db = AsyncMock()
            mock_db.__aenter__ = AsyncMock(side_effect=Exception("DB connection failed"))

            with patch.object(portal_server, '_referral_db', return_value=mock_db):
                resp = _run(portal_server.api_referral_payout_request(req))
                # Must NOT succeed -- should return 503 (service unavailable)
                self.assertEqual(resp.status_code, 503,
                                 "Payout must be denied (503) when DB query fails, not allowed through")
                body = json.loads(resp.body.decode())
                self.assertIn("error", body)


# ===========================================================================
# C4: Previously-paid amounts must be deducted from available balance
# ===========================================================================

class TestC4_PreviouslyPaidDeduction(unittest.TestCase):
    """C4: Available balance = total_earnings - completed payouts."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        _run(_setup_referral_db(self.db_path))

    def tearDown(self):
        os.unlink(self.db_path)

    def test_payout_deducts_previously_paid_amounts(self):
        """Available balance should be total_earnings minus sum of completed payouts."""
        import portal_server

        # Insert a completed payout of $500 into the DB
        async def _insert_payout():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO payout_requests (request_id, referral_code, paypal_email, amount, status, created_at, paid_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("payout-SEC500-old", "SEC500", "paypal@test.com", 500.0, "completed",
                     "2026-01-15T00:00:00+00:00", "2026-01-15T00:00:00+00:00")
                )
                await db.commit()
        _run(_insert_payout())

        req = _FakeRequest(
            "/api/referral/payout",
            headers={"authorization": f"Bearer {portal_server.BEARER_TOKEN}"},
            json_body={
                "paypal_email": "paypal@test.com",
                "referral_code": "SEC500",
                "amount": 500.0,   # Full $500, but $500 already paid out
            },
        )

        # Mock _read_payout_requests_db to return the old payout (for cooldown check - 60 days ago, past cooldown)
        existing_payouts = [
            {
                "request_id": "payout-SEC500-old",
                "referral_code": "SEC500",
                "paypal_email": "paypal@test.com",
                "amount": 500.0,
                "status": "completed",
                "created_at": "2026-01-15T00:00:00+00:00",
                "created_at_ts": time.time() - 86400 * 60,  # 60 days ago (past cooldown)
                "paid_at": "2026-01-15T00:00:00+00:00",
            }
        ]

        with patch.object(portal_server, '_read_payout_requests_db', new_callable=AsyncMock, return_value=existing_payouts):
            with patch.object(portal_server, 'REFERRALS_DB', Path(self.db_path)):
                resp = _run(portal_server.api_referral_payout_request(req))
                # $500 earned - $500 already paid = $0 available. Request for $500 must fail.
                self.assertEqual(resp.status_code, 400,
                                 "Payout for $500 should fail when $500 already paid out of $500 earned")
                body = json.loads(resp.body.decode())
                self.assertIn("exceeds", body.get("error", "").lower(),
                              "Error message should indicate amount exceeds available balance")

    def test_payout_allows_remaining_balance(self):
        """Payout should succeed when requesting less than (earnings - paid)."""
        import portal_server

        # Insert a completed payout of $250 into the DB
        async def _insert_payout():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO payout_requests (request_id, referral_code, paypal_email, amount, status, created_at, paid_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("payout-SEC500-old", "SEC500", "paypal@test.com", 250.0, "completed",
                     "2026-01-15T00:00:00+00:00", "2026-01-15T00:00:00+00:00")
                )
                await db.commit()
        _run(_insert_payout())

        req = _FakeRequest(
            "/api/referral/payout",
            headers={"authorization": f"Bearer {portal_server.BEARER_TOKEN}"},
            json_body={
                "paypal_email": "paypal@test.com",
                "referral_code": "SEC500",
                "amount": 200.0,   # $200 of $500 earned, $250 already paid => $250 available
            },
        )

        # Mock _read_payout_requests_db for cooldown check (60 days ago, past cooldown)
        existing_payouts = [
            {
                "request_id": "payout-SEC500-old",
                "referral_code": "SEC500",
                "paypal_email": "paypal@test.com",
                "amount": 250.0,
                "status": "completed",
                "created_at": "2026-01-15T00:00:00+00:00",
                "created_at_ts": time.time() - 86400 * 60,  # 60 days ago
                "paid_at": "2026-01-15T00:00:00+00:00",
            }
        ]

        with patch.object(portal_server, '_read_payout_requests_db', new_callable=AsyncMock, return_value=existing_payouts):
            with patch.object(portal_server, 'REFERRALS_DB', Path(self.db_path)):
                with patch.object(portal_server, '_write_payout_request_db', new_callable=AsyncMock):
                    with patch.object(portal_server, '_execute_paypal_payout',
                                      new_callable=AsyncMock, return_value={"ok": True, "batch_id": "test"}):
                        with patch.object(portal_server, '_update_payout_status_db', new_callable=AsyncMock):
                            with patch.object(portal_server, '_send_telegram_notification'):
                                resp = _run(portal_server.api_referral_payout_request(req))
                                # $500 earned - $250 paid = $250 available. $200 request should pass balance check.
                                # (it may still fail for other reasons, but NOT for "exceeds balance")
                                if resp.status_code == 400:
                                    body = json.loads(resp.body.decode())
                                    self.assertNotIn("exceeds", body.get("error", "").lower(),
                                                     "$200 request with $250 available should not fail balance check")


if __name__ == "__main__":
    unittest.main()
