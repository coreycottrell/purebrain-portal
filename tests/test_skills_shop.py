"""Tests for Skills Shop — core panel for browsing, installing, and managing skills.

TDD: These tests are written BEFORE the implementation.
"""
import json
import re
from pathlib import Path

import pytest

PORTAL_DIR = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# 1. Backend module exists and follows portal patterns
# ---------------------------------------------------------------------------

class TestSkillsModule:
    """Verify portal_skills.py exists and follows module patterns."""

    def test_module_exists(self):
        assert (PORTAL_DIR / "portal_skills.py").exists(), (
            "portal_skills.py must exist as a core module"
        )

    def test_imports_portal_config(self):
        src = (PORTAL_DIR / "portal_skills.py").read_text()
        assert "from portal_config import" in src, (
            "portal_skills.py must import from portal_config (standard pattern)"
        )

    def test_has_skills_list_endpoint(self):
        src = (PORTAL_DIR / "portal_skills.py").read_text()
        assert "async def api_skills_list" in src, (
            "Must have api_skills_list endpoint"
        )

    def test_has_skills_detail_endpoint(self):
        src = (PORTAL_DIR / "portal_skills.py").read_text()
        assert "async def api_skills_detail" in src, (
            "Must have api_skills_detail endpoint"
        )

    def test_has_skills_install_endpoint(self):
        src = (PORTAL_DIR / "portal_skills.py").read_text()
        assert "async def api_skills_install" in src, (
            "Must have api_skills_install endpoint"
        )

    def test_has_skills_uninstall_endpoint(self):
        src = (PORTAL_DIR / "portal_skills.py").read_text()
        assert "async def api_skills_uninstall" in src, (
            "Must have api_skills_uninstall endpoint"
        )

    def test_has_registry_endpoint(self):
        src = (PORTAL_DIR / "portal_skills.py").read_text()
        assert "async def api_skills_registry" in src, (
            "Must have api_skills_registry endpoint"
        )


# ---------------------------------------------------------------------------
# 2. Endpoint routes registered in portal_server.py
# ---------------------------------------------------------------------------

class TestRouteRegistration:
    """Verify skills routes are registered in portal_server.py."""

    @pytest.fixture
    def server_src(self):
        return (PORTAL_DIR / "portal_server.py").read_text()

    def test_skills_list_route(self, server_src):
        assert '"/api/skills"' in server_src, (
            "Route /api/skills must be registered in portal_server.py"
        )

    def test_skills_install_route(self, server_src):
        assert '"/api/skills/install"' in server_src, (
            "Route /api/skills/install must be registered"
        )

    def test_skills_uninstall_route(self, server_src):
        assert '"/api/skills/uninstall"' in server_src, (
            "Route /api/skills/uninstall must be registered"
        )

    def test_skills_registry_route(self, server_src):
        assert '"/api/skills/registry"' in server_src, (
            "Route /api/skills/registry must be registered"
        )

    def test_imports_skills_module(self, server_src):
        assert "portal_skills" in server_src, (
            "portal_server.py must import from portal_skills"
        )


# ---------------------------------------------------------------------------
# 3. Panel registration in frontend
# ---------------------------------------------------------------------------

class TestPanelRegistration:
    """Verify Skills Shop is registered as a built-in panel."""

    def test_builtin_map_has_skills(self):
        src = (PORTAL_DIR / "static/js/core/panel-manager.js").read_text()
        assert "'skills'" in src and "'skillsArea'" in src, (
            "panel-manager.js BUILTIN map must include 'skills': 'skillsArea'"
        )

    def test_tab_callback_registered(self):
        src = (PORTAL_DIR / "static/js/core/panel-manager.js").read_text()
        assert "_portalSkills" in src, (
            "panel-manager.js must have a tab callback for skills panel"
        )

    def test_html_has_skills_area(self):
        html = (PORTAL_DIR / "portal-pb-styled.html").read_text()
        assert 'id="skillsArea"' in html, (
            "portal-pb-styled.html must have a skillsArea panel div"
        )

    def test_sidebar_has_skills_tab(self):
        html = (PORTAL_DIR / "portal-pb-styled.html").read_text()
        assert 'data-tab="skills"' in html, (
            "Sidebar must have a skills tab item"
        )

    def test_skills_js_loaded(self):
        html = (PORTAL_DIR / "portal-pb-styled.html").read_text()
        assert "skills.js" in html, (
            "portal-pb-styled.html must load static/js/features/skills.js"
        )


# ---------------------------------------------------------------------------
# 4. Frontend module structure
# ---------------------------------------------------------------------------

class TestFrontendModule:
    """Verify skills.js follows portal module patterns."""

    def test_module_exists(self):
        assert (PORTAL_DIR / "static/js/features/skills.js").exists(), (
            "static/js/features/skills.js must exist"
        )

    def test_exposes_portal_skills(self):
        src = (PORTAL_DIR / "static/js/features/skills.js").read_text()
        assert "window._portalSkills" in src, (
            "skills.js must expose window._portalSkills for panel-manager callback"
        )

    def test_has_load_function(self):
        src = (PORTAL_DIR / "static/js/features/skills.js").read_text()
        assert "load" in src, (
            "skills.js must have a load function"
        )

    def test_has_search_functionality(self):
        src = (PORTAL_DIR / "static/js/features/skills.js").read_text()
        assert "search" in src.lower(), (
            "skills.js must have search functionality"
        )

    def test_has_category_filter(self):
        src = (PORTAL_DIR / "static/js/features/skills.js").read_text()
        assert "categor" in src.lower(), (
            "skills.js must have category filtering"
        )

    def test_has_install_function(self):
        src = (PORTAL_DIR / "static/js/features/skills.js").read_text()
        assert "install" in src.lower(), (
            "skills.js must have install functionality"
        )


# ---------------------------------------------------------------------------
# 5. Security
# ---------------------------------------------------------------------------

class TestSkillsSecurity:
    """Verify security measures in skill installation."""

    def test_auth_check_on_endpoints(self):
        src = (PORTAL_DIR / "portal_skills.py").read_text()
        assert "check_auth" in src, (
            "Skills endpoints must check authentication"
        )

    def test_path_traversal_prevention(self):
        src = (PORTAL_DIR / "portal_skills.py").read_text()
        assert ".." in src, (
            "Skill install must check for path traversal (.. in name)"
        )

    def test_no_overwrite_local_skills(self):
        src = (PORTAL_DIR / "portal_skills.py").read_text()
        assert "installed-skills.json" in src, (
            "Must track installed skills in manifest to distinguish local vs external"
        )


# ---------------------------------------------------------------------------
# 6. Data files
# ---------------------------------------------------------------------------

class TestDataFiles:
    """Verify data file patterns."""

    def test_installed_skills_in_gitignore(self):
        gitignore = (PORTAL_DIR / ".gitignore").read_text()
        assert "installed-skills.json" in gitignore, (
            "installed-skills.json must be in .gitignore (user data)"
        )

    def test_installed_skills_in_preserved_files(self):
        src = (PORTAL_DIR / "portal_updates.py").read_text()
        assert "installed-skills.json" in src, (
            "installed-skills.json must be in _PRESERVED_FILES (survive updates)"
        )


# ---------------------------------------------------------------------------
# 7. Custom panel migration
# ---------------------------------------------------------------------------

class TestMigration:
    """Verify custom panel is removed after migration."""

    def test_custom_skills_shop_removed(self):
        assert not (PORTAL_DIR / "custom/panels/skills-shop.html").exists(), (
            "custom/panels/skills-shop.html must be removed after migration to core panel"
        )
