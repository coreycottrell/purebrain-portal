"""
IDOR security test for /api/referral/paypal-email (portal_referrals.api_referral_paypal_email).

VULNERABILITY (IDOR / Broken Access Control):
  With a valid affiliate session token, the handler verifies the session (which is
  tied to a specific referral_code) but then runs:
      UPDATE referrers SET paypal_email = ? WHERE user_email = ?
  using the CLIENT-SUPPLIED body `email` instead of the identity bound to the
  session token. So affiliate A (valid session) can overwrite affiliate B's
  paypal_email by simply putting B's email in the request body — redirecting
  B's future payouts to an attacker-controlled PayPal account.

VULNERABILITY (Passwordless password-fallback bypass / Broken Authentication):
  When the caller is NOT portal-authed and has NO valid affiliate session, the
  handler falls back to a password check:
      if stored_hash and not _verify_affiliate_password(password, stored_hash): 401
  If the target referrer has NO password set (password_hash is NULL/empty), the
  `stored_hash` operand is falsy, the whole condition short-circuits to False, the
  401 is SKIPPED, and execution falls through to the email-targeted UPDATE — with
  NO ownership proof at all. An unauthenticated attacker who merely knows a
  passwordless referrer's email can hijack that referrer's PayPal payout address
  by POSTing {email, paypal_email} (and any/empty password), with NO session
  token and NO portal bearer.

TDD RED phase:
  - test_affiliate_cannot_set_other_affiliates_paypal_email  -> MUST FAIL on current code
  - test_affiliate_can_set_own_paypal_email                  -> MUST PASS now (no regression after fix)
  - test_password_path_still_works                           -> legitimate password self-update
  - test_portal_bearer_admin_path_still_works                -> admin portal-bearer path
  - test_passwordless_referrer_cannot_be_hijacked_unauthed   -> MUST FAIL on current code (passwordless bypass)
  - test_passwordless_referrer_with_session_can_set_own      -> positive control (passwordless owner via session)
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Add portal root to path so we can import portal_referrals / portal_server
PORTAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PORTAL_DIR)

import aiosqlite
import portal_referrals


# ---------------------------------------------------------------------------
# Helpers (mirrors the harness used in tests/test_referral_security.py)
# ---------------------------------------------------------------------------

class _FakeHeaders:
    """Minimal headers-like object supporting .get()."""
    def __init__(self, d: dict):
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


async def _setup_two_referrers(db_path: str, a_password_hash: str = ""):
    """Create referral tables and seed two distinct referrers A and B.

    A: affiliate@a.com / code AAA111 / paypal a-original@paypal.com
    B: affiliate@b.com / code BBB222 / paypal b-original@paypal.com (NO password)
    """
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
        await db.execute(
            "INSERT INTO referrers (id, user_name, user_email, referral_code, paypal_email, password_hash, created_at) "
            "VALUES (1, 'AffiliateA', 'affiliate@a.com', 'AAA111', 'a-original@paypal.com', ?, '2026-01-01')",
            (a_password_hash,)
        )
        await db.execute(
            "INSERT INTO referrers (id, user_name, user_email, referral_code, paypal_email, password_hash, created_at) "
            "VALUES (2, 'AffiliateB', 'affiliate@b.com', 'BBB222', 'b-original@paypal.com', '', '2026-01-01')"
        )
        await db.commit()


async def _setup_passwordless_referrer(db_path: str, password_hash: str = ""):
    """Create referral tables and seed a SINGLE referrer C with NO password set.

    C: victim@c.com / code CCC333 / paypal c-original@paypal.com / password_hash = ''
    (When password_hash is left as the default empty string, this models a
    referrer who never set an affiliate password.)
    """
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
        await db.execute(
            "INSERT INTO referrers (id, user_name, user_email, referral_code, paypal_email, password_hash, created_at) "
            "VALUES (3, 'VictimC', 'victim@c.com', 'CCC333', 'c-original@paypal.com', ?, '2026-01-01')",
            (password_hash,)
        )
        await db.commit()


async def _get_paypal_email(db_path: str, user_email: str) -> str:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT paypal_email FROM referrers WHERE user_email = ? COLLATE NOCASE", (user_email,)
        )
        row = await cur.fetchone()
        return row[0] if row else None


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ===========================================================================
# IDOR: affiliate A must NOT be able to change affiliate B's paypal_email
# ===========================================================================

class TestPaypalEmailIDOR(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        # Snapshot the in-memory session store so tests don't leak into each other.
        self._saved_sessions = dict(portal_referrals._AFFILIATE_SESSIONS)
        portal_referrals._AFFILIATE_SESSIONS.clear()

    def tearDown(self):
        os.unlink(self.db_path)
        portal_referrals._AFFILIATE_SESSIONS.clear()
        portal_referrals._AFFILIATE_SESSIONS.update(self._saved_sessions)

    def test_affiliate_cannot_set_other_affiliates_paypal_email(self):
        """RED: A's valid session token must NOT let A overwrite B's paypal_email.

        Current (vulnerable) code overwrites B's row because the UPDATE targets the
        client-supplied body `email` rather than the session-bound identity.
        """
        _run(_setup_two_referrers(self.db_path))

        # A holds a legitimate session token (tied to A's code AAA111).
        a_token = "session-token-for-A"
        portal_referrals._AFFILIATE_SESSIONS[a_token] = {
            "code": "AAA111",
            "expires": __import__("time").time() + 3600,
        }

        # A attacks: uses own session token, but body email = B's email.
        req = _FakeRequest(
            "/api/referral/paypal-email",
            json_body={
                "email": "affiliate@b.com",          # victim B
                "paypal_email": "attacker@evil.com",  # attacker-controlled
                "session_token": a_token,             # A's valid session
            },
        )

        with patch.object(portal_referrals, "REFERRALS_DB", Path(self.db_path)):
            resp = _run(portal_referrals.api_referral_paypal_email(req))

        b_after = _run(_get_paypal_email(self.db_path, "affiliate@b.com"))

        # The core security assertion: B's paypal_email MUST be unchanged.
        self.assertEqual(
            b_after,
            "b-original@paypal.com",
            "IDOR: affiliate A overwrote affiliate B's paypal_email using A's session token "
            f"(B is now '{b_after}', expected 'b-original@paypal.com'). "
            "The UPDATE must be bound to the session identity, not the client-supplied email.",
        )
        # And the request should be rejected (forbidden / unauthorized).
        self.assertIn(
            resp.status_code, (401, 403),
            f"Cross-account paypal_email change should be rejected (401/403), got {resp.status_code}",
        )

    def test_affiliate_can_set_own_paypal_email(self):
        """GREEN-now: A's session token updating A's OWN email must succeed (no regression)."""
        _run(_setup_two_referrers(self.db_path))

        a_token = "session-token-for-A"
        portal_referrals._AFFILIATE_SESSIONS[a_token] = {
            "code": "AAA111",
            "expires": __import__("time").time() + 3600,
        }

        req = _FakeRequest(
            "/api/referral/paypal-email",
            json_body={
                "email": "affiliate@a.com",        # A's OWN email
                "paypal_email": "a-new@paypal.com",
                "session_token": a_token,
            },
        )

        with patch.object(portal_referrals, "REFERRALS_DB", Path(self.db_path)):
            resp = _run(portal_referrals.api_referral_paypal_email(req))

        a_after = _run(_get_paypal_email(self.db_path, "affiliate@a.com"))
        self.assertEqual(resp.status_code, 200,
                         f"Self-update should succeed (200), got {resp.status_code}")
        self.assertEqual(a_after, "a-new@paypal.com",
                         "A's own paypal_email should be updated to the new value")
        # And B must remain untouched by A's self-update.
        b_after = _run(_get_paypal_email(self.db_path, "affiliate@b.com"))
        self.assertEqual(b_after, "b-original@paypal.com",
                         "B's paypal_email must remain unchanged by A's self-update")

    def test_password_path_still_works(self):
        """Legitimate password-auth self-update must keep working (no session token)."""
        pw = "correct-horse-battery-staple"
        pw_hash = portal_referrals._hash_affiliate_password(pw)
        _run(_setup_two_referrers(self.db_path, a_password_hash=pw_hash))

        req = _FakeRequest(
            "/api/referral/paypal-email",
            json_body={
                "email": "affiliate@a.com",          # A's own email
                "paypal_email": "a-via-password@paypal.com",
                "password": pw,                       # correct password for A
            },
        )

        with patch.object(portal_referrals, "REFERRALS_DB", Path(self.db_path)):
            resp = _run(portal_referrals.api_referral_paypal_email(req))

        a_after = _run(_get_paypal_email(self.db_path, "affiliate@a.com"))
        self.assertEqual(resp.status_code, 200,
                         f"Password self-update should succeed (200), got {resp.status_code}")
        self.assertEqual(a_after, "a-via-password@paypal.com",
                         "A's paypal_email should be updated via correct password")

    def test_portal_bearer_admin_path_still_works(self):
        """Admin path: portal bearer token can set paypal_email for any referrer."""
        import portal_server
        _run(_setup_two_referrers(self.db_path))

        req = _FakeRequest(
            "/api/referral/paypal-email",
            headers={"authorization": f"Bearer {portal_server.BEARER_TOKEN}"},
            json_body={
                "email": "affiliate@b.com",          # admin updating B
                "paypal_email": "b-via-admin@paypal.com",
            },
        )

        with patch.object(portal_referrals, "REFERRALS_DB", Path(self.db_path)):
            resp = _run(portal_referrals.api_referral_paypal_email(req))

        b_after = _run(_get_paypal_email(self.db_path, "affiliate@b.com"))
        self.assertEqual(resp.status_code, 200,
                         f"Admin (portal bearer) update should succeed (200), got {resp.status_code}")
        self.assertEqual(b_after, "b-via-admin@paypal.com",
                         "Admin with portal bearer token should be able to set any referrer's paypal_email")


# ===========================================================================
# Passwordless bypass: an unauthenticated attacker must NOT be able to hijack
# a referrer who has NO password set, simply by knowing their email.
# ===========================================================================

class TestPaypalEmailPasswordlessBypass(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        # Snapshot the in-memory session store so tests don't leak into each other.
        self._saved_sessions = dict(portal_referrals._AFFILIATE_SESSIONS)
        portal_referrals._AFFILIATE_SESSIONS.clear()

    def tearDown(self):
        os.unlink(self.db_path)
        portal_referrals._AFFILIATE_SESSIONS.clear()
        portal_referrals._AFFILIATE_SESSIONS.update(self._saved_sessions)

    def test_passwordless_referrer_cannot_be_hijacked_unauthed(self):
        """RED: passwordless referrer C must NOT be hijackable by an unauthed attacker.

        C has password_hash = '' (no password set). The attacker:
          - sends NO portal bearer token
          - sends NO (and no valid) affiliate session token
          - supplies C's email + an attacker PayPal address + any/empty password

        The vulnerable code's check `if stored_hash and not _verify_...` short-circuits
        on the empty stored_hash, skips the 401, and falls through to the
        email-targeted UPDATE — silently setting C's payout to the attacker.

        This MUST be rejected (401/403) and C's paypal_email MUST be unchanged.
        """
        # password_hash defaults to '' => C is passwordless.
        _run(_setup_passwordless_referrer(self.db_path, password_hash=""))

        # Unauthenticated attacker: no bearer header, no session token, no real password.
        req = _FakeRequest(
            "/api/referral/paypal-email",
            json_body={
                "email": "victim@c.com",                 # passwordless victim
                "paypal_email": "attacker@evil.com",     # attacker-controlled payout
                "password": "",                          # attacker has no password
                # NOTE: deliberately NO session_token, NO authorization header.
            },
        )

        with patch.object(portal_referrals, "REFERRALS_DB", Path(self.db_path)):
            resp = _run(portal_referrals.api_referral_paypal_email(req))

        c_after = _run(_get_paypal_email(self.db_path, "victim@c.com"))

        # Core security assertion: passwordless victim's payout MUST NOT change.
        self.assertEqual(
            c_after,
            "c-original@paypal.com",
            "PASSWORDLESS BYPASS: an unauthenticated attacker hijacked passwordless "
            f"referrer C's paypal_email (C is now '{c_after}', expected 'c-original@paypal.com'). "
            "A NULL/empty password_hash must NOT be treated as 'auth satisfied' — "
            "the password fallback must require a session OR an actual set password.",
        )
        # And the request must be rejected outright.
        self.assertIn(
            resp.status_code, (401, 403),
            f"Unauthenticated update of a passwordless referrer must be rejected "
            f"(401/403), got {resp.status_code}",
        )

    def test_passwordless_referrer_with_session_can_set_own(self):
        """Positive control: a passwordless referrer CAN set their own payout via a
        valid affiliate session token (the legit path that replaces the unsafe
        passwordless fallback). MUST keep working after the fix."""
        _run(_setup_passwordless_referrer(self.db_path, password_hash=""))

        # C authenticates via a valid affiliate session bound to C's code CCC333.
        c_token = "session-token-for-C"
        portal_referrals._AFFILIATE_SESSIONS[c_token] = {
            "code": "CCC333",
            "expires": __import__("time").time() + 3600,
        }

        req = _FakeRequest(
            "/api/referral/paypal-email",
            json_body={
                "email": "victim@c.com",            # C's OWN email
                "paypal_email": "c-new@paypal.com",
                "session_token": c_token,           # C's valid session
            },
        )

        with patch.object(portal_referrals, "REFERRALS_DB", Path(self.db_path)):
            resp = _run(portal_referrals.api_referral_paypal_email(req))

        c_after = _run(_get_paypal_email(self.db_path, "victim@c.com"))
        self.assertEqual(resp.status_code, 200,
                         f"Passwordless owner self-update via session should succeed (200), "
                         f"got {resp.status_code}")
        self.assertEqual(c_after, "c-new@paypal.com",
                         "Passwordless referrer should be able to set their own paypal_email "
                         "via a valid affiliate session token")


if __name__ == "__main__":
    unittest.main()
