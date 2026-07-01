"""Referral & Client Admin system for the PureBrain Portal.

Extracted from portal_server.py for modularity.
Contains: referral DB, affiliate auth, PayPal payouts, admin endpoints,
client management, and the affiliate portal.
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiosqlite
import httpx

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from portal_config import (
    SCRIPT_DIR, BEARER_TOKEN,
    REFERRALS_DB, CLIENTS_DB,
    REFERRAL_CODE_PREFIX, REFERRAL_CODE_CHARS, REFERRAL_CODE_LENGTH,
    REFERRAL_COMMISSION_RATE,
    PAYOUT_REQUESTS_FILE, PAYOUT_MIN_AMOUNT, PAYOUT_AUTO_APPROVE_LIMIT,
    PAYOUT_COOLDOWN_DAYS,
    PAYPAL_SANDBOX, PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET,
    WEB_CONVERSATIONS_LOG, PAYMENTS_LOG, PAY_TEST_LOG,
    check_auth,
)

from tracking import ensure_tracking_columns

def _find_tg_send() -> "Path | None":
    """Search standard locations for tg_send.sh. Returns Path or None."""
    civ_root = Path(os.environ.get("CIV_ROOT", str(Path.home())))
    candidates = [
        civ_root / "tools" / "tg_send.sh",
        Path.home() / "civ" / "tools" / "tg_send.sh",
        Path.home() / "tools" / "tg_send.sh",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None

# ─── Affiliate login rate-limiting (in-memory, resets on restart) ───────────
_AFFILIATE_LOGIN_ATTEMPTS: dict = {}
_LOGIN_MAX_ATTEMPTS = 10          # per window
_LOGIN_WINDOW_SECS  = 900         # 15 minutes

# ─── Affiliate session tokens ──────────────────────────────────────────────
_AFFILIATE_SESSIONS: dict = {}
_SESSION_TTL_SECS = 86400 * 7     # 7 days

# ─── Referral track rate-limiting (prevents click-spam abuse) ──────────────
_TRACK_RATE_LIMITS: dict = {}
_TRACK_MAX_PER_WINDOW = 30
_TRACK_WINDOW_SECS = 300

# ---------------------------------------------------------------------------
# Referral System — SQLite-backed (replaces dead WP proxy endpoints)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _referral_db():
    """Open referral DB with WAL mode and foreign keys enabled."""
    async with aiosqlite.connect(str(REFERRALS_DB)) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA foreign_keys = ON")
        yield db


async def _init_referral_db() -> None:
    """Create referral tables on startup if they don't exist."""
    async with aiosqlite.connect(str(REFERRALS_DB)) as db:
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
        # Migration: add password_hash column to existing DBs without it
        try:
            await db.execute("ALTER TABLE referrers ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass  # column already exists
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
            CREATE TABLE IF NOT EXISTS referral_clicks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                referral_code TEXT NOT NULL COLLATE NOCASE,
                ip_hash      TEXT NOT NULL DEFAULT '',
                clicked_at   TEXT NOT NULL
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
        # commission_payments tracks recurring 5% commissions from referred member payments
        await db.execute("""
            CREATE TABLE IF NOT EXISTS commission_payments (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id     INTEGER NOT NULL REFERENCES referrers(id),
                referral_id     INTEGER NOT NULL REFERENCES referrals(id),
                payer_email     TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
                order_id        TEXT NOT NULL DEFAULT '',
                payment_amount  REAL NOT NULL DEFAULT 0.0,
                commission_rate REAL NOT NULL DEFAULT 0.05,
                commission_value REAL NOT NULL DEFAULT 0.0,
                tier            TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL
            )
        """)
        # admin_tokens table for read-only admin viewers
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_tokens (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                token      TEXT NOT NULL UNIQUE,
                email      TEXT NOT NULL DEFAULT '',
                name       TEXT NOT NULL DEFAULT '',
                role       TEXT NOT NULL DEFAULT 'viewer',
                created_at TEXT NOT NULL
            )
        """)
        # ── payout_requests (replaces JSONL file) ──
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
        await db.execute("CREATE INDEX IF NOT EXISTS idx_payouts_code ON payout_requests(referral_code)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_payouts_status ON payout_requests(status)")

        # ── financial_audit_log (immutable ledger) ──
        await db.execute("""
            CREATE TABLE IF NOT EXISTS financial_audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type  TEXT NOT NULL,
                actor       TEXT NOT NULL DEFAULT '',
                referral_code TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
                amount      REAL NOT NULL DEFAULT 0.0,
                details     TEXT NOT NULL DEFAULT '',
                ip_address  TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL
            )
        """)

        # ── Performance indexes for common query patterns ──
        await db.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer_status ON referrals(referrer_id, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referred_email ON referrals(referred_email)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rewards_referrer ON rewards(referrer_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_clicks_code ON referral_clicks(referral_code)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_commissions_referrer ON commission_payments(referrer_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_commissions_order ON commission_payments(order_id)")

        await db.commit()

        # ── One-time migration: import existing JSONL payout requests into SQLite ──
        if PAYOUT_REQUESTS_FILE.exists():
            try:
                with PAYOUT_REQUESTS_FILE.open("r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            await db.execute(
                                """INSERT OR IGNORE INTO payout_requests
                                   (request_id, referral_code, paypal_email, amount, status, batch_id, notes, created_at, paid_at)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (entry.get("request_id", ""), entry.get("referral_code", ""),
                                 entry.get("paypal_email", ""), entry.get("amount", 0.0),
                                 entry.get("status", "pending"), entry.get("batch_id", ""),
                                 entry.get("notes", ""), entry.get("created_at", ""),
                                 entry.get("paid_at"))
                            )
                        except Exception:
                            continue
                await db.commit()
                # Rename JSONL file to .migrated to prevent re-import
                PAYOUT_REQUESTS_FILE.rename(PAYOUT_REQUESTS_FILE.with_suffix(".jsonl.migrated"))
                print("[referral] Migrated payout requests from JSONL to SQLite")
            except Exception as e:
                print(f"[referral] JSONL migration error (non-fatal): {e}")

    print(f"[referral] SQLite DB ready: {REFERRALS_DB}")


async def _log_financial_event(
    event_type: str,
    referral_code: str = "",
    amount: float = 0.0,
    actor: str = "",
    details: str = "",
    ip_address: str = "",
) -> None:
    """Write an immutable audit log entry for financial operations."""
    try:
        async with _referral_db() as db:
            await db.execute(
                """INSERT INTO financial_audit_log
                   (event_type, actor, referral_code, amount, details, ip_address, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (event_type, actor, referral_code, amount, details, ip_address,
                 datetime.now(timezone.utc).isoformat())
            )
            await db.commit()
    except Exception as e:
        print(f"[referral][AUDIT] Failed to log event: {e}")


def _generate_referral_code() -> str:
    """Generate a unique PB-XXXX referral code."""
    chars = REFERRAL_CODE_CHARS
    suffix = "".join(secrets.choice(chars) for _ in range(REFERRAL_CODE_LENGTH))
    return f"{REFERRAL_CODE_PREFIX}{suffix}"


async def _generate_unique_code(db: aiosqlite.Connection) -> str:
    """Keep generating until we find a code not already in DB."""
    for _ in range(50):
        code = _generate_referral_code()
        cur = await db.execute(
            "SELECT id FROM referrers WHERE referral_code = ? COLLATE NOCASE", (code,)
        )
        if await cur.fetchone() is None:
            return code
    raise RuntimeError("Could not generate unique referral code after 50 attempts")


def _referral_link(code: str, base_url: str = "https://purebrain.ai") -> str:
    return f"{base_url}/?ref={code}"


def _affiliate_login_rate_check(ip: str) -> bool:
    """Returns True if this IP is allowed to attempt login (not rate-limited)."""
    now = time.time()
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16]
    entry = _AFFILIATE_LOGIN_ATTEMPTS.get(ip_hash)
    if entry is None:
        _AFFILIATE_LOGIN_ATTEMPTS[ip_hash] = {"count": 1, "window_start": now}
        return True
    if now - entry["window_start"] > _LOGIN_WINDOW_SECS:
        # Window expired — reset
        _AFFILIATE_LOGIN_ATTEMPTS[ip_hash] = {"count": 1, "window_start": now}
        return True
    if entry["count"] >= _LOGIN_MAX_ATTEMPTS:
        return False
    entry["count"] += 1
    return True


def _create_affiliate_session(referral_code: str) -> str:
    """Create and store a session token for an affiliate. Returns the token."""
    token = secrets.token_urlsafe(32)
    _AFFILIATE_SESSIONS[token] = {
        "code": referral_code.upper(),
        "expires": time.time() + _SESSION_TTL_SECS,
    }
    return token


def _verify_affiliate_session(token: str) -> str | None:
    """Verify a session token. Returns the referral_code on success, None otherwise."""
    if not token:
        return None
    entry = _AFFILIATE_SESSIONS.get(token)
    if entry is None:
        return None
    if time.time() > entry["expires"]:
        del _AFFILIATE_SESSIONS[token]
        return None
    return entry["code"]


async def _paypal_get_access_token() -> str | None:
    """Fetch a short-lived PayPal OAuth2 access token."""
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        return None
    base = "https://api-m.sandbox.paypal.com" if PAYPAL_SANDBOX else "https://api-m.paypal.com"
    url  = f"{base}/v1/oauth2/token"
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    credentials = f"{PAYPAL_CLIENT_ID}:{PAYPAL_CLIENT_SECRET}"
    b64 = __import__("base64").b64encode(credentials.encode()).decode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Basic {b64}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            return body.get("access_token")
    except Exception as e:
        print(f"[paypal] Failed to get access token: {e}")
        return None


async def _execute_paypal_payout(paypal_email: str, amount: float, request_id: str, note: str = "") -> dict:
    """Execute a PayPal payout via the Payouts API.

    Returns dict with keys: ok (bool), batch_id (str|None), error (str|None).
    """
    access_token = await _paypal_get_access_token()
    if not access_token:
        return {"ok": False, "batch_id": None, "error": "Could not obtain PayPal access token. Check credentials."}

    base = "https://api-m.sandbox.paypal.com" if PAYPAL_SANDBOX else "https://api-m.paypal.com"
    url  = f"{base}/v1/payments/payouts"

    sender_batch_id = f"pb-payout-{request_id}-{int(time.time())}"
    payload = {
        "sender_batch_header": {
            "sender_batch_id": sender_batch_id,
            "email_subject":   "PureBrain Affiliate Payout",
            "email_message":   note or "Your PureBrain affiliate commission payout has been sent.",
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "amount":         {"value": f"{amount:.2f}", "currency": "USD"},
                "receiver":       paypal_email,
                "note":           note or "PureBrain affiliate commission",
                "sender_item_id": request_id,
            }
        ],
    }
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization":  f"Bearer {access_token}",
            "Content-Type":   "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())
            batch_id = body.get("batch_header", {}).get("payout_batch_id", sender_batch_id)
            return {"ok": True, "batch_id": batch_id, "error": None}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        print(f"[paypal] Payout HTTP error {e.code}: {err_body}")
        return {"ok": False, "batch_id": None, "error": f"PayPal error {e.code}: {err_body[:200]}"}
    except Exception as e:
        print(f"[paypal] Payout exception: {e}")
        return {"ok": False, "batch_id": None, "error": str(e)}


def _hash_affiliate_password(password: str, salt: str = "") -> str:
    """Bcrypt hash of password. Returns bcrypt hash string.
    The `salt` param is ignored (kept for call-site compatibility with old code).
    """
    import bcrypt as _bcrypt
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt(rounds=12)).decode("utf-8")


def _verify_affiliate_password(password: str, stored_hash: str) -> bool:
    """Verify password against stored hash.
    Supports both bcrypt (new, starts with $2b$) and legacy SHA-256 (salt:hash).
    On successful legacy verify, the caller should migrate to bcrypt.
    """
    import bcrypt as _bcrypt
    if not stored_hash:
        return False
    if stored_hash.startswith("$2b$") or stored_hash.startswith("$2a$"):
        # Bcrypt hash
        try:
            return _bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
        except Exception:
            return False
    # Legacy SHA-256 format: salt:hexdigest
    if ":" not in stored_hash:
        return False
    parts = stored_hash.split(":", 1)
    if len(parts) != 2:
        return False
    salt_val, expected_hex = parts
    h = hashlib.sha256(f"{salt_val}:{password}".encode()).hexdigest()
    return hmac.compare_digest(h, expected_hex)


# ── Password reset tokens (in-memory, expire after 1 hour) ──────────────────
_password_reset_tokens: dict = {}  # token -> {"email": str, "expires": float}
_PASSWORD_RESET_EXPIRY = 3600  # 1 hour


def _send_reset_email(to_email: str, reset_url: str) -> bool:
    """Send a password reset email via Gmail SMTP."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    smtp_user = os.environ.get("SMTP_USER", "") or os.environ.get("GMAIL_USERNAME", "")
    smtp_pass = os.environ.get("SMTP_PASS", "") or os.environ.get("GOOGLE_APP_PASSWORD", "")
    if not smtp_user or not smtp_pass:
        print("[portal] WARNING: SMTP_USER/SMTP_PASS (and GMAIL_USERNAME/GOOGLE_APP_PASSWORD) not set — cannot send reset email")
        return False

    msg = MIMEMultipart("alternative")
    msg["From"] = f"PureBrain <{smtp_user}>"
    msg["To"] = to_email
    msg["Subject"] = "Reset Your PureBrain Affiliate Password"

    text = f"Reset your PureBrain affiliate password:\n\n{reset_url}\n\nThis link expires in 1 hour. If you didn't request this, ignore this email."

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="background:#080a12;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:32px;">
<div style="max-width:500px;margin:0 auto;background:#0d1120;border:1px solid #1e2a40;border-radius:12px;padding:40px;">
  <div style="text-align:center;margin-bottom:24px;">
    <span style="font-size:22px;font-weight:700;">
      <span style="color:#2a93c1;">PUREBR</span><span style="color:#f1420b;">AI</span><span style="color:#2a93c1;">N</span>
    </span>
  </div>
  <h2 style="color:#fff;font-size:20px;margin:0 0 16px;">Reset Your Password</h2>
  <p style="color:#9ca3af;font-size:14px;line-height:1.6;margin:0 0 24px;">
    Click the button below to reset your affiliate dashboard password. This link expires in 1 hour.
  </p>
  <div style="text-align:center;margin:32px 0;">
    <a href="{reset_url}" style="display:inline-block;background:linear-gradient(135deg,#2a93c1,#1d6e99);color:#fff;font-size:15px;font-weight:700;text-decoration:none;padding:14px 36px;border-radius:8px;box-shadow:0 4px 16px rgba(42,147,193,0.4);">
      Reset Password
    </a>
  </div>
  <p style="color:#6b7280;font-size:12px;text-align:center;">If you didn't request this, you can safely ignore this email.</p>
</div>
</body></html>"""

    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, to_email, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print(f"[reset-email] SMTP error: {e}")
        return False


async def api_referral_forgot_password(request: Request) -> JSONResponse:
    """POST /api/referral/forgot-password — send a password reset email."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    email = str(body.get("email", "")).strip().lower()
    if not email or "@" not in email:
        return JSONResponse({"error": "valid email required"}, status_code=400)

    # Always return success to prevent email enumeration
    success_msg = {"ok": True, "message": "If that email is registered, a reset link has been sent."}

    async with _referral_db() as db:
        cur = await db.execute(
            "SELECT referral_code FROM referrers WHERE user_email = ? COLLATE NOCASE",
            (email,)
        )
        row = await cur.fetchone()

    if not row:
        return JSONResponse(success_msg)

    # Generate reset token
    token = secrets.token_urlsafe(32)
    _password_reset_tokens[token] = {
        "email": email,
        "expires": time.time() + _PASSWORD_RESET_EXPIRY,
    }

    # Clean up expired tokens
    now = time.time()
    expired = [t for t, v in _password_reset_tokens.items() if v["expires"] < now]
    for t in expired:
        del _password_reset_tokens[t]

    reset_url = f"https://purebrain.ai/refer/?reset={token}"
    sent = _send_reset_email(email, reset_url)
    if not sent:
        print(f"[reset] Failed to send reset email to {email}")

    return JSONResponse(success_msg)


async def api_referral_reset_password(request: Request) -> JSONResponse:
    """POST /api/referral/reset-password — set new password using reset token."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    token = str(body.get("token", "")).strip()
    new_password = str(body.get("password", "")).strip()

    if not token:
        return JSONResponse({"error": "reset token required"}, status_code=400)
    if not new_password or len(new_password) < 6:
        return JSONResponse({"error": "password must be at least 6 characters"}, status_code=400)

    token_data = _password_reset_tokens.get(token)
    if not token_data:
        return JSONResponse({"error": "invalid or expired reset link. Please request a new one."}, status_code=400)

    if time.time() > token_data["expires"]:
        del _password_reset_tokens[token]
        return JSONResponse({"error": "reset link has expired. Please request a new one."}, status_code=400)

    email = token_data["email"]
    pw_hash = _hash_affiliate_password(new_password)

    async with _referral_db() as db:
        await db.execute(
            "UPDATE referrers SET password_hash = ? WHERE user_email = ? COLLATE NOCASE",
            (pw_hash, email)
        )
        await db.commit()

    # Consume the token
    del _password_reset_tokens[token]

    return JSONResponse({"ok": True, "message": "Password updated successfully. You can now log in."})



async def api_referral_register(request: Request) -> JSONResponse:
    """POST /api/referral/register — register as referrer, get unique code back."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    name     = str(body.get("name", "")).strip()
    email    = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", "")).strip()
    paypal_email = str(body.get("paypal_email", "")).strip()

    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return JSONResponse({"error": "invalid email"}, status_code=400)

    # Auto-generate password if not provided (public form doesn't include a password field)
    is_portal_auth = check_auth(request)
    if not password or len(password) < 6:
        password = secrets.token_urlsafe(16)

    pw_hash = _hash_affiliate_password(password)

    async with _referral_db() as db:
        # Check if already registered
        cur = await db.execute(
            "SELECT id, referral_code FROM referrers WHERE user_email = ? COLLATE NOCASE",
            (email,)
        )
        row = await cur.fetchone()
        if row:
            code = row[1]
            return JSONResponse({
                "ok": True,
                "referral_code": code,
                "referral_link": _referral_link(code),
                "existing": True,
                "message": "You are already registered. Here is your existing referral link.",
            })

        code = await _generate_unique_code(db)
        now  = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO referrers (user_name, user_email, referral_code, password_hash, paypal_email, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, email, code, pw_hash, paypal_email, now)
        )
        await db.commit()

    return JSONResponse({
        "ok": True,
        "referral_code": code,
        "referral_link": _referral_link(code),
        "existing": False,
        "message": "Registration successful!",
    })


async def api_referral_login(request: Request) -> JSONResponse:
    """POST /api/referral/login — verify affiliate password, return referral code."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    code     = str(body.get("referral_code", "")).strip().upper()
    email    = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", "")).strip()

    if not password:
        return JSONResponse({"error": "password required"}, status_code=400)
    if not code and not email:
        return JSONResponse({"error": "referral_code or email required"}, status_code=400)

    # Rate-limit by IP (H1 fix — matches /session endpoint)
    client_ip = request.client.host if request.client else "unknown"
    if not _affiliate_login_rate_check(client_ip):
        return JSONResponse({"error": "too many login attempts — try again later"}, status_code=429)

    async with _referral_db() as db:
        db.row_factory = aiosqlite.Row
        if code:
            cur = await db.execute(
                "SELECT referral_code, password_hash FROM referrers WHERE referral_code = ? COLLATE NOCASE",
                (code,)
            )
        else:
            cur = await db.execute(
                "SELECT referral_code, password_hash FROM referrers WHERE user_email = ? COLLATE NOCASE",
                (email,)
            )
        row = await cur.fetchone()

    if row is None:
        return JSONResponse({"error": "referrer not found"}, status_code=404)

    stored_hash = row["password_hash"]
    if not stored_hash:
        # Account created before password system — allow any password and set it now
        pw_hash = _hash_affiliate_password(password)
        async with _referral_db() as db:
            await db.execute(
                "UPDATE referrers SET password_hash = ? WHERE referral_code = ? COLLATE NOCASE",
                (pw_hash, row["referral_code"])
            )
            await db.commit()
        # Log the first-time password claim for security audit (H2 fix)
        print(f"[referral][SECURITY] First password claim for {row['referral_code']} from IP {client_ip}")
        try:
            _send_telegram_notification(
                f"FIRST PASSWORD SET\n"
                f"Code: {row['referral_code']}\n"
                f"IP: {client_ip}\n"
                f"Via: /api/referral/login"
            )
        except Exception:
            pass  # notification is best-effort
    elif not _verify_affiliate_password(password, stored_hash):
        return JSONResponse({"error": "incorrect password"}, status_code=401)
    elif not (stored_hash.startswith("$2b$") or stored_hash.startswith("$2a$")):
        # Auto-migrate legacy SHA-256 hash to bcrypt on successful login
        migrated_hash = _hash_affiliate_password(password)
        async with _referral_db() as db:
            await db.execute(
                "UPDATE referrers SET password_hash = ? WHERE referral_code = ? COLLATE NOCASE",
                (migrated_hash, row["referral_code"])
            )
            await db.commit()

    return JSONResponse({"ok": True, "referral_code": row["referral_code"]})


async def api_referral_session(request: Request) -> JSONResponse:
    """POST /api/referral/session — login and receive a session token for dashboard access.

    Body: { email, password } or { referral_code, password }
    Returns: { ok, session_token, referral_code, expires_in }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    code     = str(body.get("referral_code", "")).strip().upper()
    email    = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", "")).strip()

    if not password:
        return JSONResponse({"error": "password required"}, status_code=400)
    if not code and not email:
        return JSONResponse({"error": "referral_code or email required"}, status_code=400)

    # Rate-limit by IP
    client_ip = request.client.host if request.client else ""
    if not _affiliate_login_rate_check(client_ip):
        return JSONResponse({"error": "too many login attempts. Please wait 15 minutes."}, status_code=429)

    async with _referral_db() as db:
        db.row_factory = aiosqlite.Row
        if code:
            cur = await db.execute(
                "SELECT referral_code, password_hash FROM referrers WHERE referral_code = ? COLLATE NOCASE",
                (code,)
            )
        else:
            cur = await db.execute(
                "SELECT referral_code, password_hash FROM referrers WHERE user_email = ? COLLATE NOCASE",
                (email,)
            )
        row = await cur.fetchone()

    if row is None:
        return JSONResponse({"error": "account not found"}, status_code=404)

    stored_hash = row["password_hash"]
    if not stored_hash:
        # First login — set the password
        pw_hash = _hash_affiliate_password(password)
        async with _referral_db() as db:
            await db.execute(
                "UPDATE referrers SET password_hash = ? WHERE referral_code = ? COLLATE NOCASE",
                (pw_hash, row["referral_code"])
            )
            await db.commit()
        # FIX 3: Log and notify on first-login password claim
        _code_for_log = row["referral_code"]
        print(f"[SECURITY] First-login password claim for affiliate code {_code_for_log} from IP {client_ip}")
        _tg_send = _find_tg_send()
        if _tg_send and _tg_send.exists():
            try:
                subprocess.Popen(
                    [str(_tg_send), f"[SECURITY] First-login claim: affiliate {_code_for_log} from IP {client_ip}. Verify this is legitimate."],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except Exception as _e:
                print(f"[SECURITY] TG notify failed: {_e}")
    elif not _verify_affiliate_password(password, stored_hash):
        return JSONResponse({"error": "incorrect password"}, status_code=401)
    elif not (stored_hash.startswith("$2b$") or stored_hash.startswith("$2a$")):
        # Auto-migrate legacy SHA-256 hash to bcrypt on successful login
        migrated_hash = _hash_affiliate_password(password)
        async with _referral_db() as db:
            await db.execute(
                "UPDATE referrers SET password_hash = ? WHERE referral_code = ? COLLATE NOCASE",
                (migrated_hash, row["referral_code"])
            )
            await db.commit()

    referral_code = row["referral_code"]
    session_token = _create_affiliate_session(referral_code)

    return JSONResponse({
        "ok":           True,
        "session_token": session_token,
        "referral_code": referral_code,
        "expires_in":    _SESSION_TTL_SECS,
    })


async def api_referral_dashboard(request: Request) -> JSONResponse:
    """GET /api/referral/dashboard?code=PB-XXXX — referrer stats. Requires session token or portal bearer token."""
    code     = request.query_params.get("code", "").strip().upper()
    email    = request.query_params.get("email", "").strip().lower()
    # Portal owner (admin) can see any dashboard without affiliate password
    portal_authed = check_auth(request)

    if not code and not email:
        return JSONResponse({"error": "missing code or email"}, status_code=400)

    async with _referral_db() as db:
        db.row_factory = aiosqlite.Row
        if code:
            cur = await db.execute(
                "SELECT * FROM referrers WHERE referral_code = ? COLLATE NOCASE", (code,)
            )
        else:
            cur = await db.execute(
                "SELECT * FROM referrers WHERE user_email = ? COLLATE NOCASE", (email,)
            )
        referrer = await cur.fetchone()
        if referrer is None:
            return JSONResponse({"error": "referrer not found"}, status_code=404)

        # Security: dashboard requires either:
        #   1. Portal bearer token (admin)
        #   2. A valid affiliate session token (?session=TOKEN, or X-Affiliate-Session header)
        #   3. Direct password param (?password=...) as fallback for API callers
        if not portal_authed:
            session_token = (
                request.query_params.get("session", "").strip()
                or request.headers.get("x-affiliate-session", "").strip()
            )
            session_code = _verify_affiliate_session(session_token)
            if session_code:
                # Session token valid — verify it belongs to this referrer
                if session_code.upper() != referrer["referral_code"].upper():
                    return JSONResponse({"error": "session token does not match this account"}, status_code=403)
            else:
                # Password-in-URL removed (security: passwords in query params leak to logs/history).
                # Use POST /api/referral/session to get a session token, then pass ?session=TOKEN.
                return JSONResponse({"error": "authentication required — use a session token (?session=TOKEN) or login at purebrain.ai/refer/"}, status_code=401)

        referrer_id   = referrer["id"]
        referral_code = referrer["referral_code"]

        # Referral counts (exclude rejected paypal placeholder ghosts)
        cur = await db.execute(
            """SELECT COUNT(*) FROM referrals WHERE referrer_id = ?
               AND NOT (status = 'rejected' AND referred_email LIKE 'paypal_%@pending')""",
            (referrer_id,)
        )
        total_referrals = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND status = 'completed'",
            (referrer_id,)
        )
        completed = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND status = 'pending'",
            (referrer_id,)
        )
        pending = (await cur.fetchone())[0]

        # Total earnings from rewards table
        cur = await db.execute(
            "SELECT COALESCE(SUM(reward_value), 0) FROM rewards WHERE referrer_id = ?",
            (referrer_id,)
        )
        earnings = float((await cur.fetchone())[0])

        # Click count
        cur = await db.execute(
            "SELECT COUNT(*) FROM referral_clicks WHERE referral_code = ? COLLATE NOCASE",
            (referral_code,)
        )
        total_clicks = (await cur.fetchone())[0]

        # Referral history
        # Referral history with total commission earned per referred member
        # Exclude rejected placeholder entries (paypal_*@pending) — they are
        # unresolved webhook artifacts, not real referrals.
        cur = await db.execute(
            """SELECT r.referred_name, r.referred_email, r.status, r.created_at,
                      COALESCE(SUM(cp.commission_value), 0) AS earnings,
                      COUNT(cp.id) AS payment_count
               FROM referrals r
               LEFT JOIN commission_payments cp ON cp.referral_id = r.id
               WHERE r.referrer_id = ?
                 AND NOT (r.status = 'rejected' AND r.referred_email LIKE 'paypal_%@pending')
               GROUP BY r.id
               ORDER BY r.created_at DESC""",
            (referrer_id,)
        )
        history = [dict(row) async for row in cur]

    reward_tiers = [
        {"label": "Commission Rate", "reward": f"{REFERRAL_COMMISSION_RATE * 100:.0f}% of every payment"},
        {"label": "Frequency", "reward": "Every month, for as long as they are a member"},
        {"label": "Awakened ($149/mo)", "reward": "$7.45/month per referral"},
        {"label": "Partnered ($499/mo)", "reward": "$24.95/month per referral"},
        {"label": "Unified ($999/mo)", "reward": "$49.95/month per referral"},
        {"label": "Enterprise (Custom)", "reward": "5% of custom monthly rate"},
    ]

    return JSONResponse({
        "referral_code": referral_code,
        "referral_link": _referral_link(referral_code),
        "email": referrer["user_email"],
        "name": referrer["user_name"],
        "paypal_email": referrer["paypal_email"],
        "total_referrals": total_referrals,
        "completed": completed,
        "pending": pending,
        "earnings": round(earnings, 2),
        "total_clicks": total_clicks,
        "history": history,
        "reward_tiers": reward_tiers,
        "commission_rate": REFERRAL_COMMISSION_RATE,
        "commission_rate_pct": f"{REFERRAL_COMMISSION_RATE * 100:.0f}%",
        "model": "recurring",
    })


async def api_referral_track(request: Request) -> JSONResponse:
    """POST /api/referral/track — log a referral link click."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    code = str(body.get("referral_code", "")).strip().upper()
    if not code:
        return JSONResponse({"error": "missing referral_code"}, status_code=400)

    # Hash IP for privacy
    client_ip = request.client.host if request.client else ""
    ip_hash = hashlib.sha256(client_ip.encode()).hexdigest()[:16]

    # HIGH-007: Rate limit click tracking to prevent spam/inflation
    now_ts = time.time()
    entry = _TRACK_RATE_LIMITS.get(ip_hash)
    if entry is None:
        _TRACK_RATE_LIMITS[ip_hash] = {"count": 1, "window_start": now_ts}
    elif now_ts - entry["window_start"] > _TRACK_WINDOW_SECS:
        _TRACK_RATE_LIMITS[ip_hash] = {"count": 1, "window_start": now_ts}
    elif entry["count"] >= _TRACK_MAX_PER_WINDOW:
        return JSONResponse({"error": "rate limited"}, status_code=429)
    else:
        entry["count"] += 1

    now = datetime.now(timezone.utc).isoformat()

    async with _referral_db() as db:
        # Verify code exists
        cur = await db.execute(
            "SELECT id FROM referrers WHERE referral_code = ? COLLATE NOCASE", (code,)
        )
        if await cur.fetchone() is None:
            return JSONResponse({"error": "invalid referral code"}, status_code=404)

        await db.execute(
            "INSERT INTO referral_clicks (referral_code, ip_hash, clicked_at) VALUES (?, ?, ?)",
            (code, ip_hash, now)
        )
        await db.commit()

    return JSONResponse({"ok": True})


async def api_referral_complete(request: Request) -> JSONResponse:
    """POST /api/referral/complete — mark a referral as completed and issue reward.

    NOTE: This endpoint is intentionally PUBLIC (no auth required).
    It is called from browser JS on the landing pages immediately after PayPal payment.
    The browser has no bearer token to send. The referral_code itself acts as the
    credential — only existing referrer codes proceed past the lookup step.
    Single-referrer enforcement: any previous completed referral for this email under
    a DIFFERENT referrer is deleted before recording the new one.
    """
    # If a completion secret is configured, require it (C2 security fix)
    complete_secret = os.environ.get("REFERRAL_COMPLETE_SECRET", "")
    if complete_secret:
        provided = request.headers.get("x-referral-secret", "")
        if not hmac.compare_digest(provided, complete_secret):
            return JSONResponse({"error": "unauthorized"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    referral_code  = str(body.get("referral_code", "")).strip().upper()
    referred_email = str(body.get("referred_email", "")).strip().lower()
    referred_name  = str(body.get("referred_name", "")).strip()
    order_id       = str(body.get("order_id", "")).strip()  # PayPal subscription/order ID

    if not referral_code:
        return JSONResponse({"error": "missing referral_code"}, status_code=400)

    # referred_email is optional for subscription payments where PayPal doesn't
    # provide payer email in the onApprove callback. We accept an empty email and
    # use a placeholder derived from order_id so the referral is still recorded.
    # The admin can manually update the email later via PUT /api/admin/referral/update.
    if not referred_email or "@" not in referred_email:
        if order_id:
            # Try to resolve real client info from clients.db by PayPal subscription ID
            resolved = False
            try:
                async with aiosqlite.connect(str(CLIENTS_DB)) as cdb:
                    cdb.row_factory = aiosqlite.Row
                    ccur = await cdb.execute(
                        "SELECT name, email FROM clients WHERE paypal_subscription_id = ? COLLATE NOCASE LIMIT 1",
                        (order_id,)
                    )
                    client_row = await ccur.fetchone()
                    if client_row and client_row["email"]:
                        referred_email = client_row["email"].strip().lower()
                        if not referred_name:
                            referred_name = client_row["name"].strip()
                        resolved = True
                        print(f"[referral] Resolved PayPal order {order_id} -> {referred_name} <{referred_email}>")
            except Exception as e:
                print(f"[referral] Client lookup failed for order {order_id}: {e}")

            if not resolved:
                # Fallback: record with a placeholder email so the row is traceable
                referred_email = f"paypal_{order_id.lower()}@pending"
        else:
            return JSONResponse({"error": "invalid referred_email"}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()

    async with _referral_db() as db:
        cur = await db.execute(
            "SELECT id FROM referrers WHERE referral_code = ? COLLATE NOCASE", (referral_code,)
        )
        row = await cur.fetchone()
        if row is None:
            return JSONResponse({"error": "invalid referral code"}, status_code=404)
        referrer_id = row[0]

        # Single-referrer enforcement: remove any existing completed referral for
        # this email under a DIFFERENT referrer so a client is never double-counted.
        # Skip single-referrer check for placeholder emails (they are unique per order).
        if "@pending" not in referred_email:
            await db.execute(
                """DELETE FROM referrals
                   WHERE referred_email = ? COLLATE NOCASE
                     AND referrer_id != ?""",
                (referred_email, referrer_id)
            )

        # Prevent double-completion for same referred email under this referrer.
        # For placeholder emails (subscription path), always insert a new row since
        # each order_id is unique and the real email is unknown at this point.
        existing = None
        if "@pending" not in referred_email:
            cur = await db.execute(
                """SELECT id, status FROM referrals
                   WHERE referrer_id = ? AND referred_email = ? COLLATE NOCASE""",
                (referrer_id, referred_email)
            )
            existing = await cur.fetchone()

        if existing:
            if existing[1] == "completed":
                await db.commit()
                return JSONResponse({"ok": True, "message": "already completed"})
            # Update existing pending row
            referral_id = existing[0]
            await db.execute(
                "UPDATE referrals SET status='completed', completed_at=?, referred_name=? WHERE id=?",
                (now, referred_name or "", referral_id)
            )
        else:
            cur = await db.execute(
                """INSERT INTO referrals (referrer_id, referred_email, referred_name, status, created_at, completed_at)
                   VALUES (?, ?, ?, 'completed', ?, ?)""",
                (referrer_id, referred_email, referred_name, now, now)
            )
            referral_id = cur.lastrowid

        await db.commit()

    print(f"[referral] complete: {referral_code} → {referred_email}")
    # Referral relationship recorded. Commission (5% recurring) will be issued
    # automatically each time this referred member makes a payment.
    return JSONResponse({"ok": True, "message": "Referral recorded. You will earn 5% of every payment this member makes."})


async def api_referral_record_commission(request: Request) -> JSONResponse:
    """POST /api/referral/commission — record a 5% recurring commission payment.

    Called by purebrain_log_server when a payment is verified.
    Payload: { payer_email, order_id, amount, tier }
    Requires bearer token authentication.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    payer_email = str(body.get("payer_email", "")).strip().lower()
    order_id    = str(body.get("order_id", "")).strip()
    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0.0
    tier = str(body.get("tier", "")).strip()

    if not payer_email or "@" not in payer_email:
        return JSONResponse({"error": "missing valid payer_email"}, status_code=400)
    if not order_id:
        return JSONResponse({"error": "missing order_id"}, status_code=400)
    if amount <= 0:
        return JSONResponse({"ok": True, "skipped": "zero amount, no commission"})

    now = datetime.now(timezone.utc).isoformat()
    commission_value = round(amount * REFERRAL_COMMISSION_RATE, 2)

    async with _referral_db() as db:
        # Find a completed referral where this payer was referred
        cur = await db.execute(
            """SELECT ref.id, ref.referrer_id
               FROM referrals ref
               WHERE ref.referred_email = ? COLLATE NOCASE AND ref.status = 'completed'
               LIMIT 1""",
            (payer_email,)
        )
        row = await cur.fetchone()
        if row is None:
            # This payer was not referred — no commission
            return JSONResponse({"ok": True, "skipped": "payer not in referrals"})

        referral_id  = row[0]
        referrer_id  = row[1]

        # Prevent duplicate commission for same order_id
        cur = await db.execute(
            "SELECT id FROM commission_payments WHERE order_id = ?", (order_id,)
        )
        if await cur.fetchone():
            return JSONResponse({"ok": True, "skipped": "duplicate order_id"})

        # Record commission payment
        await db.execute(
            """INSERT INTO commission_payments
               (referrer_id, referral_id, payer_email, order_id, payment_amount,
                commission_rate, commission_value, tier, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (referrer_id, referral_id, payer_email, order_id, amount,
             REFERRAL_COMMISSION_RATE, commission_value, tier, now)
        )
        # Also insert into rewards table so balance queries stay consistent
        await db.execute(
            """INSERT INTO rewards (referrer_id, referral_id, reward_type, reward_value, issued_at)
               VALUES (?, ?, 'commission', ?, ?)""",
            (referrer_id, referral_id, commission_value, now)
        )
        await db.commit()

        # Fetch referrer info for notification
        cur = await db.execute(
            "SELECT user_name, user_email FROM referrers WHERE id = ?", (referrer_id,)
        )
        referrer_row = await cur.fetchone()
        referrer_name  = referrer_row[0] if referrer_row else "Unknown"
        referrer_email = referrer_row[1] if referrer_row else ""

    print(f"[referral] Commission recorded: ${commission_value:.2f} for referrer {referrer_email} "
          f"(order {order_id}, payer {payer_email}, amount ${amount:.2f})")

    return JSONResponse({
        "ok": True,
        "commission_value": commission_value,
        "referrer_email": referrer_email,
        "referrer_name": referrer_name,
        "payer_email": payer_email,
        "order_id": order_id,
        "payment_amount": amount,
        "tier": tier,
    })


async def api_referral_code_lookup(request: Request) -> JSONResponse:
    """GET /api/referral/code/{email} — get referral code for a registered email."""
    # Require portal bearer token or affiliate session (H4 fix — prevent email enumeration)
    if not check_auth(request):
        session_token = request.query_params.get("session", "") or request.headers.get("x-affiliate-session", "")
        if not session_token or not _verify_affiliate_session(session_token):
            return JSONResponse({"error": "authentication required"}, status_code=401)

    email = request.path_params.get("email", "").strip().lower()
    if not email:
        return JSONResponse({"error": "missing email"}, status_code=400)

    async with _referral_db() as db:
        cur = await db.execute(
            "SELECT referral_code FROM referrers WHERE user_email = ? COLLATE NOCASE", (email,)
        )
        row = await cur.fetchone()

    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    code = row[0]
    return JSONResponse({
        "referral_code": code,
        "referral_link": _referral_link(code),
    })


async def api_referral_paypal_email(request: Request) -> JSONResponse:
    """POST /api/referral/paypal-email — save PayPal email for a referrer. Requires affiliate password."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    email        = str(body.get("email", "")).strip().lower()
    paypal_email = str(body.get("paypal_email", "")).strip().lower()
    password     = str(body.get("password", "")).strip()

    if not email or "@" not in email:
        return JSONResponse({"error": "invalid email"}, status_code=400)
    if not paypal_email or "@" not in paypal_email:
        return JSONResponse({"error": "invalid paypal_email"}, status_code=400)

    # Auth: portal bearer, affiliate session token, or password.
    # SECURITY (IDOR fix): the write key MUST depend on which auth path succeeded.
    #   - portal bearer admin  -> may target ANY referrer by client `email` (+ auto-create)
    #   - affiliate session     -> scoped to the session's OWN referral_code; client
    #                              `email` can NEVER select another affiliate's row
    #   - password fallback     -> targets `email` (ownership proven via password hash)
    portal_authed = check_auth(request)
    session_code = None  # set when authenticated via a valid affiliate session token

    async with _referral_db() as db:
        if not portal_authed:
            session_token = (
                str(body.get("session_token", "")).strip()
                or request.headers.get("x-affiliate-session", "").strip()
            )
            session_code = _verify_affiliate_session(session_token)
            if not session_code:
                # Fallback to password
                cur_pw = await db.execute(
                    "SELECT password_hash FROM referrers WHERE user_email = ? COLLATE NOCASE", (email,)
                )
                row_pw = await cur_pw.fetchone()
                if row_pw is None:
                    return JSONResponse({"error": "referrer not found"}, status_code=404)
                stored_hash = row_pw[0]
                # SECURITY (passwordless IDOR fix): a NULL/empty password_hash means
                # the password path is UNAVAILABLE for this account — NOT that auth is
                # satisfied. Fail closed: without portal auth or a valid session, a
                # passwordless referrer can only be reached via the session path.
                if not stored_hash:
                    return JSONResponse(
                        {"error": "password not set for this account — session or portal auth required"},
                        status_code=401,
                    )
                if not _verify_affiliate_password(password, stored_hash):
                    return JSONResponse({"error": "incorrect password or session required"}, status_code=401)

        if session_code:
            # Affiliate session: bind the write to the session identity, NOT the
            # client-supplied `email`. This prevents affiliate A from overwriting
            # affiliate B's paypal_email by submitting B's email in the body.
            #
            # If the client supplied an `email` that resolves to a DIFFERENT
            # referrer than the session owns, reject it outright (mirrors the
            # sibling dashboard guard: session_code must match the target account).
            cur_target = await db.execute(
                "SELECT referral_code FROM referrers WHERE user_email = ? COLLATE NOCASE",
                (email,)
            )
            row_target = await cur_target.fetchone()
            if row_target is not None and row_target[0].upper() != session_code.upper():
                return JSONResponse(
                    {"error": "session token does not match this account"},
                    status_code=403,
                )
            # Update strictly by the session's own referral_code so the client
            # `email` can never select another affiliate's row.
            cur = await db.execute(
                "UPDATE referrers SET paypal_email = ? WHERE referral_code = ? COLLATE NOCASE",
                (paypal_email, session_code)
            )
            await db.commit()
            if cur.rowcount == 0:
                # A valid session implies a real referrer row; if it's gone, do NOT
                # fall back to creating/overwriting by client email.
                return JSONResponse({"error": "referrer not found"}, status_code=404)
        else:
            # Portal bearer admin OR password-proven owner: target the row by email.
            cur = await db.execute(
                "UPDATE referrers SET paypal_email = ? WHERE user_email = ? COLLATE NOCASE",
                (paypal_email, email)
            )
            await db.commit()
            if cur.rowcount == 0:
                # If the caller is authenticated via portal bearer token but has no
                # referrer row yet (auto-registration hasn't run or raced), create
                # one now so the PayPal save succeeds on first attempt.
                if portal_authed:
                    code = await _generate_unique_code(db)
                    now  = datetime.now(timezone.utc).isoformat()
                    name = email.split("@")[0]
                    pw_hash = _hash_affiliate_password(secrets.token_urlsafe(16))
                    await db.execute(
                        "INSERT INTO referrers (user_name, user_email, referral_code, password_hash, paypal_email, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (name, email, code, pw_hash, paypal_email, now)
                    )
                    await db.commit()
                else:
                    return JSONResponse({"error": "referrer not found"}, status_code=404)

    return JSONResponse({"ok": True})


async def api_referral_leaderboard(request: Request) -> JSONResponse:
    """GET /api/referral/leaderboard -- top referrers by completed referrals.

    FIX (2026-03-31): Use subqueries instead of double LEFT JOIN to avoid
    cartesian product between referrals and rewards tables.
    """
    limit = min(int(request.query_params.get("limit", "10")), 50)

    async with _referral_db() as db:
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
        rows = await cur.fetchall()

    leaders = [
        {
            "name": row[0] or "Anonymous",
            "referral_code": row[1],
            "completed": row[2],
            "total_earned": round(float(row[3]), 2),
        }
        for row in rows
    ]
    return JSONResponse({"leaderboard": leaders})


async def api_portal_owner(request: Request) -> JSONResponse:
    """Return portal owner identity for dynamic referral/share features."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    owner_file = SCRIPT_DIR / "portal_owner.json"
    try:
        owner = json.loads(owner_file.read_text())
        return JSONResponse(owner)
    except Exception:
        return JSONResponse({"name": "Portal User", "email": "", "referral_code": ""})


# ---------------------------------------------------------------------------
# Payout Request API (Phase 3a — Manual Bridge)
# ---------------------------------------------------------------------------

def _send_telegram_notification(message: str) -> bool:
    """Send a Telegram notification via tg_send.sh (searches standard locations)."""
    try:
        tg_send = _find_tg_send()
        if tg_send:
            subprocess.run(
                ["bash", str(tg_send), message],
                timeout=15, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL
            )
            return True
    except Exception:
        pass
    return False


def _read_payout_requests_legacy() -> list:
    """(LEGACY) Read all payout requests from JSONL file. Kept for migration only."""
    requests_list = []
    if not PAYOUT_REQUESTS_FILE.exists():
        return requests_list
    try:
        with PAYOUT_REQUESTS_FILE.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    requests_list.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return requests_list


def _write_payout_request_legacy(entry: dict) -> None:
    """(LEGACY) Append a payout request to JSONL file. Kept for migration only."""
    with PAYOUT_REQUESTS_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _update_payout_status_legacy(request_id: str, status: str, batch_id: str = "") -> bool:
    """(LEGACY) Update payout request in JSONL file. Kept for migration only."""
    all_requests = _read_payout_requests_legacy()
    found = False
    for req in all_requests:
        if req.get("request_id") == request_id:
            req["status"] = status
            if batch_id:
                req["batch_id"] = batch_id
            if status in ("completed", "paid"):
                req["paid_at"] = datetime.now(timezone.utc).isoformat()
            found = True
            break
    if not found:
        return False
    try:
        with PAYOUT_REQUESTS_FILE.open("w") as f:
            for req in all_requests:
                f.write(json.dumps(req) + "\n")
    except Exception:
        return False
    return True


# ── New SQLite-backed payout helpers (replace JSONL) ──

async def _read_payout_requests_db() -> list:
    """Read all payout requests from SQLite."""
    async with _referral_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM payout_requests ORDER BY created_at DESC")
        rows = await cur.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            # Synthesize created_at_ts from ISO created_at for backward compat
            try:
                dt = datetime.fromisoformat(d.get("created_at", ""))
                d["created_at_ts"] = dt.timestamp()
            except (ValueError, TypeError):
                d["created_at_ts"] = 0.0
            result.append(d)
        return result


async def _write_payout_request_db(entry: dict) -> None:
    """Insert a payout request into SQLite. Raises IntegrityError on duplicate request_id."""
    async with _referral_db() as db:
        await db.execute(
            """INSERT INTO payout_requests
               (request_id, referral_code, paypal_email, amount, status, batch_id, notes, created_at, paid_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (entry["request_id"], entry["referral_code"], entry.get("paypal_email", ""),
             entry.get("amount", 0.0), entry.get("status", "pending"),
             entry.get("batch_id", ""), entry.get("notes", ""),
             entry.get("created_at", ""), entry.get("paid_at"))
        )
        await db.commit()


async def _update_payout_status_db(request_id: str, status: str, batch_id: str = "", notes: str = "") -> bool:
    """Update payout request status in SQLite. Returns True if row was found and updated."""
    async with _referral_db() as db:
        paid_at = datetime.now(timezone.utc).isoformat() if status in ("completed", "paid") else None
        cur = await db.execute(
            """UPDATE payout_requests
               SET status = ?,
                   batch_id = CASE WHEN ? != '' THEN ? ELSE batch_id END,
                   notes = CASE WHEN ? != '' THEN ? ELSE notes END,
                   paid_at = CASE WHEN ? IS NOT NULL THEN ? ELSE paid_at END
               WHERE request_id = ?""",
            (status, batch_id, batch_id, notes, notes, paid_at, paid_at, request_id)
        )
        await db.commit()
        return cur.rowcount > 0


async def api_referral_payout_request(request: Request) -> JSONResponse:
    """POST /api/referral/payout-request — user requests a payout.

    Requires affiliate session token OR portal bearer token.
    Body: { referral_code, paypal_email, amount, session_token? }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    # Auth: portal bearer OR valid affiliate session
    portal_authed = check_auth(request)
    session_code = None
    if not portal_authed:
        session_token = (
            str(body.get("session_token", "")).strip()
            or request.headers.get("x-affiliate-session", "").strip()
        )
        session_code = _verify_affiliate_session(session_token)
        if not session_code:
            return JSONResponse({"error": "authentication required"}, status_code=401)

    paypal_email = str(body.get("paypal_email", "")).strip().lower()
    referral_code = str(body.get("referral_code", "")).strip()
    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid amount"}, status_code=400)

    if not paypal_email or "@" not in paypal_email or "." not in paypal_email.split("@")[-1]:
        return JSONResponse({"error": "invalid paypal_email"}, status_code=400)

    if not referral_code:
        return JSONResponse({"error": "missing referral_code"}, status_code=400)

    # IDOR fix: affiliate sessions can only request payouts for their own code
    if not portal_authed and session_code and session_code.upper() != referral_code.upper():
        return JSONResponse({"error": "access denied"}, status_code=403)

    if amount < PAYOUT_MIN_AMOUNT:
        return JSONResponse(
            {"error": f"minimum payout is ${PAYOUT_MIN_AMOUNT:.0f}"},
            status_code=400
        )

    existing = await _read_payout_requests_db()
    cooldown_secs = PAYOUT_COOLDOWN_DAYS * 86400
    now_ts = time.time()
    for req in existing:
        if req.get("referral_code") == referral_code and req.get("status") in ("pending", "processing"):
            created_at = req.get("created_at_ts", 0)
            if (now_ts - created_at) < cooldown_secs:
                days_left = int((cooldown_secs - (now_ts - created_at)) / 86400) + 1
                return JSONResponse(
                    {"error": f"payout already requested. Please wait {days_left} more day(s)."},
                    status_code=429
                )

    # Check balance against SQLite rewards table
    actual_earnings = 0.0
    try:
        async with _referral_db() as _db:
            _cur = await _db.execute(
                """SELECT COALESCE(SUM(rw.reward_value), 0)
                   FROM rewards rw
                   JOIN referrers r ON r.id = rw.referrer_id
                   WHERE r.referral_code = ? COLLATE NOCASE""",
                (referral_code,)
            )
            _row = await _cur.fetchone()
            actual_earnings = float(_row[0]) if _row else 0.0
    except Exception as e:
        print(f"[referral] DB error during payout balance check: {e}")
        return JSONResponse(
            {"error": "unable to verify balance — please try again later"},
            status_code=503
        )

    # Subtract already-paid amounts from available balance (C4 fix, now atomic via SQLite)
    try:
        async with _referral_db() as _db:
            _cur = await _db.execute(
                """SELECT COALESCE(SUM(amount), 0) FROM payout_requests
                   WHERE referral_code = ? COLLATE NOCASE
                   AND status IN ('completed', 'paid')""",
                (referral_code,)
            )
            _row = await _cur.fetchone()
            paid_total = float(_row[0]) if _row else 0.0
            actual_earnings -= paid_total
    except Exception as e:
        print(f"[referral] Error reading payout history for balance deduction: {e}")
        return JSONResponse(
            {"error": "unable to verify payout history — please try again later"},
            status_code=503
        )

    if amount > actual_earnings:
        return JSONResponse(
            {"error": f"requested amount ${amount:.2f} exceeds available balance ${actual_earnings:.2f}"},
            status_code=400
        )

    request_id = f"payout-{referral_code}-{int(now_ts)}"
    entry = {
        "request_id": request_id,
        "referral_code": referral_code,
        "paypal_email": paypal_email,
        "amount": round(amount, 2),
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_at_ts": now_ts,
        "paid_at": None,
        "notes": "",
    }
    await _write_payout_request_db(entry)

    # Audit log: payout requested
    client_ip = request.client.host if request.client else "unknown"
    await _log_financial_event(
        event_type="payout_requested",
        referral_code=referral_code,
        amount=round(amount, 2),
        actor=f"affiliate:{referral_code}",
        details=f"paypal={paypal_email}, request_id={request_id}, available_balance=${actual_earnings:.2f}",
        ip_address=client_ip,
    )

    # Auto-approve payouts up to $1,000; larger amounts require manual approval
    if amount <= PAYOUT_AUTO_APPROVE_LIMIT:
        try:
            payout_result = await _execute_paypal_payout(
                paypal_email=paypal_email,
                amount=round(amount, 2),
                request_id=request_id,
                note=f"PureBrain referral payout for {referral_code}",
            )
            if payout_result.get("ok"):
                # Update payout status to completed
                await _update_payout_status_db(request_id, "completed", batch_id=payout_result.get("batch_id", ""))
                # Audit log: auto-payout sent
                await _log_financial_event(
                    event_type="auto_payout_sent",
                    referral_code=referral_code,
                    amount=round(amount, 2),
                    actor=f"affiliate:{referral_code}",
                    details=f"paypal={paypal_email}, batch_id={payout_result.get('batch_id', 'n/a')}, request_id={request_id}",
                    ip_address=client_ip,
                )
                tg_msg = (
                    f"AUTO-PAYOUT SENT\n"
                    f"Referral: {referral_code}\n"
                    f"Amount: ${amount:.2f}\n"
                    f"PayPal: {paypal_email}\n"
                    f"Batch ID: {payout_result.get('batch_id', 'n/a')}\n"
                    f"Request ID: {request_id}"
                )
                _send_telegram_notification(tg_msg)
                return JSONResponse({
                    "ok": True,
                    "request_id": request_id,
                    "message": f"Payout of ${amount:.2f} sent to {paypal_email}!",
                    "amount": round(amount, 2),
                    "paypal_email": paypal_email,
                    "auto_approved": True,
                    "batch_id": payout_result.get("batch_id"),
                })
            else:
                # PayPal failed — fall through to manual
                tg_msg = (
                    f"AUTO-PAYOUT FAILED — NEEDS MANUAL\n"
                    f"Referral: {referral_code}\n"
                    f"Amount: ${amount:.2f}\n"
                    f"PayPal: {paypal_email}\n"
                    f"Error: {payout_result.get('error', 'unknown')}\n"
                    f"Request ID: {request_id}"
                )
                _send_telegram_notification(tg_msg)
        except Exception as e:
            tg_msg = (
                f"AUTO-PAYOUT EXCEPTION — NEEDS MANUAL\n"
                f"Referral: {referral_code}\n"
                f"Amount: ${amount:.2f}\n"
                f"PayPal: {paypal_email}\n"
                f"Error: {str(e)[:200]}\n"
                f"Request ID: {request_id}"
            )
            _send_telegram_notification(tg_msg)
    else:
        # Over $1,000 — require manual approval
        tg_msg = (
            f"PAYOUT REQUEST — MANUAL APPROVAL REQUIRED (>${PAYOUT_AUTO_APPROVE_LIMIT:.0f})\n"
            f"Referral: {referral_code}\n"
            f"Amount: ${amount:.2f}\n"
            f"PayPal: {paypal_email}\n"
            f"Request ID: {request_id}\n"
            f"Earnings on file: ${actual_earnings:.2f}\n"
            f"To approve: POST /api/referral/payout-approve with request_id"
        )
        _send_telegram_notification(tg_msg)

    return JSONResponse({
        "ok": True,
        "request_id": request_id,
        "message": "Payout request submitted. We will process within 2 business days." if amount > PAYOUT_AUTO_APPROVE_LIMIT else "Payout is being processed.",
        "amount": round(amount, 2),
        "paypal_email": paypal_email,
    })


async def api_referral_payout_history(request: Request) -> JSONResponse:
    """GET /api/referral/payout-history?referral_code=XXX&session=TOKEN"""
    portal_authed = check_auth(request)
    session_code = None
    if not portal_authed:
        session_token = (
            request.query_params.get("session", "").strip()
            or request.headers.get("x-affiliate-session", "").strip()
        )
        session_code = _verify_affiliate_session(session_token)
        if not session_code:
            return JSONResponse({"error": "authentication required"}, status_code=401)

    referral_code = request.query_params.get("referral_code", "").strip()
    if not referral_code:
        return JSONResponse({"error": "missing referral_code"}, status_code=400)

    # HIGH-005: IDOR fix — affiliate sessions can only view their own payout history
    if not portal_authed and session_code and session_code.upper() != referral_code.upper():
        return JSONResponse({"error": "access denied"}, status_code=403)

    all_requests = await _read_payout_requests_db()
    user_requests = [r for r in all_requests if r.get("referral_code") == referral_code]
    user_requests.sort(key=lambda r: r.get("created_at_ts", 0), reverse=True)

    cooldown_secs = PAYOUT_COOLDOWN_DAYS * 86400
    now_ts = time.time()
    has_pending = False
    days_until_eligible = 0
    for req in user_requests:
        if req.get("status") in ("pending", "processing"):
            created_at = req.get("created_at_ts", 0)
            elapsed = now_ts - created_at
            if elapsed < cooldown_secs:
                has_pending = True
                days_until_eligible = int((cooldown_secs - elapsed) / 86400) + 1
                break

    return JSONResponse({
        "requests": user_requests,
        "has_pending": has_pending,
        "days_until_eligible": days_until_eligible,
    })


async def api_admin_payout_mark_paid(request: Request) -> JSONResponse:
    """POST /api/admin/payout/mark-paid — admin marks a payout as paid."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    request_id = str(body.get("request_id", "")).strip()
    notes = str(body.get("notes", "")).strip()

    if not request_id:
        return JSONResponse({"error": "missing request_id"}, status_code=400)

    # Read the target request from DB first to get details for notification
    all_requests = await _read_payout_requests_db()
    paid_entry = None
    for req in all_requests:
        if req.get("request_id") == request_id:
            paid_entry = req
            break

    if not paid_entry:
        return JSONResponse({"error": "request_id not found"}, status_code=404)

    ok = await _update_payout_status_db(request_id, "paid", notes=notes)
    if not ok:
        return JSONResponse({"error": "failed to update payout status"}, status_code=500)

    paid_at = datetime.now(timezone.utc).isoformat()

    tg_msg = (
        f"PAYOUT MARKED PAID\n"
        f"Request: {request_id}\n"
        f"Amount: ${paid_entry.get('amount', 0):.2f}\n"
        f"PayPal: {paid_entry.get('paypal_email', '')}"
    )
    _send_telegram_notification(tg_msg)

    return JSONResponse({
        "ok": True,
        "request_id": request_id,
        "status": "paid",
        "paid_at": paid_at,
    })



async def _is_valid_admin_token(token: str) -> bool:
    """Check if token is a valid admin_tokens entry in the DB."""
    if not token:
        return False
    async with _referral_db() as db:
        cur = await db.execute(
            "SELECT id FROM admin_tokens WHERE token = ?", (token,)
        )
        row = await cur.fetchone()
    return row is not None


async def _is_admin_token_readonly(token: str) -> bool:
    """Returns True if the token exists and is a viewer (read-only) role."""
    if not token:
        return True
    async with _referral_db() as db:
        cur = await db.execute(
            "SELECT role FROM admin_tokens WHERE token = ?", (token,)
        )
        row = await cur.fetchone()
    if row is None:
        return True  # unknown token = treat as read-only
    return row[0] != "admin"


async def api_admin_invite(request: Request) -> JSONResponse:
    """POST /api/admin/invite — generate a read-only admin viewer token (main bearer only)."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    email = str(body.get("email", "")).strip().lower()
    name  = str(body.get("name", "")).strip()
    if not email or "@" not in email:
        return JSONResponse({"error": "invalid email"}, status_code=400)

    token = secrets.token_urlsafe(32)
    now   = datetime.now(timezone.utc).isoformat()

    async with _referral_db() as db:
        await db.execute(
            "INSERT INTO admin_tokens (token, email, name, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (token, email, name, "viewer", now)
        )
        await db.commit()

    return JSONResponse({
        "ok": True,
        "token": token,
        "email": email,
        "name": name,
        "role": "viewer",
        "dashboard_url": f"https://portal.purebrain.ai/admin/clients?admin_token={token}",
    })


async def api_admin_invites_list(request: Request) -> JSONResponse:
    """GET /api/admin/invites — list all active admin viewer tokens. Main bearer only."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    async with _referral_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, token, email, name, role, created_at FROM admin_tokens ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
        invitees = [dict(r) for r in rows]

    return JSONResponse({"ok": True, "invitees": invitees})


async def api_admin_invite_revoke(request: Request) -> JSONResponse:
    """POST /api/admin/invite/revoke — delete an admin viewer token by id. Main bearer only."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    token_id = body.get("id")
    if not token_id:
        return JSONResponse({"error": "id required"}, status_code=400)

    async with _referral_db() as db:
        cur = await db.execute("SELECT id FROM admin_tokens WHERE id = ?", (token_id,))
        row = await cur.fetchone()
        if not row:
            return JSONResponse({"error": "token not found"}, status_code=404)
        await db.execute("DELETE FROM admin_tokens WHERE id = ?", (token_id,))
        await db.commit()

    return JSONResponse({"ok": True, "id": token_id})


async def api_referral_payout_approve(request: Request) -> JSONResponse:
    """POST /api/referral/payout-approve — approve a pending payout and execute PayPal transfer.

    Portal bearer token required (admin only).
    Body: { request_id, dry_run? }
    On success: marks payout as "completed", fires PayPal payout, notifies via Telegram.
    """
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    request_id = str(body.get("request_id", "")).strip()
    dry_run    = bool(body.get("dry_run", False))

    if not request_id:
        return JSONResponse({"error": "missing request_id"}, status_code=400)

    # Read target request from SQLite
    all_requests = await _read_payout_requests_db()
    target = None
    for req in all_requests:
        if req.get("request_id") == request_id:
            target = req
            break

    if not target:
        return JSONResponse({"error": "request_id not found"}, status_code=404)

    if target.get("status") in ("completed", "paid"):
        return JSONResponse({"error": f"payout already {target['status']}", "request_id": request_id}, status_code=409)

    paypal_email = target.get("paypal_email", "")
    amount       = float(target.get("amount", 0))

    if not paypal_email or "@" not in paypal_email:
        return JSONResponse({"error": "no valid PayPal email on this payout request"}, status_code=400)
    if amount <= 0:
        return JSONResponse({"error": "invalid amount on payout request"}, status_code=400)

    if dry_run:
        return JSONResponse({
            "ok":          True,
            "dry_run":     True,
            "request_id":  request_id,
            "paypal_email": paypal_email,
            "amount":      amount,
            "message":     "Dry run — no payment sent.",
        })

    # Execute PayPal payout
    payout_result = await _execute_paypal_payout(
        paypal_email=paypal_email,
        amount=amount,
        request_id=request_id,
        note=f"PureBrain affiliate commission — request {request_id}",
    )

    if payout_result["ok"]:
        batch_id = payout_result.get("batch_id", "")
        notes_text = f"Auto-paid via PayPal Payouts API. Batch: {batch_id}"
        await _update_payout_status_db(request_id, "completed", batch_id=batch_id, notes=notes_text)
    else:
        error_notes = f"PayPal error: {payout_result.get('error', 'unknown')}"
        await _update_payout_status_db(request_id, "failed", notes=error_notes)

    # Audit log: payout approval attempt
    await _log_financial_event(
        event_type="payout_approved" if payout_result["ok"] else "payout_approve_failed",
        referral_code=target.get("referral_code", ""),
        amount=amount,
        actor="admin:bearer",
        details=f"request_id={request_id}, batch_id={payout_result.get('batch_id', 'n/a')}, paypal={paypal_email}",
        ip_address=request.client.host if request.client else "unknown",
    )

    if payout_result["ok"]:
        tg_msg = (
            f"PAYOUT SENT via PayPal\n"
            f"Request: {request_id}\n"
            f"Amount: ${amount:.2f}\n"
            f"PayPal: {paypal_email}\n"
            f"Batch ID: {payout_result.get('batch_id', 'n/a')}"
        )
        _send_telegram_notification(tg_msg)
        return JSONResponse({
            "ok":          True,
            "request_id":  request_id,
            "batch_id":    payout_result.get("batch_id"),
            "amount":      amount,
            "paypal_email": paypal_email,
            "status":      "completed",
            "message":     f"Payout of ${amount:.2f} sent to {paypal_email}.",
        })
    else:
        tg_msg = (
            f"PAYOUT FAILED\n"
            f"Request: {request_id}\n"
            f"Amount: ${amount:.2f}\n"
            f"PayPal: {paypal_email}\n"
            f"Error: {payout_result.get('error', 'unknown')}"
        )
        _send_telegram_notification(tg_msg)
        return JSONResponse({
            "ok":         False,
            "request_id": request_id,
            "error":      payout_result.get("error"),
            "status":     "failed",
        }, status_code=502)


async def api_admin_affiliates(request: Request) -> JSONResponse:
    """GET /api/admin/affiliates — all referrers with full stats (admin or viewer token)."""
    admin_token = (
        request.query_params.get("admin_token", "").strip()
        or request.headers.get("x-admin-token", "").strip()
    )
    is_main_admin = check_auth(request)
    if not is_main_admin:
        if not await _is_valid_admin_token(admin_token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    async with _referral_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM referrers ORDER BY created_at DESC")
        referrers = await cur.fetchall()

        affiliates = []
        for r in referrers:
            rid  = r["id"]
            code = r["referral_code"]

            cur2 = await db.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (rid,)
            )
            total = (await cur2.fetchone())[0]

            cur2 = await db.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND status = \'completed\'", (rid,)
            )
            completed = (await cur2.fetchone())[0]

            cur2 = await db.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND status = \'pending\'", (rid,)
            )
            pending = (await cur2.fetchone())[0]

            cur2 = await db.execute(
                "SELECT COALESCE(SUM(reward_value), 0) FROM rewards WHERE referrer_id = ?", (rid,)
            )
            earnings = float((await cur2.fetchone())[0])

            cur2 = await db.execute(
                "SELECT COUNT(*) FROM referral_clicks WHERE referral_code = ? COLLATE NOCASE", (code,)
            )
            clicks = (await cur2.fetchone())[0]

            cur2 = await db.execute(
                """SELECT ref.id, ref.referred_name, ref.referred_email, ref.status, ref.created_at,
                          COALESCE(SUM(cp.commission_value), 0) AS earnings,
                          COUNT(cp.id) AS payment_count
                   FROM referrals ref
                   LEFT JOIN commission_payments cp ON cp.referral_id = ref.id
                   WHERE ref.referrer_id = ?
                   GROUP BY ref.id
                   ORDER BY ref.created_at DESC""",
                (rid,)
            )
            history = [dict(row) async for row in cur2]

            affiliates.append({
                "id":          rid,
                "name":        r["user_name"],
                "email":       r["user_email"],
                "code":        code,
                "paypal_email": r["paypal_email"],
                "clicks":      clicks,
                "total":       total,
                "completed":   completed,
                "pending":     pending,
                "earnings":    round(earnings, 2),
                "joined":      r["created_at"],
                "history":     history,
            })

    return JSONResponse({"affiliates": affiliates, "count": len(affiliates)})


async def api_admin_payouts(request: Request) -> JSONResponse:
    """GET /api/admin/payouts — all payout requests (admin or viewer token)."""
    admin_token = (
        request.query_params.get("admin_token", "").strip()
        or request.headers.get("x-admin-token", "").strip()
    )
    is_main_admin = check_auth(request)
    if not is_main_admin:
        if not await _is_valid_admin_token(admin_token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    requests_list = await _read_payout_requests_db()
    requests_list.sort(key=lambda r: r.get("created_at_ts", 0), reverse=True)
    return JSONResponse({"requests": requests_list, "count": len(requests_list)})


async def api_admin_affiliate_update(request: Request) -> JSONResponse:
    """PUT /api/admin/affiliate/update — update affiliate name, email, or PayPal email."""
    if request.method == "OPTIONS":
        return Response(status_code=204)
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    referral_code = str(body.get("referral_code", "")).strip()
    if not referral_code:
        return JSONResponse({"error": "referral_code required"}, status_code=400)

    user_name    = body.get("user_name")
    user_email   = body.get("user_email")
    paypal_email = body.get("paypal_email")

    # Validate emails if provided
    def _valid_email(e: str) -> bool:
        return "@" in e and "." in e.split("@")[-1]

    if user_email is not None:
        user_email = str(user_email).strip().lower()
        if user_email and not _valid_email(user_email):
            return JSONResponse({"error": "invalid user_email format"}, status_code=400)

    if paypal_email is not None:
        paypal_email = str(paypal_email).strip().lower()
        if paypal_email and not _valid_email(paypal_email):
            return JSONResponse({"error": "invalid paypal_email format"}, status_code=400)

    fields: list[str] = []
    params: list = []

    if user_name is not None:
        fields.append("user_name = ?")
        params.append(str(user_name).strip())
    if user_email is not None:
        fields.append("user_email = ?")
        params.append(user_email)
    if paypal_email is not None:
        fields.append("paypal_email = ?")
        params.append(paypal_email)

    if not fields:
        return JSONResponse({"error": "no fields to update"}, status_code=400)

    params.append(referral_code)

    async with _referral_db() as db:
        cur = await db.execute(
            "SELECT id FROM referrers WHERE referral_code = ? COLLATE NOCASE", (referral_code,)
        )
        row = await cur.fetchone()
        if not row:
            return JSONResponse({"error": "affiliate not found"}, status_code=404)

        await db.execute(
            f"UPDATE referrers SET {', '.join(fields)} WHERE referral_code = ? COLLATE NOCASE",
            params,
        )
        await db.commit()

    updated_fields = []
    if user_name is not None:
        updated_fields.append("user_name")
    if user_email is not None:
        updated_fields.append("user_email")
    if paypal_email is not None:
        updated_fields.append("paypal_email")

    print(f"[admin] Affiliate updated: {referral_code} — fields: {updated_fields}")
    return JSONResponse({"ok": True, "updated_fields": updated_fields})


async def api_admin_affiliate_delete(request: Request) -> JSONResponse:
    """DELETE /api/admin/affiliate/delete — delete an affiliate and optionally their referral records."""
    if request.method == "OPTIONS":
        return Response(status_code=204)
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    referral_code    = str(body.get("referral_code", "")).strip()
    delete_referrals = bool(body.get("delete_referrals", False))

    if not referral_code:
        return JSONResponse({"error": "referral_code required"}, status_code=400)

    referrals_deleted = 0

    async with _referral_db() as db:
        cur = await db.execute(
            "SELECT id FROM referrers WHERE referral_code = ? COLLATE NOCASE", (referral_code,)
        )
        row = await cur.fetchone()
        if not row:
            return JSONResponse({"error": "affiliate not found"}, status_code=404)

        referrer_id = row[0]

        if delete_referrals:
            # Count referrals first
            cur2 = await db.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (referrer_id,)
            )
            referrals_deleted = (await cur2.fetchone())[0]

            # Delete dependent records
            await db.execute(
                """DELETE FROM commission_payments
                   WHERE referral_id IN (SELECT id FROM referrals WHERE referrer_id = ?)""",
                (referrer_id,),
            )
            await db.execute(
                "DELETE FROM rewards WHERE referrer_id = ?", (referrer_id,)
            )
            await db.execute(
                "DELETE FROM referral_clicks WHERE referral_code = ? COLLATE NOCASE", (referral_code,)
            )
            await db.execute(
                "DELETE FROM referrals WHERE referrer_id = ?", (referrer_id,)
            )

        await db.execute(
            "DELETE FROM referrers WHERE id = ?", (referrer_id,)
        )
        await db.commit()

    print(f"[admin] Affiliate deleted: {referral_code} (referrals_deleted={referrals_deleted})")
    return JSONResponse({
        "ok": True,
        "deleted": referral_code,
        "referrals_deleted": referrals_deleted,
    })


async def api_admin_referral_update(request: Request) -> JSONResponse:
    """PUT /api/admin/referral/update — update a specific referral record."""
    if request.method == "OPTIONS":
        return Response(status_code=204)
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    referral_id = body.get("referral_id")
    if referral_id is None:
        return JSONResponse({"error": "referral_id required"}, status_code=400)

    try:
        referral_id = int(referral_id)
    except (ValueError, TypeError):
        return JSONResponse({"error": "referral_id must be an integer"}, status_code=400)

    referred_email = body.get("referred_email")
    referred_name  = body.get("referred_name")
    status         = body.get("status")

    allowed_statuses = {"pending", "completed", "rejected"}
    if status is not None:
        status = str(status).strip().lower()
        if status not in allowed_statuses:
            return JSONResponse(
                {"error": f"invalid status — must be one of: {', '.join(sorted(allowed_statuses))}"},
                status_code=400,
            )

    fields: list[str] = []
    params: list = []

    if referred_email is not None:
        referred_email = str(referred_email).strip().lower()
        fields.append("referred_email = ?")
        params.append(referred_email)
    if referred_name is not None:
        fields.append("referred_name = ?")
        params.append(str(referred_name).strip())
    if status is not None:
        fields.append("status = ?")
        params.append(status)

    if not fields:
        return JSONResponse({"error": "no fields to update"}, status_code=400)

    params.append(referral_id)

    async with _referral_db() as db:
        cur = await db.execute("SELECT id FROM referrals WHERE id = ?", (referral_id,))
        row = await cur.fetchone()
        if not row:
            return JSONResponse({"error": "referral not found"}, status_code=404)

        await db.execute(
            f"UPDATE referrals SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        await db.commit()

    updated_fields = []
    if referred_email is not None:
        updated_fields.append("referred_email")
    if referred_name is not None:
        updated_fields.append("referred_name")
    if status is not None:
        updated_fields.append("status")

    print(f"[admin] Referral {referral_id} updated — fields: {updated_fields}")

    # Audit log: admin referral update
    await _log_financial_event(
        event_type="admin_referral_update",
        referral_code=str(referral_id),
        details=f"fields={updated_fields}, email={referred_email}, name={referred_name}, status={status}",
        actor="admin:bearer",
        ip_address=request.client.host if request.client else "unknown",
    )

    return JSONResponse({"ok": True, "updated_fields": updated_fields})



async def api_admin_referral_assign(request: Request) -> JSONResponse:
    """POST /api/admin/referral/assign — manually assign an existing client to a referrer (retroactive credit).
    Body: { referral_code: str, client_email: str, client_name?: str }
    Creates or updates a referral record with status=completed.
    """
    if request.method == "OPTIONS":
        return Response(status_code=204)
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    referral_code  = str(body.get("referral_code", "")).strip().upper()
    client_email   = str(body.get("client_email", "")).strip().lower()
    client_name    = str(body.get("client_name", "")).strip()

    if not referral_code:
        return JSONResponse({"error": "referral_code required"}, status_code=400)
    if not client_email or "@" not in client_email:
        return JSONResponse({"error": "invalid client_email"}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()

    async with _referral_db() as db:
        # Verify referrer exists
        cur = await db.execute(
            "SELECT id FROM referrers WHERE referral_code = ? COLLATE NOCASE", (referral_code,)
        )
        row = await cur.fetchone()
        if row is None:
            return JSONResponse({"error": "referral code not found"}, status_code=404)
        referrer_id = row[0]

        # Look up client name from clients db if not provided
        if not client_name:
            async with _clients_db() as cdb:
                ccur = await cdb.execute(
                    "SELECT name FROM clients WHERE email = ? COLLATE NOCASE", (client_email,)
                )
                crow = await ccur.fetchone()
                if crow:
                    client_name = crow[0]

        # Single-referrer enforcement: remove any existing completed referral for
        # this email under a DIFFERENT referrer before assigning to the new one.
        # A client must never be counted under two referrers simultaneously.
        removed_cur = await db.execute(
            """DELETE FROM referrals
               WHERE referred_email = ? COLLATE NOCASE
                 AND referrer_id != ?""",
            (client_email, referrer_id)
        )
        removed_count = removed_cur.rowcount

        # Check for existing referral record under the target referrer
        cur = await db.execute(
            """SELECT id, status FROM referrals
               WHERE referrer_id = ? AND referred_email = ? COLLATE NOCASE""",
            (referrer_id, client_email)
        )
        existing = await cur.fetchone()

        if existing:
            if existing[1] == "completed" and removed_count == 0:
                await db.commit()
                return JSONResponse({"ok": True, "message": "Referral already credited — no change needed.", "action": "noop"})
            await db.execute(
                "UPDATE referrals SET status='completed', completed_at=?, referred_name=? WHERE id=?",
                (now, client_name, existing[0])
            )
            action = "updated"
        else:
            await db.execute(
                """INSERT INTO referrals (referrer_id, referred_email, referred_name, status, created_at, completed_at)
                   VALUES (?, ?, ?, 'completed', ?, ?)""",
                (referrer_id, client_email, client_name, now, now)
            )
            action = "created"

        await db.commit()

    if removed_count > 0:
        print(f"[admin] Single-referrer enforcement: removed {removed_count} prior referral record(s) for {client_email} from other referrers")
    print(f"[admin] Referral assigned: {referral_code} → {client_email} ({action})")

    # Audit log: admin referral assign
    await _log_financial_event(
        event_type="admin_referral_assign",
        referral_code=referral_code,
        details=f"client={client_email}, action={action}, removed_prior={removed_count}",
        actor="admin:bearer",
        ip_address=request.client.host if request.client else "unknown",
    )

    return JSONResponse({"ok": True, "action": action, "removed_prior": removed_count, "message": f"Client {client_email} assigned to referrer {referral_code}."})


async def serve_admin_referrals(request: Request) -> Response:
    """GET /admin/referrals — serve admin dashboard HTML."""
    html_path = SCRIPT_DIR / "admin-referrals.html"
    if html_path.exists():
        return FileResponse(str(html_path), media_type="text/html")
    return Response("<h1>Admin dashboard not found</h1>", media_type="text/html", status_code=503)


# ---------------------------------------------------------------------------
# Client Admin System
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _clients_db():
    """Open clients DB with WAL mode enabled."""
    async with aiosqlite.connect(str(CLIENTS_DB)) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        yield db


async def _init_clients_db() -> None:
    """Create clients table on startup if it doesn't exist."""
    async with aiosqlite.connect(str(CLIENTS_DB)) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("""
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
                hidden                INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Ensure hidden column exists for older databases
        try:
            await db.execute("ALTER TABLE clients ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # column already exists
        await db.commit()

    # Add tracking columns (login_count, session_count, etc.) + webhook log table
    ensure_tracking_columns(str(CLIENTS_DB))


async def serve_admin_clients(request: Request) -> Response:
    """GET /admin/clients — serve clients admin dashboard HTML."""
    html_path = SCRIPT_DIR / "admin-clients.html"
    if html_path.exists():
        return FileResponse(str(html_path), media_type="text/html")
    return Response("<h1>Client admin dashboard not found</h1>", media_type="text/html", status_code=503)


async def api_admin_clients(request: Request) -> JSONResponse:
    """GET /api/admin/clients — list all clients with stats. Bearer auth or viewer token required."""
    admin_token_param = request.query_params.get("admin_token", "")
    is_viewer = False
    if not check_auth(request):
        # Check viewer token
        if admin_token_param and await _is_valid_admin_token(admin_token_param):
            is_viewer = True
        else:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    show_hidden = request.query_params.get("show_hidden", "0") == "1"

    async with _clients_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM clients ORDER BY first_seen_at DESC")
        rows = await cur.fetchall()
        all_clients = [dict(r) for r in rows]

        # Separate hidden vs visible
        visible_clients = [c for c in all_clients if not c.get("hidden")]
        hidden_clients  = [c for c in all_clients if c.get("hidden")]

        # Stats are always based on visible (non-hidden) clients
        clients = visible_clients
        total   = len(clients)
        active  = sum(1 for c in clients if c.get("status") == "active")
        onboard = sum(1 for c in clients if c.get("status") == "onboarding")
        churned = sum(1 for c in clients if c.get("status") == "churned")
        total_rev = sum(float(c.get("total_paid") or 0) for c in clients)

        # MRR: subscription_active clients, estimate by tier
        tier_prices = {"awakened": 149, "insiders": 74.50, "partnered": 499, "unified": 999, "brainiac": 299}
        mrr = sum(
            tier_prices.get((c.get("tier") or "").lower(), 0)
            for c in clients
            if c.get("payment_status") == "subscription_active"
        )

    stats = {
        "total":         total,
        "active":        active,
        "onboarding":    onboard,
        "churned":       churned,
        "total_revenue": round(total_rev, 2),
        "mrr":           mrr,
        "hidden_count":  len(hidden_clients),
    }

    # Return visible clients by default; if show_hidden, return all
    response_clients = all_clients if show_hidden else visible_clients
    return JSONResponse({"clients": response_clients, "stats": stats})


async def api_public_client_stats(request: Request) -> JSONResponse:
    """GET /api/public/client-stats — lightweight stats for 777 dashboard. CORS enabled for 777.purebrain.ai."""
    origin = request.headers.get("origin", "")
    cors = {
        "Access-Control-Allow-Origin": "https://777.purebrain.ai" if "777.purebrain.ai" in origin else origin,
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Vary": "Origin",
    }
    if request.method == "OPTIONS":
        return Response("", status_code=204, headers=cors)

    async with _clients_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM clients WHERE hidden = 0 OR hidden IS NULL")
        rows = await cur.fetchall()
        clients = [dict(r) for r in rows]
        total = len(clients)
        active = sum(1 for c in clients if c.get("status") == "active")
        tier_prices = {"awakened": 149, "insiders": 74.50, "partnered": 499, "unified": 999, "brainiac": 299}
        mrr = sum(tier_prices.get((c.get("tier") or "").lower(), 0) for c in clients if c.get("payment_status") == "subscription_active")
        tiers = {}
        for c in clients:
            t = (c.get("tier") or "unknown").lower()
            tiers[t] = tiers.get(t, 0) + 1

    return JSONResponse({
        "subscribers": total,
        "active": active,
        "mrr": round(mrr, 2),
        "tiers": tiers,
        "updated": __import__("datetime").datetime.utcnow().isoformat() + "Z"
    }, headers=cors)


async def api_admin_clients_update(request: Request) -> JSONResponse:
    """POST /api/admin/clients/update — update client fields. Bearer auth required."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    client_id = body.get("id")
    if not client_id:
        return JSONResponse({"error": "id required"}, status_code=400)

    # Validate status
    status = str(body.get("status", "")).strip().lower()
    allowed_statuses = {"active", "onboarding", "churned", "trial", ""}
    if status and status not in allowed_statuses:
        return JSONResponse({"error": "invalid status — must be one of: active, onboarding, trial, churned"}, status_code=400)

    # Validate tier
    tier = str(body.get("tier", "")).strip().lower()
    allowed_tiers = {"awakened", "insiders", "partnered", "unified", "brainiac", "unknown", ""}
    if tier and tier not in allowed_tiers:
        return JSONResponse({"error": "invalid tier — must be one of: awakened, insiders, partnered, unified, brainiac, unknown"}, status_code=400)

    # Email uniqueness check (if email is being changed)
    new_email = str(body.get("email", "")).strip().lower()
    if new_email:
        async with _clients_db() as db:
            cur = await db.execute(
                "SELECT id FROM clients WHERE LOWER(email) = ? AND id != ?",
                (new_email, client_id)
            )
            existing = await cur.fetchone()
        if existing:
            return JSONResponse({"error": "email already in use by another client"}, status_code=409)

    # Build dynamic update — only include fields present in body
    now = datetime.now(timezone.utc).isoformat()
    fields = ["updated_at = ?"]
    params: list = [now]

    text_fields = {
        "name":    body.get("name"),
        "goes_by": body.get("goes_by"),
        "email":   new_email if new_email else None,
        "ai_name": body.get("ai_name"),
        "company": body.get("company"),
        "role":    body.get("role"),
        "goal":    body.get("goal"),
        "notes":   body.get("notes"),
    }
    for col, val in text_fields.items():
        if val is not None:
            fields.append(f"{col} = ?")
            params.append(str(val).strip())

    if status:
        fields.append("status = ?")
        params.append(status)
    elif "status" in body:
        # Allow explicit empty to keep existing — skip
        pass

    if tier:
        fields.append("tier = ?")
        params.append(tier)
    elif "tier" in body:
        pass

    params.append(client_id)
    async with _clients_db() as db:
        await db.execute(f"UPDATE clients SET {', '.join(fields)} WHERE id = ?", params)
        await db.commit()

    return JSONResponse({"ok": True, "id": client_id})


async def api_admin_clients_import(request: Request) -> JSONResponse:
    """POST /api/admin/clients/import — scan JSONL logs and upsert client records. Bearer auth required."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    imported = 0
    updated  = 0
    errors   = 0

    # Collect candidate records keyed by email (lowercased)
    # Priority: seed (pay_test) > payments > web_conversations
    candidates: dict[str, dict] = {}

    # --- 1. Parse purebrain_pay_test.jsonl (seed/questionnaire data) ---
    if PAY_TEST_LOG.exists():
        try:
            with PAY_TEST_LOG.open("r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue

                    email = (d.get("email") or "").strip().lower()
                    if not email or "@" not in email:
                        continue

                    # Skip obvious test/sandbox entries
                    order_id = (d.get("orderId") or "").strip()
                    if any(order_id.startswith(p) for p in ("SANDBOX-", "E2E-", "test-", "TEST-")):
                        continue
                    if "sandbox" in email or "test" in email.split("@")[0]:
                        continue

                    ts = d.get("server_timestamp", "")
                    rec = candidates.setdefault(email, {
                        "email": email,
                        "name": "",
                        "goes_by": "",
                        "ai_name": "",
                        "company": "",
                        "role": "",
                        "goal": "",
                        "tier": "unknown",
                        "payment_status": "none",
                        "paypal_subscription_id": "",
                        "total_paid": 0.0,
                        "payment_count": 0,
                        "referral_code": "",
                        "first_seen_at": ts,
                        "last_active_at": ts,
                        "onboarded_at": "",
                        "_sources": set(),
                    })

                    rec["_sources"].add("pay_test")

                    # Update fields if we get richer data
                    if d.get("name"):
                        rec["name"] = d["name"].strip()
                    if d.get("aiName"):
                        rec["ai_name"] = d["aiName"].strip()
                    if d.get("goesBy"):
                        rec["goes_by"] = d["goesBy"].strip()
                    if d.get("company"):
                        rec["company"] = d["company"].strip()
                    if d.get("role"):
                        rec["role"] = d["role"].strip()
                    if d.get("primaryGoal"):
                        rec["goal"] = d["primaryGoal"].strip()
                    if d.get("tier") and d["tier"] not in ("unknown", "test", ""):
                        rec["tier"] = d["tier"].strip()
                    if d.get("paypalSubscriptionId"):
                        rec["paypal_subscription_id"] = d["paypalSubscriptionId"].strip()
                    if d.get("session_uuid") and d.get("event") == "seed:complete":
                        rec["onboarded_at"] = ts

                    # Track earliest / latest timestamps
                    if ts and (not rec["first_seen_at"] or ts < rec["first_seen_at"]):
                        rec["first_seen_at"] = ts
                    if ts and ts > rec.get("last_active_at", ""):
                        rec["last_active_at"] = ts
        except Exception:
            pass

    # --- 2. Parse purebrain_payments.jsonl ---
    if PAYMENTS_LOG.exists():
        try:
            with PAYMENTS_LOG.open("r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue

                    email = (d.get("payerEmail") or "").strip().lower()
                    if not email or "@" not in email:
                        continue

                    order_id = (d.get("orderId") or "").strip()
                    if any(order_id.startswith(p) for p in ("SANDBOX-", "E2E-", "test-", "TEST-")):
                        continue
                    if "sandbox" in email or "test" in email.split("@")[0]:
                        continue

                    ts    = d.get("server_timestamp", "")
                    tier  = (d.get("tier") or "").strip()
                    amount = float(d.get("amount") or 0)

                    rec = candidates.setdefault(email, {
                        "email": email,
                        "name": "",
                        "goes_by": "",
                        "ai_name": "",
                        "company": "",
                        "role": "",
                        "goal": "",
                        "tier": "unknown",
                        "payment_status": "none",
                        "paypal_subscription_id": "",
                        "total_paid": 0.0,
                        "payment_count": 0,
                        "referral_code": "",
                        "first_seen_at": ts,
                        "last_active_at": ts,
                        "onboarded_at": "",
                        "_sources": set(),
                    })

                    rec["_sources"].add("payments")
                    if d.get("payerName") and not rec["name"]:
                        rec["name"] = d["payerName"].strip()
                    if tier and tier not in ("unknown", ""):
                        rec["tier"] = tier
                    if amount > 0:
                        rec["total_paid"] = round(rec["total_paid"] + amount, 2)
                        rec["payment_count"] += 1
                    # Subscription IDs start with I-
                    if order_id.startswith("I-"):
                        rec["paypal_subscription_id"] = order_id
                        rec["payment_status"] = "subscription_active"
                    elif amount > 0:
                        rec["payment_status"] = "paid"

                    if ts and (not rec["first_seen_at"] or ts < rec["first_seen_at"]):
                        rec["first_seen_at"] = ts
                    if ts and ts > rec.get("last_active_at", ""):
                        rec["last_active_at"] = ts
        except Exception:
            pass

    # --- 3. Parse purebrain_web_conversations.jsonl (fill gaps only) ---
    if WEB_CONVERSATIONS_LOG.exists():
        try:
            with WEB_CONVERSATIONS_LOG.open("r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue

                    ai_name  = (d.get("aiName") or "").strip()
                    user_name = (d.get("userName") or "").strip()
                    tier     = (d.get("userTier") or "").strip()
                    ref_code = (d.get("referralCode") or "").strip()
                    ts       = d.get("server_timestamp", "")

                    # Web conversations rarely have emails — skip if no useful data
                    if not ai_name and not user_name:
                        continue
                    if user_name.lower() in ("guest user", "atlas", "guest", ""):
                        continue

                    # Try to match by ai_name to existing candidate
                    matched = None
                    if ai_name:
                        for rec in candidates.values():
                            if rec.get("ai_name", "").lower() == ai_name.lower():
                                matched = rec
                                break

                    if matched:
                        if ref_code and not matched.get("referral_code"):
                            matched["referral_code"] = ref_code
                        if tier and matched.get("tier") in ("unknown", ""):
                            matched["tier"] = tier
                        if ts and ts > matched.get("last_active_at", ""):
                            matched["last_active_at"] = ts
        except Exception:
            pass

    # --- 4. Upsert into clients DB ---
    now = datetime.now(timezone.utc).isoformat()
    async with _clients_db() as db:
        db.row_factory = aiosqlite.Row
        for email, rec in candidates.items():
            # Require at minimum a name or ai_name to insert
            name = rec.get("name") or rec.get("ai_name") or email.split("@")[0]
            if not name:
                continue

            try:
                # Check existing
                cur = await db.execute(
                    "SELECT id, total_paid, payment_count, name, ai_name FROM clients WHERE email = ? COLLATE NOCASE",
                    (email,)
                )
                existing = await cur.fetchone()

                if existing:
                    # Merge: update fields only if they improve the record
                    ex_id    = existing["id"]
                    ex_paid  = float(existing["total_paid"] or 0)
                    ex_count = int(existing["payment_count"] or 0)
                    new_paid  = max(ex_paid,  rec["total_paid"])
                    new_count = max(ex_count, rec["payment_count"])

                    await db.execute("""
                        UPDATE clients SET
                            name = CASE WHEN name = '' OR name IS NULL THEN ? ELSE name END,
                            goes_by = CASE WHEN goes_by = '' OR goes_by IS NULL THEN ? ELSE goes_by END,
                            ai_name = CASE WHEN ai_name = '' OR ai_name IS NULL THEN ? ELSE ai_name END,
                            company = CASE WHEN company = '' OR company IS NULL THEN ? ELSE company END,
                            role = CASE WHEN role = '' OR role IS NULL THEN ? ELSE role END,
                            goal = CASE WHEN goal = '' OR goal IS NULL THEN ? ELSE goal END,
                            tier = CASE WHEN tier = 'unknown' OR tier = '' OR tier IS NULL THEN ? ELSE tier END,
                            payment_status = CASE WHEN payment_status = 'none' OR payment_status IS NULL THEN ? ELSE payment_status END,
                            paypal_subscription_id = CASE WHEN paypal_subscription_id = '' OR paypal_subscription_id IS NULL THEN ? ELSE paypal_subscription_id END,
                            total_paid = ?,
                            payment_count = ?,
                            referral_code = CASE WHEN referral_code = '' OR referral_code IS NULL THEN ? ELSE referral_code END,
                            last_active_at = CASE WHEN last_active_at < ? THEN ? ELSE last_active_at END,
                            onboarded_at = CASE WHEN onboarded_at = '' OR onboarded_at IS NULL THEN ? ELSE onboarded_at END,
                            updated_at = ?
                        WHERE id = ?
                    """, (
                        name,
                        rec["goes_by"],
                        rec["ai_name"],
                        rec["company"],
                        rec["role"],
                        rec["goal"],
                        rec["tier"],
                        rec["payment_status"],
                        rec["paypal_subscription_id"],
                        new_paid,
                        new_count,
                        rec["referral_code"],
                        rec["last_active_at"],
                        rec["last_active_at"],
                        rec["onboarded_at"],
                        now,
                        ex_id,
                    ))
                    updated += 1
                else:
                    await db.execute("""
                        INSERT INTO clients
                            (name, email, goes_by, ai_name, company, role, goal, tier, status,
                             payment_status, paypal_subscription_id, total_paid, payment_count,
                             referral_code, first_seen_at, last_active_at, onboarded_at,
                             created_at, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        name,
                        email,
                        rec["goes_by"],
                        rec["ai_name"],
                        rec["company"],
                        rec["role"],
                        rec["goal"],
                        rec["tier"],
                        "active",
                        rec["payment_status"],
                        rec["paypal_subscription_id"],
                        rec["total_paid"],
                        rec["payment_count"],
                        rec["referral_code"],
                        rec["first_seen_at"] or now,
                        rec["last_active_at"] or now,
                        rec["onboarded_at"],
                        now,
                        now,
                    ))
                    imported += 1
            except Exception:
                errors += 1
                continue

        await db.commit()

    return JSONResponse({"ok": True, "imported": imported, "updated": updated, "errors": errors})


async def api_admin_clients_hide(request: Request) -> JSONResponse:
    """POST /api/admin/clients/hide — soft-delete a client (hide from default view). Bearer auth required."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    client_id = body.get("id")
    if not client_id:
        return JSONResponse({"error": "id required"}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()
    async with _clients_db() as db:
        cur = await db.execute("SELECT id, name FROM clients WHERE id = ?", (client_id,))
        row = await cur.fetchone()
        if not row:
            return JSONResponse({"error": "client not found"}, status_code=404)
        await db.execute("UPDATE clients SET hidden = 1, updated_at = ? WHERE id = ?", (now, client_id))
        await db.commit()

    print(f"[admin] Client {client_id} hidden (soft-delete)")
    return JSONResponse({"ok": True, "id": client_id})


async def api_admin_clients_restore(request: Request) -> JSONResponse:
    """POST /api/admin/clients/restore — restore a hidden client back to the default view. Bearer auth required."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    client_id = body.get("id")
    if not client_id:
        return JSONResponse({"error": "id required"}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()
    async with _clients_db() as db:
        cur = await db.execute("SELECT id FROM clients WHERE id = ?", (client_id,))
        row = await cur.fetchone()
        if not row:
            return JSONResponse({"error": "client not found"}, status_code=404)
        await db.execute("UPDATE clients SET hidden = 0, updated_at = ? WHERE id = ?", (now, client_id))
        await db.commit()

    print(f"[admin] Client {client_id} restored from hidden")
    return JSONResponse({"ok": True, "id": client_id})


async def serve_affiliate_portal(request: Request) -> Response:
    """GET /affiliate — redirect to canonical /refer/ page on purebrain.ai."""
    code = request.query_params.get("code", "").strip()
    redirect_url = "https://purebrain.ai/refer/"
    if code:
        redirect_url += f"?code={code}"
    from starlette.responses import RedirectResponse
    return RedirectResponse(url=redirect_url, status_code=301)


# ---------------------------------------------------------------------------
