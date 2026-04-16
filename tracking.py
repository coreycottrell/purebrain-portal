"""
User tracking module for PureBrain Portal.

Handles login timestamps, session counting, PayPal webhook processing,
and subscription renewal date tracking.

All functions use synchronous sqlite3 for simplicity in the tracking module.
The portal_server.py integration uses aiosqlite wrappers that call these
functions in a thread pool.

# TODO: Push to Brevo when API key is configured
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional


# ── Session gap threshold (seconds) ─────────────────────────────────────────
SESSION_GAP_SECONDS = 300  # 5 minutes
ACTIVITY_THROTTLE_SECONDS = 60  # 1 minute


# ── DB Migration ─────────────────────────────────────────────────────────────

def ensure_tracking_columns(db_path: str) -> None:
    """Add tracking columns to clients table if they don't exist.

    Safe to call multiple times (idempotent).
    """
    conn = sqlite3.connect(db_path)
    columns_to_add = [
        ("last_login_at", "TEXT NOT NULL DEFAULT ''"),
        ("login_count", "INTEGER NOT NULL DEFAULT 0"),
        ("session_count", "INTEGER NOT NULL DEFAULT 0"),
        ("next_billing_date", "TEXT NOT NULL DEFAULT ''"),
    ]
    for col_name, col_def in columns_to_add:
        try:
            conn.execute(f"ALTER TABLE clients ADD COLUMN {col_name} {col_def}")
        except Exception:
            pass  # Column already exists

    # Create webhook log table
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


# ── Login Tracking ───────────────────────────────────────────────────────────

def record_login(db_path: str, email: str) -> bool:
    """Record a login event for the given email.

    Updates last_login_at and increments login_count.
    Returns True if client found and updated, False otherwise.

    # TODO: Push to Brevo when API key is configured
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        """UPDATE clients SET
           last_login_at = ?,
           login_count = login_count + 1,
           updated_at = ?
           WHERE email = ? COLLATE NOCASE""",
        (now, now, email),
    )
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    return updated


# ── Session / Activity Tracking ──────────────────────────────────────────────

def record_activity(
    db_path: str,
    email: str,
    force_new_session: bool = False,
) -> dict:
    """Record user activity, potentially starting a new session.

    - If >SESSION_GAP_SECONDS since last activity, increment session_count
    - Update last_active_at (throttled to once per ACTIVITY_THROTTLE_SECONDS)

    Returns dict with keys: new_session (bool), throttled (bool), updated (bool)

    # TODO: Push to Brevo when API key is configured
    """
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT last_active_at, session_count FROM clients WHERE email = ? COLLATE NOCASE",
        (email,),
    )
    row = cur.fetchone()
    if row is None:
        conn.close()
        return {"new_session": False, "throttled": False, "updated": False}

    last_active_str = row["last_active_at"] or ""
    session_count = row["session_count"] or 0

    # Determine if this is a new session
    new_session = force_new_session
    if not new_session and last_active_str:
        try:
            last_active = datetime.fromisoformat(last_active_str)
            gap = (now - last_active).total_seconds()
            if gap > SESSION_GAP_SECONDS:
                new_session = True
        except (ValueError, TypeError):
            new_session = True
    elif not last_active_str:
        new_session = True

    # Throttle last_active_at updates
    throttled = False
    if last_active_str and not new_session:
        try:
            last_active = datetime.fromisoformat(last_active_str)
            since_last = (now - last_active).total_seconds()
            if since_last < ACTIVITY_THROTTLE_SECONDS:
                throttled = True
        except (ValueError, TypeError):
            pass

    if throttled:
        conn.close()
        return {"new_session": False, "throttled": True, "updated": False}

    # Build update
    if new_session:
        conn.execute(
            """UPDATE clients SET
               last_active_at = ?,
               session_count = ?,
               updated_at = ?
               WHERE email = ? COLLATE NOCASE""",
            (now_iso, session_count + 1, now_iso, email),
        )
    else:
        conn.execute(
            """UPDATE clients SET
               last_active_at = ?,
               updated_at = ?
               WHERE email = ? COLLATE NOCASE""",
            (now_iso, now_iso, email),
        )

    conn.commit()
    conn.close()
    return {"new_session": new_session, "throttled": False, "updated": True}


# ── PayPal Webhook ───────────────────────────────────────────────────────────

def log_webhook_event(db_path: str, event: dict) -> int:
    """Log a raw PayPal webhook event to the database.

    Returns the row id of the inserted log entry.
    """
    now = datetime.now(timezone.utc).isoformat()
    event_id = event.get("id", "")
    event_type = event.get("event_type", "UNKNOWN")
    resource = event.get("resource", {})
    resource_id = resource.get("id", "")

    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        """INSERT INTO paypal_webhook_log
           (event_id, event_type, resource_id, payload, received_at, processed)
           VALUES (?, ?, ?, ?, ?, 0)""",
        (event_id, event_type, resource_id, json.dumps(event), now),
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def process_webhook_event(db_path: str, event: dict) -> dict:
    """Process a PayPal webhook event and update clients.db accordingly.

    Supported event types:
    - BILLING.SUBSCRIPTION.CANCELLED
    - BILLING.SUBSCRIPTION.SUSPENDED
    - PAYMENT.SALE.COMPLETED
    - BILLING.SUBSCRIPTION.PAYMENT.FAILED
    - BILLING.SUBSCRIPTION.ACTIVATED
    - BILLING.SUBSCRIPTION.UPDATED

    Returns dict with: processed (bool), event_type (str), detail (str)

    # TODO: Push to Brevo when API key is configured
    """
    event_type = event.get("event_type", "")
    resource = event.get("resource", {})
    resource_id = resource.get("id", "")
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(db_path)

    result = {"processed": False, "event_type": event_type, "detail": ""}

    if event_type == "BILLING.SUBSCRIPTION.CANCELLED":
        # resource_id is the subscription ID (I-XXXX)
        sub_id = resource_id
        cur = conn.execute(
            """UPDATE clients SET
               payment_status = 'subscription_cancelled',
               updated_at = ?
               WHERE paypal_subscription_id = ?""",
            (now, sub_id),
        )
        result["processed"] = True
        result["detail"] = f"Cancelled subscription {sub_id}, {cur.rowcount} row(s) updated"

    elif event_type == "BILLING.SUBSCRIPTION.SUSPENDED":
        sub_id = resource_id
        cur = conn.execute(
            """UPDATE clients SET
               payment_status = 'subscription_suspended',
               updated_at = ?
               WHERE paypal_subscription_id = ?""",
            (now, sub_id),
        )
        result["processed"] = True
        result["detail"] = f"Suspended subscription {sub_id}, {cur.rowcount} row(s) updated"

    elif event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
        sub_id = resource_id
        cur = conn.execute(
            """UPDATE clients SET
               payment_status = 'subscription_active',
               updated_at = ?
               WHERE paypal_subscription_id = ?""",
            (now, sub_id),
        )
        result["processed"] = True
        result["detail"] = f"Activated subscription {sub_id}, {cur.rowcount} row(s) updated"

    elif event_type == "PAYMENT.SALE.COMPLETED":
        billing_agreement_id = resource.get("billing_agreement_id", "")
        amount_val = resource.get("amount", {}).get("total", "0")
        try:
            amount = float(amount_val)
        except (ValueError, TypeError):
            amount = 0.0

        if billing_agreement_id:
            cur = conn.execute(
                """UPDATE clients SET
                   total_paid = total_paid + ?,
                   payment_count = payment_count + 1,
                   payment_status = 'subscription_active',
                   updated_at = ?
                   WHERE paypal_subscription_id = ?""",
                (amount, now, billing_agreement_id),
            )
            result["processed"] = True
            result["detail"] = (
                f"Payment ${amount} for {billing_agreement_id}, "
                f"{cur.rowcount} row(s) updated"
            )
        else:
            result["detail"] = "No billing_agreement_id in PAYMENT.SALE.COMPLETED"

    elif event_type == "BILLING.SUBSCRIPTION.PAYMENT.FAILED":
        sub_id = resource_id
        cur = conn.execute(
            """UPDATE clients SET
               payment_status = 'payment_failed',
               updated_at = ?
               WHERE paypal_subscription_id = ?""",
            (now, sub_id),
        )
        result["processed"] = True
        result["detail"] = f"Payment failed for {sub_id}, {cur.rowcount} row(s) updated"

    else:
        result["detail"] = f"Unhandled event type: {event_type}"

    conn.commit()
    conn.close()
    return result


# ── Renewal Date Tracking ────────────────────────────────────────────────────

def update_next_billing_date(
    db_path: str,
    email: str,
    next_billing_date: str,
) -> bool:
    """Store the next billing date for a client.

    Returns True if client found and updated.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        """UPDATE clients SET
           next_billing_date = ?,
           updated_at = ?
           WHERE email = ? COLLATE NOCASE""",
        (next_billing_date, now, email),
    )
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    return updated


# ── Computed Stats ───────────────────────────────────────────────────────────

def get_tracking_stats(db_path: str, email: str) -> dict:
    """Get computed tracking stats for a client.

    Returns dict with:
    - days_since_login: int (-1 if never logged in)
    - days_since_active: int (-1 if never active)
    - login_count: int
    - session_count: int
    - next_billing_date: str
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT last_login_at, last_active_at, login_count, session_count,
                  next_billing_date
           FROM clients WHERE email = ? COLLATE NOCASE""",
        (email,),
    )
    row = cur.fetchone()
    conn.close()

    if row is None:
        return {
            "days_since_login": -1,
            "days_since_active": -1,
            "login_count": 0,
            "session_count": 0,
            "next_billing_date": "",
        }

    now = datetime.now(timezone.utc)

    # Compute days_since_login
    last_login_str = row["last_login_at"] or ""
    if last_login_str:
        try:
            last_login = datetime.fromisoformat(last_login_str)
            days_since_login = (now - last_login).days
        except (ValueError, TypeError):
            days_since_login = -1
    else:
        days_since_login = -1

    # Compute days_since_active
    last_active_str = row["last_active_at"] or ""
    if last_active_str:
        try:
            last_active = datetime.fromisoformat(last_active_str)
            days_since_active = (now - last_active).days
        except (ValueError, TypeError):
            days_since_active = -1
    else:
        days_since_active = -1

    return {
        "days_since_login": days_since_login,
        "days_since_active": days_since_active,
        "login_count": row["login_count"] or 0,
        "session_count": row["session_count"] or 0,
        "next_billing_date": row["next_billing_date"] or "",
    }
