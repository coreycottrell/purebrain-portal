"""Portal Google Drive Integration for the PureBrain Portal.

Extracted from portal_server.py for modularity.
Contains: Google Drive OAuth2, file listing, download, upload, folder creation,
storage quota.
"""
import base64
import json
import os
import secrets
import time
import urllib.parse
from html import escape

import httpx

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from portal_config import SCRIPT_DIR, check_auth, sanitize_error as _sanitize_error

# ---------------------------------------------------------------------------
# Google Drive Integration
# ---------------------------------------------------------------------------
GDRIVE_TOKEN_FILE = SCRIPT_DIR / ".gdrive-tokens.json"
GDRIVE_CLIENT_ID = os.environ.get(
    "GOOGLE_DRIVE_CLIENT_ID",
    "212727789757-mpva6vtr7lgvmmd6nl88gjjoe7ufoqpm.apps.googleusercontent.com",
)
GDRIVE_TOKEN_PROXY = os.environ.get(
    "GDRIVE_TOKEN_PROXY",
    "https://app.purebrain.ai/api/gdrive/refresh",
)
GDRIVE_REDIRECT_URI = os.environ.get(
    "GOOGLE_DRIVE_REDIRECT_URI",
    "https://app.purebrain.ai/api/gdrive/callback",
)
# In-memory CSRF state tokens for OAuth: {state_string: expiry_epoch}
_gdrive_oauth_states: dict = {}


def _gdrive_load_tokens() -> dict | None:
    """Load stored Google Drive tokens from disk."""
    if GDRIVE_TOKEN_FILE.exists():
        try:
            return json.loads(GDRIVE_TOKEN_FILE.read_text())
        except Exception:
            pass
    return None


def _gdrive_save_tokens(tokens: dict) -> None:
    """Persist Google Drive tokens to disk."""
    GDRIVE_TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    GDRIVE_TOKEN_FILE.chmod(0o600)


def _gdrive_clear_tokens() -> None:
    """Remove stored tokens."""
    if GDRIVE_TOKEN_FILE.exists():
        GDRIVE_TOKEN_FILE.unlink()


async def _gdrive_ensure_token() -> str | None:
    """Return a valid access_token, refreshing via the CF Worker proxy. Returns None on failure."""
    tokens = _gdrive_load_tokens()
    if not tokens:
        return None
    # Check expiry (with 60s buffer)
    if tokens.get("expires_at", 0) < time.time() + 60:
        refresh = tokens.get("refresh_token")
        if not refresh:
            return None
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    GDRIVE_TOKEN_PROXY,
                    json={"refresh_token": refresh},
                )
                if resp.status_code != 200:
                    return None
                data = resp.json()
                tokens["access_token"] = data["access_token"]
                tokens["expires_at"] = time.time() + data.get("expires_in", 3600)
                if data.get("refresh_token"):
                    tokens["refresh_token"] = data["refresh_token"]
                _gdrive_save_tokens(tokens)
        except Exception:
            return None
    return tokens.get("access_token")


async def api_gdrive_status(request: Request) -> JSONResponse:
    """Check if Google Drive is configured and connected."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not GDRIVE_CLIENT_ID:
        return JSONResponse({"connected": False, "available": False})
    tokens = _gdrive_load_tokens()
    if not tokens:
        return JSONResponse({"connected": False, "available": True})
    # Try to ensure token is valid
    access = await _gdrive_ensure_token()
    if not access:
        return JSONResponse({"connected": False, "available": True, "error": "token_expired"})
    return JSONResponse({"connected": True, "available": True})


def _extract_subdomain(request: Request) -> str:
    """Extract portal subdomain from Host header (e.g. 'myportal' from 'myportal.app.purebrain.ai')."""
    host = request.headers.get("host", "")
    # Strip port if present
    host = host.split(":")[0]
    suffix = ".app.purebrain.ai"
    if host.endswith(suffix):
        return host[: -len(suffix)]
    # Fallback for local dev -- use env var or 'local'
    return os.environ.get("PORTAL_SUBDOMAIN", "local")


async def api_gdrive_auth_url(request: Request) -> JSONResponse:
    """Generate the Google OAuth2 authorization URL."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not GDRIVE_CLIENT_ID:
        return JSONResponse({"error": "Google Drive not configured"}, status_code=400)
    # Generate CSRF state token
    csrf_token = secrets.token_urlsafe(32)
    # Encode subdomain into state so the Cloudflare Worker can route the callback
    subdomain = _extract_subdomain(request)
    state = f"{subdomain}:{csrf_token}"
    _gdrive_oauth_states[csrf_token] = time.time() + 600  # 10 min expiry
    # Clean expired states
    now = time.time()
    expired = [k for k, v in _gdrive_oauth_states.items() if v < now]
    for k in expired:
        _gdrive_oauth_states.pop(k, None)

    url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urllib.parse.urlencode({
            "client_id": GDRIVE_CLIENT_ID,
            "redirect_uri": GDRIVE_REDIRECT_URI,
            "response_type": "code",
            "scope": "https://www.googleapis.com/auth/drive",
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        })
    )
    return JSONResponse({"url": url})


async def api_gdrive_callback(request: Request) -> Response:
    """Handle Google OAuth2 callback -- receive tokens forwarded by CF Worker."""
    state = request.query_params.get("state", "")
    tokens_b64 = request.query_params.get("tokens", "")
    error = request.query_params.get("error", "")

    if error:
        return Response(
            content=f"<html><body><script>window.opener.postMessage({{type:'gdrive-error',error:'{escape(error)}'}},'*');window.close();</script></body></html>",
            media_type="text/html",
        )

    # Extract CSRF token from compound state "subdomain:csrf_token"
    # The Cloudflare Worker already used the subdomain to route here;
    # we only need the csrf_token portion for validation.
    csrf_token = state
    colon_idx = state.find(":")
    if colon_idx != -1:
        csrf_token = state[colon_idx + 1:]

    # Validate CSRF state
    expected_expiry = _gdrive_oauth_states.pop(csrf_token, None)
    if not expected_expiry or expected_expiry < time.time():
        return Response(
            content="<html><body><script>window.opener.postMessage({type:'gdrive-error',error:'invalid_state'},'*');window.close();</script></body></html>",
            media_type="text/html",
        )

    # Handle raw code (Worker passthrough) or base64 tokens (Worker exchange)
    auth_code = request.query_params.get("code", "")

    if auth_code:
        # Direct exchange: Worker forwarded the raw code, portal exchanges it
        client_secret = os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET", "")
        if not client_secret:
            return Response(
                content="<html><body><script>window.opener.postMessage({type:'gdrive-error',error:'no_client_secret'},'*');window.close();</script></body></html>",
                media_type="text/html",
            )
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "code": auth_code,
                        "client_id": GDRIVE_CLIENT_ID,
                        "client_secret": client_secret,
                        "redirect_uri": GDRIVE_REDIRECT_URI,
                        "grant_type": "authorization_code",
                    },
                )
                import logging
                logging.info(f"[GDRIVE] Direct exchange: status={resp.status_code} body={resp.text[:300]}")
                if resp.status_code != 200:
                    err_detail = resp.text[:200]
                    return Response(
                        content=f"<html><body><script>window.opener.postMessage({{type:'gdrive-error',error:'exchange_failed: {escape(err_detail)}'}},'*');window.close();</script></body></html>",
                        media_type="text/html",
                    )
                data = resp.json()
                tokens = {
                    "access_token": data["access_token"],
                    "refresh_token": data.get("refresh_token", ""),
                    "expires_at": time.time() + data.get("expires_in", 3600),
                    "token_type": data.get("token_type", "Bearer"),
                }
                _gdrive_save_tokens(tokens)
        except Exception as e:
            return Response(
                content=f"<html><body><script>window.opener.postMessage({{type:'gdrive-error',error:'exchange_exception: {escape(str(e)[:100])}'}},'*');window.close();</script></body></html>",
                media_type="text/html",
            )
    elif tokens_b64:
        # Decode tokens forwarded by the CF Worker (base64-encoded JSON)
        try:
            data = json.loads(base64.b64decode(tokens_b64))
            tokens = {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", ""),
                "expires_at": time.time() + data.get("expires_in", 3600),
                "token_type": data.get("token_type", "Bearer"),
            }
            _gdrive_save_tokens(tokens)
        except Exception:
            return Response(
                content="<html><body><script>window.opener.postMessage({type:'gdrive-error',error:'token_decode_failed'},'*');window.close();</script></body></html>",
                media_type="text/html",
            )
    else:
        return Response(
            content="<html><body><script>window.opener.postMessage({type:'gdrive-error',error:'no_tokens_or_code'},'*');window.close();</script></body></html>",
            media_type="text/html",
        )

    return Response(
        content="<html><body><script>window.opener.postMessage({type:'gdrive-connected'},'*');window.close();</script></body></html>",
        media_type="text/html",
    )


async def api_gdrive_disconnect(request: Request) -> JSONResponse:
    """Clear stored Google Drive tokens."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    _gdrive_clear_tokens()
    return JSONResponse({"ok": True})


async def api_gdrive_files(request: Request) -> JSONResponse:
    """List files in a Google Drive folder."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    access = await _gdrive_ensure_token()
    if not access:
        return JSONResponse({"error": "not_connected"}, status_code=401)

    folder_id = request.query_params.get("folder_id", "root")
    page_token = request.query_params.get("page_token", "")
    query_type = request.query_params.get("type", "")  # "recent" or "shared"

    params: dict = {
        "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime,iconLink)",
        "orderBy": "folder,name",
        "pageSize": "100",
    }
    if query_type == "recent":
        params["orderBy"] = "viewedByMeTime desc"
        params["pageSize"] = "50"
    elif query_type == "shared":
        params["q"] = "sharedWithMe=true and trashed=false"
        params["pageSize"] = "50"
    else:
        params["q"] = f"'{folder_id}' in parents and trashed=false"

    if page_token:
        params["pageToken"] = page_token

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://www.googleapis.com/drive/v3/files",
                params=params,
                headers={"Authorization": f"Bearer {access}"},
            )
            if resp.status_code == 401:
                _gdrive_clear_tokens()
                return JSONResponse({"error": "token_expired"}, status_code=401)
            if resp.status_code != 200:
                return JSONResponse({"error": f"drive_api_error: {resp.status_code}"}, status_code=502)
            data = resp.json()
            return JSONResponse({
                "files": data.get("files", []),
                "nextPageToken": data.get("nextPageToken"),
            })
    except Exception as e:
        return JSONResponse({"error": _sanitize_error(e, "git status")}, status_code=500)


async def api_gdrive_download(request: Request) -> Response:
    """Proxy download a file from Google Drive."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    access = await _gdrive_ensure_token()
    if not access:
        return JSONResponse({"error": "not_connected"}, status_code=401)

    file_id = request.path_params.get("file_id", "")
    if not file_id:
        return JSONResponse({"error": "missing file_id"}, status_code=400)

    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            # First get file metadata for the name
            meta_resp = await client.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}",
                params={"fields": "name,mimeType"},
                headers={"Authorization": f"Bearer {access}"},
            )
            filename = "download"
            if meta_resp.status_code == 200:
                meta = meta_resp.json()
                filename = meta.get("name", "download")

            # Download content
            resp = await client.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}",
                params={"alt": "media"},
                headers={"Authorization": f"Bearer {access}"},
            )
            if resp.status_code == 401:
                _gdrive_clear_tokens()
                return JSONResponse({"error": "token_expired"}, status_code=401)
            if resp.status_code != 200:
                return JSONResponse({"error": f"download_failed: {resp.status_code}"}, status_code=502)

            content_type = resp.headers.get("content-type", "application/octet-stream")
            safe_filename = filename.replace('"', '\\"')
            return Response(
                content=resp.content,
                media_type=content_type,
                headers={"Content-Disposition": f'attachment; filename="{safe_filename}"'},
            )
    except Exception as e:
        return JSONResponse({"error": _sanitize_error(e, "git log")}, status_code=500)


async def api_gdrive_upload(request: Request) -> JSONResponse:
    """Upload a file to Google Drive."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    access = await _gdrive_ensure_token()
    if not access:
        return JSONResponse({"error": "not_connected"}, status_code=401)

    folder_id = request.query_params.get("folder_id", "root")

    form = await request.form()
    upload_file = form.get("file")
    if not upload_file:
        return JSONResponse({"error": "no file"}, status_code=400)

    file_content = await upload_file.read()
    file_name = upload_file.filename or "untitled"
    content_type = upload_file.content_type or "application/octet-stream"

    try:
        # Use multipart upload
        metadata = json.dumps({"name": file_name, "parents": [folder_id]})
        boundary = "---gdrive_upload_boundary"
        body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{metadata}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8") + file_content + f"\r\n--{boundary}--\r\n".encode("utf-8")

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
                content=body,
                headers={
                    "Authorization": f"Bearer {access}",
                    "Content-Type": f"multipart/related; boundary={boundary}",
                },
            )
            if resp.status_code == 401:
                _gdrive_clear_tokens()
                return JSONResponse({"error": "token_expired"}, status_code=401)
            if resp.status_code not in (200, 201):
                return JSONResponse({"error": f"upload_failed: {resp.status_code}"}, status_code=502)
            data = resp.json()
            return JSONResponse({"ok": True, "file": {"id": data.get("id"), "name": data.get("name")}})
    except Exception as e:
        return JSONResponse({"error": _sanitize_error(e, "git diff")}, status_code=500)


async def api_gdrive_create_folder(request: Request) -> JSONResponse:
    """Create a new folder in Google Drive."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    access = await _gdrive_ensure_token()
    if not access:
        return JSONResponse({"error": "not_connected"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    name = body.get("name", "")
    parent_id = body.get("parent_id", "root")
    if not name:
        return JSONResponse({"error": "missing name"}, status_code=400)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://www.googleapis.com/drive/v3/files",
                json={
                    "name": name,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent_id],
                },
                headers={
                    "Authorization": f"Bearer {access}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 401:
                _gdrive_clear_tokens()
                return JSONResponse({"error": "token_expired"}, status_code=401)
            if resp.status_code not in (200, 201):
                return JSONResponse({"error": f"create_failed: {resp.status_code}"}, status_code=502)
            data = resp.json()
            return JSONResponse({"ok": True, "folder": {"id": data.get("id"), "name": data.get("name")}})
    except Exception as e:
        return JSONResponse({"error": _sanitize_error(e, "git commit")}, status_code=500)


async def api_gdrive_about(request: Request) -> JSONResponse:
    """Get Drive storage quota info."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    access = await _gdrive_ensure_token()
    if not access:
        return JSONResponse({"error": "not_connected"}, status_code=401)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://www.googleapis.com/drive/v3/about",
                params={"fields": "storageQuota,user"},
                headers={"Authorization": f"Bearer {access}"},
            )
            if resp.status_code != 200:
                return JSONResponse({"error": f"api_error: {resp.status_code}"}, status_code=502)
            return JSONResponse(resp.json())
    except Exception as e:
        return JSONResponse({"error": _sanitize_error(e, "git push")}, status_code=500)
