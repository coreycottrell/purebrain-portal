# AUDIT: Panel / Dock / Inbox Interaction Conflicts

**Date**: 2026-06-04
**Auditor**: coder-agent
**Files Analyzed**:
- `/home/aiciv/purebrain_portal/static/js/features/dock.js` (258 lines)
- `/home/aiciv/purebrain_portal/static/js/core/panel-manager.js` (248 lines)
- `/home/aiciv/purebrain_portal/static/js/features/inbox.js` lines 227-365
- `/home/aiciv/purebrain_portal/portal-pb-styled.html` (DOM structure)
- `/home/aiciv/purebrain_portal/static/css/dock.css` (layout rules)

---

## Executive Summary

There is a **fundamental architectural gap**: the three systems (PanelManager, DockManager, switchChatView) each manage overlapping DOM state but have no shared model. The "promoted panel" pattern -- physically moving inbox/ccView DOM nodes out of chatArea into main-col -- creates a hidden state dimension that PanelManager is completely unaware of. This causes cascading failures when PanelManager executes panel switches while promoted panels exist.

**Root cause**: PanelManager treats panels as static DOM elements toggled by CSS classes. The promoted panel pattern *relocates* DOM elements, violating PanelManager's assumption that all panels are always in their original positions.

---

## System Architecture (As-Is)

### System 1: PanelManager (`panel-manager.js`)
- **Manages**: Top-level panels (letstalk, chat, tasks, files, agents, settings, etc.)
- **Mechanism**: `classList.add/remove('visible')` for non-chat panels, `style.display` for chat
- **Entry point**: `switchTo(panelId)` -- dock-aware, calls DockManager for expand/collapse
- **State**: `_activePanel` (string), `_lastNonChatTab` (string)
- **Blind spots**: Has ZERO knowledge of promoted panels, inbox/cc sub-views, or DOM relocation

### System 2: DockManager (`dock.js`)
- **Manages**: Dock CSS states on `.app` element (`chat-docked`, `dock-expanded`)
- **Mechanism**: CSS classes on `.app`, inline `height` on chatArea, `paddingBottom` on panels
- **State machine**: `UNDOCKED` -> `DOCKED` -> `DOCKED_EXPANDED` (via CSS class presence)
- **Dependencies**: Calls `PanelManager._hideAllExceptChat()`, `_showPanelElement()`, etc.
- **Knows about promoted panels**: Partially -- calls `_restorePromotedPanels()` on undock, detects active sub-view

### System 3: switchChatView (`inbox.js`)
- **Manages**: Sub-views inside chatArea (chat messages, inbox, cc)
- **Mechanism**: Shows/hides internal chatArea children; when DOCKED, **relocates** inbox/ccView DOM nodes to main-col
- **State**: Implicit -- determined by which sub-view elements are visible and where they are in DOM
- **Side effects**: Modifies `paddingBottom`, adds/removes `docked-promoted-panel` class, calls DockManager.expand/collapse

### DOM Structure (normal state)
```
.main-col
  #letstalkArea     (.chat-area, visibility: 'visible' class)
  #chatArea          (.chat-area, visibility: style.display)
    .chat-header
      .chat-subtabs  (Chat | Inbox | CC buttons -- call switchChatView)
    .chat-messages-wrapper
    .chat-messages
    .brain-banner
    .composer
    #inboxView       (normally display:none, inside chatArea)
    #ccView          (normally no 'visible' class, inside chatArea)
  #tasksArea         (visibility: 'visible' class)
  #filesArea         (etc.)
  ...
```

### DOM Structure (docked + inbox promoted)
```
.main-col
  #letstalkArea      (hidden)
  #chatArea           (FIXED to bottom of viewport via CSS)
    .chat-header     (subtabs still here, still clickable)
    .chat-messages   (visible -- chat still shows in dock)
    .composer        (visible)
    [inboxView is GONE from here -- moved out]
  #tasksArea          (hidden)
  ...
  #inboxView          (MOVED HERE -- child of main-col, not chatArea!)
    .docked-promoted-panel class
    paddingBottom set for dock clearance
```

---

## State Transition Matrix

### Legend
- OK = works correctly
- BUG = broken behavior
- FRAGILE = works but relies on lucky ordering/side effects

---

### 1. UNDOCKED + chat -> dock

**Action**: User clicks Dock button
**Code path**: `DockManager.toggle()` -> `_getActiveChatSubView()` returns `'chat'` -> `_enterDocked('chat')`

```
_enterDocked('chat'):
  app.classList.add('chat-docked')         // CSS: chatArea becomes fixed-bottom
  chatArea.style.display = 'flex'          // ensure visible
  app.classList.add('dock-expanded')        // CSS: chatArea goes fullscreen (static, flex:1)
  PM._setDockPadding(0)                    // no padding needed when expanded
  PM._hideAllExceptChat()                  // hide all other panels
  PM._highlightSidebar('chat')             // highlight chat in sidebar
  PM.setActivePanel('chat')                // update internal state
```

**Result**: OK. Chat becomes fullscreen docked. All other panels hidden. Sidebar shows chat active.

---

### 2. UNDOCKED + inbox -> dock

**Action**: User is viewing inbox (inside chatArea, undocked), clicks Dock button
**Code path**: `toggle()` -> `_getActiveChatSubView()` returns `'inbox'` -> condition `currentPanel === 'chat' && activeSubView === 'inbox'` -> `_enterDocked('__subview__')` then `switchChatView('inbox')`

```
_enterDocked('__subview__'):
  app.classList.add('chat-docked')         // chatArea becomes fixed-bottom
  chatArea.style.display = 'flex'
  app.classList.remove('dock-expanded')     // NOT expanded -- dock at saved height
  chatArea.style.height = savedH + 'px'    // dock at saved height
  PM._setDockPadding(savedH)               // add padding to panels
  PM.setActivePanel('chat')                // sets _activePanel = 'chat'

switchChatView('inbox'):  [called next]
  _isDocked = true, view = 'inbox'
  _restorePromotedPanels()                 // no-op (nothing promoted yet)
  msgs/wrapper/banner/composer.style.display = ''  // show chat content in dock
  inbox.style.display = 'none'             // hide inbox in chatArea (about to move it)
  DockManager.collapse()                   // no-op (already not expanded)
  mainCol.appendChild(inbox)               // MOVE inbox to main-col
  PM._hideAllExceptChat()                  // hide all panels except chat
  inbox.classList.add('docked-promoted-panel')  // style as main panel
  inbox.style.display = 'flex'             // show it
  inbox.style.paddingBottom = (savedH+8)+'px'  // clear the dock
```

**Result**: OK. Inbox appears above dock, chat messages visible in dock below. **However**: `PM._activePanel` is `'chat'`, but the *visually* active panel is inbox (promoted). PanelManager's internal state is out of sync with visual reality. This is the seed of all bugs.

---

### 3. UNDOCKED + cc -> dock

**Code path**: Same as #2 but with `'cc'` instead of `'inbox'`.

```
_enterDocked('__subview__') -> switchChatView('cc')
```

Same mechanics -- ccView moved to main-col, `.visible` added, `docked-promoted-panel` class.

**Result**: OK visually. Same `_activePanel` mismatch (says 'chat', actually showing cc).

---

### 4. UNDOCKED + letstalk -> dock

**Action**: User is on Let's Talk panel, clicks Dock button
**Code path**: `toggle()` -> `_getActiveChatSubView()` -- inbox not visible, ccView not visible, no active subtab (we're on letstalk, not chat) -> returns `'chat'` -> `currentPanel = PM.getActivePanel()` = `'letstalk'` -> condition `currentPanel === 'chat'` is FALSE -> falls through to `_enterDocked('letstalk')`

```
_enterDocked('letstalk'):
  app.classList.add('chat-docked')
  chatArea.style.display = 'flex'
  app.classList.remove('dock-expanded')
  chatArea.style.height = savedH + 'px'
  PM._setDockPadding(savedH)
  PM._hideAllExceptChat()                  // hides letstalkArea too!
  PM._showPanelElement('letstalk')         // re-shows letstalkArea with .visible class
  PM._highlightSidebar('letstalk')         // highlight letstalk in sidebar
  PM.setActivePanel('letstalk')            // correct
  PM._fireTabCallbacks('letstalk')         // opens voice overlay
```

**Result**: OK. Let's Talk shows above dock, chat in dock below. State is consistent.

---

### 5. DOCKED + chat (expanded) -> click inbox subtab

**Action**: User is in fullscreen docked chat, clicks Inbox subtab in chat header
**Code path**: `switchChatView('inbox')` directly (onclick handler)

```
switchChatView('inbox'):
  _isDocked = true, view = 'inbox'
  _restorePromotedPanels()                 // no-op (nothing promoted)
  msgs/wrapper/banner/composer: display=''  // show chat content
  inbox.style.display = 'none'             // hide inbox (about to move)
  ccView.classList.remove('visible')       // hide cc
  DockManager.collapse()                   // TRANSITIONS from DOCKED_EXPANDED to DOCKED
                                            // app removes dock-expanded
                                            // chatArea gets height = savedH
                                            // PM._setDockPadding(savedH)
  mainCol.appendChild(inbox)               // move inbox to main-col
  PM._hideAllExceptChat()                  // hide all panels
  inbox.classList.add('docked-promoted-panel')
  inbox.style.display = 'flex'
  inbox.style.paddingBottom = ...
```

**Result**: OK visually. Inbox shows above dock. **But PM._activePanel is still 'chat'** (nobody updated it). Sidebar still highlights 'chat'. This means:

- **BUG**: Sidebar highlighting is wrong -- chat is highlighted but inbox is showing above dock.

---

### 6. DOCKED + chat (expanded) -> click cc subtab

Same as #5 with 'cc'. Same sidebar highlighting bug.

**Result**: BUG -- same as #5. `_activePanel` = 'chat', cc is visually showing.

---

### 7. DOCKED + chat (expanded) -> click letstalk sidebar

**Action**: User clicks Let's Talk in sidebar while in fullscreen docked chat
**Code path**: `PanelManager.switchTo('letstalk')` (sidebar click handler)

```
switchTo('letstalk'):
  state = 'DOCKED_EXPANDED'
  panelId = 'letstalk' (not 'chat', so falls to else branch)
  dock.collapse()                          // DOCKED_EXPANDED -> DOCKED
                                            // removes dock-expanded, sets chatArea height
  _hideAllExceptChat()                     // hides all non-chat panels
  _showPanelElement('letstalk')            // shows letstalkArea
  _highlightSidebar('letstalk')            // highlights letstalk in sidebar
  _activePanel = 'letstalk'
  _fireTabCallbacks('letstalk')            // opens voice overlay
```

**Result**: OK. Let's Talk shows above dock. Chat visible in dock. State consistent.

---

### 8. DOCKED + chat (expanded) -> click tasks sidebar

**Code path**: `PanelManager.switchTo('tasks')` -- same flow as #7 but with 'tasks'.

**Result**: OK. Tasks shows above dock. State consistent.

---

### 9. DOCKED + inbox (promoted) -> click chat subtab

**Action**: Inbox is promoted above dock. User clicks Chat subtab in dock's chat header.
**Code path**: `switchChatView('chat')` directly

```
switchChatView('chat'):
  _isDocked = true, view = 'chat'
  _restorePromotedPanels()                 // MOVES inbox BACK into chatArea
                                            // inbox.classList.remove('docked-promoted-panel')
                                            // inbox.style.display = 'none'
                                            // inbox.style.paddingBottom = ''
                                            // chatArea.insertBefore(inbox, banner)
  DockManager.expand()                     // DOCKED -> DOCKED_EXPANDED
                                            // adds dock-expanded to app
                                            // PM._setDockPadding(0)
                                            // PM._hideAllExceptChat()
                                            // chatArea.style.display = 'flex'
                                            // PM._highlightSidebar('chat')
                                            // PM.setActivePanel('chat')
  msgs/wrapper/banner/composer: display=''  // show chat content
  inbox.style.display = 'none'
  ccView.classList.remove('visible')
```

**Result**: OK. Inbox restored into chatArea, dock expands to fullscreen chat. State is now correct again (`_activePanel = 'chat'`). **But there is a subtle issue**: `expand()` calls `_hideAllExceptChat()` which iterates BUILTIN panels. If any promoted panel had been a *different* panel type, it wouldn't know. In this specific case it works because inbox/cc aren't in BUILTIN.

---

### 10. DOCKED + inbox (promoted) -> click letstalk sidebar **BUG**

**Action**: Inbox is promoted above dock (as child of main-col). User clicks Let's Talk in sidebar.
**Code path**: `PanelManager.switchTo('letstalk')`

```
switchTo('letstalk'):
  state = 'DOCKED' (not expanded -- inbox is promoted above dock at DOCKED height)
  panelId = 'letstalk'
  // falls into state === 'DOCKED' branch
  panelId !== 'chat', so:
    _hideAllExceptChat()                   // Hides all BUILTIN panels except chat
                                            // BUT: inbox is NOT in its original position!
                                            // inbox is a child of main-col with docked-promoted-panel class
                                            // _hideAllExceptChat hides BUILTIN panels by ID
                                            // inbox is NOT a BUILTIN panel (it's inboxView inside chatArea normally)
                                            // So _hideAllExceptChat does NOT hide promoted inbox!
    _showPanelElement('letstalk')           // shows letstalkArea
    _highlightSidebar('letstalk')
    dock.restoreHeight()
```

**RESULT: BUG!** Both letstalkArea AND the promoted inboxView are now visible simultaneously in main-col. The inbox is sitting there as a `docked-promoted-panel` child of main-col with `display:flex`, and letstalkArea also has `.visible`. Two panels overlap or stack.

**Why it fails**: `_hideAllExceptChat()` iterates `BUILTIN` keys and removes `.visible` from their elements. But `#inboxView` is NOT in the BUILTIN map -- it's a child element of chatArea normally. When promoted, it becomes a direct child of main-col styled with `display:flex` and `docked-promoted-panel`. PanelManager has no code to hide it.

**The deeper problem**: `switchTo()` never calls `_restorePromotedPanels()`. It doesn't know promoted panels exist.

---

### 11. DOCKED + inbox (promoted) -> click tasks sidebar **BUG**

Exact same bug as #10. Tasks panel appears but inbox remains visible above dock.

```
switchTo('tasks'):
  _hideAllExceptChat()     // doesn't hide promoted inbox
  _showPanelElement('tasks')  // shows tasks
  // Result: BOTH tasks and promoted inbox visible
```

**RESULT: BUG!** Same as #10.

---

### 12. DOCKED + inbox (promoted) -> undock

**Action**: Inbox promoted above dock. User clicks Undock.
**Code path**: `DockManager.toggle()` -> `_exitDock()`

```
_exitDock():
  panelToShow = PM.getActivePanel()        // Returns 'chat' (PM doesn't know about promoted inbox!)
  wasSubView = _getActiveChatSubView()     // Returns 'inbox' (checks inbox display/button state)
                                            // BUT WAIT: inbox.style.display was set to 'none' in chatArea
                                            // inbox was MOVED to main-col and shown there
                                            // _getActiveChatSubView checks:
                                            //   inbox.style.display === 'flex' || 'block'?
                                            //   inbox is in main-col with display:flex -> YES -> returns 'inbox'
  _restorePromotedPanels()                 // Moves inbox back into chatArea, hides it
  app.classList.remove('chat-docked', 'dock-expanded')
  chatArea.style.height = ''
  PM._setDockPadding(0)
  _updateButtons(false)
  PM._hideAllPanels()                      // hides everything including chatArea
  PM._showPanelElement('chat')             // shows chatArea
  PM._highlightSidebar('chat')
  PM.setActivePanel('chat')
  // Then: panelToShow === 'chat' && wasSubView === 'inbox'
  switchChatView('inbox')                  // Shows inbox INSIDE chatArea (undocked mode)
```

**Result**: FRAGILE but works. The undock correctly detects the promoted inbox via `_getActiveChatSubView()` and restores it. The fragility: `_getActiveChatSubView()` checks `inbox.style.display` which works because the promoted element still has `display:flex` even though it's been moved to main-col. If the display check was position-dependent, this would break.

---

### 13. DOCKED + cc (promoted) -> click letstalk sidebar **BUG**

Same as #10/#11. CC is promoted in main-col with `classList.contains('visible')` and `docked-promoted-panel`.

```
switchTo('letstalk'):
  _hideAllExceptChat()     // doesn't hide promoted ccView (ccView is not in BUILTIN map)
  _showPanelElement('letstalk')
```

**RESULT: BUG!** Both letstalk and promoted cc visible.

---

### 14. DOCKED + tasks above dock -> click inbox subtab **BUG**

**Action**: Tasks panel visible above dock, chat in dock. User clicks Inbox subtab in dock's chat header.
**Code path**: `switchChatView('inbox')` directly

```
switchChatView('inbox'):
  _isDocked = true, view = 'inbox'
  _restorePromotedPanels()                 // no-op (no promoted panels)
  msgs/wrapper/banner/composer: display='' // show chat in dock
  inbox.style.display = 'none'             // hide inbox in chatArea
  ccView.classList.remove('visible')
  DockManager.collapse()                   // no-op (already DOCKED, not expanded)
  mainCol.appendChild(inbox)               // move inbox to main-col
  PM._hideAllExceptChat()                  // HIDES tasksArea! Good.
  inbox.classList.add('docked-promoted-panel')
  inbox.style.display = 'flex'
  inbox.style.paddingBottom = ...
```

**Result**: FRAGILE but works. `_hideAllExceptChat()` happens to hide the tasks panel. Inbox takes over. **However**: `PM._activePanel` is still `'tasks'` (from when tasks was selected via sidebar). But visually inbox is showing. Sidebar still highlights tasks.

**BUG (minor)**: Sidebar highlights 'tasks' but inbox is showing. `_activePanel` = 'tasks' but actual visible panel is promoted inbox.

If user now clicks tasks in sidebar again:
```
switchTo('tasks'):
  state = 'DOCKED'
  panelId = 'tasks'
  _hideAllExceptChat()     // doesn't hide promoted inbox (not in BUILTIN)
  _showPanelElement('tasks') // shows tasks
```
**BUG (compounded)**: Now BOTH tasks AND promoted inbox visible!

---

### 15. DOCKED + letstalk above dock -> click inbox subtab **BUG**

Same mechanics as #14.

```
switchChatView('inbox'):
  PM._hideAllExceptChat()  // hides letstalkArea (it's in BUILTIN)
  // promotes inbox
```

**Result**: FRAGILE -- letstalk gets hidden correctly. But same problems as #14: sidebar still highlights letstalk, `_activePanel` = 'letstalk'.

If user clicks letstalk sidebar again -> same compound bug: both letstalk and promoted inbox visible.

---

## Summary of All Bugs

| # | Scenario | Bug Type | Severity |
|---|----------|----------|----------|
| 5 | DOCKED expanded + click inbox subtab | Sidebar highlighting wrong | Medium |
| 6 | DOCKED expanded + click cc subtab | Sidebar highlighting wrong | Medium |
| 10 | DOCKED + inbox promoted + click letstalk | **Two panels visible simultaneously** | **High** |
| 11 | DOCKED + inbox promoted + click tasks | **Two panels visible simultaneously** | **High** |
| 13 | DOCKED + cc promoted + click letstalk | **Two panels visible simultaneously** | **High** |
| 14 | DOCKED + tasks above dock + click inbox subtab | Sidebar wrong + compound overlap on re-click | **High** |
| 15 | DOCKED + letstalk above dock + click inbox subtab | Sidebar wrong + compound overlap on re-click | **High** |

---

## Root Cause Analysis

### Primary Root Cause: No Shared State Model

The three systems each track state independently:

| System | State Variable | What It Tracks |
|--------|---------------|----------------|
| PanelManager | `_activePanel` | Which top-level panel is "active" |
| DockManager | CSS classes on `.app` | Dock geometry (undocked/docked/expanded) |
| switchChatView | DOM element visibility + position | Which sub-view is showing inside/outside chatArea |

**Nobody tracks**: "Is there a promoted panel in main-col right now?"

### Secondary Root Cause: switchChatView Bypasses PanelManager

When a user clicks a chat subtab (Inbox/CC), `switchChatView()` is called directly. It manipulates DOM and CSS on its own, calling some PanelManager internals (`_hideAllExceptChat`) but never going through `switchTo()`. This means:

1. `_activePanel` is never updated to reflect the promoted panel
2. Sidebar highlighting is never updated
3. PanelManager's dock-aware logic in `switchTo()` is completely bypassed

### Tertiary Root Cause: DOM Relocation as State

The promoted panel pattern uses *physical DOM position* as implicit state. A panel is "promoted" if:
- It has class `docked-promoted-panel`
- It is a child of `main-col` (not chatArea)
- It has inline `paddingBottom` set

But PanelManager queries panels by ID, not by position. So `_hideAllExceptChat()` will try to find `#inboxView` by ID -- it finds it, but since inbox is not in the BUILTIN map, it doesn't touch it at all. The inbox DOM element is simply invisible to PanelManager.

---

## Minimal Fixes (Per Bug)

### Fix for Bugs #10, #11, #13 (promoted panel not hidden on sidebar switch)

**Location**: `PanelManager.switchTo()` (panel-manager.js lines 135-171)

Add `_restorePromotedPanels()` call at the top of `switchTo()`:

```javascript
function switchTo(panelId) {
  // CRITICAL: restore any promoted inbox/cc panels before switching
  if (window._restorePromotedPanels) window._restorePromotedPanels();

  // Close voice overlay when leaving Let's Talk
  if (_activePanel === 'letstalk' && panelId !== 'letstalk' && typeof closeHmiVoiceOverlay === 'function') closeHmiVoiceOverlay();
  // ... rest unchanged
```

**Why**: When the user clicks a sidebar item, any promoted panel should be de-promoted first. This ensures `_hideAllExceptChat()` doesn't need to know about promoted panels -- they're already back in chatArea.

### Fix for Bugs #5, #6, #14, #15 (sidebar highlighting wrong after subtab click)

**Location**: `switchChatView()` in inbox.js (around line 297 and line 314)

After promoting a panel or expanding chat, update PanelManager state:

```javascript
// At end of the DOCKED + inbox/cc promotion block (before the return on line 297):
// Update PanelManager so sidebar knows what's active
// Note: we don't highlight sidebar for sub-views, but we track it
PM.setActivePanel('chat');  // sub-views are "inside" chat conceptually

// At end of the DOCKED + chat expansion block (before return on line 314):
// PM state already updated by expand()
```

Actually the deeper fix: `switchChatView` for promoted panels should call `PM._highlightSidebar('chat')` to ensure the chat sidebar item is highlighted (since inbox/cc are sub-views of chat):

```javascript
// After line 285 (after promoted panel is shown):
if (window.PanelManager) {
  window.PanelManager._highlightSidebar('chat');
  window.PanelManager.setActivePanel('chat');
}
```

### Fix for Bug #14 compound (re-clicking tasks after inbox promoted)

This is already fixed by the `switchTo()` fix above -- calling `_restorePromotedPanels()` at the start of `switchTo()` means clicking tasks will first de-promote inbox, then show tasks cleanly.

---

## Structural Recommendation

### Option A: Reconciliation Hook (Minimal Invasive)

Add a single reconciliation point: `switchTo()` calls `_restorePromotedPanels()` before any panel switch. This is a 2-line fix that addresses all high-severity bugs.

**Pros**: Minimal code change, low risk, addresses all bugs.
**Cons**: Doesn't fix the underlying architectural issue. Still two parallel state systems. Future features that add more sub-views will hit the same pattern.

### Option B: Promote-Aware PanelManager (Medium Refactor)

Make PanelManager aware of the concept of "promoted sub-panels":

```javascript
// Add to PanelManager state:
var _promotedPanel = null;  // 'inbox' | 'cc' | null

// Add methods:
function promoteSubPanel(subPanelId) { ... }
function demoteSubPanel() { ... }
function getPromotedPanel() { return _promotedPanel; }
```

Then `switchChatView` calls `PM.promoteSubPanel('inbox')` instead of doing DOM manipulation directly. PanelManager's `switchTo()` calls `demoteSubPanel()` before switching. `_hideAllExceptChat()` also hides promoted panels.

**Pros**: Single source of truth. PanelManager always knows what's visible. Sidebar highlighting naturally correct.
**Cons**: More code change. Need to coordinate with existing switchChatView callers.

### Option C: Eliminate DOM Relocation (Ideal Refactor)

Stop moving inbox/ccView between chatArea and main-col. Instead:

1. Keep inbox/ccView permanently as siblings of chatArea in main-col
2. When "promoting" in docked mode, just show them and hide chatArea's sub-content
3. Use CSS to position them above the dock (they're already in main-col structure conceptually)

```html
<div class="main-col">
  <div id="letstalkArea">...</div>
  <div id="inboxPanel" class="panel" style="display:none"><!-- inbox content --></div>
  <div id="ccPanel" class="panel" style="display:none"><!-- cc content --></div>
  <div id="chatArea">
    <!-- chat-only content, no inbox/cc -->
  </div>
  <div id="tasksArea">...</div>
  ...
</div>
```

Make inbox and cc proper BUILTIN panels in PanelManager. The subtab buttons in chat header call `PanelManager.switchTo('inbox')` instead of `switchChatView('inbox')`. switchChatView becomes a simple toggle for the UNDOCKED case only (or is eliminated entirely).

**Pros**: Eliminates DOM relocation entirely. Single visibility system. All panels in BUILTIN. No special-case code.
**Cons**: Significant refactor. Would change how inbox/cc appear in undocked mode (they'd be separate panels, not sub-views of chat). May require HTML restructuring. Could break custom panel injection that assumes chatArea contains inbox.

---

## Recommended Path

**Immediate (today)**: Apply Option A. Two lines in `switchTo()`. Fixes all high-severity overlapping panel bugs.

**Next sprint**: Apply Option B selectively -- add `_promotedPanel` tracking to PanelManager and have `switchChatView` update it. This prevents future regressions and makes the sidebar highlighting correct.

**Future**: Consider Option C when doing a larger portal refactor. The current sub-view pattern was reasonable for undocked-only, but adding docking made it untenable.

---

## Exact Code Changes for Option A (Immediate Fix)

### File: `/home/aiciv/purebrain_portal/static/js/core/panel-manager.js`

In `switchTo()` function, add at the very top (before the voice overlay close):

```javascript
function switchTo(panelId) {
  // Restore any promoted inbox/cc panels back to chatArea before switching
  if (window._restorePromotedPanels) window._restorePromotedPanels();

  // Close voice overlay when leaving Let's Talk
  // ... existing code ...
```

### File: `/home/aiciv/purebrain_portal/static/js/features/inbox.js`

In `switchChatView()`, after the promoted panel block (before `return;` on line 297), add sidebar state update:

```javascript
    // ... existing promotion code ...
    targetEl.style.paddingBottom=(savedH+8)+'px';

    // Keep PanelManager in sync -- promoted sub-views are conceptually "chat"
    if(window.PanelManager){
      window.PanelManager._highlightSidebar('chat');
      window.PanelManager.setActivePanel('chat');
    }

    // Update header actions
    // ... existing code ...
```

These two changes address all 7 identified bugs.
