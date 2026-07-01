# ADR-003: Skills Shop as Core Built-In Panel

**Status:** Proposed
**Date:** 2026-06-06
**Deciders:** architect-agent, primary-ai, product owner (Alex)
**Supersedes:** ADR-002 Section 1 recommendation (which said "keep as custom panel")

---

## Context

The product owner has determined that skills are core to working with AIs. The Skills Shop MUST be a first-class built-in panel, shipping with every portal -- not a per-CIV customization. This ADR provides the exact implementation blueprint.

---

## 1. Exact File Locations

Following the existing patterns, these files are needed:

| File | Purpose | Pattern Source |
|------|---------|---------------|
| `static/js/features/skills.js` | Feature JS module (IIFE) | `static/js/features/hub.js`, `agents.js`, `deployments.js` |
| `static/css/panels.css` | Add CSS rules (append) | Lines 722-1313 in existing `panels.css` |
| `portal-pb-styled.html` | Add sidebar item + panel area div | Lines 195-211 (sidebar), lines 818-1042 (panel area pattern) |
| `portal_skills.py` | Backend module (extracted) | `portal_constitution.py`, `portal_gdrive.py`, `portal_referrals.py` |
| `portal_server.py` | Import + route registration | Lines 5582-5593 (import pattern), lines 6542-6547 (route pattern) |

---

## 2. Panel Registration in panel-manager.js

**File:** `static/js/core/panel-manager.js`
**Location:** Line 9, the `BUILTIN` map

**Add this entry:**

```js
var BUILTIN = {
    'letstalk': 'letstalkArea',
    'chat': 'chatArea',
    'tasks': 'tasksArea',
    'files': 'filesArea',
    'teams': 'teamsArea',
    'todo': 'todoArea',
    'agents': 'agentsArea',
    'agent-roster': 'agent-rosterArea',
    'refer': 'referArea',
    'about': 'aboutArea',
    'hub': 'hubArea',
    'constitution': 'constitutionArea',
    'deployments': 'deploymentsArea',
    'payments': 'paymentsArea',
    'settings': 'settingsArea',
    'clients': 'clientsArea',
    'skills': 'skillsArea'          // <-- ADD THIS
};
```

**data-tab name:** `skills`
**Element ID:** `skillsArea`

**Also add the tab callback** at line 100-116 in `_fireTabCallbacks`:

```js
if (tab === 'skills' && window._portalSkills) window._portalSkills.load();
```

---

## 3. Sidebar Placement

**File:** `portal-pb-styled.html`
**Location:** Inside the `data-group="agents"` sidebar group, lines 195-212

The Skills Shop belongs in the **Agents** group because skills are agent capabilities. Place it after "Agent Hub" (line 211), before the closing `</div>` of the agents group (line 212).

**Add after line 211:**

```html
      <a class="sidebar-item" href="#" data-tab="skills">
        <svg viewBox="0 0 24 24"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>
        Skills Shop
      </a>
```

This SVG is the Lucide "wrench" icon, consistent with the portal's icon style (all existing sidebar icons use Lucide-style SVG paths).

**Sidebar structure after change:**

```
Agents (group)
  |-- My AI Fleet      [data-tab="agents"]
  |-- Agent Roster      [data-tab="agent-roster"]
  |-- Agent Hub         [data-tab="hub"]
  |-- Skills Shop       [data-tab="skills"]    <-- NEW
```

---

## 4. Panel Area Div

**File:** `portal-pb-styled.html`
**Location:** Add after the last existing panel area div and before the `<!-- /panels -->` marker (line 1949)

**The convention:** Every built-in panel uses a `<div>` with:
- A CSS class named `{name}-area` (e.g., `hub-area`, `deploy-area`, `agents-area`)
- An ID matching the BUILTIN map value (e.g., `hubArea`, `deploymentsArea`, `skillsArea`)

**Add before line 1949 (`<!-- /panels -->`):**

```html
    <div class="skills-area" id="skillsArea">
      <!-- Skills Shop content rendered by static/js/features/skills.js -->
    </div>
```

The JS module will populate this div's innerHTML on first load, matching how `hub.js`, `agents.js`, and `deployments.js` work -- the HTML file provides the empty container, the JS module builds the DOM.

---

## 5. CSS Rules

**File:** `static/css/panels.css`
**Location:** Append after the deploy/settings area rules (around line 1313)

**Pattern (from lines 1096-1097, 1311-1313):**

```css
/* ── Skills Shop ────────────────────────────────────── */
.skills-area{display:none;flex-direction:column;flex:1;overflow:hidden;z-index:1;position:relative;background:rgba(10,14,26,0.92)}
.skills-area.visible{display:flex}
```

**Also add theme overrides** (following the existing pattern at lines 1477 and 1505):

```css
/* In the [data-theme="light"] section: */
[data-theme="light"] .skills-area{background:#e8eef4}

/* In the [data-theme="girly"] section: */
[data-theme="girly"] .skills-area{background:#fff1f2}
```

**Also add to the mobile overflow rule** at line 1603:

```css
/* Change from: */
.tasks-area,.files-area,.teams-area,.todo-area,.agents-area,.refer-area,.about-area,.hub-area,.payments-area,.clients-area{overflow-y:auto}
/* To: */
.tasks-area,.files-area,.teams-area,.todo-area,.agents-area,.refer-area,.about-area,.hub-area,.payments-area,.clients-area,.skills-area{overflow-y:auto}
```

The remaining CSS (cards, grid, overlays, search bar, etc.) goes inside `static/js/features/skills.js` as inline styles injected on init, OR preferably appended to `panels.css` with `.skills-area` scoping. The existing `custom/panels/skills-shop.html` has all the CSS scoped to `#panel-skills-shop` -- during migration, change the prefix from `#panel-skills-shop` to `.skills-area` (or `#skillsArea`).

**Recommendation:** Use `.skills-area` as the scoping prefix (consistent with `.hub-area`, `.agents-area`, etc.) for all internal component styles. Append them to `panels.css`.

---

## 6. Backend Endpoints -- portal_skills.py

**Create new file:** `portal_skills.py`

**Pattern source:** `portal_constitution.py` (lines 1-50), `portal_gdrive.py` (lines 1-50)

**Structure:**

```python
"""Portal Skills Shop API for the PureBrain Portal.

Extracted as a built-in module following portal_constitution.py pattern.
Contains: skill listing, skill detail, skill registry, install/uninstall.
"""
import json
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from portal_config import SCRIPT_DIR, check_auth

SKILLS_DIR = Path.home() / ".claude" / "skills"

async def api_skills_list(request: Request) -> JSONResponse:
    """GET /api/skills -- list all available skills."""
    ...

async def api_skills_detail(request: Request) -> JSONResponse:
    """GET /api/skills/{name} -- read full skill content."""
    ...
```

**Migrate the logic from** `custom/routes.py` lines 276-341 (`api_skills_list` and `api_skills_detail` functions). These become the canonical implementations.

---

## 7. Endpoint Namespace -- /api/skills/*

**Verification of no collision:**

Grepped the entire codebase. The existing endpoint families are:

| Existing Namespace | Location | Purpose |
|---|---|---|
| `/api/boops` | portal_server.py lines 6544-6547 | BOOP daemon management (list, toggle, update scheduled boops) |
| `/api/boop/*` | portal_server.py lines 6542-6543 | BOOP config read/write, daemon status |
| `/api/custom/skills` | custom/routes.py lines 346-347 | Custom panel skill listing/detail |

**`/api/skills/*` does NOT collide with anything.** The `/api/boops` endpoints serve a different purpose (BOOP daemon scheduling/management). The `/api/custom/skills` endpoints are the ones being superseded (see migration section below).

**New endpoint namespace:**

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/skills` | GET | List all skills with metadata |
| `/api/skills/{name}` | GET | Full skill content for detail view |
| `/api/skills/registry` | GET | (Future) External skills index |
| `/api/skills/install` | POST | (Future) Install external skill |
| `/api/skills/uninstall` | POST | (Future) Uninstall external skill |

Note: The `{name}` path parameter will NOT collide with the literal paths `registry`, `install`, `uninstall` because Starlette evaluates literal routes before parameterized ones when they are listed first in the routes array.

---

## 8. Route Registration in portal_server.py

### Import Block

**File:** `portal_server.py`
**Location:** After line 5593 (the portal_constitution import block), before line 5595 (portal_gdrive import)

**Add:**

```python
from portal_skills import (
    SKILLS_DIR as BUILTIN_SKILLS_DIR,
    api_skills_list, api_skills_detail,
)
```

Note: Rename the import to `BUILTIN_SKILLS_DIR` to avoid collision with the existing `SKILLS_DIR` at line 2249 of portal_server.py (used by the BOOP endpoints). Alternatively, since the BOOP endpoints' `SKILLS_DIR` at line 2249 serves the same path (`~/.claude/skills`), you could remove the portal_server.py definition and import it from portal_skills.py. But the safer approach is the aliased import.

### Route Registration

**File:** `portal_server.py`
**Location:** In the `routes = [...]` array. Add after the BOOP routes (line 6547) and before the agents routes (line 6548).

**Add:**

```python
    Route("/api/skills/registry", endpoint=api_skills_registry),   # literal first
    Route("/api/skills/install", endpoint=api_skills_install, methods=["POST"]),
    Route("/api/skills/uninstall", endpoint=api_skills_uninstall, methods=["POST"]),
    Route("/api/skills", endpoint=api_skills_list),
    Route("/api/skills/{name}", endpoint=api_skills_detail),
```

For Phase 1 (before marketplace), only the last two routes are needed:

```python
    Route("/api/skills", endpoint=api_skills_list),
    Route("/api/skills/{name:path}", endpoint=api_skills_detail),
```

Use `{name:path}` instead of `{name}` to handle skill names that might contain slashes in the future. Starlette's `path` converter matches the rest of the URL.

---

## 9. Feature JS Module -- static/js/features/skills.js

**Pattern:** Follow `static/js/features/hub.js`, `deployments.js`

**Structure:**

```js
(function(){
  'use strict';

  // Auth helpers from shared auth.js (_tok, _auth, _authJson)
  var _loaded = false;

  function _load() {
    if (_loaded) return;
    _loaded = true;
    _fetchSkills();
  }

  function _fetchSkills() {
    fetch('/api/skills', { headers: _auth() })
      .then(function(r) { return r.json(); })
      .then(function(data) { _render(data.skills || []); })
      .catch(function(e) { /* error handling */ });
  }

  // ... render grid, search, detail overlay, inject-to-chat ...

  // Expose for panel-manager callback
  window._portalSkills = { load: _load, refresh: _fetchSkills };
})();
```

**Key differences from custom panel JS:**
1. Uses `_auth()` from shared `auth.js` instead of manually building headers
2. Uses `escHtml` from global scope (provided by portal core) instead of defining its own
3. Calls `/api/skills` instead of `/api/custom/skills`
4. Registered via `window._portalSkills` (for `_fireTabCallbacks`) instead of `window._customPanelHandlers`

**Script tag in HTML:** Add to the bottom of `portal-pb-styled.html`, after the other feature script tags:

```html
<script src="/static/js/features/skills.js?v=20260606"></script>
```

(Find the existing `<script src="/static/js/features/...">` tags and add after them.)

---

## 10. Migration from Custom Panel

### Phase 1: Build the built-in version

1. Create `portal_skills.py` with the endpoint logic from `custom/routes.py` lines 276-341
2. Create `static/js/features/skills.js` migrating JS from `custom/panels/skills-shop.html` lines 382-661
3. Add CSS to `static/css/panels.css` migrating styles from `custom/panels/skills-shop.html` lines 46-378
4. Add sidebar item and panel area div to `portal-pb-styled.html`
5. Add `'skills': 'skillsArea'` to BUILTIN map in `panel-manager.js`
6. Add tab callback for `skills` in `_fireTabCallbacks`
7. Import and register routes in `portal_server.py`

### Phase 2: Update tests

Update `tests/test_skills_shop.py` to point to the new locations:
- `PANEL_FILE` should test against `static/js/features/skills.js` for JS tests
- CSS tests should read from `static/css/panels.css`
- API endpoint tests should reference `/api/skills` not `/api/custom/skills`
- Remove the custom panel frontmatter tests (no longer applicable -- built-in panels do not use frontmatter)

### Phase 3: Remove custom panel

**After the built-in version is verified working:**

1. **Delete** `custom/panels/skills-shop.html`
2. **Remove** the `api_skills_list` and `api_skills_detail` functions from `custom/routes.py` (lines 276-341)
3. **Remove** the two skill routes from the `routes = [...]` array in `custom/routes.py` (lines 346-347)
4. **Remove** the `SKILLS_DIR` constant from `custom/routes.py` (line 19) -- it now lives in `portal_skills.py`

**Do NOT keep both temporarily.** The custom panel injection system will inject `skills-shop` into the DOM alongside the built-in `skills` panel, creating two panels for the same concept. Delete the custom panel as part of the same changeset.

### Phase 4: Backward compatibility for existing CIVs

Other CIVs using the portal may still have `custom/panels/skills-shop.html`. The custom panel injection system will still work for them -- it is additive and does not break if a built-in panel with a different name exists. The custom panel uses `panel-skills-shop` as its ID while the built-in uses `skills` / `skillsArea`, so there is no DOM ID collision. However, once those CIVs update their portal, the custom panel becomes redundant. The release notes should mention this.

---

## 11. Conflict with Existing Skill-Related Code in portal_server.py

**Identified conflicts and resolutions:**

### A. SKILLS_DIR constant (line 2249)

```python
SKILLS_DIR = Path.home() / ".claude" / "skills"
```

This is used by the BOOP endpoints (`api_boops_list` at line 2300, `api_boop_read` at line 2314). It points to the same directory as the new `portal_skills.py` will use.

**Resolution:** Leave the portal_server.py `SKILLS_DIR` in place (BOOP endpoints depend on it). The new `portal_skills.py` defines its own `SKILLS_DIR` constant -- same value, independent declaration. This follows the pattern of `portal_constitution.py` and `portal_gdrive.py` which each define their own constants.

### B. api_boops_list (line 2300) vs api_skills_list

`api_boops_list` returns `{"boops": [{"name": ..., "path": ...}]}` -- a simpler listing for the BOOP config UI.
`api_skills_list` returns `{"skills": [{"name": ..., "description": ..., "path": ..., "has_skill_file": ...}]}` -- richer metadata for the Skills Shop.

**Resolution:** Keep both. They serve different UIs with different data shapes. The BOOP listing is consumed by the Settings panel's BOOP configuration. The skills listing is consumed by the Skills Shop panel. Same underlying directory, different views.

### C. api_boop_read (line 2314) vs api_skills_detail

Both read a skill's SKILL.md content. `api_boop_read` is at `/api/boops/{name}` (not currently in the routes array -- it exists as a function but is not mounted as a route). The custom `api_skills_detail` is at `/api/custom/skills/{name}`.

**Resolution:** The new `/api/skills/{name}` endpoint replaces the custom one. The `api_boop_read` function can remain as dead code or be removed in a cleanup pass -- it is not mounted to any route.

---

## 12. Summary Checklist

| Step | File | Change |
|------|------|--------|
| 1 | `portal_skills.py` | CREATE -- migrate skill list + detail endpoints |
| 2 | `static/js/features/skills.js` | CREATE -- migrate JS from custom panel |
| 3 | `static/css/panels.css` | APPEND -- add `.skills-area` styles + migrate component CSS |
| 4 | `portal-pb-styled.html` | ADD sidebar item in Agents group (after line 211) |
| 5 | `portal-pb-styled.html` | ADD `<div class="skills-area" id="skillsArea">` before `<!-- /panels -->` |
| 6 | `portal-pb-styled.html` | ADD `<script src="/static/js/features/skills.js">` tag |
| 7 | `static/js/core/panel-manager.js` | ADD `'skills': 'skillsArea'` to BUILTIN (line 26) |
| 8 | `static/js/core/panel-manager.js` | ADD tab callback in `_fireTabCallbacks` (line 111) |
| 9 | `portal_server.py` | ADD import from portal_skills (after line 5593) |
| 10 | `portal_server.py` | ADD Route entries in routes array (after line 6547) |
| 11 | `custom/panels/skills-shop.html` | DELETE |
| 12 | `custom/routes.py` | REMOVE skill endpoints (lines 19, 276-347) |
| 13 | `tests/test_skills_shop.py` | UPDATE to test new locations + endpoints |

---

## Implementation Notes for Coder

- The existing `custom/panels/skills-shop.html` is 661 lines of working, tested code. The migration is primarily structural (moving code to proper locations) not functional. The UI logic, CSS, and behavior should be preserved with minimal changes.
- The main code changes during migration are:
  1. Replace `escHtml` local definition with the global `escHtml` from `auth.js`
  2. Replace `authHeaders()` with `_auth()` from shared `auth.js`
  3. Replace `'/api/custom/skills'` with `'/api/skills'` in fetch calls
  4. Replace `window._customPanelHandlers['skills-shop']` with `window._portalSkills = { load: ... }`
  5. Replace `#panel-skills-shop` CSS scoping with `.skills-area` or `#skillsArea`
- TDD: Write tests FIRST for the new locations, then migrate code to make them pass.
