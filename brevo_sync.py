"""
Brevo (email marketing) sync for PureBrain Portal tracking.

Pushes contact attribute updates and events to Brevo for email automation.
All calls are fire-and-forget -- Brevo sync failures never block portal operations.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional


BREVO_API_URL = "https://api.brevo.com/v3"
SHARED_CONFIG_URL = "https://cc.purebrain.ai/api/config/shared-keys"
_brevo_key_cache: Optional[str] = None


def _fetch_shared_key() -> str:
    """Fetch Brevo API key from the central config endpoint (cc.purebrain.ai)."""
    try:
        req = urllib.request.Request(SHARED_CONFIG_URL, method="GET")
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read().decode("utf-8"))
        return data.get("brevo_api_key", "")
    except Exception:
        return ""


def _get_brevo_key() -> str:
    """Get Brevo API key: .env first, then central config endpoint, cached after first call."""
    global _brevo_key_cache
    if _brevo_key_cache is not None:
        return _brevo_key_cache
    # Priority 1: .env (base64-encoded JSON format)
    encoded = os.environ.get("BREVO_API_KEY", "")
    if encoded:
        try:
            decoded = json.loads(base64.b64decode(encoded))
            key = decoded.get("api_key", "")
            if key:
                _brevo_key_cache = key
                return _brevo_key_cache
        except Exception:
            pass
    # Priority 2: Central config endpoint (cc.purebrain.ai)
    key = _fetch_shared_key()
    if key:
        _brevo_key_cache = key
        return _brevo_key_cache
    # No key available — Brevo sync disabled
    _brevo_key_cache = ""
    return _brevo_key_cache


def _brevo_request(method: str, path: str, body: Optional[dict] = None) -> dict:
    """Make a request to the Brevo API. Returns {"ok": bool, "status": int, "body": ...}."""
    key = _get_brevo_key()
    if not key:
        return {"ok": False, "status": 0, "body": "no API key configured"}

    url = f"{BREVO_API_URL}{path}"
    headers = {
        "api-key": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        resp = urllib.request.urlopen(req, timeout=10)
        resp_body = resp.read().decode("utf-8")
        return {
            "ok": True,
            "status": resp.status,
            "body": json.loads(resp_body) if resp_body else {},
        }
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")[:500]
        return {"ok": False, "status": e.code, "body": err_body}
    except Exception as e:
        return {"ok": False, "status": 0, "body": str(e)[:200]}


def sync_contact_login(email: str, login_count: int) -> dict:
    """Push login event + updated attributes to Brevo."""
    return _brevo_request("POST", "/events", {
        "event_name": "portal_login",
        "identifiers": {"email_id": email},
        "contact_properties": {
            "LAST_LOGIN_AT": time.strftime("%Y-%m-%d"),
            "LOGIN_COUNT": login_count,
        },
    })


def sync_contact_session(email: str, session_count: int) -> dict:
    """Push session start event to Brevo."""
    return _brevo_request("POST", "/events", {
        "event_name": "session_start",
        "identifiers": {"email_id": email},
        "contact_properties": {
            "SESSION_COUNT": session_count,
        },
    })


def sync_contact_payment(
    email: str,
    amount: float,
    payment_status: str,
    subscription_id: str = "",
) -> dict:
    """Push payment event to Brevo."""
    return _brevo_request("POST", "/events", {
        "event_name": "payment_completed",
        "identifiers": {"email_id": email},
        "event_properties": {
            "amount": amount,
            "subscription_id": subscription_id,
        },
        "contact_properties": {
            "PAYMENT_STATUS": payment_status,
        },
    })


def sync_contact_status(email: str, payment_status: str) -> dict:
    """Update contact payment status in Brevo (for cancellations, suspensions, etc)."""
    return _brevo_request("PUT", f"/contacts/{urllib.parse.quote(email)}", {
        "attributes": {
            "PAYMENT_STATUS": payment_status,
        },
    })


def sync_billing_date(email: str, next_billing_date: str) -> dict:
    """Update next billing date attribute in Brevo."""
    return _brevo_request("PUT", f"/contacts/{urllib.parse.quote(email)}", {
        "attributes": {
            "NEXT_BILLING_DATE": next_billing_date,
        },
    })


def ensure_brevo_attributes() -> list:
    """Create custom contact attributes in Brevo if they don't exist.

    Safe to call multiple times -- Brevo returns 400 if attribute already exists.
    Returns list of results.
    """
    attributes = [
        ("LAST_LOGIN_AT", "date"),
        ("LOGIN_COUNT", "float"),
        ("SESSION_COUNT", "float"),
        ("NEXT_BILLING_DATE", "date"),
        ("PAYMENT_STATUS", "text"),
    ]
    results = []
    for name, attr_type in attributes:
        result = _brevo_request("POST", f"/contacts/attributes/normal/{name}", {
            "type": attr_type,
        })
        results.append({"name": name, "type": attr_type, **result})
    return results


def upsert_contact(
    email: str,
    first_name: str = "",
    last_name: str = "",
    attributes: Optional[dict] = None,
    list_ids: Optional[list] = None,
) -> dict:
    """Create or update a contact in Brevo."""
    body: dict = {
        "email": email,
        "updateEnabled": True,
    }
    attrs: dict = {}
    if first_name:
        attrs["FIRSTNAME"] = first_name
    if last_name:
        attrs["LASTNAME"] = last_name
    if attributes:
        attrs.update(attributes)
    if attrs:
        body["attributes"] = attrs
    if list_ids:
        body["listIds"] = list_ids
    return _brevo_request("POST", "/contacts", body)
