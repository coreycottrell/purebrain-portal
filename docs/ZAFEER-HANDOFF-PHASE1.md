# Command Center Phase 1 -- Handoff to Zafeer

**Date**: 2026-04-23
**Author**: Flux2 (AI Coder Agent)
**Repo**: puretechnyc/command-center
**Deploy target**: cc.purebrain.ai

---

## TL;DR

You are building 3 PRs (26 endpoints total) for the Command Center backend.
Models already exist in `models.py`. Test strategy already written. Follow TDD strictly.

| PR | Scope | Endpoints | Priority |
|----|-------|-----------|----------|
| #3 | Users API | 10 | HIGH |
| #4 | Roles + Divisions API | 10 | HIGH |
| #6 | Task Comments + Dependencies | 6 | MEDIUM |

> **Note:** Endpoints #21-25 (Auth migration + Activity logging) are handled in
> separate PRs (#2 and #5) by Flux. They are NOT your responsibility. The numbering
> gap in the spec is intentional.

## PR Dependencies

```
PR #1 (Models) ✅ DONE — foundation for everything
PR #2 (Seed Migration) — being built by Flux (no dependency for your PRs)
PR #3 (Users API) — depends on PR #1 only (tests self-seed via helpers)
PR #4 (Roles + Divisions) — depends on PR #1 only (tests self-seed)
PR #5 (Auth Migration) — being built by Flux
PR #6 (Comments + Deps) — depends on PR #1 only (tests self-seed)
```

**Your PRs (#3, #4, #6) have NO dependency on PR #2.** Test helpers create their
own data — you don't need seed migration to run tests.

---

## Getting Started

### Repo & Branch

```bash
git clone git@github.com:puretechnyc/command-center.git
cd command-center
git checkout feat/phase1-users-roles-db   # has the models already
git checkout -b feat/pr3-users-api         # your working branch for PR #3
```

### Backend Code Location

All backend code lives in:

```
tools/comms-gateway/
  main.py              # FastAPI app, router registration
  models.py            # SQLAlchemy models (OrgUser, OrgRole, etc. already here)
  config.py            # Config constants, TEAM_ROSTER, CIV_API_KEYS
  api/                 # Route modules (one file per domain)
    auth.py            # Auth endpoints + auth_or_api_key helper
    projects.py        # Reference pattern -- study this file
    users.py           # YOU CREATE THIS (PR #3)
    roles.py           # YOU CREATE THIS (PR #4)
    divisions.py       # YOU CREATE THIS (PR #4)
    comments.py        # YOU CREATE THIS (PR #6)
    dependencies.py    # YOU CREATE THIS (PR #6)
  tests/               # Test files
```

### Run Tests

```bash
cd tools/comms-gateway
pip install -r requirements.txt   # if not already
python3 -m pytest tests/ -v       # run all tests
python3 -m pytest tests/test_phase1_users_api.py -v   # run your file only
```

### Key Dependencies

- **FastAPI** with async SQLAlchemy (aiosqlite for SQLite)
- **bcrypt** for password hashing
- **SQLite** database at `data/comms.db`
- Auth: session cookie OR `X-CIV-Key` header (both supported)

---

## Architecture Quick Reference

### Framework Pattern

Every router follows the same structure. Study `api/projects.py` -- it is the canonical example.

```python
# api/users.py

import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models import AsyncSessionLocal, OrgUser
from api.auth import auth_or_api_key

logger = logging.getLogger("comms_gateway.api.users")
router = APIRouter(prefix="/api/v1", tags=["users"])
```

**IMPORTANT: API Versioning**
Phase 1 endpoints use `/api/v1/` prefix — this is intentional. Existing endpoints
(projects, calendar, chat) use `/api/`. The `/v1/` prefix marks the new standardized
API layer that will eventually replace the older endpoints. Do NOT change this to `/api/`
for consistency — we want the new namespace.

```python
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


@router.get("/users")
async def list_users(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _auth=Depends(auth_or_api_key),
):
    # ... query and return
    pass
```

### Auth Pattern

Every endpoint uses `_auth=Depends(auth_or_api_key)` as a dependency. This function:

1. Checks `X-CIV-Key` header first (format: `civ_name:key`)
2. Falls back to session cookie
3. Raises `HTTPException(401)` if neither is valid
4. Returns a dict with `role`, `username`, `display_name`, `email`, `is_ai`

To check who the caller is:

```python
@router.post("/users")
async def create_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth_user=Depends(auth_or_api_key),
):
    # auth_user is a dict: {"role": "admin", "username": "ahsen", ...}
    if auth_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
```

### Role-Based Access for CIV Keys

`auth_or_api_key` returns `role: "api"` for CIV-key authentication — NOT `"admin"`.
This means if you write `if auth_user["role"] != "admin": raise 403`, CIV-key requests
will be rejected.

**Pattern for admin-only endpoints:**
```python
ADMIN_ROLES = {"admin", "api"}  # "api" role has admin-equivalent access

@router.delete("/users/{user_id}")
async def delete_user(user_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    auth_user = await auth_or_api_key(request, db)
    if auth_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(403, "Admin access required")
    ...
```

**Pattern for auth_user field access (defensive):**
```python
# ALWAYS use .get() with defaults — different auth paths return different shapes
username = auth_user.get("username", "unknown")
role = auth_user.get("role", "member")
email = auth_user.get("email", "")
```

### Response Envelope

**All responses MUST use this shape** (matches existing frontend expectations):

```python
# Success - list
return JSONResponse(content={"ok": True, "data": [user.to_dict() for user in users]})

# Success - single item
return JSONResponse(content={"ok": True, "data": user.to_dict()})

# Success - create (201)
return JSONResponse(status_code=201, content={"ok": True, "data": user.to_dict()})

# Success - delete
return JSONResponse(content={"ok": True})

# Error
raise HTTPException(status_code=404, detail="User not found")
# FastAPI converts this to: {"detail": "User not found"}
# OR for consistency, return:
return JSONResponse(status_code=404, content={"ok": False, "error": "User not found"})
```

### How to Register Your Router

In `main.py`, add your import at the top with the others and register at the bottom:

```python
# At the top, with other imports:
from api.users import router as users_router
from api.roles import router as roles_router
from api.divisions import router as divisions_router
from api.comments import router as comments_router
from api.dependencies import router as dependencies_router

# After the existing include_router calls:
app.include_router(users_router)
app.include_router(roles_router)
app.include_router(divisions_router)
app.include_router(comments_router)
app.include_router(dependencies_router)
```

---

## CSRF Protection

The gateway has CSRF middleware (`middleware/csrf.py`) that blocks POST/PUT/PATCH/DELETE
requests unless they carry one of:
- `X-CSRF-Token` header matching session token
- `X-CIV-Key`, `X-API-Key`, `X-AI-Key`, or `Bearer` header (auto-exempt)

**For your tests:** The `_civ_headers()` helper uses `X-CIV-Key` which is CSRF-exempt.
All your tests will work without CSRF tokens.

**For the frontend:** Session-based requests from the web UI need `X-CSRF-Token`.
The frontend already handles this — you don't need to do anything special. But know
that if you ever test with session cookies instead of API keys, you'll get 403
"CSRF token missing" without the token header.

---

## TDD Workflow (MANDATORY)

This project uses strict TDD. Every PR must follow this cycle:

1. **Write failing tests first** -- create the test file with all test cases
2. **Verify they fail** -- `python3 -m pytest tests/your_test_file.py -v` should show failures
3. **Write the simplest code to pass** -- implement the endpoint
4. **Verify they pass** -- all tests green
5. **Refactor if needed** -- clean up while keeping tests green
6. **Create PR** with both test file and implementation

---

## Existing Test Pattern (Copy This)

Every test file follows the same `_make_app()` factory pattern. Here is the skeleton you should use for all your test files:

```python
"""
Tests for [Your Feature] API.

TDD: Written FIRST -- all tests expected to FAIL before implementation.
"""

import json
import pytest
from unittest.mock import MagicMock, AsyncMock
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# App setup (same pattern as test_projects_api.py)
# ---------------------------------------------------------------------------

def _make_app():
    """Import and return the FastAPI app with heavy deps stubbed out."""
    import importlib
    import os
    import sys

    os.environ["SESSION_HTTPS_ONLY"] = "false"

    gateway_dir = str(__import__("pathlib").Path(__file__).parent.parent)
    if gateway_dir not in sys.path:
        sys.path.insert(0, gateway_dir)

    for mod_name in [
        "sync.calendar_sync",
        "sync.email_sync",
        "auth.microsoft",
        "auth.google_cal",
        "auth.google_oauth",
    ]:
        if mod_name not in sys.modules:
            mock = MagicMock()
            mock.has_valid_token = AsyncMock(return_value=False)
            mock.is_google_auth_available = MagicMock(return_value=False)
            mock.has_google_token = AsyncMock(return_value=False)
            mock.revoke_google_token = AsyncMock(return_value=False)
            mock.exchange_code_for_tokens = AsyncMock(return_value={})
            mock.save_token = AsyncMock()
            mock.save_google_token = AsyncMock()
            mock.get_google_userinfo = AsyncMock(return_value={})
            mock.build_auth_url = MagicMock(return_value="https://example.com/auth")
            mock.build_google_auth_url = MagicMock(return_value="https://example.com/auth")
            mock.sync_microsoft_calendar = AsyncMock(return_value=0)
            mock.sync_google_calendar = AsyncMock(return_value=0)
            mock.sync_google_calendar_for_user = AsyncMock()
            mock.sync_inbox = AsyncMock(return_value=0)
            sys.modules[mod_name] = mock

    import models
    import tempfile

    _test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _test_db_path = _test_db.name
    _test_db.close()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    test_sync_engine = create_engine(f"sqlite:///{_test_db_path}", echo=False)
    test_async_engine = create_async_engine(
        f"sqlite+aiosqlite:///{_test_db_path}", echo=False
    )

    models.sync_engine = test_sync_engine
    models.async_engine = test_async_engine
    models.SyncSession = sessionmaker(bind=test_sync_engine)
    models.AsyncSessionLocal = async_sessionmaker(
        test_async_engine, expire_on_commit=False
    )

    models.Base.metadata.create_all(test_sync_engine)
    models.init_db = lambda: None

    import main as main_mod
    importlib.reload(main_mod)
    return main_mod.app


@pytest.fixture(scope="module")
def app():
    return _make_app()


@pytest.fixture()
def client(app):
    return TestClient(app, raise_server_exceptions=False)


def _civ_headers() -> dict:
    """Return X-CIV-Key headers for authenticated requests."""
    import sys
    gateway_dir = str(__import__("pathlib").Path(__file__).parent.parent)
    if gateway_dir not in sys.path:
        sys.path.insert(0, gateway_dir)
    import config as _cfg
    civ_key = _cfg.CIV_API_KEYS.get("flux", "flux-dev-key")
    return {"X-CIV-Key": f"flux:{civ_key}"}


# ---------------------------------------------------------------------------
# Helper: seed a test user directly into the DB
# ---------------------------------------------------------------------------

def _seed_user(app, **overrides):
    """Insert a user directly into the DB for testing. Returns the user dict."""
    import sys
    gateway_dir = str(__import__("pathlib").Path(__file__).parent.parent)
    if gateway_dir not in sys.path:
        sys.path.insert(0, gateway_dir)
    import models
    import bcrypt

    defaults = {
        "id": "testuser",
        "org_id": "purebrain",
        "full_name": "Test User",
        "email": "test@puretechnology.nyc",
        "password_hash": bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode(),
        "role": "member",
        "status": "active",
        "is_ai": False,
        "human_skills": [],
    }
    defaults.update(overrides)

    session = models.SyncSession()
    user = models.OrgUser(**defaults)
    session.add(user)
    session.commit()
    result = user.to_dict()
    session.close()
    return result


def _seed_role(app, **overrides):
    """Insert a role directly into the DB for testing."""
    import sys
    gateway_dir = str(__import__("pathlib").Path(__file__).parent.parent)
    if gateway_dir not in sys.path:
        sys.path.insert(0, gateway_dir)
    import models

    defaults = {
        "id": "test-role",
        "org_id": "purebrain",
        "name": "Test Role",
        "description": "For testing",
        "level": 5,
        "permissions": ["org.view"],
        "is_default": False,
    }
    defaults.update(overrides)

    session = models.SyncSession()
    role = models.OrgRole(**defaults)
    session.add(role)
    session.commit()
    result = role.to_dict()
    session.close()
    return result


def _seed_division(app, **overrides):
    """Insert a division directly into the DB for testing."""
    import sys
    gateway_dir = str(__import__("pathlib").Path(__file__).parent.parent)
    if gateway_dir not in sys.path:
        sys.path.insert(0, gateway_dir)
    import models

    defaults = {
        "id": "test-div",
        "org_id": "purebrain",
        "name": "Test Division",
        "color": "#3B82F6",
    }
    defaults.update(overrides)

    session = models.SyncSession()
    div = models.OrgDivision(**defaults)
    session.add(div)
    session.commit()
    result = div.to_dict()
    session.close()
    return result


def _seed_default_roles(app):
    """Seed the 5 default roles for testing default-role protection."""
    import sys
    gateway_dir = str(__import__("pathlib").Path(__file__).parent.parent)
    if gateway_dir not in sys.path:
        sys.path.insert(0, gateway_dir)
    import models

    defaults = [
        ("admin", "Admin", ["org.view","org.edit","org.add","org.remove","dept.view","dept.create","dept.edit","dept.delete","proj.view","proj.create","proj.edit","proj.delete","proj.assign","fin.view","fin.edit","fin.approve","admin.roles","admin.settings","admin.audit","admin.export"]),
        ("manager", "Manager", ["org.view","org.edit","org.add","org.remove","dept.view","dept.create","dept.edit","dept.delete","proj.view","proj.create","proj.edit","proj.delete","proj.assign","fin.view","fin.edit","fin.approve"]),
        ("team-lead", "Team Lead", ["org.view","org.edit","dept.view","proj.view","proj.create","proj.edit","proj.delete","proj.assign"]),
        ("member", "Member", ["org.view","proj.view","proj.create","proj.edit","dept.view"]),
        ("viewer", "Viewer", ["org.view","dept.view","proj.view","fin.view"]),
    ]

    session = models.SyncSession()
    for role_id, name, perms in defaults:
        role = models.OrgRole(id=role_id, org_id="puretechnyc", name=name, permissions=perms, is_default=True)
        session.add(role)
    session.commit()
    session.close()
```

**Usage in tests:**

```python
class TestUsersCRUD:

    def test_list_users_returns_200(self, client, app):
        _seed_user(app, id="alice", email="alice@test.com")
        resp = client.get("/api/v1/users", headers=_civ_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert isinstance(data["data"], list)

    def test_list_users_excludes_password_hash(self, client, app):
        _seed_user(app, id="bob", email="bob@test.com")
        resp = client.get("/api/v1/users", headers=_civ_headers())
        for user in resp.json()["data"]:
            assert "password_hash" not in user
```

---

## PR #3: Users API (10 endpoints)

**File to create**: `api/users.py`
**Test file to create**: `tests/test_phase1_users_api.py`
**Estimated tests**: ~75

### Endpoint Signatures

```
 1. GET    /api/v1/users                         -- list users (with query filters)
 2. GET    /api/v1/users/{id}                     -- get single user
 3. POST   /api/v1/users                         -- create user
 4. PATCH  /api/v1/users/{id}                     -- partial update
 5. DELETE /api/v1/users/{id}                     -- delete user
 6. GET    /api/v1/users/{id}/agent               -- get AI agent config
 7. PATCH  /api/v1/users/{id}/agent/key           -- set agent API key
 8. POST   /api/v1/users/{id}/agent/memory        -- add agent memory
 9. DELETE /api/v1/users/{id}/agent/memory/{memId} -- remove agent memory
10. PATCH  /api/v1/users/{id}/status              -- activate/deactivate
```

### Endpoint #1: GET /api/v1/users

**Query params** (all optional):
- `role` -- filter by role (admin, manager, member, viewer)
- `division_id` -- filter by division
- `status` -- filter by status (active, inactive, suspended)
- `is_ai` -- filter by AI flag (true/false)
- `limit` -- pagination limit (default: all)
- `offset` -- pagination offset (default: 0)

**Response**: `{"ok": true, "data": [<user_dict>, ...]}`

```python
@router.get("/users")
async def list_users(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth_user=Depends(auth_or_api_key),
):
    query = select(OrgUser)

    role = request.query_params.get("role")
    if role:
        if role not in VALID_ROLES:
            raise HTTPException(status_code=400, detail=f"Invalid role: {role}")
        query = query.where(OrgUser.role == role)

    division_id = request.query_params.get("division_id")
    if division_id:
        query = query.where(OrgUser.division_id == division_id)

    status = request.query_params.get("status")
    if status:
        query = query.where(OrgUser.status == status)

    is_ai = request.query_params.get("is_ai")
    if is_ai is not None:
        query = query.where(OrgUser.is_ai == (is_ai.lower() == "true"))

    # Pagination
    limit = request.query_params.get("limit")
    offset = request.query_params.get("offset")
    if offset:
        query = query.offset(int(offset))
    if limit:
        query = query.limit(int(limit))

    result = await db.execute(query)
    users = result.scalars().all()
    return JSONResponse(content={"ok": True, "data": [u.to_dict() for u in users]})
```

### Endpoint #2: GET /api/v1/users/{id}

**Response**: `{"ok": true, "data": <user_dict>}` or 404

```python
@router.get("/users/{user_id}")
async def get_user(
    user_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth_user=Depends(auth_or_api_key),
):
    result = await db.execute(select(OrgUser).where(OrgUser.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return JSONResponse(status_code=404, content={"ok": False, "error": "User not found"})
    return JSONResponse(content={"ok": True, "data": user.to_dict()})
```

### Endpoint #3: POST /api/v1/users

**Request body** (JSON):
```json
{
    "id": "zafeer",
    "full_name": "Zafeer Khan",
    "email": "zafeer@puretechnology.nyc",
    "password": "securepassword123",
    "role": "member",
    "division_id": "engineering",
    "manager_id": "ahsen",
    "job_title": "Software Engineer",
    "is_ai": false,
    "human_skills": ["python", "fastapi"]
}
```

**Required fields**: id, full_name, email, password (for humans; AI users may skip)
**Response**: 201 + `{"ok": true, "data": <user_dict>}`

**Validation rules**:
- `id` must be unique (409 if duplicate)
- `email` must be unique (409 if duplicate)
- `email` must be a valid format (422 if invalid)
- `role` must be one of: admin, manager, member, viewer (422 if invalid)
- `password` is bcrypt-hashed before storage (NEVER store plaintext)
- `status` defaults to "active"
- `org_id` is hardcoded to "purebrain" for now
- Only admin users can create users (403 otherwise)

**Email uniqueness:** The `email` column has an INDEX but NOT a UNIQUE constraint
at the DB level. You must check for duplicate emails in application code before
inserting:
```python
existing = await db.execute(select(OrgUser).where(OrgUser.email == body["email"]))
if existing.scalar_one_or_none():
    raise HTTPException(409, f"Email {body['email']} already in use")
```

### Endpoint #4: PATCH /api/v1/users/{id}

**Request body**: partial JSON -- only fields being updated
```json
{
    "job_title": "Senior Engineer",
    "phone": "+1-555-0123"
}
```

**Rules**:
- `id` and `org_id` are immutable (422 if attempted)
- `updated_at` auto-refreshes on update
- Only admin or the user themselves can update (403 otherwise)
- Returns 404 if user not found

### Endpoint #5: DELETE /api/v1/users/{id}

**Rules**:
- Prefer soft delete: set `status="deleted"` rather than removing the row
- Returns 404 if user not found
- Admin-only (403 for non-admins)
- Admin cannot delete themselves (400 safety check)
- If the user is referenced as `manager_id` by other users, nullify those references

### Endpoint #6: GET /api/v1/users/{id}/agent

**Returns** the `ai_agent` JSON field for an AI user.
- Human users: return `{"ok": true, "data": null}` (not an error)
- User not found: 404

### Endpoint #7: PATCH /api/v1/users/{id}/agent/key

**Request body**: `{"api_key": "sk-abc123..."}`

**Rules**:
- Hash the key before storing (do NOT store plaintext)
- Store in `ai_agent.api_key_hash` (update the JSON field)
- Only works on AI users (is_ai=true); returns 400 for human users
- Admin-only (403)
- User not found: 404

### Endpoint #8: POST /api/v1/users/{id}/agent/memory

**Request body**: `{"text": "Learned about project X today", "type": "observation"}`

**Rules**:
- Appends to `ai_agent.memories` array in the JSON field
- Each memory entry: `{"id": <uuid>, "text": "...", "type": "...", "created_at": "..."}`
- Only works on AI users (400 for humans)
- Empty text: 422
- User not found: 404

### Endpoint #9: DELETE /api/v1/users/{id}/agent/memory/{memId}

**Rules**:
- Removes the memory entry matching `memId` from `ai_agent.memories`
- Memory not found: 404
- User not found: 404

### Endpoint #10: PATCH /api/v1/users/{id}/status

**Request body**: `{"status": "active"}` or `{"status": "inactive"}`

**Rules**:
- Valid statuses: active, inactive, suspended
- Admin-only (403)
- User not found: 404
- Idempotent: setting "active" on already-active user returns 200

### Security Requirements (PR #3)

- **Sensitive fields:** `to_dict()` already excludes `password_hash`, `profile_photo`,
  `cv_filename`, and `cv_data` from ALL responses. You don't need to add any special
  exclusion logic — the model handles it. Verify this in tests.
- All endpoints require auth (session or API key). No auth = 401.
- Validate email format on create/update (basic regex: contains @ and a dot after it).
- Validate role is one of: admin, manager, member, viewer.
- Validate status is one of: active, inactive, suspended.

### Constants to Define (top of users.py)

```python
VALID_ROLES = {"admin", "manager", "member", "viewer"}
VALID_STATUSES = {"active", "inactive", "suspended"}
```

### Edge Cases to Test

From the test strategy (full list):
- List with no users returns empty list
- Filtering by non-existent division returns empty list
- Pagination beyond total returns empty list
- Duplicate email on create returns 409
- XSS in full_name -- stored as-is (sanitization is frontend concern)
- Empty string for required fields returns 422
- Unicode in full_name (international names)

---

## PR #4: Roles + Divisions API (10 endpoints)

**Files to create**: `api/roles.py`, `api/divisions.py`
**Test file to create**: `tests/test_phase1_roles_divisions_api.py`
**Estimated tests**: ~55

### Roles Endpoints (5)

```
11. GET    /api/v1/roles             -- list all roles
12. GET    /api/v1/roles/{id}         -- get single role
13. POST   /api/v1/roles             -- create custom role
14. PATCH  /api/v1/roles/{id}         -- update permissions/name/description
15. DELETE /api/v1/roles/{id}         -- delete role (block if default)
```

### The 20 Valid Permissions

Hardcode this set in your roles module. These are the ONLY valid permission IDs:

```python
VALID_PERMISSIONS = {
    # Organization (4)
    "org.view", "org.edit", "org.add", "org.remove",
    # Departments / Divisions (4)
    "dept.view", "dept.create", "dept.edit", "dept.delete",
    # Projects (5)
    "proj.view", "proj.create", "proj.edit", "proj.delete", "proj.assign",
    # Finance (3)
    "fin.view", "fin.edit", "fin.approve",
    # Admin (4)
    "admin.roles", "admin.settings", "admin.audit", "admin.export",
}
```

### Default Roles (5, pre-seeded)

These are created by the seed migration (PR #2) and have `is_default=True`:

| Role | Level | # Permissions |
|------|-------|--------------|
| Admin | 100 | 20 (all) |
| Manager | 80 | 16 |
| Team Lead | 60 | 8 |
| Member | 40 | 5 |
| Viewer | 20 | 4 |

### Endpoint #11: GET /api/v1/roles

**Response**: `{"ok": true, "data": [<role_dict>, ...]}`

All roles (default + custom). Requires auth.

### Endpoint #12: GET /api/v1/roles/{id}

**Response**: `{"ok": true, "data": <role_dict>}` or 404

### Endpoint #13: POST /api/v1/roles

**Request body**:
```json
{
    "id": "project-lead",
    "name": "Project Lead",
    "description": "Can manage assigned projects",
    "level": 65,
    "permissions": ["proj.view", "proj.create", "proj.edit", "proj.assign"]
}
```

**Rules**:
- Custom roles ALWAYS have `is_default=false` (ignore if client sends true)
- Validate all permissions are in the VALID_PERMISSIONS set (422 for invalid)
- Duplicate name returns 409
- Missing name returns 422
- `org_id` hardcoded to "purebrain"
- Admin-only (403)
- Level must be a positive integer

### Endpoint #14: PATCH /api/v1/roles/{id}

**Request body**: partial JSON
```json
{
    "permissions": ["proj.view", "proj.create", "proj.edit"]
}
```

**Rules**:
- **Cannot modify default roles** (is_default=true) -- return 403 with "Cannot modify default role"
- Not found: 404
- Admin-only (403)
- Validate permissions against VALID_PERMISSIONS set

### Endpoint #15: DELETE /api/v1/roles/{id}

**Rules**:
- **Cannot delete default roles** -- return 403 with "Cannot delete default role"
- Not found: 404
- Admin-only (403)
- If role is assigned to any users, return 400 with "Role is in use by N users"

### Divisions Endpoints (5)

```
16. GET    /api/v1/divisions             -- list all divisions
17. GET    /api/v1/divisions/{id}         -- get single division
18. POST   /api/v1/divisions             -- create division
19. PATCH  /api/v1/divisions/{id}         -- update division
20. DELETE /api/v1/divisions/{id}         -- delete division
```

### Endpoint #16: GET /api/v1/divisions

**Response**: `{"ok": true, "data": [<division_dict>, ...]}`

Optionally include `member_count` (count of users with that division_id).

### Endpoint #17: GET /api/v1/divisions/{id}

**Response**: `{"ok": true, "data": <division_dict>}` or 404

Optionally include a list of users in this division.

### Endpoint #18: POST /api/v1/divisions

**Request body**:
```json
{
    "id": "engineering",
    "name": "Engineering",
    "color": "#3B82F6",
    "parent_division_id": null,
    "division_head_id": "ahsen"
}
```

**Rules**:
- `org_id` hardcoded to "purebrain"
- Validate `parent_division_id` exists if provided (400 if not)
- Validate `division_head_id` references a valid user if provided (400 if not)
- Duplicate name returns 409
- Missing name returns 422
- Admin-only (403)

### Endpoint #19: PATCH /api/v1/divisions/{id}

**Rules**:
- **Circular reference detection**: cannot set parent to self (400)
- **Ancestor loop detection**: if A -> B -> C, cannot set C's parent to A (400). Implement as graph traversal:

```python
async def _would_create_cycle(db, division_id: str, new_parent_id: str) -> bool:
    """Walk up from new_parent_id; if we reach division_id, it's a cycle."""
    visited = set()
    current = new_parent_id
    while current:
        if current == division_id:
            return True
        if current in visited:
            break  # already a cycle in the data (shouldn't happen)
        visited.add(current)
        result = await db.execute(
            select(OrgDivision.parent_division_id).where(OrgDivision.id == current)
        )
        current = result.scalar_one_or_none()
    return False
```

- Not found: 404
- Admin-only (403)

### Endpoint #20: DELETE /api/v1/divisions/{id}

**Rules**:
- Division with users: nullify `division_id` on those users (do NOT cascade-delete users)
- Division with child divisions: return 400 "Division has child divisions; re-parent them first"
- Not found: 404
- Admin-only (403)

### Divisions Edge Cases

- Deleting a division should NOT delete its users -- just set their `division_id = null`
- A division can have no parent (top-level)
- `division_head_id` is a soft reference to a user (not an FK in the model, but validate on create/update)

---

## PR #6: Task Comments + Dependencies (6 endpoints)

**Files to create**: `api/comments.py`, `api/dependencies.py`
**Test file to create**: `tests/test_phase1_comments_deps.py`
**Estimated tests**: ~45

### Test Helper: _seed_task()

```python
def _seed_task(app, task_id="task-001", project_id="proj-001", title="Test Task", **overrides):
    """Seed a ProjectTask for comment/dependency testing."""
    import sys
    gateway_dir = str(__import__("pathlib").Path(__file__).parent.parent)
    if gateway_dir not in sys.path:
        sys.path.insert(0, gateway_dir)
    import models

    fields = {
        "id": task_id, "project_id": project_id, "title": title,
        "status": "todo", "priority": "medium",
    }
    fields.update(overrides)

    session = models.SyncSession()
    t = models.ProjectTask(**fields)
    session.add(t)
    session.commit()
    session.close()
    return fields
```

### Comments Endpoints (3)

```
26. GET    /api/v1/tasks/{task_id}/comments                   -- list comments
27. POST   /api/v1/tasks/{task_id}/comments                   -- add comment
28. DELETE /api/v1/tasks/{task_id}/comments/{comment_id}       -- delete comment
```

### Endpoint #26: GET /api/v1/tasks/{task_id}/comments

**Response**: `{"ok": true, "data": [<comment_dict>, ...]}`

**Rules**:
- Comments ordered by `created_at` ascending (oldest first, like a chat thread)
- Task not found: 404 (query ProjectTask table to verify task exists)
- Requires auth (401)

### Endpoint #27: POST /api/v1/tasks/{task_id}/comments

**Request body**: `{"text": "Great progress on this task!"}`

**Rules**:
- `author_id` is auto-set from the authenticated user's username (do NOT trust client)
- `id` is auto-generated UUID
- Empty text or whitespace-only: 422
- Task not found: 404
- Requires auth (401)
- Returns 201

```python
@router.post("/tasks/{task_id}/comments", status_code=201)
async def add_comment(
    task_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth_user=Depends(auth_or_api_key),
):
    # Verify task exists
    task_result = await db.execute(
        select(ProjectTask).where(ProjectTask.id == task_id)
    )
    if not task_result.scalar_one_or_none():
        return JSONResponse(status_code=404, content={"ok": False, "error": "Task not found"})

    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="Comment text is required")

    comment = TaskComment(
        task_id=task_id,
        author_id=auth_user.get("username", "unknown"),
        text=text,
    )
    db.add(comment)
    await db.commit()
    await db.refresh(comment)

    return JSONResponse(
        status_code=201,
        content={"ok": True, "data": comment.to_dict()},
    )
```

**Note:** `TaskComment.author_id` is a soft reference (no FK constraint to org_users).
Validate the author exists if you want strictness, but it's not enforced at the DB level.
The `author_id` is auto-set from the authenticated user's username — never trust client-provided values.

### Endpoint #28: DELETE /api/v1/tasks/{task_id}/comments/{comment_id}

**Rules**:
- Only the comment author OR an admin can delete (403 otherwise)
- Comment not found: 404
- Comment belongs to different task: 404 (verify `comment.task_id == task_id`)
- Requires auth (401)

### Dependencies Endpoints (3)

```
29. GET    /api/v1/tasks/{task_id}/dependencies                -- list dependencies
30. POST   /api/v1/tasks/{task_id}/dependencies                -- add dependency
31. DELETE /api/v1/tasks/{task_id}/dependencies/{dep_id}        -- remove dependency
```

### Endpoint #29: GET /api/v1/tasks/{task_id}/dependencies

**Response**: `{"ok": true, "data": [<dependency_dict>, ...]}`

Each dependency should include the `depends_on_id` and ideally the task title for display.

### Endpoint #30: POST /api/v1/tasks/{task_id}/dependencies

**Request body**: `{"depends_on_id": "TASK-456"}`

**Rules**:
- **Self-dependency blocked**: `task_id == depends_on_id` returns 400
- **Duplicate blocked**: same pair already exists returns 409
- **Circular dependency detection**: implement graph traversal

```python
async def _would_create_circular_dep(db, task_id: str, depends_on_id: str) -> bool:
    """Check if adding task_id -> depends_on_id would create a cycle.

    Walk the dependency chain starting from depends_on_id.
    If we reach task_id, it's circular.
    """
    visited = set()
    to_check = [depends_on_id]
    while to_check:
        current = to_check.pop()
        if current == task_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        result = await db.execute(
            select(TaskDependency.depends_on_id).where(
                TaskDependency.task_id == current
            )
        )
        for row in result.scalars().all():
            to_check.append(row)
    return False
```

- Task not found: 404
- `depends_on_id` task not found: 404
- Requires auth (401)
- Returns 201

### Endpoint #31: DELETE /api/v1/tasks/{task_id}/dependencies/{dep_id}

**Rules**:
- Dependency not found: 404
- Dependency belongs to different task: 404
- Requires auth (401)

### Important: Task Table Reference

Comments and dependencies reference tasks. The existing task model is `ProjectTask` in `models.py`. Check which table name and ID format it uses:

```python
from models import ProjectTask
# Use this to verify task existence before creating comments/deps
```

---

## Models Reference (to_dict() Output Shapes)

These models are already defined in `models.py`. Here are the exact shapes their `to_dict()` methods return:

### OrgUser.to_dict()

```json
{
    "id": "ahsen",
    "org_id": "purebrain",
    "full_name": "Ahsen Saeed",
    "email": "ahsen@puretechnology.nyc",
    "role": "admin",
    "division_id": "engineering",
    "manager_id": null,
    "job_title": "CTO",
    "avatar_initials": "AS",
    "designation": "Chief Technology Officer",
    "department": "Technology",
    "phone": "+1-555-0100",
    "location": "New York, NY",
    "timezone": "America/New_York",
    "employment_type": "full_time",
    "start_date": "2024-01-15T00:00:00Z",
    "status": "active",
    "bio": "Technology leader...",
    "linkedin_url": "https://linkedin.com/in/ahsen",
    "human_skills": ["python", "leadership", "architecture"],
    "is_ai": false,
    "human_partner_id": null,
    "ai_agent": null,
    "created_at": "2026-04-23T12:00:00Z",
    "updated_at": "2026-04-23T12:00:00Z"
}
```

**NOTE**: `password_hash` and `cv_data` are intentionally EXCLUDED.

### OrgRole.to_dict()

```json
{
    "id": "admin",
    "org_id": "purebrain",
    "name": "Admin",
    "description": "Full system access",
    "level": 100,
    "permissions": ["org.view", "org.edit", "org.manage_users", "...all 20..."],
    "is_default": true,
    "created_at": "2026-04-23T12:00:00Z"
}
```

### OrgDivision.to_dict()

```json
{
    "id": "engineering",
    "org_id": "purebrain",
    "name": "Engineering",
    "color": "#3B82F6",
    "parent_division_id": null,
    "division_head_id": "ahsen",
    "created_at": "2026-04-23T12:00:00Z"
}
```

### TaskComment.to_dict()

```json
{
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "task_id": "TASK-123",
    "author_id": "zafeer",
    "text": "Great progress on this!",
    "created_at": "2026-04-23T14:30:00Z"
}
```

### TaskDependency.to_dict()

```json
{
    "id": 1,
    "task_id": "TASK-123",
    "depends_on_id": "TASK-456",
    "created_at": "2026-04-23T14:30:00Z"
}
```

---

## File Checklist

### PR #3: Users API

- [ ] `api/users.py` -- 10 endpoints
- [ ] `tests/test_phase1_users_api.py` -- ~75 tests
- [ ] Update `main.py` -- add `from api.users import router as users_router` + `app.include_router(users_router)`

### PR #4: Roles + Divisions API

- [ ] `api/roles.py` -- 5 endpoints
- [ ] `api/divisions.py` -- 5 endpoints
- [ ] `tests/test_phase1_roles_divisions_api.py` -- ~55 tests
- [ ] Update `main.py` -- add both routers

### PR #6: Task Comments + Dependencies

- [ ] `api/comments.py` -- 3 endpoints
- [ ] `api/dependencies.py` -- 3 endpoints
- [ ] `tests/test_phase1_comments_deps.py` -- ~45 tests
- [ ] Update `main.py` -- add both routers

---

## Common Pitfalls

1. **SQLite does not enforce FKs by default.** If you need FK enforcement in tests, add `PRAGMA foreign_keys = ON` to your engine connect event. For now, validate references in application code.

2. **Async vs Sync in tests.** Tests use `TestClient` which is synchronous. The endpoints are async. FastAPI handles this transparently -- just write normal synchronous test code.

3. **Module reload in tests.** The `_make_app()` function uses `importlib.reload(main_mod)` to pick up your new router registrations. Make sure your router is imported in `main.py` before the test can find it.

4. **JSON columns.** `human_skills`, `ai_agent`, and `permissions` are JSON columns. SQLAlchemy's JSON type works fine with SQLite. Store Python lists/dicts directly.

5. **Password hashing.** Use bcrypt:
   ```python
   import bcrypt
   hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
   ```

6. **Date serialization.** The `to_dict()` methods append "Z" to ISO dates. Tests should expect this format: `"2026-04-23T12:00:00Z"`.

7. **org_id hardcoded.** Always set `org_id="purebrain"` on every created record. This is for future multi-tenant support.

---

## Reference Documents

| Document | Path | What it covers |
|----------|------|----------------|
| Full Phase 1 Spec | `docs/CC-PHASE1-SPEC.md` | All 31 endpoints, models, decisions |
| Test Strategy | `docs/PHASE1-TEST-STRATEGY.md` | Every test case for every PR |
| Projects API (pattern) | `api/projects.py` | Canonical router pattern to follow |
| Auth module | `api/auth.py` | `auth_or_api_key` implementation |
| Models | `models.py` (lines 743-907) | All 5 new models |
| Existing API tests | `tests/test_projects_api.py` | Test pattern to follow |
| Model tests | `tests/test_org_models.py` | Model test pattern |

---

## Questions?

Email flux.civ@agentmail.to or ask Alex directly.

---

**End of Handoff**
