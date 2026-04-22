# Portal Modification Protocol

**Status**: Active -- MANDATORY before any portal modification
**Applies to**: ALL agents modifying the PureBrain Portal
**Source**: ADR-001 (Overlay Architecture), ADR-002 (Git Strategy)

---

## THE RULE

**NEVER edit tracked files. ALL modifications go through `custom/`.**

If you edit `portal-pb-styled.html`, `portal_server.py`, or any other tracked file directly, your changes WILL be destroyed the next time someone clicks "Check for Updates" and `git pull --ff-only` runs. No exceptions. No "I'll just add one line." No "this is faster." Your work will be gone.

The portal has a built-in overlay system specifically designed so you never need to touch tracked files. Use it.

---

## Quick Decision Tree

| I want to...                          | Do this                                                        |
|---------------------------------------|----------------------------------------------------------------|
| Add a new UI panel/tab                | Create `custom/panels/my-panel.html`                          |
| Add a new API endpoint                | Add `Route()` to `custom/routes.py`                           |
| Change MAX_TOKENS or other config     | Edit `custom/config.json`                                     |
| Run code on portal startup            | Add `async def on_startup()` to `custom/startup.py`           |
| Modify an existing panel's appearance | Create a custom panel that replaces it (see HOW-TO below)     |
| Add JavaScript functionality          | Put it in a `<script>` IIFE inside a custom panel HTML file   |
| Change the portal theme/CSS           | Put a `<style>` block inside a custom panel HTML file         |
| Add a Quick Fire command              | Edit `custom/quickfire.json`                                  |

If your modification doesn't fit any of these categories, **stop and ask** before editing a tracked file.

---

## What's Safe vs What's Dangerous

### SAFE -- edit freely (gitignored, survives updates)

| File/Path | Purpose |
|-----------|---------|
| `custom/config.json` | Config value overrides |
| `custom/routes.py` | Custom API endpoints |
| `custom/startup.py` | Startup hooks (background tasks, init logic) |
| `custom/quickfire.json` | Custom Quick Fire commands |
| `custom/panels/*.html` | Custom UI panels (each file = one panel) |
| `custom/__pycache__/` | Python bytecode cache (auto-generated) |

### DANGEROUS -- never edit for customizations (tracked by git, overwritten on update)

| File | What happens if you edit it |
|------|-----------------------------|
| `portal_server.py` | `git pull` overwrites it. Your endpoints, config changes, startup code -- all gone. |
| `portal-pb-styled.html` | `git pull` overwrites it. Your panels, buttons, CSS -- all gone. |
| `static/commands-shortcuts.js` | `git pull` overwrites it. Your Quick Fire customizations -- gone. |
| `*.template` files in `custom/` | These ARE tracked. They are reference examples, not runtime files. Editing them changes the template for everyone, not your local config. |

### SAFE BUT READ-ONLY -- do not edit (tracked reference material)

| File | Purpose |
|------|---------|
| `custom/routes.py.template` | Example showing how to write custom routes |
| `custom/config.json.template` | Example showing config override format |
| `custom/panels/SAMPLE-PANEL.html.template` | Example showing panel file format |
| `custom/panels/system-status.html.template` | Example system status panel |

---

## HOW-TO: Create a Custom Panel

A custom panel is a single `.html` file in `custom/panels/`. The portal server reads all `*.html` files from that directory and injects them into the UI automatically.

### Step 1: Create the file

```bash
cp custom/panels/SAMPLE-PANEL.html.template custom/panels/my-dashboard.html
```

### Step 2: Set the metadata comments

The first 10 lines are scanned for metadata. These four comments are required:

```html
<!-- panel-id: my-dashboard -->
<!-- panel-label: My Dashboard -->
<!-- panel-icon: &#x1F4CA; -->
<!-- panel-tooltip: Overview of key metrics and status -->
```

| Field | Rules |
|-------|-------|
| `panel-id` | Unique, lowercase, hyphens OK. Becomes `id="panel-my-dashboard"` in the DOM. |
| `panel-label` | Display name shown in sidebar nav. |
| `panel-icon` | HTML entity for the icon. Use hex entities like `&#x1F4CA;` (chart), `&#x1F6D2;` (cart), `&#x2726;` (star). |
| `panel-tooltip` | Hover text on the nav item. |

### Step 3: Write the panel content

```html
<!-- panel-id: my-dashboard -->
<!-- panel-label: My Dashboard -->
<!-- panel-icon: &#x1F4CA; -->
<!-- panel-tooltip: Overview of key metrics and status -->

<div style="padding: 20px;">
  <h2 style="color: var(--gold); font-family: var(--font-heading);">
    My Dashboard
  </h2>
  <p style="color: var(--text-dim); margin-bottom: 20px;">
    Your custom content here.
  </p>
  <div id="my-dashboard-content"></div>
</div>

<style>
  /* ALWAYS scope styles to #panel-{your-id} to avoid conflicts */
  #panel-my-dashboard #my-dashboard-content {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 16px;
  }
</style>

<script>
(function() {
  // IIFE keeps variables out of global scope
  var loaded = false;

  // Register a lazy-load handler -- runs when user clicks this panel
  var handlers = window._customPanelHandlers || {};
  handlers['my-dashboard'] = function() {
    if (!loaded) {
      init();
      loaded = true;
    }
  };
  window._customPanelHandlers = handlers;

  function init() {
    fetch('/api/custom/my-dashboard-data', {
      headers: {
        'Authorization': 'Bearer ' + (localStorage.getItem('pb_token') || '')
      }
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var el = document.getElementById('my-dashboard-content');
      el.innerHTML = '<p style="color:var(--text-dim);">Loaded.</p>';
    })
    .catch(function(err) {
      console.error('[my-dashboard] Load error:', err);
    });
  }
})();
</script>
```

### Step 4: Restart the portal

The portal reads panel files on each request (no caching by default), but a restart ensures clean loading:

```bash
# From within the portal directory
kill -HUP $(pgrep -f portal_server.py)
# Or restart via the portal UI "Restart Portal" button
```

### Key rules for panels

- **No `<html>`, `<head>`, or `<body>` tags.** Your file is injected into the existing page.
- **Scope all CSS** to `#panel-{your-id}` to avoid breaking other panels.
- **Wrap all JS in an IIFE** `(function() { ... })();` to avoid polluting the global scope.
- **Use portal CSS variables** for consistent theming: `var(--gold)`, `var(--text-dim)`, `var(--bg-darker)`, `var(--font-heading)`, etc.
- **Use `window._customPanelHandlers`** for lazy loading -- your JS only runs when the user navigates to your panel.
- **Use `localStorage.getItem('pb_token')`** for auth tokens in fetch calls.

### Injection markers

The portal injects your panel at three points in the HTML (you do not need to know these, but for reference):

1. `<!-- /nav-panels -->` -- sidebar nav item inserted before this marker
2. `<!-- /panels -->` -- panel `<div>` inserted before this marker
3. `<!-- /mobile-menu-items -->` -- mobile menu item inserted before this marker

If any marker is missing after an upstream update, the portal logs a warning: `[portal-custom] WARNING: marker not found`.

---

## HOW-TO: Add a Custom API Route

Custom routes live in `custom/routes.py`. The shim loads this file automatically and reads the `routes` list.

### Step 1: Create routes.py (if it doesn't exist)

```bash
cp custom/routes.py.template custom/routes.py
```

### Step 2: Add your endpoint

```python
"""Custom API routes for this AiCIV portal instance."""
import json
import sys
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# Import auth helper from the main portal
sys.path.insert(0, str(Path(__file__).parent.parent))
from portal_server import check_auth


async def api_my_feature(request: Request) -> JSONResponse:
    """GET /api/custom/my-feature -- describe what it does."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # Your logic here
    return JSONResponse({"ok": True, "data": "hello"})


async def api_my_feature_create(request: Request) -> JSONResponse:
    """POST /api/custom/my-feature -- create something."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)

    title = (body.get("title") or "").strip()
    if not title:
        return JSONResponse({"ok": False, "error": "title is required"}, status_code=400)

    # Your creation logic here
    return JSONResponse({"ok": True, "created": title}, status_code=201)


# EVERY Route MUST appear in this list or the shim won't load it
routes = [
    Route("/api/custom/my-feature", endpoint=api_my_feature, methods=["GET"]),
    Route("/api/custom/my-feature", endpoint=api_my_feature_create, methods=["POST"]),
]
```

### Key rules for routes

- **Namespace under `/api/custom/`** to avoid collisions with upstream routes.
- **Always use `check_auth(request)`** to protect endpoints. Import it from `portal_server`.
- **Every `Route` must be in the `routes` list** at the bottom of the file. The shim reads `routes` -- if your endpoint isn't listed, it won't be loaded.
- **Use `starlette.routing.Route`**, not Flask or FastAPI decorators.
- **Path parameters** use `{name}` syntax: `Route("/api/custom/tasks/{id}", ...)`. Access via `request.path_params["id"]`.
- **Return `JSONResponse`** for APIs. Set `status_code` for non-200 responses.

### Real example: Kanban API (from Flux2)

See `custom/routes.py` for a full working example with CRUD operations, file-based storage, thread-safe locking, and query parameter filtering.

---

## HOW-TO: Override Config Values

Config overrides live in `custom/config.json`. The shim reads this at startup and overrides matching global variables in `portal_server.py`.

### Step 1: Create config.json (if it doesn't exist)

```bash
cp custom/config.json.template custom/config.json
```

### Step 2: Set your overrides

```json
{
  "MAX_TOKENS": 870000,
  "PORTAL_VERSION": "1.2.0-myinstance"
}
```

### Allowlisted keys

Only these keys can be overridden (security measure):

| Key | Type | What it controls |
|-----|------|-----------------|
| `MAX_TOKENS` | int | Maximum token limit for conversations |
| `PORTAL_VERSION` | string | Version string displayed in the UI |
| `PAYOUT_MIN_AMOUNT` | float | Minimum payout threshold |
| `REFERRAL_COMMISSION_RATE` | float | Referral commission percentage |

If you set a key that is not in the allowlist, the shim logs a warning and skips it:
```
[portal-custom] WARNING: config override blocked for key 'MY_KEY' (not in allowlist)
```

### To add a new allowed key

This requires editing `portal_server.py` (the `_ALLOWED_CONFIG_OVERRIDES` set near the CUSTOMIZATION LAYER marker). This is a rare case where editing a tracked file is justified -- but coordinate with upstream so the key persists across updates. The current allowlist is:

```python
_ALLOWED_CONFIG_OVERRIDES = {"MAX_TOKENS", "PORTAL_VERSION", "PAYOUT_MIN_AMOUNT", "REFERRAL_COMMISSION_RATE"}
```

---

## HOW-TO: Add a Startup Hook

Startup hooks run once when the portal starts. They are ideal for background tasks, polling loops, or initialization code.

### Step 1: Create startup.py

```python
"""Custom startup hooks for this AiCIV portal instance."""
import asyncio
import logging

logger = logging.getLogger("custom_startup")


async def on_startup() -> None:
    """Called once at portal startup by the customization shim.

    This is your entry point. Launch background tasks here.
    """
    logger.info("[custom] Startup hook running")

    # Example: start a background polling loop
    asyncio.create_task(_my_background_task())

    logger.info("[custom] Startup hook complete")


async def _my_background_task() -> None:
    """Example background task that runs every 60 seconds."""
    while True:
        try:
            # Your periodic logic here
            logger.debug("[custom] Background task tick")
        except Exception as exc:
            logger.warning(f"[custom] Background task error: {exc}")
        await asyncio.sleep(60)
```

### Key rules for startup hooks

- **Must define `async def on_startup()`** -- the shim looks for this exact name.
- **Use `asyncio.create_task()`** for background work. Do not block `on_startup()` with long-running logic.
- **Import from portal_server if needed** using the `sys.path.insert(0, ...)` pattern (see real example in `custom/startup.py`).
- **Handle errors gracefully.** A crash in your startup hook logs a warning but does not kill the portal.

### Real example: CC Bridge (from Flux2)

See `custom/startup.py` for a production startup hook that runs three background loops (polling, queue drain, heartbeat) with state persistence and busy-detection.

---

## HOW-TO: Add JavaScript or CSS Without a Panel

If you need to add JS or CSS that applies globally (not scoped to one panel), create a "utility panel" that has no visible content:

```html
<!-- panel-id: custom-utils -->
<!-- panel-label: _hidden -->
<!-- panel-icon: &#x2699; -->
<!-- panel-tooltip: Internal utilities -->

<!-- No visible HTML needed -->

<style>
  /* Global CSS additions -- scoped to body to be explicit */
  body .my-custom-class {
    border: 1px solid var(--gold);
    border-radius: 8px;
  }
</style>

<script>
(function() {
  // Global JS that runs on page load
  // Example: add keyboard shortcut
  document.addEventListener('keydown', function(e) {
    if (e.ctrlKey && e.key === 'k') {
      // Custom keyboard shortcut logic
    }
  });
})();
</script>
```

Note: This panel will appear in the sidebar. If you truly want it hidden, you need to handle that via CSS (`display:none` on the nav item) or accept the minor UI cost.

---

## HOW-TO: Test Your Modifications

### Verify panels load

1. Restart the portal (or refresh the page -- panels are injected per-request)
2. Check the portal server log for:
   ```
   [portal-custom] Injecting panel: my-dashboard (My Dashboard)
   ```
3. If you see warnings about missing markers, the upstream HTML may have changed structure

### Verify routes load

1. Restart the portal
2. Check the log for:
   ```
   [portal-custom] Loaded 5 custom route(s)
   ```
3. Test your endpoint:
   ```bash
   curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:5001/api/custom/my-feature
   ```

### Verify config overrides

1. Restart the portal
2. Check the log for:
   ```
   [portal-custom] Config override: MAX_TOKENS = 870000
   ```

### Simulate an update (the real test)

```bash
cd /home/aiciv/purebrain_portal

# Check what's tracked vs gitignored
git status

# Your custom files should NOT appear as modified:
#   custom/config.json     -> gitignored (safe)
#   custom/routes.py       -> gitignored (safe)
#   custom/panels/*.html   -> gitignored (safe)
#   custom/startup.py      -> gitignored (safe)

# If you accidentally edited a tracked file, it WILL show as modified.
# That means git pull will conflict or overwrite it.
```

---

## The Update Mechanism (Why This Matters)

When someone clicks "Check for Updates" in the portal UI, here is what happens:

1. **`git pull --ff-only`** runs in the portal directory
2. Every tracked file is updated to match the upstream repo
3. `portal_server.py` and `portal-pb-styled.html` are **completely replaced**
4. The update process **verifies the CUSTOMIZATION LAYER marker** survived the pull
5. If the marker is missing, the update **aborts and rolls back** to protect your customizations
6. Tests run. If they fail, the update rolls back.
7. The portal restarts.

**What survives:**
- Everything in `custom/` that is gitignored: `config.json`, `routes.py`, `startup.py`, `panels/*.html`
- The shim in `portal_server.py` (as long as upstream includes it)

**What gets destroyed:**
- Any direct edits to `portal_server.py` (overwritten by upstream version)
- Any direct edits to `portal-pb-styled.html` (overwritten by upstream version)
- Any direct edits to `static/` files (overwritten by upstream version)
- Any direct edits to `.template` files (overwritten by upstream version)

The update mechanism has a built-in safety check: if the CUSTOMIZATION LAYER marker disappears from `portal_server.py` after the pull, the update aborts. But this only protects the shim itself -- it does NOT protect any ad-hoc edits you made to tracked files.

---

## Common Mistakes

### "I edited portal-pb-styled.html to add a button"

**What happens**: Next update overwrites the HTML file. Your button is gone.

**Fix**: Create `custom/panels/my-button-panel.html` with the button. The panel injection system adds it to the sidebar automatically.

### "I added an endpoint directly to portal_server.py"

**What happens**: Next update replaces `portal_server.py`. Your endpoint vanishes. Users get 404s.

**Fix**: Add the endpoint to `custom/routes.py` under the `/api/custom/` namespace.

### "I changed MAX_TOKENS in portal_server.py"

**What happens**: Next update resets it to the upstream default.

**Fix**: Set `"MAX_TOKENS": 870000` in `custom/config.json`.

### "I edited routes.py.template instead of routes.py"

**What happens**: The template is tracked by git. Your changes either get committed to the shared repo (affecting all CIVs) or create merge conflicts on pull.

**Fix**: Copy the template to `routes.py` first, then edit `routes.py`.

### "I put CSS in portal-pb-styled.html"

**What happens**: Gone on next update.

**Fix**: Put the CSS in a `<style>` block inside a custom panel file. Scope it with `#panel-{id}` selectors.

### "My custom panel doesn't show up"

**Checklist**:
1. Is the file in `custom/panels/` with a `.html` extension? (Not `.html.template`)
2. Does it have `<!-- panel-id: something -->` in the first 10 lines?
3. Is the `panel-id` unique (not duplicating another panel)?
4. Check the portal log for `[portal-custom] WARNING` messages about missing markers.

### "My config override isn't taking effect"

**Checklist**:
1. Is the key in the allowlist? Check for `WARNING: config override blocked` in the log.
2. Is the JSON valid? A syntax error in `config.json` causes the entire file to fail silently.
3. Did you restart the portal? Config is read at startup, not dynamically.

---

## Enforcement

This skill is **MANDATORY** before any portal modification.

**Before you write a single line of code that touches any file in `purebrain_portal/`:**

1. Load this skill
2. Identify which file you plan to modify
3. Check the "Safe vs Dangerous" table above
4. If the file is dangerous (tracked), find the custom/ equivalent
5. If there is no custom/ equivalent for what you need, raise the question -- do not silently edit a tracked file

**If you are reviewing another agent's portal work:**

Verify that NO tracked files were modified for customization purposes. If `git diff` shows changes to `portal_server.py` or `portal-pb-styled.html`, those changes will not survive an update and must be moved to `custom/`.

**The only acceptable edits to tracked files are:**
- Fixing a bug in the shim itself (the CUSTOMIZATION LAYER block)
- Adding a new key to `_ALLOWED_CONFIG_OVERRIDES` (coordinate with upstream)
- Adding a new injection marker to the HTML (coordinate with upstream)

Everything else goes through `custom/`.

---

## HOW-TO: Migrate Existing Tracked File Modifications

If you've already edited a tracked file and need to move your changes to the overlay system:

### Step 1: Identify what you changed

```bash
cd /path/to/purebrain_portal
git diff portal-pb-styled.html  # See your HTML changes
git diff portal_server.py       # See your Python changes
```

### Step 2: Extract and migrate each change

**For HTML/CSS/JS changes to portal-pb-styled.html:**
1. Copy the diff output -- identify what you added
2. Create a new panel file: `custom/panels/my-feature.html`
3. Wrap your HTML in the panel metadata format (see HOW-TO: Create a Custom Panel above)
4. If you added CSS, scope it to `#panel-my-feature`
5. If you added JS, wrap it in an IIFE `<script>(function(){ ... })();</script>`

**For API endpoint changes to portal_server.py:**
1. Copy your endpoint function(s)
2. Add them to `custom/routes.py` as Starlette Route objects
3. The portal loads custom routes automatically on startup

**For config value changes to portal_server.py:**
1. Identify which values you changed (e.g., MAX_TOKENS)
2. Add them to `custom/config.json` (only allowlisted keys work)

### Step 3: Verify your migration

```bash
# Restart the portal to load custom/ changes
# Then verify your feature still works

# Check the health endpoint for tracked modifications
curl -s http://localhost:8097/api/health/mods -H "Authorization: Bearer $(cat .portal-token)"
```

### Step 4: Discard the tracked file changes

```bash
# ONLY after verifying your custom/ migration works:
git checkout -- portal-pb-styled.html
git checkout -- portal_server.py
```

### Step 5: Verify update safety

```bash
# The health endpoint should now show:
# "has_tracked_modifications": false, "update_safe": true
curl -s http://localhost:8097/api/health/mods -H "Authorization: Bearer $(cat .portal-token)"
```
