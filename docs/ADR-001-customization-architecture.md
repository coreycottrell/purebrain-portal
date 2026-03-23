# ADR-001: Plugin/Extension Architecture for PureBrain Portal

**Status:** Proposed
**Date:** 2026-03-22
**Author:** architect-agent

## Context

The PureBrain Portal is a two-file monolith: `portal_server.py` (~7000 lines, Starlette) and `portal-pb-styled.html` (~17800 lines, single-file SPA). Upstream (the Prodigy/Pure Brain team) ships updates as wholesale file replacements. Individual AiCIVs customize the portal with new panels (e.g., Skills Shop), new API endpoints, config overrides (e.g., MAX_TOKENS = 870k), and custom Quick Fire commands. Every upstream update risks overwriting these customizations -- Alex's Skills Shop section was already lost this way.

There is currently zero separation between core portal code and per-CIV customizations.

## Decision Drivers

1. **Upstream must keep shipping easily.** They currently drop in replacement files. Any solution that requires them to adopt a complex build system or plugin API will not be adopted.
2. **AiCIV operators may not be technical.** The solution must be understandable by non-developers.
3. **Incremental adoption.** Cannot require rewriting 25,000 lines of existing code in one pass.
4. **Must handle both backend (Python routes) and frontend (HTML/CSS/JS panels).**
5. **Must survive `cp portal_server.py portal_server.py` -- the literal update mechanism.**

## Considered Options

### Option A: Git Branch Rebase Workflow
Maintain a `core` branch tracking upstream and a `custom` branch with CIV-specific changes. On upstream update, rebase `custom` onto new `core`.

**Rejected.** Requires every AiCIV operator to understand git rebase, merge conflicts, and branching. Merge conflicts in a 17,800-line HTML file are nightmarish. Non-technical operators cannot do this.

### Option B: Full Plugin Framework with Registration API
Build a formal plugin system: plugins register routes, panels, and hooks via a Python API. Panels are defined as separate HTML files loaded via iframes or dynamic injection.

**Rejected.** Too much upfront work. Requires upstream to fundamentally restructure their code around a plugin API. Would take weeks to implement and would change how upstream ships code.

### Option C: Overlay Files with Auto-Loading (RECOMMENDED)
Core files stay exactly as upstream ships them. CIV customizations live in separate, conventionally-named files that are auto-loaded by a thin shim layer. The shim itself is small enough to survive upstream updates (or be trivially re-added).

## Decision Outcome

**Chosen Option:** Option C -- Overlay Files with Auto-Loading

This approach adds exactly two small, stable integration points to the core files. All customization lives in separate files that upstream never touches.

---

## Architecture

### Directory Structure

```
purebrain_portal/
  portal_server.py          # UPSTREAM (never edit for customizations)
  portal-pb-styled.html     # UPSTREAM (never edit for customizations)
  static/
    commands-shortcuts.js    # UPSTREAM

  custom/                   # CIV CUSTOMIZATIONS (upstream never touches this dir)
    config.json             # Config overrides (MAX_TOKENS, CIV_NAME, etc.)
    routes.py               # Custom API endpoints
    panels/                 # Custom sidebar panels
      skills-shop.html      # Each file = one panel (HTML + CSS + JS)
      my-dashboard.html     # Another custom panel
    quickfire.json           # Custom Quick Fire commands to merge with defaults
    startup.py              # Custom startup hooks (optional)
```

### How It Works: Backend (portal_server.py)

**The shim:** Add exactly ONE code block near the end of `portal_server.py`, right before the `routes = [...]` list. This block:

1. Loads `custom/config.json` to override config values
2. Imports `custom/routes.py` to get additional Route objects
3. Appends custom routes to the routes list

```python
# ─── CUSTOMIZATION LAYER (do not remove on upstream update) ────────────
_CUSTOM_DIR = SCRIPT_DIR / "custom"
_CUSTOM_ROUTES_FILE = _CUSTOM_DIR / "routes.py"
_CUSTOM_CONFIG_FILE = _CUSTOM_DIR / "config.json"

# 1. Config overrides
if _CUSTOM_CONFIG_FILE.exists():
    try:
        _custom_cfg = json.loads(_CUSTOM_CONFIG_FILE.read_text())
        for _k, _v in _custom_cfg.items():
            if _k in globals():
                globals()[_k] = _v
                print(f"[portal-custom] Config override: {_k} = {_v}")
    except Exception as _e:
        print(f"[portal-custom] WARNING: config.json load failed: {_e}")

# 2. Custom routes
_custom_routes = []
if _CUSTOM_ROUTES_FILE.exists():
    try:
        import importlib.util
        _spec = importlib.util.spec_from_file_location("custom_routes", str(_CUSTOM_ROUTES_FILE))
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        if hasattr(_mod, 'routes'):
            _custom_routes = _mod.routes
            print(f"[portal-custom] Loaded {len(_custom_routes)} custom route(s)")
    except Exception as _e:
        print(f"[portal-custom] WARNING: routes.py load failed: {_e}")

# 3. Custom startup hooks
_custom_startup_hooks = []
_custom_startup_file = _CUSTOM_DIR / "startup.py"
if _custom_startup_file.exists():
    try:
        _spec2 = importlib.util.spec_from_file_location("custom_startup", str(_custom_startup_file))
        _mod2 = importlib.util.module_from_spec(_spec2)
        _spec2.loader.exec_module(_mod2)
        if hasattr(_mod2, 'on_startup'):
            _custom_startup_hooks.append(_mod2.on_startup)
            print("[portal-custom] Loaded custom startup hook")
    except Exception as _e:
        print(f"[portal-custom] WARNING: startup.py load failed: {_e}")
# ─── END CUSTOMIZATION LAYER ──────────────────────────────────────────
```

Then in the routes list, append custom routes:

```python
routes = [
    # ... all existing routes ...
    *_custom_routes,   # <-- ONE LINE ADDED
]
```

And in the startup function:

```python
async def _startup() -> None:
    # ... existing startup code ...
    for _hook in _custom_startup_hooks:  # <-- TWO LINES ADDED
        await _hook()
```

**Total lines added to portal_server.py: ~35 (the shim) + 2 (route append + startup hook).**

If upstream replaces portal_server.py, the CIV just re-adds the shim. The shim is small, self-contained, and clearly delimited with comment markers.

### How It Works: Frontend (portal-pb-styled.html)

**The approach:** Instead of editing the HTML file directly, serve it through a thin templating layer that injects custom panels at runtime.

**Change to index/index_pb endpoints:**

Replace `FileResponse` with a response that reads the HTML, injects custom panel HTML at a marker point, and returns it:

```python
async def index_pb(request: Request) -> Response:
    if not PORTAL_PB_HTML.exists():
        return Response("<h1>PB Portal not found</h1>", media_type="text/html", status_code=503)

    html = PORTAL_PB_HTML.read_text()

    # Inject custom panels
    custom_panels_dir = SCRIPT_DIR / "custom" / "panels"
    if custom_panels_dir.exists():
        nav_items = []
        panel_html = []
        mobile_items = []
        for panel_file in sorted(custom_panels_dir.glob("*.html")):
            content = panel_file.read_text()
            # Each panel file has a frontmatter-style header:
            # <!-- panel-id: skills-shop -->
            # <!-- panel-label: Skills Shop -->
            # <!-- panel-icon: &#x1F6D2; -->
            # <!-- panel-tooltip: Browse and install AI skills -->
            meta = _parse_panel_meta(content)
            if not meta.get('id'):
                continue

            nav_items.append(
                f'<div class="nav-item" data-panel="{meta["id"]}" '
                f'data-tooltip="{meta.get("tooltip", "")}">'
                f'<span class="nav-icon">{meta.get("icon", "&#x2726;")}</span>'
                f'{meta.get("label", meta["id"])}</div>'
            )
            panel_html.append(
                f'<div class="panel" id="panel-{meta["id"]}">{content}</div>'
            )
            mobile_items.append(
                f'<div class="tab-menu-item" data-panel="{meta["id"]}" '
                f'onclick="selectMobileMenuItem(\'{meta["id"]}\')">'
                f'<span style="margin-right:10px;">{meta.get("icon", "&#x2726;")}</span>'
                f'{meta.get("label", meta["id"])}</div>'
            )

        if nav_items:
            # Inject nav items before the sidebar footer (Quick Fire section)
            nav_inject = '\n'.join(nav_items)
            html = html.replace(
                '<!-- Quick Fire pills -->',
                f'{nav_inject}\n    <!-- Quick Fire pills -->'
            )
            # Inject panels before closing </div> of content area
            panels_inject = '\n'.join(panel_html)
            html = html.replace(
                '</div>\n</div>\n\n<!-- Mobile bottom tabs -->',
                f'{panels_inject}\n</div>\n</div>\n\n<!-- Mobile bottom tabs -->'
            )
            # Inject mobile menu items
            mobile_inject = '\n'.join(mobile_items)
            html = html.replace(
                '</div>\n\n<!-- Toast -->',
                f'{mobile_inject}\n</div>\n\n<!-- Toast -->'
            )

    resp = Response(html, media_type="text/html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp
```

The `_parse_panel_meta` helper:

```python
def _parse_panel_meta(html_content: str) -> dict:
    """Extract panel metadata from HTML comment headers."""
    meta = {}
    for line in html_content.split('\n')[:10]:
        m = re.match(r'<!--\s*panel-(\w+):\s*(.+?)\s*-->', line)
        if m:
            meta[m.group(1)] = m.group(2)
    return meta
```

**What a custom panel file looks like** (`custom/panels/skills-shop.html`):

```html
<!-- panel-id: skills-shop -->
<!-- panel-label: Skills Shop -->
<!-- panel-icon: &#x1F6D2; -->
<!-- panel-tooltip: Browse and install AI skills for your civilization -->

<div style="padding:20px;">
  <h2 style="color:var(--gold);font-family:var(--font-heading);">Skills Shop</h2>
  <p style="color:var(--text-dim);margin-bottom:20px;">Browse available skills for your AI civilization.</p>
  <div id="skills-shop-grid"></div>
</div>

<style>
  #panel-skills-shop #skills-shop-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 16px;
  }
</style>

<script>
(function() {
  // Skills Shop initialization
  // This runs when the panel HTML is injected into the DOM
  window._skillsShopLoaded = false;

  // Register with the switchPanel system
  var _origSwitch = window._customPanelHandlers || {};
  _origSwitch['skills-shop'] = function() {
    if (!window._skillsShopLoaded) {
      loadSkillsShop();
      window._skillsShopLoaded = true;
    }
  };
  window._customPanelHandlers = _origSwitch;

  function loadSkillsShop() {
    fetch('/api/custom/skills-shop', {
      headers: { 'Authorization': 'Bearer ' + (localStorage.getItem('pb_token') || '') }
    })
    .then(r => r.json())
    .then(data => {
      var grid = document.getElementById('skills-shop-grid');
      // ... render skills ...
    });
  }
})();
</script>
```

**The switchPanel hook** -- add one line to the existing `switchPanel` function to support custom panels:

```javascript
// At the end of switchPanel(), add:
if (window._customPanelHandlers && window._customPanelHandlers[panel]) {
  window._customPanelHandlers[panel]();
}
```

This is a single line in the core HTML that calls into custom panel JS handlers. If it gets overwritten by upstream, it is trivially re-added.

### How It Works: Config Overrides

`custom/config.json`:

```json
{
  "MAX_TOKENS": 870000,
  "PAYOUT_MIN_AMOUNT": 50.0,
  "PORTAL_VERSION": "1.0.1-flux2"
}
```

The shim loads this and overwrites matching globals. Simple, declarative, no code.

### How It Works: Custom Quick Fire Commands

`custom/quickfire.json`:

```json
{
  "mode": "append",
  "commands": [
    { "label": "Skills", "text": "/skills-shop" },
    { "label": "Night Watch", "text": "/night-watch" }
  ]
}
```

The frontend can load this via a custom endpoint or the server can inject it into the HTML.

---

## Migration Path

### Phase 1: Create the infrastructure (1-2 hours)

1. Create `custom/` directory in the portal
2. Add the backend shim to `portal_server.py` (~35 lines, clearly delimited)
3. Modify `index_pb()` to inject custom panels (~30 lines)
4. Add the `switchPanel` hook (1 line in HTML)

### Phase 2: Extract existing customizations (1-2 hours per CIV)

For each CIV that has customizations:

1. Create `custom/config.json` with their config overrides
2. Extract custom panels into `custom/panels/*.html`
3. Extract custom endpoints into `custom/routes.py`
4. Verify everything works
5. Revert portal_server.py and portal-pb-styled.html to upstream versions
6. Confirm custom features still work via the overlay

### Phase 3: Upstream adopts the shim (coordination with Prodigy)

Ask upstream to include the shim in their core files. Once they do:
- Upstream updates no longer break customizations
- The `custom/` directory is never touched by upstream
- No manual shim re-addition needed

### Phase 3 Alternative: Auto-patch script

If upstream will not adopt the shim, create a `apply_shim.sh` script that patches portal_server.py after any upstream update:

```bash
#!/bin/bash
# Run after any upstream portal_server.py update
PORTAL="$HOME/purebrain_portal/portal_server.py"
SHIM="$HOME/purebrain_portal/custom/shim.py.patch"

if ! grep -q "CUSTOMIZATION LAYER" "$PORTAL"; then
    echo "[shim] Applying customization shim to portal_server.py..."
    patch "$PORTAL" "$SHIM"
    echo "[shim] Done. Restart portal to activate."
else
    echo "[shim] Shim already present."
fi
```

---

## Example: Skills Shop Surviving an Upstream Update

**Before the architecture:**
1. Upstream ships new `portal_server.py` and `portal-pb-styled.html`
2. Operator copies them into place
3. Skills Shop is gone -- it was embedded in the HTML
4. Custom `/api/skills-shop` endpoint is gone -- it was in portal_server.py
5. Panic, manual re-integration

**After the architecture:**
1. Upstream ships new `portal_server.py` and `portal-pb-styled.html`
2. Operator copies them into place
3. Operator runs `./custom/apply_shim.sh` (or the shim is already in upstream's file)
4. Restarts portal
5. Skills Shop loads from `custom/panels/skills-shop.html` -- untouched
6. Custom endpoint loads from `custom/routes.py` -- untouched
7. MAX_TOKENS override loads from `custom/config.json` -- untouched

---

## What Upstream (Prodigy) Would Need to Change

**Minimal ask (strongly recommended):**
Include the ~35 line customization shim in their canonical `portal_server.py`. This is:
- Zero risk (if `custom/` dir does not exist, nothing happens)
- Zero maintenance (the shim auto-discovers files)
- Massive value (prevents every CIV from losing customizations on every update)

**Secondary ask:**
Include ONE line in the switchPanel function:
```javascript
if (window._customPanelHandlers && window._customPanelHandlers[panel]) {
  window._customPanelHandlers[panel]();
}
```

And ensure these three HTML comment markers are stable (they already exist naturally in the file structure):
- `<!-- Quick Fire pills -->` (nav injection point)
- The `</div></div>` before `<!-- Mobile bottom tabs -->` (panel injection point)
- The `</div>` before `<!-- Toast -->` (mobile menu injection point)

**If upstream refuses:**
Each CIV runs `apply_shim.sh` after updates. It is a 5-second operation.

---

## What Custom Routes Look Like

`custom/routes.py`:

```python
"""Custom routes for this AiCIV portal instance."""
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
import json
from pathlib import Path

SKILLS_DIR = Path.home() / ".claude" / "skills"

async def api_skills_shop(request: Request) -> JSONResponse:
    """List available skills for the Skills Shop panel."""
    skills = []
    registry = Path.home() / "user-civs" / "witness-corey" / "memories" / "skills" / "registry.json"
    if registry.exists():
        data = json.loads(registry.read_text())
        for name, info in data.items():
            skills.append({
                "name": name,
                "description": info.get("description", ""),
                "status": info.get("status", "available"),
            })
    return JSONResponse({"skills": skills})

async def api_skills_install(request: Request) -> JSONResponse:
    """Install a skill from the shop."""
    body = await request.json()
    skill_name = body.get("skill")
    # ... installation logic ...
    return JSONResponse({"ok": True, "installed": skill_name})

# This list is auto-loaded by the customization shim
routes = [
    Route("/api/custom/skills-shop", endpoint=api_skills_shop),
    Route("/api/custom/skills-install", endpoint=api_skills_install, methods=["POST"]),
]
```

Note the `/api/custom/` prefix convention -- keeps custom routes namespaced and avoids collisions with upstream routes.

---

## Consequences

**Positive:**
- Custom features survive upstream updates with zero manual effort (once shim is in upstream)
- Clear separation: `custom/` is the CIV's domain, everything else is upstream's
- Non-technical operators only touch files in `custom/`
- Each custom panel is a single, self-contained HTML file -- easy to write, easy to share between CIVs
- Config overrides are declarative JSON -- no code editing
- Zero breaking changes to existing codebase

**Negative:**
- HTML injection uses string replacement, which is fragile if upstream significantly restructures the HTML markers. Mitigation: the markers are natural structural points unlikely to change.
- The index_pb() function now reads + modifies HTML in memory instead of serving a static file. Performance impact: negligible (one string read + a few replaces per request, cached trivially if needed).
- Custom routes cannot easily override existing routes (they append). If a CIV needs to change the behavior of an existing endpoint, they still need to edit portal_server.py. This is an acceptable trade-off -- overriding core behavior should be rare and deliberate.

---

## Implementation Notes for Coder Agent

1. **Start with backend shim.** It is the smallest change with the biggest impact.
2. **Use `importlib.util`** for dynamic loading -- no `exec()`, no `eval()`.
3. **Namespace custom routes** under `/api/custom/` to avoid collisions.
4. **Each panel file is self-contained**: HTML + scoped CSS + IIFE JS. No build step.
5. **Cache the assembled HTML** in memory after first load if performance matters. Invalidate on file change (check mtime).
6. **Log all custom loading** with `[portal-custom]` prefix so operators can diagnose issues.
7. **Fail gracefully**: if any custom file has errors, log a warning and continue. Never let a bad custom file crash the entire portal.
