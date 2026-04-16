#!/usr/bin/env python3
"""
PayPal Subscription Sync — One-shot + scheduled background tool.

Pulls real subscriber data from the PayPal Subscriptions API and patches
the clients.db so payment_status, paypal_subscription_id, and total_paid
are accurate.

Usage:
    python3 paypal_sync_subscriptions.py          # one-shot sync, prints report
    python3 paypal_sync_subscriptions.py --dry-run # preview changes without writing

The background loop version is embedded inside portal_server.py as
_paypal_subscription_sync_loop(), started from _startup().
"""

import argparse
import base64
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
CLIENTS_DB = SCRIPT_DIR / "clients.db"
PAYMENTS_LOG = Path("/home/jared/projects/AI-CIV/aether/logs/purebrain_payments.jsonl")
PAY_TEST_LOG = Path("/home/jared/projects/AI-CIV/aether/logs/purebrain_pay_test.jsonl")
SPOTS_STATE = Path("/home/jared/projects/AI-CIV/aether/logs/spots_state.json")
ENV_FILE = Path("/home/jared/projects/AI-CIV/aether/.env")

# Plan ID → tier mapping
# P-2SA65600MT088594TNGLTFKY = $149/mo = "Awakened" plan
# P-8AU4270420374002JNGY3VYQ = $74.50/mo = "Insiders" plan (original)
# P-6C122944BP930974LNGVP6PQ = $74.50/mo = "Insiders" plan (confirmed 2026-03-19 via Hannah Khokhar)
PLAN_TIER_MAP = {
    "P-2SA65600MT088594TNGLTFKY": "Awakened",
    "P-8AU4270420374002JNGY3VYQ": "Insiders",
    "P-6C122944BP930974LNGVP6PQ": "Insiders",
}

# Tier → monthly price (for total_paid calculation when last_payment is 0)
TIER_PRICES = {
    "Insiders": 74.5,
    "Awakened": 149.0,
    "Partnered": 579.0,
    "Unified": 1089.0,
}


def _load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        with ENV_FILE.open() as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
    # Also read from os.environ (in case running inside portal process)
    env.update(os.environ)
    return env


def _paypal_base(sandbox: bool) -> str:
    return "https://api-m.sandbox.paypal.com" if sandbox else "https://api-m.paypal.com"


def _get_access_token(client_id: str, secret: str, sandbox: bool) -> str | None:
    base = _paypal_base(sandbox)
    url = f"{base}/v1/oauth2/token"
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    creds = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            return body.get("access_token")
    except Exception as e:
        print(f"[paypal-sync] Failed to get access token: {e}", file=sys.stderr)
        return None


def _fetch_subscription(sub_id: str, token: str, base: str) -> dict | None:
    url = f"{base}/v1/billing/subscriptions/{sub_id}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # subscription doesn't exist in this environment
        body = e.read().decode(errors="replace")
        print(f"[paypal-sync] HTTP {e.code} for {sub_id}: {body[:200]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[paypal-sync] Error fetching {sub_id}: {e}", file=sys.stderr)
        return None


def _collect_subscription_ids_from_logs() -> dict[str, dict]:
    """Collect subscription IDs from payments.jsonl and pay_test.jsonl.
    Returns {sub_id: {email, tier, ...}}."""
    subs: dict[str, dict] = {}

    # payments.jsonl — may have blank payerEmail but known orderId
    if PAYMENTS_LOG.exists():
        with PAYMENTS_LOG.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                order_id = (d.get("orderId") or "").strip()
                if not order_id.startswith("I-"):
                    continue
                subs.setdefault(order_id, {
                    "email": (d.get("payerEmail") or "").strip().lower(),
                    "tier": (d.get("tier") or "").strip(),
                    "amount": float(d.get("amount") or 0),
                })

    # pay_test.jsonl — has paypalSubscriptionId field
    if PAY_TEST_LOG.exists():
        with PAY_TEST_LOG.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                order_id = (d.get("orderId") or d.get("paypalSubscriptionId") or "").strip()
                if not order_id.startswith("I-"):
                    continue
                subs.setdefault(order_id, {
                    "email": (d.get("email") or d.get("payerEmail") or "").strip().lower(),
                    "tier": (d.get("tier") or "").strip(),
                    "amount": 0.0,
                })

    return subs


def _collect_subscription_ids_from_spots_state() -> dict[str, dict]:
    """Pull subscription IDs logged in spots_state.json (claimed_orders list).
    Returns {sub_id: {tier, ...}}."""
    subs: dict[str, dict] = {}
    if not SPOTS_STATE.exists():
        return subs
    try:
        with SPOTS_STATE.open() as f:
            data = json.load(f)
        for order in data.get("claimed_orders", []):
            order_id = (order.get("order_id") or "").strip()
            if not order_id.startswith("I-"):
                continue
            subs.setdefault(order_id, {
                "email": (order.get("payer_email") or "").strip().lower(),
                "tier": (order.get("tier") or "").strip(),
                "amount": 0.0,
            })
    except Exception:
        pass
    return subs


def _collect_subscription_ids_from_db() -> dict[str, str]:
    """Returns {sub_id: email} for all clients that have a subscription ID."""
    conn = sqlite3.connect(str(CLIENTS_DB))
    cur = conn.cursor()
    cur.execute(
        "SELECT paypal_subscription_id, email FROM clients "
        "WHERE paypal_subscription_id != '' AND paypal_subscription_id IS NOT NULL"
    )
    rows = {row[0]: row[1] for row in cur.fetchall()}
    conn.close()
    return rows


def _get_all_clients() -> list[dict]:
    """Return all client rows as dicts."""
    conn = sqlite3.connect(str(CLIENTS_DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT id, email, tier, payment_status, paypal_subscription_id, total_paid, payment_count "
        "FROM clients"
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def run_sync(dry_run: bool = False) -> dict:
    """Main sync function. Returns summary dict."""
    env = _load_env()
    # Always use LIVE PayPal credentials — all real customer subscriptions are on live PayPal.
    # Never use sandbox for the production sync; sandbox customers don't exist in clients.db.
    live_client_id = env.get("PAYPAL_CLIENT_ID", "")
    live_secret = env.get("PAYPAL_SECRET", "")

    if not live_client_id or not live_secret:
        return {"error": "Missing PAYPAL_CLIENT_ID / PAYPAL_SECRET in .env", "updated": 0, "skipped": 0}

    token = _get_access_token(live_client_id, live_secret, False)
    base = _paypal_base(False)

    if not token:
        return {"error": "Could not obtain PayPal LIVE access token", "updated": 0, "skipped": 0}

    # Gather all subscription IDs we know about
    log_subs = _collect_subscription_ids_from_logs()
    db_subs = _collect_subscription_ids_from_db()
    spots_subs = _collect_subscription_ids_from_spots_state()
    all_sub_ids: set[str] = set(log_subs.keys()) | set(db_subs.keys()) | set(spots_subs.keys())

    print(f"[paypal-sync] Found {len(all_sub_ids)} unique subscription IDs to check")

    # Fetch subscription details from PayPal
    paypal_data: dict[str, dict] = {}  # sub_id -> paypal subscription object
    for sub_id in sorted(all_sub_ids):
        data = _fetch_subscription(sub_id, token, base)
        if data:
            paypal_data[sub_id] = data
            subscriber = data.get("subscriber", {})
            print(f"  {sub_id}: status={data.get('status')} email={subscriber.get('email_address')}")
        else:
            print(f"  {sub_id}: not found / 404")

    # Build email -> subscription mapping from PayPal responses
    # email -> best subscription data
    email_to_sub: dict[str, dict] = {}
    for sub_id, data in paypal_data.items():
        subscriber = data.get("subscriber", {})
        paypal_email = (subscriber.get("email_address") or "").strip().lower()
        if not paypal_email:
            continue

        billing = data.get("billing_info", {})
        last_payment = billing.get("last_payment", {})
        amount = float((last_payment.get("amount") or {}).get("value") or 0)
        last_payment_time = last_payment.get("time", "")
        next_billing_time = billing.get("next_billing_time", "")

        plan_id = data.get("plan_id", "")
        status = data.get("status", "")

        tier_from_plan = PLAN_TIER_MAP.get(plan_id, "")

        existing = email_to_sub.get(paypal_email)
        if existing is None or last_payment_time > existing.get("last_payment_time", ""):
            email_to_sub[paypal_email] = {
                "sub_id": sub_id,
                "status": status,
                "amount": amount,
                "last_payment_time": last_payment_time,
                "next_billing_time": next_billing_time,
                "tier_from_plan": tier_from_plan,
                "given_name": subscriber.get("name", {}).get("given_name", ""),
                "surname": subscriber.get("name", {}).get("surname", ""),
            }

    # Also check log-sourced sub IDs where PayPal returned data by cross-referencing
    # some sub IDs with emails we already know from pay_test log
    for sub_id, log_info in log_subs.items():
        email = log_info.get("email", "")
        if email and sub_id in paypal_data:
            data = paypal_data[sub_id]
            subscriber = data.get("subscriber", {})
            paypal_email = (subscriber.get("email_address") or "").strip().lower()
            if paypal_email and paypal_email != email:
                # Trust PayPal email over log email
                email = paypal_email
            if email and email not in email_to_sub:
                billing = data.get("billing_info", {})
                last_payment = billing.get("last_payment", {})
                amount = float((last_payment.get("amount") or {}).get("value") or 0)
                last_payment_time = last_payment.get("time", "")
                next_billing_time = billing.get("next_billing_time", "")
                plan_id = data.get("plan_id", "")
                tier_from_plan = PLAN_TIER_MAP.get(plan_id, "")
                email_to_sub[email] = {
                    "sub_id": sub_id,
                    "status": data.get("status", ""),
                    "amount": amount,
                    "last_payment_time": last_payment_time,
                    "next_billing_time": next_billing_time,
                    "tier_from_plan": tier_from_plan,
                    "given_name": subscriber.get("name", {}).get("given_name", ""),
                    "surname": subscriber.get("name", {}).get("surname", ""),
                }

    print(f"\n[paypal-sync] Resolved {len(email_to_sub)} email->subscription mappings")

    # Load all clients and patch
    clients = _get_all_clients()
    updated = 0
    skipped = 0
    changes: list[dict] = []

    now_iso = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(str(CLIENTS_DB))
    conn.execute("PRAGMA journal_mode = WAL")
    cur = conn.cursor()

    for client in clients:
        email = client["email"].strip().lower()
        sub_data = email_to_sub.get(email)

        if sub_data is None:
            skipped += 1
            continue

        status = sub_data["status"]
        sub_id = sub_data["sub_id"]
        amount = sub_data["amount"]
        tier_from_plan = sub_data["tier_from_plan"]
        next_billing = sub_data.get("next_billing_time", "")

        # Determine new payment_status
        if status == "ACTIVE":
            new_payment_status = "subscription_active"
        elif status in ("CANCELLED", "EXPIRED", "SUSPENDED"):
            new_payment_status = "subscription_cancelled"
        else:
            new_payment_status = "paid"

        # Use amount from PayPal; fall back to tier price if 0
        if amount <= 0 and tier_from_plan:
            amount = TIER_PRICES.get(tier_from_plan, 0.0)
        if amount <= 0 and client.get("tier"):
            amount = TIER_PRICES.get(client["tier"], 0.0)

        # Determine new payment count (at least 1 if we have a subscription)
        new_payment_count = max(client["payment_count"], 1)

        # For total_paid: use existing if it's already correct; otherwise use amount
        new_total_paid = client["total_paid"]
        if new_total_paid <= 0 and amount > 0:
            new_total_paid = amount

        # Determine tier update
        new_tier = client["tier"]
        if tier_from_plan and (not new_tier or new_tier in ("unknown", "")):
            new_tier = tier_from_plan

        # Check if anything actually changes
        needs_update = (
            client["payment_status"] != new_payment_status
            or (not client["paypal_subscription_id"] and sub_id)
            or client["total_paid"] != new_total_paid
            or client["payment_count"] != new_payment_count
            or client["tier"] != new_tier
        )

        if not needs_update:
            skipped += 1
            continue

        change = {
            "email": email,
            "payment_status": f"{client['payment_status']} -> {new_payment_status}",
            "paypal_subscription_id": f"{client['paypal_subscription_id']!r} -> {sub_id!r}",
            "total_paid": f"{client['total_paid']} -> {new_total_paid}",
            "payment_count": f"{client['payment_count']} -> {new_payment_count}",
            "tier": f"{client['tier']} -> {new_tier}",
        }
        changes.append(change)

        if not dry_run:
            cur.execute(
                """UPDATE clients SET
                   payment_status = ?,
                   paypal_subscription_id = CASE WHEN ? != '' THEN ? ELSE paypal_subscription_id END,
                   total_paid = ?,
                   payment_count = ?,
                   tier = ?,
                   updated_at = ?
                   WHERE email = ? COLLATE NOCASE""",
                (
                    new_payment_status,
                    sub_id, sub_id,
                    new_total_paid,
                    new_payment_count,
                    new_tier,
                    now_iso,
                    email,
                ),
            )
            # Store next_billing_date if the column exists and we have data
            if next_billing:
                try:
                    cur.execute(
                        "UPDATE clients SET next_billing_date = ? WHERE email = ? COLLATE NOCASE",
                        (next_billing, email),
                    )
                except Exception:
                    pass  # Column may not exist yet on older schemas
        updated += 1

    if not dry_run:
        conn.commit()
    conn.close()

    print(f"\n[paypal-sync] Results: {updated} updated, {skipped} skipped (no change or no PayPal data)")
    if changes:
        print("\nChanges made:")
        for c in changes:
            print(f"  {c['email']}")
            print(f"    payment_status: {c['payment_status']}")
            print(f"    sub_id:         {c['paypal_subscription_id']}")
            print(f"    total_paid:     {c['total_paid']}")
            print(f"    tier:           {c['tier']}")
            print()

    return {
        "updated": updated,
        "skipped": skipped,
        "changes": changes,
        "dry_run": dry_run,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync PayPal subscription data into clients.db")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing to DB")
    args = parser.parse_args()
    result = run_sync(dry_run=args.dry_run)
    if "error" in result:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        sys.exit(1)
