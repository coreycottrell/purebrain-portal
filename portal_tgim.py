"""Portal TGIM Proxy — integration with TGIM v4.x for the PureBrain Portal.

Extracted from portal_server.py for modularity.
Contains: TGIM reverse proxy (Russell Korus / Parallax + Keel),
service key auth, user email passthrough.
"""
import os
from pathlib import Path

import httpx

from starlette.requests import Request
from starlette.responses import JSONResponse

from portal_config import check_auth, sanitize_error as _sanitize_error

# ---------------------------------------------------------------------------
# TGIM proxy — integration with TGIM v4.x (Russell Korus / Parallax + Keel)
# Auth: Option B — service key + user email passthrough
# ---------------------------------------------------------------------------
def _read_tgim_env(key: str, default: str = "") -> str:
    """Read from os.environ first, then fall back to ~/.env file."""
    val = os.environ.get(key)
    if val:
        return val
    env_path = Path.home() / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip()
    return default


TGIM_BACKEND_URL = _read_tgim_env("TGIM_BACKEND_URL", "http://157.230.191.4:8089")
TGIM_SERVICE_KEY = _read_tgim_env("TGIM_SERVICE_KEY")
TGIM_DEFAULT_USER_EMAIL = _read_tgim_env("TGIM_DEFAULT_USER_EMAIL", "")


def _tgim_headers(user_email: str | None = None) -> dict:
    return {
        "X-TGIM-Service-Key": TGIM_SERVICE_KEY,
        "X-TGIM-User": user_email or TGIM_DEFAULT_USER_EMAIL,
        "Content-Type": "application/json",
    }


def _tgim_upstream_url(path: str) -> str:
    trimmed = path.removeprefix("/api/tgim/").removeprefix("/api/tgim")
    return f"{TGIM_BACKEND_URL}/api/v1/{trimmed}"


async def api_tgim_proxy(request: Request) -> JSONResponse:
    """Generic proxy for /api/tgim/* -> TGIM backend /api/v1/*."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not TGIM_SERVICE_KEY:
        return JSONResponse({"error": "Service not available"}, status_code=503)

    upstream_url = _tgim_upstream_url(request.url.path)
    headers = _tgim_headers()
    params = dict(request.query_params)
    method = request.method.upper()

    body = None
    if method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.json()
        except Exception:
            body = None

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.request(
                method=method, url=upstream_url,
                headers=headers, params=params,
                json=body if body is not None else None,
            )
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text}
            return JSONResponse(data, status_code=resp.status_code)
    except httpx.TimeoutException:
        print(f"[tgim] Timeout proxying {method} {upstream_url}")
        return JSONResponse({"error": "TGIM backend timeout"}, status_code=504)
    except Exception as e:
        print(f"[tgim] Proxy error: {e}")
        return JSONResponse({"error": _sanitize_error(e, "proxy")}, status_code=502)
