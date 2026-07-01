"""Tests for portal_config.py — the shared configuration module.

Verifies that all constants, helpers, and shared functions work correctly
and stay in sync with portal_server.py.
"""
import sys
import os
from pathlib import Path
from unittest import mock

import pytest

# Add parent dir so we can import portal_config
sys.path.insert(0, str(Path(__file__).parent.parent))

import portal_config as cfg


class TestConstants:
    """Verify immutable config constants are set correctly."""

    def test_script_dir_exists(self):
        assert cfg.SCRIPT_DIR.exists()

    def test_portal_version_is_string(self):
        import re
        assert isinstance(cfg.PORTAL_VERSION, str)
        assert re.match(r'^\d+\.\d+\.\d+', cfg.PORTAL_VERSION), \
            f"PORTAL_VERSION should be valid semver, got '{cfg.PORTAL_VERSION}'"

    def test_bearer_token_is_nonempty(self):
        assert len(cfg.BEARER_TOKEN) > 10

    def test_db_paths_are_paths(self):
        assert isinstance(cfg.REFERRALS_DB, Path)
        assert isinstance(cfg.CLIENTS_DB, Path)
        assert isinstance(cfg.AGENTS_DB, Path)

    def test_uploads_dir_exists(self):
        assert cfg.UPLOADS_DIR.exists()

    def test_referral_constants(self):
        assert cfg.REFERRAL_CODE_PREFIX == "PB-"
        assert cfg.REFERRAL_CODE_LENGTH == 4
        assert cfg.REFERRAL_COMMISSION_RATE == 0.05

    def test_payout_constants(self):
        assert cfg.PAYOUT_MIN_AMOUNT == 25.0
        assert cfg.PAYOUT_AUTO_APPROVE_LIMIT == 1000.0
        assert cfg.PAYOUT_COOLDOWN_DAYS == 30

    def test_auth_screen_patterns_complete(self):
        expected_keys = {
            'oauth_url', 'login_menu', 'csat_survey', 'update_prompt',
            'trust_folder', 'theme_picker', 'logged_in', 'shell_prompt', 'error',
        }
        assert set(cfg.AUTH_SCREEN_PATTERNS.keys()) == expected_keys

    def test_auth_screen_priority_complete(self):
        assert len(cfg.AUTH_SCREEN_PRIORITY) == 9
        for key in cfg.AUTH_SCREEN_PRIORITY:
            assert key in cfg.AUTH_SCREEN_PATTERNS


class TestSanitizeError:
    """Verify _sanitize_error returns safe messages."""

    def test_basic_error(self):
        result = cfg.sanitize_error(ValueError("secret path /etc/passwd"), "test")
        assert "secret" not in result
        assert "/etc/passwd" not in result
        assert "Internal error: test" == result

    def test_no_context(self):
        result = cfg.sanitize_error(RuntimeError("boom"))
        assert result == "Internal error"

    def test_with_context(self):
        result = cfg.sanitize_error(Exception("details"), "file read")
        assert result == "Internal error: file read"


class TestSubprocessHelpers:
    """Verify subprocess wrapper functions exist and have correct signatures."""

    def test_run_subprocess_sync_returns_none_on_bad_cmd(self):
        result = cfg.run_subprocess_sync(["__nonexistent_command__"], timeout=1)
        assert result is None

    def test_run_subprocess_sync_returns_result(self):
        result = cfg.run_subprocess_sync(["echo", "hello"], capture=True, text=True)
        assert result is not None
        assert "hello" in result.stdout


class TestCheckAuth:
    """Verify check_auth works with bearer tokens."""

    def test_valid_bearer(self):
        req = mock.MagicMock()
        req.headers = {"authorization": f"Bearer {cfg.BEARER_TOKEN}"}
        req.url.path = "/api/status"
        assert cfg.check_auth(req) is True

    def test_invalid_bearer(self):
        req = mock.MagicMock()
        req.headers = {"authorization": "Bearer wrong-token"}
        req.url.path = "/api/status"
        assert cfg.check_auth(req) is False

    def test_no_auth_header(self):
        req = mock.MagicMock()
        req.headers = {}
        req.url.path = "/api/status"
        assert cfg.check_auth(req) is False

    def test_ws_query_param_token(self):
        req = mock.MagicMock()
        req.headers = {}
        req.url.path = "/ws/chat"
        req.query_params = {"token": cfg.BEARER_TOKEN}
        assert cfg.check_auth(req) is True

    def test_ws_wrong_query_param(self):
        req = mock.MagicMock()
        req.headers = {}
        req.url.path = "/ws/chat"
        req.query_params = {"token": "wrong"}
        assert cfg.check_auth(req) is False

    def test_download_query_param(self):
        req = mock.MagicMock()
        req.headers = {}
        req.url.path = "/api/download"
        req.query_params = {"token": cfg.BEARER_TOKEN}
        assert cfg.check_auth(req) is True

    def test_upload_serve_query_param(self):
        req = mock.MagicMock()
        req.headers = {}
        req.url.path = "/api/chat/uploads/image.png"
        req.query_params = {"token": cfg.BEARER_TOKEN}
        assert cfg.check_auth(req) is True


class TestFireAndForget:
    """Verify fire_and_forget tracks tasks."""

    @pytest.mark.asyncio
    async def test_tracks_and_cleans(self):
        import asyncio
        initial_count = len(cfg.background_tasks)

        async def quick():
            return 42

        task = cfg.fire_and_forget(quick())
        assert task in cfg.background_tasks
        await task
        # After completion, callback removes it
        await asyncio.sleep(0.01)
        assert task not in cfg.background_tasks


class TestSourceSync:
    """Verify portal_config.py functions match portal_server.py source."""

    def _read_function_body(self, source: str, func_name: str) -> str:
        """Extract function body from source code."""
        start = source.find(f"def {func_name}(")
        if start < 0:
            return ""
        end = source.find("\ndef ", start + 1)
        if end < 0:
            end = len(source)
        return source[start:end]

    def test_sanitize_error_defined(self):
        """Verify sanitize_error is defined in portal_config.py with logging."""
        source = (cfg.SCRIPT_DIR / "portal_config.py").read_text()
        body = self._read_function_body(source, "sanitize_error")
        assert 'print(f"[portal] ERROR {context}:' in body
        assert "Internal error" in body

    def test_check_auth_defined(self):
        """Verify check_auth is defined in portal_config.py."""
        source = (cfg.SCRIPT_DIR / "portal_config.py").read_text()
        body = self._read_function_body(source, "check_auth")
        assert "Bearer " in body
        assert "hmac.compare_digest" in body
        assert "/ws" in body
        assert "/api/chat/uploads/" in body
        assert "/api/download" in body

    def test_portal_server_imports_from_config(self):
        """Verify portal_server.py imports shared functions from portal_config."""
        source = (cfg.SCRIPT_DIR / "portal_server.py").read_text()
        assert "from portal_config import" in source
        assert "sanitize_error" in source
        assert "check_auth" in source

    def test_bearer_token_matches(self):
        """Token file is the single source of truth — both modules read it."""
        token_file = cfg.SCRIPT_DIR / ".portal-token"
        if token_file.exists():
            assert cfg.BEARER_TOKEN == token_file.read_text().strip()
