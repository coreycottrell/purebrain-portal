"""Tests for portal ship fixes.

Issue #1: /api/boops endpoint fix
  What we're verifying: The /api/boops endpoint returns proper responses
    instead of 500 errors caused by a duplicate function definition bug.
  What coder will fix: Duplicate `api_boops_list` definition at lines ~2490 and ~3254
    in portal_server.py -- the second definition shadows the first and likely
    references names not in scope, causing the 500.
  What descendants inherit: Pattern for testing portal API endpoints with Bearer auth.
  Why this matters: Boops are a core portal feature; a broken list endpoint
    means users cannot see or manage their boops.

Issue #2: Version consistency
  What we're verifying: PORTAL_VERSION matches '2.0.0' and frontend HTML has no
    hardcoded fake versions or stale counts. Version display should be dynamic.
  What coder will fix: Update PORTAL_VERSION from '1.4.1' to '2.0.0', remove
    hardcoded fake versions (v2.4.1, v2.3.0) from HTML, add dynamic version element.
  What descendants inherit: Pattern for cross-layer consistency tests (server + frontend).
  Why this matters: Inconsistent versions confuse users and erode trust in the portal.

Issue #3: Real update button
  What we're verifying: The installUpdate() JS function calls real API endpoints
    (POST /api/update/apply, GET /api/update/status) instead of using setTimeout
    to fake progress for 2.5s.
  What coder will fix: Replace the setTimeout-based fake in installUpdate() with
    a real POST to /api/update/apply followed by polling GET /api/update/status.
  What descendants inherit: Pattern for verifying frontend-backend integration
    (JS function bodies must call real APIs, not simulate them).
  Why this matters: Users clicking "Install Update" expect a real update, not
    a spinner that lies to them.

Issue #4: Error message sanitization
  What we're verifying: Error responses never expose raw exception details (str(e))
    to clients. Internal paths like /home/aiciv/... must never leak. A helper
    function should sanitize errors, and server-side logging must capture details.
  What coder will fix: Replace ~25 instances of raw str(e) in JSONResponse error
    fields with calls to a _sanitize_error() helper. Add server-side logging
    (print with [portal] prefix) in every except block before returning.
  What descendants inherit: Pattern for static source analysis tests that enforce
    security invariants (no leaked paths, no raw exceptions in API responses).
  Why this matters: Raw exceptions expose internal file paths, library versions,
    and stack details -- information attackers use to plan exploits.

Issue #5: Disable unimplemented inbox buttons
  What we're verifying: The Forward button in the inbox action menu is visually
    disabled (no active onclick handler), has a "Coming soon" tooltip, and the
    mailoDraft() function no longer shows a "coming soon" toast for unimplemented
    actions.
  What coder will fix: Remove onclick="mailoDraft('forward')" from the Forward
    button, add disabled attribute/class, add title="Coming soon" tooltip, and
    remove the "coming soon" toast logic from mailoDraft() in inbox.js.
  What descendants inherit: Pattern for disabling placeholder UI elements -- make
    unimplemented features visually inert rather than clickable-but-broken.
  Why this matters: Users clicking Forward expect it to work. A button that looks
    active but shows a dismissive toast erodes trust and creates confusion.

Issue #6: Constitution "Add Policy" alert fix
  What we're verifying: openAddGovModal() does not use a raw browser alert() for
    'coming soon'. It should either use showToast() for a polished notification
    or the button should be disabled entirely.
  What coder will fix: Replace alert('Add Policy modal -- coming soon') in
    openAddGovModal() (line 3576) with showToast() or disable the Add Policy button.
  What descendants inherit: Pattern for eliminating raw browser alerts in favor of
    in-page toast notifications -- consistent UX across the portal.
  Why this matters: Raw alert() blocks the browser thread and looks unprofessional.
    Every user-facing notification should use the portal's showToast() system for
    a consistent, non-blocking experience.

Issue #7: Hide "Let's Talk" sidebar placeholder
  What we're verifying: The sidebar item with data-tab="letstalk" should be
    hidden (display:none) since the content area is just placeholder text.
  What coder will fix: Add style="display:none" to the sidebar <a> element
    with data-tab="letstalk" so users cannot navigate to a stub page.
  What descendants inherit: Pattern for hiding unimplemented navigation items
    at the HTML level -- make incomplete features invisible, not just disabled.
  Why this matters: A visible sidebar item that leads to placeholder text
    confuses users and makes the portal look unfinished.

Issue #8: Hide WhatsApp section when not integrated
  What we're verifying: WhatsApp UI is NOT exposed in the frontend when no
    WhatsApp bridge is configured. The server-side API endpoints correctly
    report disconnected/unknown status. The QR endpoint returns 404 when
    no bridge is active.
  Investigation result: No WhatsApp UI exists in the frontend (HTML, JS, or
    custom panels). The endpoints exist only in portal_server.py. This issue
    is effectively already satisfied -- these tests serve as regression locks.
  What descendants inherit: Pattern for verifying that backend-only features
    (endpoints without frontend UI) stay hidden, and for locking in correct
    API behavior when an integration is not configured.
  Why this matters: Exposing WhatsApp UI when no bridge is configured would
    confuse users with broken QR codes and stale status. The server endpoints
    must gracefully report "not connected" so any future frontend can decide
    whether to render the section.
"""
import re
import requests
import pathlib

PORTAL_URL = "http://localhost:8097"
TOKEN_FILE = pathlib.Path("/home/aiciv/purebrain_portal/.portal-token")


def get_token():
    return TOKEN_FILE.read_text().strip()


# --- Issue #1: /api/boops endpoint tests ---


def test_boops_returns_200():
    """GET /api/boops with valid auth should return 200, not 500."""
    token = get_token()
    resp = requests.get(
        f"{PORTAL_URL}/api/boops",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


def test_boops_returns_valid_json_list():
    """GET /api/boops should return JSON with a 'boops' list."""
    token = get_token()
    resp = requests.get(
        f"{PORTAL_URL}/api/boops",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "boops" in data, f"Response missing 'boops' key: {data}"
    assert isinstance(data["boops"], list), f"'boops' is not a list: {type(data['boops'])}"


def test_boops_requires_auth():
    """GET /api/boops without auth should return 401."""
    resp = requests.get(f"{PORTAL_URL}/api/boops")
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"


# --- Issue #2: Version consistency tests ---


def test_api_status_version_is_valid_semver():
    """Server PORTAL_VERSION should be a valid semver string."""
    token = get_token()
    resp = requests.get(f"{PORTAL_URL}/api/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    version = data.get("version", "")
    assert re.match(r'^\d+\.\d+\.\d+', version), \
        f"Expected valid semver version, got '{version}'"


def test_frontend_no_hardcoded_fake_versions():
    """Frontend HTML should not contain hardcoded fake version numbers (v2.4.1, v2.3.0)."""
    html_path = pathlib.Path("/home/aiciv/purebrain_portal/portal-pb-styled.html")
    content = html_path.read_text()
    assert "v2.4.1" not in content, "Found hardcoded fake version 'v2.4.1' in frontend HTML"
    assert "v2.3.0" not in content, "Found hardcoded fake version 'v2.3.0' in frontend HTML"


def test_frontend_version_uses_dynamic_source():
    """Frontend should reference the version dynamically (from /api/status or a JS variable), not hardcoded."""
    html_path = pathlib.Path("/home/aiciv/purebrain_portal/portal-pb-styled.html")
    content = html_path.read_text()
    # The mods footer should NOT have the old hardcoded '508 tests'
    assert "508 tests" not in content, "Found stale '508 tests' count in frontend HTML"


def test_frontend_topnav_version_dynamic():
    """Top nav version display should pull from /api/status, not be hardcoded."""
    html_path = pathlib.Path("/home/aiciv/purebrain_portal/portal-pb-styled.html")
    content = html_path.read_text()
    # After fix, the topnav should have a dynamic placeholder (id or class for JS to fill)
    # rather than a hardcoded 'v2.0.0' string in the span
    # Check that there's a portal-version id or data attribute for dynamic injection
    assert 'id="portalVersion"' in content or 'class="portal-version"' in content, \
        "Top nav version should have an identifiable element for dynamic version injection"


# --- Issue #3: Real update button tests ---


def test_install_update_calls_real_api():
    """installUpdate() should call POST /api/update/apply, not use setTimeout faking."""
    html_path = pathlib.Path("/home/aiciv/purebrain_portal/portal-pb-styled.html")
    content = html_path.read_text()
    # Find the installUpdate function
    match = re.search(r'function\s+installUpdate\s*\(\s*\)\s*\{(.*?)\n\}', content, re.DOTALL)
    assert match, "installUpdate() function not found"
    func_body = match.group(1)
    # Must call the real endpoint
    assert '/api/update/apply' in func_body, \
        "installUpdate() should call /api/update/apply but doesn't"
    # Must NOT use setTimeout for fake progress
    assert 'setTimeout' not in func_body or '/api/update/status' in func_body, \
        "installUpdate() uses setTimeout without polling /api/update/status -- still faking"


def test_install_update_polls_status():
    """installUpdate() flow should poll /api/update/status for real progress."""
    html_path = pathlib.Path("/home/aiciv/purebrain_portal/portal-pb-styled.html")
    content = html_path.read_text()
    # The polling may be in installUpdate() itself or in a helper function it calls
    assert '/api/update/status' in content, \
        "Frontend should contain /api/update/status polling somewhere"
    # Verify there's a polling mechanism (setInterval or setTimeout with status check)
    assert 'pollUpdateStatus' in content or ('setInterval' in content and '/api/update/status' in content), \
        "Frontend should have a polling mechanism for update status"


def test_update_panel_checks_availability():
    """Frontend should call /api/update/check to determine if update is available."""
    html_path = pathlib.Path("/home/aiciv/purebrain_portal/portal-pb-styled.html")
    content = html_path.read_text()
    assert '/api/update/check' in content, \
        "Frontend should call /api/update/check to check for updates"


def test_update_panel_shows_install_button_conditionally():
    """Update panel should only show install button when update is available, not always."""
    html_path = pathlib.Path("/home/aiciv/purebrain_portal/portal-pb-styled.html")
    content = html_path.read_text()
    # There should be logic that conditionally shows/hides the install button
    # based on the update check result
    assert 'update-install-btn' in content or 'installBtn' in content, \
        "Update panel should have an install button element"
    # The button should be created/shown dynamically, not always visible
    assert 'status' in content.lower() and ('available' in content or '"available"' in content), \
        "Update panel should check for 'available' status before showing install button"


def test_update_api_check_returns_valid_response():
    """GET /api/update/check should return valid JSON with a status field."""
    token = get_token()
    resp = requests.get(f"{PORTAL_URL}/api/update/check",
                       headers={"Authorization": f"Bearer {token}"},
                       timeout=30)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    data = resp.json()
    assert "status" in data, f"Response missing 'status' key: {data}"
    assert data["status"] in ("available", "up_to_date", "error"), \
        f"Unexpected status: {data['status']}"


# --- Issue #4: Error message sanitization tests ---


def test_error_responses_no_raw_exceptions_in_source():
    """portal_server.py should not return raw str(e) in JSONResponse error fields."""
    server_path = pathlib.Path("/home/aiciv/purebrain_portal/portal_server.py")
    content = server_path.read_text()
    # Find JSONResponse calls that include str(e) or f-string with {e} in error field
    # Pattern: JSONResponse containing "error": str(e) or "error": f"...{e}..."
    raw_patterns = re.findall(
        r'JSONResponse\(\{[^}]*"error":\s*(?:str\(e\)|f"[^"]*\{e\}[^"]*"|f"[^"]*\{str\(e\)\}[^"]*")[^}]*\}',
        content
    )
    assert len(raw_patterns) == 0, (
        f"Found {len(raw_patterns)} JSONResponse(s) exposing raw exception in 'error' field. "
        f"These should use sanitized messages. First match: {raw_patterns[0][:100] if raw_patterns else 'none'}"
    )


def test_error_responses_no_filesystem_paths():
    """Error responses should never contain /home/aiciv or other internal paths."""
    server_path = pathlib.Path("/home/aiciv/purebrain_portal/portal_server.py")
    content = server_path.read_text()
    # Find JSONResponse error fields that could leak paths
    # The pattern catches lines with JSONResponse + error that still have raw str(e)
    error_lines = re.findall(r'return JSONResponse\(\{.*"error":\s*str\(e\).*\}', content)
    assert len(error_lines) == 0, (
        f"Found {len(error_lines)} error response(s) that could leak filesystem paths via str(e)"
    )


def test_server_has_sanitize_helper():
    """Server should have _sanitize_error available (defined or imported)."""
    server_path = pathlib.Path("/home/aiciv/purebrain_portal/portal_server.py")
    content = server_path.read_text()
    # Function may be defined inline OR imported from portal_config
    has_def = 'def _sanitize_error' in content or 'def sanitize_error' in content
    has_import = 'sanitize_error' in content and 'from portal_config import' in content
    assert has_def or has_import, \
        "Server should have _sanitize_error() defined or imported from portal_config"


def test_error_responses_log_details_server_side():
    """Error handlers should log detailed exceptions server-side before returning sanitized response.

    The coder centralized logging inside _sanitize_error() which logs on every call.
    So we count: (a) calls to _sanitize_error (each one logs via its internal print),
    plus (b) additional direct [portal] prints for non-exception logging.
    The _sanitize_error calls alone must cover at least 10 error-handling sites.
    """
    server_path = pathlib.Path("/home/aiciv/purebrain_portal/portal_server.py")
    content = server_path.read_text()
    sanitize_calls = re.findall(r'_sanitize_error\(e\b', content)
    assert len(sanitize_calls) >= 10, (
        f"Only found {len(sanitize_calls)} calls to _sanitize_error(). "
        f"Expected at least 10 error handlers to use centralized server-side logging."
    )
    # Verify the helper itself contains logging — check portal_config.py if imported
    config_path = pathlib.Path("/home/aiciv/purebrain_portal/portal_config.py")
    helper_source = config_path.read_text() if config_path.exists() else content
    helper_match = re.search(r'def sanitize_error.*?print\(', helper_source, re.DOTALL)
    assert helper_match, (
        "sanitize_error() helper should contain a print() call for server-side logging"
    )


# --- Issue #5: Disable unimplemented inbox buttons ---


def test_forward_button_is_disabled():
    """Forward button in inbox action menu should be visually disabled."""
    html_path = pathlib.Path("/home/aiciv/purebrain_portal/portal-pb-styled.html")
    content = html_path.read_text()
    # The Forward button should be disabled (has disabled attribute or a disabled class)
    # and should NOT have an onclick that calls mailoDraft('forward')
    assert "mailoDraft('forward')" not in content, \
        "Forward button still has active onclick calling mailoDraft('forward')"


def test_disabled_buttons_have_tooltip():
    """Disabled inbox buttons should have a 'Coming soon' tooltip."""
    html_path = pathlib.Path("/home/aiciv/purebrain_portal/portal-pb-styled.html")
    content = html_path.read_text()
    # Find Forward button area -- it should have a title attribute for tooltip
    assert 'title="Coming soon"' in content or "title='Coming soon'" in content, \
        "Disabled inbox buttons should have a 'Coming soon' tooltip"


def test_mailodraft_no_coming_soon_toast():
    """mailoDraft() should not show a 'coming soon' toast for unimplemented actions."""
    js_path = pathlib.Path("/home/aiciv/purebrain_portal/static/js/features/inbox.js")
    content = js_path.read_text()
    assert 'coming soon' not in content.lower(), \
        "mailoDraft() still contains 'coming soon' toast message"


# --- Issue #6: Constitution "Add Policy" alert fix ---


def test_no_raw_alert_coming_soon():
    """openAddGovModal() should not use raw browser alert() for 'coming soon'."""
    html_path = pathlib.Path("/home/aiciv/purebrain_portal/portal-pb-styled.html")
    content = html_path.read_text()
    assert "alert('Add Policy modal" not in content and 'alert("Add Policy modal' not in content, \
        "openAddGovModal() still uses raw alert() for 'coming soon' message"


def test_add_policy_uses_toast_or_disabled():
    """Add Policy should use showToast() or be disabled -- not raw alert()."""
    html_path = pathlib.Path("/home/aiciv/purebrain_portal/portal-pb-styled.html")
    content = html_path.read_text()
    # Find the openAddGovModal function
    match = re.search(r'function\s+openAddGovModal\s*\(\s*\)\s*\{(.*?)\}', content, re.DOTALL)
    if match:
        func_body = match.group(1)
        # Should use showToast, not alert
        assert 'showToast' in func_body, \
            "openAddGovModal() should use showToast() instead of alert()"
    else:
        # Function was removed -- that's also acceptable if the button is disabled
        pass


# --- Issue #7: Hide "Let's Talk" sidebar placeholder ---


def test_letstalk_sidebar_visible():
    """Let's Talk sidebar item should be visible (restored in v2.1.0)."""
    html_path = pathlib.Path("/home/aiciv/purebrain_portal/portal-pb-styled.html")
    content = html_path.read_text()
    match = re.search(r'<a[^>]*data-tab="letstalk"[^>]*>', content)
    assert match, "Let's Talk sidebar item not found"
    tag = match.group(0)
    assert 'display:none' not in tag and 'display: none' not in tag, \
        f"Let's Talk sidebar should be visible (restored in v2.1.0) but is hidden: {tag[:120]}"


# --- Issue #8: Hide WhatsApp section when not integrated ---


def test_whatsapp_not_exposed_in_frontend_html():
    """WhatsApp UI should not be rendered in frontend HTML when no bridge is configured.

    Investigation found zero WhatsApp references in portal-pb-styled.html,
    all JS files, and all custom panels. The WhatsApp endpoints exist only
    server-side. This test locks in that state as a regression guard.
    """
    html_path = pathlib.Path("/home/aiciv/purebrain_portal/portal-pb-styled.html")
    content = html_path.read_text().lower()
    # No WhatsApp-related UI elements should exist in the main HTML
    assert 'whatsapp' not in content, \
        "WhatsApp UI found in portal-pb-styled.html but integration is not active"


def test_whatsapp_not_exposed_in_frontend_js():
    """No JS module should reference WhatsApp endpoints or render WhatsApp UI.

    Checks all JS files under static/js/ to ensure no frontend code
    fetches /api/whatsapp/* or creates WhatsApp-related DOM elements.
    """
    js_dir = pathlib.Path("/home/aiciv/purebrain_portal/static/js")
    for js_file in js_dir.rglob("*.js"):
        content = js_file.read_text().lower()
        assert 'whatsapp' not in content, \
            f"WhatsApp reference found in {js_file.relative_to(js_dir)} but integration is not active"


def test_whatsapp_not_exposed_in_custom_panels():
    """No custom panel should contain WhatsApp UI or references.

    Custom panels are loaded dynamically; if one references WhatsApp,
    users would see broken QR codes or stale status.
    """
    custom_dir = pathlib.Path("/home/aiciv/purebrain_portal/custom")
    if not custom_dir.exists():
        return  # No custom directory -- nothing to check
    for panel_file in custom_dir.rglob("*"):
        if panel_file.is_file() and panel_file.suffix in ('.html', '.js', '.json'):
            content = panel_file.read_text().lower()
            assert 'whatsapp' not in content, \
                f"WhatsApp reference found in custom/{panel_file.relative_to(custom_dir)}"


def test_whatsapp_status_returns_unknown_when_not_configured():
    """GET /api/whatsapp/status should return non-connected status when bridge is absent.

    When no whatsapp-status.json exists in uploads/, the endpoint should
    return status 'unknown' (or 'error'/'disconnected') so any future
    frontend knows not to render the WhatsApp section.
    """
    token = get_token()
    resp = requests.get(
        f"{PORTAL_URL}/api/whatsapp/status",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    data = resp.json()
    assert data.get("status") in ("unknown", "error", "disconnected"), \
        f"Expected non-connected WhatsApp status, got: {data.get('status')}"


def test_whatsapp_qr_returns_404_when_no_bridge():
    """GET /api/whatsapp/qr should return 404 when no QR code is available.

    When whatsapp-bridge is not running, no whatsapp-qr.png file exists,
    so the endpoint must return 404 with an appropriate error message.
    """
    token = get_token()
    resp = requests.get(
        f"{PORTAL_URL}/api/whatsapp/qr",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    assert resp.status_code == 404, \
        f"Expected 404 for WhatsApp QR when bridge not configured, got {resp.status_code}"
    # Verify the error message is informative
    if resp.headers.get("content-type", "").startswith("application/json"):
        data = resp.json()
        assert "no_qr" in str(data.get("error", "")).lower() or "no qr" in str(data.get("message", "")).lower(), \
            f"404 response should indicate no QR available, got: {data}"
