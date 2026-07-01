# Portal Settings Audit: Missing User-Configurable Options

**Date**: 2026-05-28
**Auditor**: ux-specialist
**Files Analyzed**: portal-pb-styled.html, chat.js, inbox.js, cc-chat.js, cc_bridge.py, panels.css, base.css, dock.js

---

## Current Settings (Preferences tab)

Only 7 options exist today:
1. Theme (dark / light / girly)
2. Email Alerts toggle
3. Push Notifications toggle
4. Sound toggle
5. Digest Frequency cycle
6. Bearer Token input
7. CC Key input

---

## P1 -- High Value, Users Expect These

### 1. Chat Font Size
**What it controls**: Text size in chat messages (msg-bubble, msg-meta, code blocks)
**Current hardcoded value**: `font-size: 13px` on `.msg-bubble` (panels.css line ~208), `font-size: 10px` on `.msg-meta`
**Where in code**: `static/css/panels.css` -- `.msg-bubble`, `.msg-meta`
**Setting type**: Slider or dropdown (Small 12px / Default 13px / Medium 14px / Large 16px)
**Why P1**: Chat is the primary interface. Users on large monitors want bigger text; mobile users may want smaller. This is the #1 most common "settings" request in any chat app.

### 2. Chat History Load Count
**What it controls**: How many messages are fetched when chat loads
**Current hardcoded value**: `last=200` in `fetch('/api/chat/history?last=200'`
**Where in code**: `static/js/features/chat.js` line 470
**Setting type**: Dropdown (50 / 100 / 200 / 500)
**Why P1**: 200 messages is slow on poor connections. Power users may want more history. Directly affects load time and scrollback depth.

### 3. Send Behavior (Enter vs Shift+Enter)
**What it controls**: Whether Enter sends the message or inserts a newline
**Current hardcoded value**: Enter sends, Shift+Enter inserts newline
**Where in code**: `static/js/features/chat.js` line 1073 -- `if (e.key === 'Enter' && !e.shiftKey)`
**Setting type**: Toggle (Enter sends / Ctrl+Enter sends)
**Why P1**: This is the most divisive UX preference in chat apps. Slack users expect Enter=send, email users expect Enter=newline. Deeply personal preference.

### 4. Inbox Auto-Sync Interval
**What it controls**: How often inbox thread list cache is considered stale and triggers background refresh
**Current hardcoded value**: `_CACHE_TTL = 60000` (1 minute)
**Where in code**: `static/js/features/inbox.js` line 15
**Setting type**: Dropdown (30s / 1 min / 5 min / 15 min / Manual only)
**Why P1**: Users with multiple email accounts get hammered with API calls. Some want near-real-time; others check email once per session.

### 5. CC Poll Interval
**What it controls**: How frequently Command Center polls for new messages
**Current hardcoded values**: `POLL_INTERVAL = 10000` (10s active), `60000` (60s background)
**Where in code**: `static/js/features/cc-chat.js` line 11, line 289/434
**Setting type**: Dropdown (5s / 10s / 30s / 60s)
**Why P1**: Direct trade-off between real-time responsiveness and battery/bandwidth usage. CC polling is always-on once configured.

### 6. Timezone Selection (Persist in Settings)
**What it controls**: Which timezone the live clock displays
**Current behavior**: Clock widget in header has a dropdown to change timezone, but it is only stored in a JS variable per session
**Where in code**: `portal-pb-styled.html` lines 344-360, clock JS around line 6039
**Setting type**: Dropdown (already exists in clock widget -- just needs to persist via /api/settings)
**Why P1**: Users in different timezones need this to persist across sessions. Currently resets on reload.

---

## P2 -- Nice to Have, Improves Experience

### 7. Message Bubble Width
**What it controls**: Maximum width of chat message bubbles
**Current hardcoded value**: `max-width: 85%`
**Where in code**: `static/css/panels.css` line 196 -- `.msg{...max-width:85%}`
**Setting type**: Dropdown (Compact 65% / Default 85% / Wide 95% / Full Width 100%)
**Why P2**: On ultrawide monitors, 85% still leaves messages in a narrow column. Users with code-heavy conversations want full width.

### 8. Thinking Block Display
**What it controls**: Whether AI thinking/reasoning blocks are shown, and how much text to show
**Current hardcoded value**: `THINKING_TRUNCATE = 300` characters, always visible
**Where in code**: `static/js/features/chat.js` line 257
**Setting type**: Dropdown (Hidden / Collapsed (show on click) / Truncated 300 chars / Full)
**Why P2**: Some users find thinking blocks noisy. Others want full reasoning transparency. Currently forces 300-char truncation on everyone.

### 9. Image Compression Quality
**What it controls**: JPEG quality when compressing uploaded images
**Current hardcoded value**: `0.7` quality, max width `1920px`
**Where in code**: `static/js/features/chat.js` lines 764, 775
**Setting type**: Dropdown (Low 0.5 / Medium 0.7 / High 0.85 / Original -- no compression)
**Why P2**: Users uploading screenshots for debugging want lossless. Users on mobile want aggressive compression.

### 10. Dock Chat Default Height
**What it controls**: Default height of the docked chat panel
**Current hardcoded value**: `DEFAULT_HEIGHT = 280` px
**Where in code**: `static/js/features/dock.js` line 8
**Setting type**: Slider (150px - 600px) or Small/Medium/Large presets
**Why P2**: The dock is a key productivity feature. 280px may be too small for users who primarily use docked mode.

### 11. Default Inbox Folder
**What it controls**: Which folder is shown when Inbox tab opens
**Current hardcoded value**: Always shows "All" (inbox)
**Where in code**: `static/js/features/inbox.js` -- `filterInbox()` defaults to all
**Setting type**: Dropdown (All / Unread / Sent)
**Why P2**: Users who primarily monitor incoming mail want "Unread" as default. Saves a click every time.

### 12. CC Bridge Busy Threshold
**What it controls**: How long after last tool use the CC bridge considers Claude "busy" (delays message delivery)
**Current hardcoded value**: `_BUSY_THRESHOLD_S = 30` seconds
**Where in code**: `cc_bridge.py` line 90
**Setting type**: Slider (10s / 30s / 60s / 120s) -- advanced/power user
**Why P2**: Users who want CC messages to interrupt Claude sooner set this lower. Users who want uninterrupted work set it higher.

---

## P3 -- Power User Settings

### 13. Sidebar Default State
**What it controls**: Whether sidebar groups (Main, Work, Agents, Workspace) start expanded or collapsed
**Current behavior**: All groups expanded on load. User can toggle via `toggleSidebarGroup()` but it does not persist.
**Where in code**: `portal-pb-styled.html` sidebar groups, `static/css/components.css` line 115 -- `.sidebar-group.collapsed`
**Setting type**: Multi-toggle (one per group: Main / Work / Agents / Workspace -- expanded or collapsed by default)
**Why P3**: Power users who only use Chat/Terminal want sidebar minimal. New users want everything visible.

### 14. CC Message Stale Threshold
**What it controls**: Maximum age of CC messages before the bridge skips them entirely
**Current hardcoded value**: `_MAX_MESSAGE_AGE_S = 900` (15 minutes)
**Where in code**: `cc_bridge.py` line 92
**Setting type**: Dropdown (5 min / 15 min / 1 hour / Never skip)
**Why P3**: Operators want tighter windows; users who leave portals open overnight want longer windows.

### 15. CC Queue Max Age (Force Delivery)
**What it controls**: How long queued CC messages wait before being force-delivered regardless of Claude busy state
**Current hardcoded value**: `_QUEUE_MAX_AGE_S = 600` (10 minutes)
**Where in code**: `cc_bridge.py` line 89
**Setting type**: Dropdown (2 min / 5 min / 10 min / 30 min)
**Why P3**: Trade-off between message freshness and not interrupting long-running agent work.

### 16. Timestamp Format
**What it controls**: How timestamps display in chat and inbox
**Current hardcoded value**: `toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})` -- uses browser locale (12h or 24h depending on OS)
**Where in code**: `static/js/features/chat.js` line 28, `cc-chat.js` line 108, `inbox.js` line 402
**Setting type**: Dropdown (12-hour / 24-hour / Relative "5m ago")
**Why P3**: International users strongly prefer 24-hour. Some users prefer relative timestamps in chat.

### 17. Email External Images Default
**What it controls**: Whether external images in HTML emails load automatically or require "Show images" click
**Current hardcoded value**: Images are always blocked, user must click "Show images" per-email
**Where in code**: `static/js/features/inbox.js` lines 367-378, 380-391 -- `_sandboxHtml()` and `_showEmailImages()`
**Setting type**: Toggle (Always block images / Always show images)
**Why P3**: Security-conscious users want images blocked. Users who trust their senders want them auto-loaded.

---

## Summary

| Priority | Count | Examples |
|----------|-------|---------|
| **P1** | 6 | Font size, history depth, send behavior, inbox sync, CC poll, timezone |
| **P2** | 6 | Bubble width, thinking display, image quality, dock height, default folder, CC busy threshold |
| **P3** | 5 | Sidebar state, CC stale threshold, CC queue age, timestamp format, email images |
| **Total** | **17** | |

## Implementation Notes

All settings should persist via the existing `/api/settings` endpoint (which already stores theme, cc_civ_key, cc_since_id, etc. in `user-settings.json`).

**Recommended Settings tab structure** (new sub-sections within Preferences):

```
Preferences
  Theme .............. [existing]

  Chat
    Font Size ........ [slider/dropdown]
    Message Width .... [dropdown]
    History Depth .... [dropdown]
    Send Behavior .... [toggle]
    Thinking Blocks .. [dropdown]

  Notifications ...... [existing toggles]

  Inbox
    Auto-Sync ........ [dropdown]
    Default Folder ... [dropdown]
    Email Images ..... [toggle]

  CC Integration
    Poll Interval .... [dropdown]
    Busy Threshold ... [slider]
    Queue Max Age .... [dropdown]
    Message Stale .... [dropdown]

  Display
    Timezone ......... [dropdown]
    Timestamp Format . [dropdown]
    Sidebar Groups ... [multi-toggle]

  Connection ......... [existing token + CC key]
```

Each JS module should read its configurable values from a shared `window._portalSettings` object (hydrated from `/api/settings` on boot) rather than local `var` declarations, so changes apply immediately without reload.
