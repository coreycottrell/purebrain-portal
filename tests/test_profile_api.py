"""
Tests for the Profile API data pipeline.

What we're verifying: GET /api/profile merges identity + settings data,
    POST /api/profile saves editable fields to user-settings.json
What coder discovered: Profile tab shows hardcoded HTML defaults, needs real data pipeline
What descendants inherit: Regression tests for profile data merge + edit flow
Why this matters: The Settings Profile tab should show real CIV identity, not placeholders

Phase 1 (RED): These tests MUST FAIL against current code (no /api/profile endpoint exists yet).
Phase 2 (GREEN): Implement endpoint, confirm all tests pass.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest import mock
from unittest.mock import patch, AsyncMock

import pytest

# Ensure portal_server is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeRequest:
    """Minimal request object compatible with check_auth + request.method + request.json()."""
    def __init__(self, method="GET", body=None):
        self.method = method
        self.headers = {"authorization": "Bearer test-token"}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def identity_file(tmp_path):
    """Create a temp identity file with realistic data."""
    p = tmp_path / ".aiciv-identity.json"
    data = {
        "civ_name": "Flux",
        "civ_id": "flux",
        "human_name": "Alex Seant",
        "human_email": "ipushdabutton@gmail.com",
        "container": "flux-alex-seant",
        "parent_civ": "Witness",
        "born": "2026-03-12T18:08:07Z",
        "status": "active"
    }
    p.write_text(json.dumps(data))
    return p


@pytest.fixture
def settings_file(tmp_path):
    """Create a temp user-settings.json with profile section."""
    p = tmp_path / "user-settings.json"
    data = {
        "theme": "dark",
        "profile": {
            "role": "Conductor of Conductors",
            "model": "Claude Opus 4",
            "architecture": "Multi-Agent Civilization",
            "agent_count": "32",
            "human_title": "CEO & Founder",
            "human_org": "PureBrain",
            "human_background": "Tech entrepreneur",
            "human_style": "Direct, hands-on",
            "human_values": "Innovation, authenticity",
            "human_role": "Creator & Steward",
            "human_relationship": "Trust-based",
            "website": "purebrain.ai",
            "civ_email": "flux.civ@agentmail.to"
        },
        "email_accounts": [
            {"address": "flux.civ@agentmail.to", "provider": "agentmail"}
        ]
    }
    p.write_text(json.dumps(data))
    return p


@pytest.fixture
def empty_settings_file(tmp_path):
    """Create a temp user-settings.json with no profile section."""
    p = tmp_path / "user-settings.json"
    p.write_text(json.dumps({"theme": "dark"}))
    return p


# ===========================================================================
# Test 1: GET /api/profile returns identity data from ~/.aiciv-identity.json
# ===========================================================================

class TestProfileGetIdentity:

    def test_returns_civ_name_from_identity_file(self, identity_file, settings_file):
        """GET /api/profile should return civ_name from the identity file."""
        import portal_server

        async def _test():
            with patch.object(portal_server, "check_auth", return_value=True), \
                 patch.object(portal_server, "SETTINGS_FILE", settings_file), \
                 patch("portal_server.Path.home", return_value=identity_file.parent):
                resp = await portal_server.api_profile(FakeRequest("GET"))
            return json.loads(resp.body.decode())

        data = _run(_test())
        assert data["civ_name"] == "Flux", f"Expected civ_name='Flux', got '{data.get('civ_name')}'"

    def test_returns_human_name_from_identity_file(self, identity_file, settings_file):
        """GET /api/profile should return human_name from the identity file."""
        import portal_server

        async def _test():
            with patch.object(portal_server, "check_auth", return_value=True), \
                 patch.object(portal_server, "SETTINGS_FILE", settings_file), \
                 patch("portal_server.Path.home", return_value=identity_file.parent):
                resp = await portal_server.api_profile(FakeRequest("GET"))
            return json.loads(resp.body.decode())

        data = _run(_test())
        assert data["human_name"] == "Alex Seant"

    def test_returns_born_and_parent_from_identity(self, identity_file, settings_file):
        """GET /api/profile should include born, parent_civ, status from identity."""
        import portal_server

        async def _test():
            with patch.object(portal_server, "check_auth", return_value=True), \
                 patch.object(portal_server, "SETTINGS_FILE", settings_file), \
                 patch("portal_server.Path.home", return_value=identity_file.parent):
                resp = await portal_server.api_profile(FakeRequest("GET"))
            return json.loads(resp.body.decode())

        data = _run(_test())
        assert data["born"] == "2026-03-12T18:08:07Z"
        assert data["parent_civ"] == "Witness"
        assert data["status"] == "active"


# ===========================================================================
# Test 2: GET /api/profile returns extended profile fields from settings
# ===========================================================================

class TestProfileGetSettings:

    def test_returns_editable_ai_fields_from_settings(self, identity_file, settings_file):
        """GET /api/profile should include role, model, architecture from settings."""
        import portal_server

        async def _test():
            with patch.object(portal_server, "check_auth", return_value=True), \
                 patch.object(portal_server, "SETTINGS_FILE", settings_file), \
                 patch("portal_server.Path.home", return_value=identity_file.parent):
                resp = await portal_server.api_profile(FakeRequest("GET"))
            return json.loads(resp.body.decode())

        data = _run(_test())
        assert data["role"] == "Conductor of Conductors"
        assert data["model"] == "Claude Opus 4"
        assert data["architecture"] == "Multi-Agent Civilization"
        assert data["agent_count"] == "32"

    def test_returns_human_partner_fields_from_settings(self, identity_file, settings_file):
        """GET /api/profile should include human partner editable fields."""
        import portal_server

        async def _test():
            with patch.object(portal_server, "check_auth", return_value=True), \
                 patch.object(portal_server, "SETTINGS_FILE", settings_file), \
                 patch("portal_server.Path.home", return_value=identity_file.parent):
                resp = await portal_server.api_profile(FakeRequest("GET"))
            return json.loads(resp.body.decode())

        data = _run(_test())
        assert data["human_title"] == "CEO & Founder"
        assert data["human_org"] == "PureBrain"
        assert data["human_background"] == "Tech entrepreneur"
        assert data["human_style"] == "Direct, hands-on"
        assert data["human_values"] == "Innovation, authenticity"

    def test_returns_contact_card_fields(self, identity_file, settings_file):
        """GET /api/profile should include contact card fields."""
        import portal_server

        async def _test():
            with patch.object(portal_server, "check_auth", return_value=True), \
                 patch.object(portal_server, "SETTINGS_FILE", settings_file), \
                 patch("portal_server.Path.home", return_value=identity_file.parent):
                resp = await portal_server.api_profile(FakeRequest("GET"))
            return json.loads(resp.body.decode())

        data = _run(_test())
        assert data["civ_email"] == "flux.civ@agentmail.to"
        assert data["human_email"] == "ipushdabutton@gmail.com"
        assert data["website"] == "purebrain.ai"


# ===========================================================================
# Test 3: POST /api/profile saves editable fields to user-settings.json
# ===========================================================================

class TestProfilePost:

    def test_saves_editable_field_to_settings(self, identity_file, settings_file):
        """POST /api/profile should save editable fields to user-settings.json."""
        import portal_server

        async def _test():
            body = {"human_title": "CTO", "human_org": "NewCo"}
            with patch.object(portal_server, "check_auth", return_value=True), \
                 patch.object(portal_server, "SETTINGS_FILE", settings_file), \
                 patch("portal_server.Path.home", return_value=identity_file.parent):
                resp = await portal_server.api_profile(FakeRequest("POST", body))
            return json.loads(resp.body.decode())

        result = _run(_test())
        assert result.get("ok") is True

        # Verify file was updated
        saved = json.loads(settings_file.read_text())
        assert saved["profile"]["human_title"] == "CTO"
        assert saved["profile"]["human_org"] == "NewCo"

    def test_post_does_not_save_readonly_fields(self, identity_file, settings_file):
        """POST /api/profile must NOT save identity-file fields (civ_name, human_name, etc.)."""
        import portal_server

        async def _test():
            body = {"civ_name": "Hacked", "human_name": "Evil", "role": "Updated Role"}
            with patch.object(portal_server, "check_auth", return_value=True), \
                 patch.object(portal_server, "SETTINGS_FILE", settings_file), \
                 patch("portal_server.Path.home", return_value=identity_file.parent):
                resp = await portal_server.api_profile(FakeRequest("POST", body))
            return json.loads(resp.body.decode())

        result = _run(_test())
        assert result.get("ok") is True

        # civ_name and human_name must NOT be in settings profile
        saved = json.loads(settings_file.read_text())
        profile = saved.get("profile", {})
        assert profile.get("civ_name") is None, "civ_name must not be saved to settings"
        assert profile.get("human_name") is None, "human_name must not be saved to settings"
        # But the editable field should have been saved
        assert profile.get("role") == "Updated Role"

    def test_post_preserves_existing_profile_fields(self, identity_file, settings_file):
        """POST /api/profile should merge, not replace, the profile section."""
        import portal_server

        async def _test():
            body = {"website": "newsite.com"}
            with patch.object(portal_server, "check_auth", return_value=True), \
                 patch.object(portal_server, "SETTINGS_FILE", settings_file), \
                 patch("portal_server.Path.home", return_value=identity_file.parent):
                resp = await portal_server.api_profile(FakeRequest("POST", body))
            return json.loads(resp.body.decode())

        _run(_test())

        saved = json.loads(settings_file.read_text())
        profile = saved.get("profile", {})
        # New field saved
        assert profile["website"] == "newsite.com"
        # Old fields preserved
        assert profile["role"] == "Conductor of Conductors"
        assert profile["human_title"] == "CEO & Founder"


# ===========================================================================
# Test 4: GET /api/profile merges both sources correctly
# ===========================================================================

class TestProfileMerge:

    def test_merge_identity_and_settings(self, identity_file, settings_file):
        """Response should contain fields from BOTH identity file and settings."""
        import portal_server

        async def _test():
            with patch.object(portal_server, "check_auth", return_value=True), \
                 patch.object(portal_server, "SETTINGS_FILE", settings_file), \
                 patch("portal_server.Path.home", return_value=identity_file.parent):
                resp = await portal_server.api_profile(FakeRequest("GET"))
            return json.loads(resp.body.decode())

        data = _run(_test())

        # From identity file
        assert data["civ_name"] == "Flux"
        assert data["human_name"] == "Alex Seant"
        assert data["human_email"] == "ipushdabutton@gmail.com"

        # From settings file
        assert data["role"] == "Conductor of Conductors"
        assert data["human_title"] == "CEO & Founder"
        assert data["civ_email"] == "flux.civ@agentmail.to"

    def test_civ_email_falls_back_to_email_accounts(self, identity_file, tmp_path):
        """When profile has no civ_email, fall back to first email_accounts entry."""
        import portal_server

        settings_file = tmp_path / "user-settings.json"
        settings_file.write_text(json.dumps({
            "profile": {},
            "email_accounts": [{"address": "fallback@agentmail.to"}]
        }))

        async def _test():
            with patch.object(portal_server, "check_auth", return_value=True), \
                 patch.object(portal_server, "SETTINGS_FILE", settings_file), \
                 patch("portal_server.Path.home", return_value=identity_file.parent):
                resp = await portal_server.api_profile(FakeRequest("GET"))
            return json.loads(resp.body.decode())

        data = _run(_test())
        assert data["civ_email"] == "fallback@agentmail.to"


# ===========================================================================
# Test 5: GET /api/profile returns sensible defaults when identity missing
# ===========================================================================

class TestProfileDefaults:

    def test_defaults_when_no_identity_file(self, tmp_path, empty_settings_file):
        """When identity file doesn't exist, should return empty strings for identity fields."""
        import portal_server

        # Point home to tmp_path which has NO .aiciv-identity.json
        async def _test():
            with patch.object(portal_server, "check_auth", return_value=True), \
                 patch.object(portal_server, "SETTINGS_FILE", empty_settings_file), \
                 patch("portal_server.Path.home", return_value=tmp_path):
                resp = await portal_server.api_profile(FakeRequest("GET"))
            return json.loads(resp.body.decode())

        data = _run(_test())
        assert data["civ_name"] == ""
        assert data["human_name"] == ""
        assert data["born"] == ""
        assert data["parent_civ"] == ""
        assert data["status"] == "active"  # default status

    def test_defaults_for_editable_fields_when_no_profile_section(self, identity_file, empty_settings_file):
        """When settings has no profile section, editable fields get defaults."""
        import portal_server

        async def _test():
            with patch.object(portal_server, "check_auth", return_value=True), \
                 patch.object(portal_server, "SETTINGS_FILE", empty_settings_file), \
                 patch("portal_server.Path.home", return_value=identity_file.parent):
                resp = await portal_server.api_profile(FakeRequest("GET"))
            return json.loads(resp.body.decode())

        data = _run(_test())
        assert data["role"] == "AI Agent"  # default
        assert data["model"] == "Claude"   # default
        assert data["human_role"] == "Creator & Steward"  # default
        assert data["human_relationship"] == "Trust-based"  # default

    def test_unauthorized_returns_401(self, identity_file, settings_file):
        """Request without valid auth should return 401."""
        import portal_server

        async def _test():
            with patch.object(portal_server, "check_auth", return_value=False), \
                 patch.object(portal_server, "SETTINGS_FILE", settings_file), \
                 patch("portal_server.Path.home", return_value=identity_file.parent):
                resp = await portal_server.api_profile(FakeRequest("GET"))
            return resp.status_code

        status = _run(_test())
        assert status == 401

    def test_post_invalid_json_returns_400(self, identity_file, settings_file):
        """POST with invalid JSON body should return 400."""
        import portal_server

        class BadJsonRequest:
            method = "POST"
            headers = {"authorization": "Bearer test-token"}
            async def json(self):
                raise ValueError("bad json")

        async def _test():
            with patch.object(portal_server, "check_auth", return_value=True), \
                 patch.object(portal_server, "SETTINGS_FILE", settings_file), \
                 patch("portal_server.Path.home", return_value=identity_file.parent):
                resp = await portal_server.api_profile(BadJsonRequest())
            return resp.status_code

        status = _run(_test())
        assert status == 400
