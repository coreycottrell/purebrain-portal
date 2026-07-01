# Inbox Tab UX Audit -- 2026-05-28

**Auditor**: ux-specialist
**Scope**: Inbox tab (inbox.js, portal-pb-styled.html inbox section, panels.css inbox styles, portal_server.py email backend)
**Method**: Code-level analysis (all 727 lines of inbox.js, HTML template, CSS, backend email extraction)

---

## P1: Bugs / Broken Behavior (Must Fix)

### P1-1. HTML emails render with raw `<div>` tags (CRITICAL)

**Files**:
- Backend: `portal_server.py:7210-7217` (`_body_text()` method)
- Frontend: `inbox.js:476` (renders `m.text` with `escHtml()`)

**Root cause**: The backend `_body_text()` method has a fallback path for multipart emails. When no `text/plain` part exists, it falls back to `text/html` (line 7213) and returns the raw HTML string as-is -- no tag stripping. The frontend then runs `escHtml()` on it (line 476), which HTML-entity-encodes the tags, so the user sees literal `<div>`, `<span>`, `<table>` text in the email body.

For non-multipart HTML-only emails, the same problem exists at lines 7219-7222 -- raw payload returned regardless of content type.

**Fix**: Add an HTML-to-plain-text stripping function in the backend. Minimal approach:

```python
import re

@staticmethod
def _strip_html(html_str: str) -> str:
    """Convert HTML to readable plain text."""
    # Remove style and script blocks entirely
    text = re.sub(r'<(style|script)[^>]*>.*?</\1>', '', html_str, flags=re.DOTALL | re.IGNORECASE)
    # Replace <br>, <p>, <div>, <li> with newlines
    text = re.sub(r'<br\s*/?>','\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</(p|div|tr|li|h[1-6])>', '\n', text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r'<[^>]+>', '', text)
    # Decode common HTML entities
    import html
    text = html.unescape(text)
    # Collapse excessive whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()
```

Then in `_body_text()`, wrap the HTML fallback paths:

```python
# Line 7217 -- change:
return payload.decode(charset, errors="replace")
# to:
return self._strip_html(payload.decode(charset, errors="replace"))
```

Also handle the non-multipart case (line 7220-7222) by checking `msg.get_content_type()`:

```python
if msg.get_content_type() == "text/html":
    return self._strip_html(payload.decode(charset, errors="replace"))
return payload.decode(charset, errors="replace")
```

**Impact**: Every Gmail HTML email (marketing emails, notifications, rich-text replies) is currently unreadable.

---

### P1-2. "Synced just now" shown for stale cached data (BUG)

**File**: `inbox.js:690`

**Issue**: In `_initInbox()`, when cached threads are served from `_threadCache`, the status text is set to `"synced just now"` even though the data could be up to 60 seconds old (or older if the user navigated away and came back after the cache expired but before a refresh completed).

Similarly at line 430, after every successful `syncInbox()` call, the status always says `"synced just now"` -- this is fine immediately after a sync, but the text never updates. If the user stares at the inbox for 5 minutes, it still says "just now."

**Fix (two-part)**:

Part A -- Show actual cache age when using cached data (line 690):
```javascript
// Replace line 690:
var age = Math.floor((Date.now() - cached.timestamp) / 1000);
var agoText = age < 60 ? 'just now' : Math.floor(age/60) + 'm ago';
document.querySelector('.inbox-status-text').textContent = emailAddress + ' \u00B7 synced ' + agoText;
```

Part B -- Update the timestamp periodically with a simple interval:
```javascript
// After syncInbox success (line 430), store the sync time:
_lastSyncTime = Date.now();

// Add a 30-second interval to update the status text:
setInterval(function() {
  if (!_lastSyncTime || !emailConfigured) return;
  var age = Math.floor((Date.now() - _lastSyncTime) / 1000);
  var statusText = document.querySelector('.inbox-status-text');
  if (!statusText) return;
  var agoText = age < 60 ? 'just now' : age < 3600 ? Math.floor(age/60) + 'm ago' : Math.floor(age/3600) + 'h ago';
  statusText.textContent = emailAddress + ' \u00B7 synced ' + agoText;
}, 30000);
```

---

### P1-3. Sync button has no success feedback (BUG)

**File**: `inbox.js:400-439` (`syncInbox()`)

**Issue**: When the user clicks "Sync Now," the button text changes to "Syncing..." with a spinner. On success, it silently reverts to "Sync Now" (line 414). The only signal is the status text changing to "synced just now" -- but that text was already there. There is no visual confirmation that the sync completed successfully vs. that nothing happened.

**Fix**: Add a brief success state to the button after sync completes:

```javascript
// After line 414, add a success flash:
btn.innerHTML = '<svg ...>...</svg> Synced!';
btn.style.borderColor = 'var(--pb-green)';
btn.style.color = 'var(--pb-green)';
setTimeout(function() {
  btn.innerHTML = '<svg ...>...</svg> Sync Now';
  btn.style.borderColor = '';
  btn.style.color = '';
}, 2000);
```

Also show a toast on sync if new messages arrived:
```javascript
var prevCount = _inboxThreads.length;
// ... after updating _inboxThreads ...
var newCount = _inboxThreads.length;
if (!silent && newCount > prevCount) {
  showToast((newCount - prevCount) + ' new message(s)');
}
```

---

### P1-4. Filter "Flagged" exists but flagging is impossible (BUG)

**Files**:
- HTML: `portal-pb-styled.html:402` (filter option `<option value="flagged">Flagged</option>`)
- JS: `inbox.js:576` (`filterInbox` checks for `.inbox-item-flag.flagged`)
- CSS: `panels.css:95-99` (flag styles defined)

**Issue**: The filter dropdown has a "Flagged" option, and the CSS has `.inbox-item-flag.flagged` styles, but the thread list rendering (`_renderThreadList`, line 376-388) never renders a flag element. The flag UI element referenced in `filterInbox` (`.inbox-item-flag.flagged`) does not exist in the DOM. Selecting "Flagged" always shows zero results.

**Fix (two options)**:

Option A (quick): Remove the "Flagged" filter option from the HTML since there is no flagging capability.

Option B (proper): Add a star/flag toggle to each thread item in `_renderThreadList` and persist flag state in `_threadCache` (client-side is fine for MVP):

```javascript
// In the thread item HTML generation (line 381-386), add before the closing </div>:
+ '<button class="inbox-item-flag" onclick="event.stopPropagation();toggleFlag(\'' + escHtml(t.thread_id) + '\',this)" title="Flag">'
+ '<svg viewBox="0 0 24 24"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/></svg></button>'
```

**Recommendation**: Option A for now. Flagging requires backend persistence to be meaningful.

---

### P1-5. `_initInbox` error silently swallowed (BUG)

**File**: `inbox.js:704` -- `.catch(function() {});`

**Issue**: If the `/api/inbox/status` call fails (network error, auth expired, server down), the error is completely swallowed. The user sees "Inbox not connected" with no way to know something went wrong, and no retry mechanism.

**Fix**:
```javascript
.catch(function(err) {
  _inboxInitDone = false; // Allow retry on next tab switch
  document.querySelector('.inbox-status-text').textContent = 'Connection error - tap Sync to retry';
  document.querySelector('.inbox-status-dot').style.background = 'var(--pb-red, #ef4444)';
});
```

---

## P2: Missing Features Users Expect

### P2-1. No sent messages view

**File**: `inbox.js` (entire file -- no sent mail functionality)

**Issue**: Users can compose and send emails, and reply to threads, but have no way to see what they sent. There is no "Sent" folder view.

**Fix approach**:
1. Add a "Sent" option to the filter dropdown (or a separate subtab)
2. Backend: add a `list_sent()` method that searches the "Sent" / "[Gmail]/Sent Mail" IMAP folder
3. Frontend: when filter is "sent," fetch from `/api/inbox/threads?folder=sent&account=X`
4. Reuse the same `_renderThreadList` for display

**Estimated effort**: Backend (30 min IMAP folder switch), Frontend (15 min filter wiring).

---

### P2-2. Search uses `window.prompt()` instead of inline search

**File**: `inbox.js:579-587` (`searchInbox()`)

**Issue**: Clicking the Search button triggers `prompt('Search inbox:')` -- a blocking browser dialog that is jarring, cannot be styled, breaks the visual flow, and is impossible to improve (no autocomplete, no clear button, no "X results found" feedback). Also, the search is client-side only on currently loaded thread text, missing subject/sender not visible in snippet.

**Fix**: Replace with an inline search bar that appears in the toolbar area:

```javascript
function searchInbox() {
  var toolbar = document.querySelector('.inbox-toolbar');
  if (document.getElementById('inboxSearchBar')) {
    // Toggle off
    document.getElementById('inboxSearchBar').remove();
    _renderThreadList(_inboxThreads); // Reset view
    return;
  }
  var bar = document.createElement('div');
  bar.id = 'inboxSearchBar';
  bar.style.cssText = 'display:flex;gap:8px;padding:8px 20px;border-bottom:1px solid var(--pb-border);align-items:center';
  bar.innerHTML = '<input id="inboxSearchInput" type="text" placeholder="Search messages..." '
    + 'style="flex:1;padding:6px 12px;background:var(--pb-bg);color:var(--pb-text);'
    + 'border:1px solid var(--pb-border);border-radius:6px;font-size:12px;font-family:inherit;box-sizing:border-box">'
    + '<button onclick="searchInbox()" style="background:none;border:none;color:var(--pb-text-dim);cursor:pointer;font-size:14px">&times;</button>';
  toolbar.parentNode.insertBefore(bar, toolbar.nextSibling);
  var input = document.getElementById('inboxSearchInput');
  input.focus();
  input.addEventListener('input', function() {
    var q = this.value.toLowerCase();
    document.querySelectorAll('.inbox-item').forEach(function(item) {
      item.style.display = item.textContent.toLowerCase().includes(q) ? '' : 'none';
    });
  });
}
```

---

### P2-3. No email forwarding

**File**: `portal-pb-styled.html:429`

**Issue**: The "Forward" button exists but is `disabled` with `title="Coming soon"`. Users who need to forward emails cannot.

**Effort estimate**: Medium -- requires a compose modal pre-filled with quoted content and a "To" field. Similar to reply flow.

---

### P2-4. No delete / archive capability

**File**: `inbox.js` (entire file)

**Issue**: There is no way to delete or archive emails. The action dropdown only has Reply and (disabled) Forward.

**Fix**: Add "Delete" and "Archive" to the `emailActionMenu`:
```html
<button onclick="mailoDraft('archive')">Archive</button>
<button onclick="mailoDraft('delete')" style="color:var(--pb-red,#ef4444)">Delete</button>
```
Backend needs IMAP STORE/COPY commands for flag/move.

---

### P2-5. No pagination or lazy loading for thread list

**File**: `inbox.js:369-388` (`_renderThreadList`)

**Issue**: All threads are rendered at once into the DOM. The backend fetches up to `limit * 3` recent UIDs (line 7244, default limit=30, so ~90 messages). For an active inbox, this means 90+ DOM nodes rendered immediately with no pagination or virtual scroll.

**Fix (simple "Load More" approach)**:
```javascript
var _THREADS_PER_PAGE = 20;
var _visibleCount = _THREADS_PER_PAGE;

function _renderThreadList(threads) {
  // ... existing empty state check ...
  var visible = threads.slice(0, _visibleCount);
  // render visible threads
  if (threads.length > _visibleCount) {
    html += '<div class="inbox-load-more" onclick="_loadMoreThreads()" '
      + 'style="text-align:center;padding:16px;color:var(--pb-blue);cursor:pointer;font-size:12px">'
      + 'Load more (' + (threads.length - _visibleCount) + ' remaining)</div>';
  }
  list.innerHTML = html;
}
```

---

### P2-6. No "To" recipients shown in email detail

**File**: `inbox.js:468-470`

**Issue**: The email detail header shows "From" and "Date" but not "To" or "CC." For threads with multiple participants, the user cannot see who received the email.

**Fix**: Add To/CC lines in `openThread()`:

```javascript
// After line 470, add:
if (msgs[0].to && msgs[0].to.length) {
  document.getElementById('emailDetailMeta').innerHTML +=
    '<span><strong>To:</strong> ' + escHtml(msgs[0].to.join(', ')) + '</span>';
}
if (msgs[0].cc && msgs[0].cc.length) {
  document.getElementById('emailDetailMeta').innerHTML +=
    '<span><strong>CC:</strong> ' + escHtml(msgs[0].cc.join(', ')) + '</span>';
}
```

HTML needs a wrapping element with an id (`emailDetailMeta`) around the meta spans for this to work cleanly.

---

## P3: Polish / Nice-to-Have Improvements

### P3-1. Zero keyboard navigation

**File**: `inbox.js` (entire file -- grep for `aria-`, `role=`, `tabindex`, `keydown` returns zero matches)

**Issue**: The entire inbox is mouse-only. Thread items are `<div onclick>` with no `tabindex`, no `role="listitem"`, no keyboard handlers. Users who Tab through the interface skip the inbox entirely. The compose modal has no Escape-to-close. The email detail has no keyboard back-navigation.

**Fix priorities**:
1. Add `tabindex="0"` and `role="option"` to thread items, `role="listbox"` to `.inbox-list`
2. Add Enter/Space handlers to open threads
3. Add Escape to close email detail, compose modal, reply box
4. Add arrow key navigation through thread list

Minimal keyboard support (compose modal):
```javascript
// In _openComposeModal, add:
overlay.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') overlay.remove();
});
```

---

### P3-2. Zero ARIA attributes / screen reader support

**File**: `portal-pb-styled.html:388-434`, `inbox.js` (all rendered HTML)

**Issue**: No `aria-label`, `aria-live`, `role` attributes anywhere. The unread badge has no `aria-label`. The loading state ("Loading messages...") is not an `aria-live` region. Screen readers cannot navigate the inbox.

**Fix highlights**:
- `.inbox-list` needs `role="list"` and `aria-label="Email threads"`
- Each `.inbox-item` needs `role="listitem"` and `aria-label` with sender + subject
- `.inbox-badge` needs `aria-label="X unread messages"`
- Loading/empty states need `role="status"` and `aria-live="polite"`
- Sync button needs `aria-label="Sync inbox"` and `aria-busy="true"` while syncing

---

### P3-3. Thread items lack visual hover feedback for touch targets

**File**: `panels.css:89` (`.inbox-item:hover{background:rgba(255,255,255,0.02)}`)

**Issue**: The hover effect is extremely subtle (`rgba(255,255,255,0.02)` = 2% white overlay). On most monitors this is invisible. Thread items feel unresponsive to hover.

**Fix**: Increase hover contrast:
```css
.inbox-item:hover { background: rgba(255,255,255,0.06); }
```

---

### P3-4. Compose modal has no unsaved-changes protection

**File**: `inbox.js:620` (`overlay.onclick = function(e) { if (e.target === overlay) overlay.remove(); }`)

**Issue**: Clicking outside the compose modal dismisses it instantly with no confirmation, even if the user has typed content. The Cancel button also dismisses without warning.

**Fix**:
```javascript
overlay.onclick = function(e) {
  if (e.target !== overlay) return;
  var body = document.getElementById('composeBody');
  var to = document.getElementById('composeTo');
  if ((body && body.value.trim()) || (to && to.value.trim())) {
    if (!confirm('Discard unsaved email?')) return;
  }
  overlay.remove();
};
```

---

### P3-5. No loading skeleton / shimmer for thread list

**File**: `inbox.js:391-398` (`_showInboxLoading()`)

**Issue**: The loading state is a single centered spinner with "Loading messages..." text. This causes layout shift when threads appear. A skeleton UI with 5-6 placeholder thread items would be smoother.

**Fix**: Replace spinner with skeleton items:
```javascript
function _showInboxLoading() {
  var list = document.querySelector('.inbox-list');
  if (!list) return;
  var html = '';
  for (var i = 0; i < 5; i++) {
    html += '<div class="inbox-item" style="animation:pulse 1.5s ease-in-out infinite;pointer-events:none">'
      + '<div class="inbox-item-body">'
      + '<div class="inbox-item-header"><span style="width:120px;height:12px;background:var(--pb-border);border-radius:4px;display:inline-block"></span>'
      + '<span style="width:40px;height:10px;background:var(--pb-border);border-radius:4px;display:inline-block"></span></div>'
      + '<div style="width:200px;height:12px;background:var(--pb-border);border-radius:4px;margin:6px 0 4px"></div>'
      + '<div style="width:280px;height:10px;background:var(--pb-border);border-radius:4px"></div>'
      + '</div></div>';
  }
  list.innerHTML = html;
}
```

---

### P3-6. Reply composer is plain text only, no quoting

**File**: `inbox.js:524-537` (`_openReplyComposer`)

**Issue**: The reply textarea is empty -- no quoted original message, no "On [date], [sender] wrote:" prefix. This is unusual for email and forces users to manually provide context.

**Fix**: Pre-fill the textarea with quoted content:
```javascript
function _openReplyComposer() {
  // Get the last message text for quoting
  var bodyEl = document.getElementById('emailDetailBody');
  var lastMsgDiv = bodyEl ? bodyEl.querySelector('div:last-child') : null;
  var quotedText = '';
  if (lastMsgDiv) {
    var fromEl = lastMsgDiv.querySelector('strong');
    var from = fromEl ? fromEl.textContent : '';
    quotedText = '\n\nOn ' + new Date().toLocaleDateString() + ', ' + from + ' wrote:\n> '
      + lastMsgDiv.querySelector('div:last-child').textContent.trim().replace(/\n/g, '\n> ');
  }
  // ... existing box creation ...
  // Set textarea value to quotedText
  document.getElementById('replyTextarea').value = quotedText;
  // Place cursor at top
  document.getElementById('replyTextarea').setSelectionRange(0, 0);
}
```

---

### P3-7. Account tab "X" remove button too easy to accidentally click

**File**: `inbox.js:153`, `panels.css:60-62`

**Issue**: The remove-account "X" button appears on hover of the account tab. It is very close to the tab label. On touch devices, hover is triggered on tap, making accidental deletion easy. While there is a `confirm()` dialog, this is still jarring.

**Fix**: Move the remove button to account settings or use a long-press gesture. At minimum, increase the hit area gap:
```css
#inboxView .inbox-acct-remove { margin-left: 8px; padding: 2px 4px; }
```

---

### P3-8. Thread time display inconsistencies

**File**: `inbox.js:359-367` (`_inboxTimeAgo`)

**Issue**: The function shows "just now" for <60s, "Xm ago" for <1h, "Xh ago" for <24h, "Xd ago" for <7d, then jumps to `toLocaleDateString()` (e.g., "5/28/2026"). The jump from relative to absolute time is jarring. Also, no tooltip with exact date/time.

**Fix**: Add a tooltip with the full date on every time element, and smooth the transition:
```javascript
// In _renderThreadList, change time span:
'<span class="inbox-item-time" title="' + new Date(t.timestamp).toLocaleString() + '">'
  + _inboxTimeAgo(t.timestamp) + '</span>'
```

---

### P3-9. Empty state messaging is confusing when filtered

**File**: `inbox.js:572-577` (`filterInbox`)

**Issue**: When the user selects "Unread" filter and no unread emails exist, all items are hidden but the empty state from `_renderThreadList` does not show -- the user just sees a blank list. There is no "No unread messages" contextual empty state.

**Fix**: After filtering, check if any items are visible. If none, show a filter-specific empty message:
```javascript
function filterInbox(val) {
  var anyVisible = false;
  document.querySelectorAll('.inbox-item').forEach(function(item) {
    var show = val === 'all'
      || (val === 'unread' && item.classList.contains('unread'))
      || (val === 'flagged' && item.querySelector('.inbox-item-flag.flagged'));
    item.style.display = show ? '' : 'none';
    if (show) anyVisible = true;
  });
  var emptyMsg = document.getElementById('inboxFilterEmpty');
  if (!anyVisible && document.querySelectorAll('.inbox-item').length > 0) {
    if (!emptyMsg) {
      emptyMsg = document.createElement('div');
      emptyMsg.id = 'inboxFilterEmpty';
      emptyMsg.style.cssText = 'text-align:center;padding:40px 20px;color:var(--pb-text-dim);font-size:13px';
    }
    emptyMsg.textContent = val === 'unread' ? 'No unread messages' : 'No flagged messages';
    document.querySelector('.inbox-list').appendChild(emptyMsg);
  } else if (emptyMsg) {
    emptyMsg.remove();
  }
}
```

---

### P3-10. Compose modal input validation is minimal

**File**: `inbox.js:641`

**Issue**: Only checks `!to || !text` -- no email format validation on the "To" field. Subject is optional (which is fine) but no hint that it will send without subject.

**Fix**: Add basic email validation:
```javascript
if (!to.match(/^[^\s@]+@[^\s@]+\.[^\s@]+$/)) {
  showToast('Please enter a valid email address');
  return;
}
if (!subject && !confirm('Send without a subject?')) return;
```

---

### P3-11. Inline reply "Send Reply" button has no keyboard shortcut

**File**: `inbox.js:530-533`

**Issue**: Ctrl+Enter / Cmd+Enter to send is standard email UX. The reply textarea has no such handler.

**Fix**:
```javascript
document.getElementById('replyTextarea').addEventListener('keydown', function(e) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    e.preventDefault();
    _sendReply();
  }
});
```

Same for compose modal textarea.

---

### P3-12. Account switching has no transition animation

**File**: `inbox.js:160-178` (`switchAccount`)

**Issue**: Switching accounts either instantly shows cached threads or shows a loading spinner. There is no fade/slide transition, making the switch feel abrupt especially when cached.

**Low priority** -- functional but unpolished.

---

## Summary Table

| ID | Priority | Issue | File:Line | Effort |
|----|----------|-------|-----------|--------|
| P1-1 | P1 CRITICAL | HTML emails show raw tags | portal_server.py:7210-7217 | 30 min |
| P1-2 | P1 | "Synced just now" for stale data | inbox.js:430,690 | 15 min |
| P1-3 | P1 | No sync success feedback | inbox.js:414 | 10 min |
| P1-4 | P1 | Flagged filter broken (no flag UI) | portal-pb-styled.html:402 | 5 min (remove option) |
| P1-5 | P1 | Init error silently swallowed | inbox.js:704 | 5 min |
| P2-1 | P2 | No sent messages view | inbox.js (new) + server | 45 min |
| P2-2 | P2 | Search uses prompt() | inbox.js:579-587 | 20 min |
| P2-3 | P2 | No forwarding capability | inbox.js + server | 1 hr |
| P2-4 | P2 | No delete/archive | inbox.js + server | 45 min |
| P2-5 | P2 | No pagination | inbox.js:369-388 | 20 min |
| P2-6 | P2 | No To/CC in email detail | inbox.js:468 | 10 min |
| P3-1 | P3 | Zero keyboard navigation | inbox.js (whole file) | 1 hr |
| P3-2 | P3 | Zero ARIA / screen reader | inbox.js + HTML | 45 min |
| P3-3 | P3 | Hover feedback too subtle | panels.css:89 | 2 min |
| P3-4 | P3 | No unsaved-changes guard on compose | inbox.js:620 | 10 min |
| P3-5 | P3 | No loading skeleton | inbox.js:391-398 | 15 min |
| P3-6 | P3 | Reply has no quoted text | inbox.js:524-537 | 15 min |
| P3-7 | P3 | Account remove button too easy to hit | panels.css:60 | 5 min |
| P3-8 | P3 | Time display jump + no tooltip | inbox.js:359-367 | 10 min |
| P3-9 | P3 | No empty state when filtering | inbox.js:572-577 | 10 min |
| P3-10 | P3 | No email validation on compose | inbox.js:641 | 5 min |
| P3-11 | P3 | No Ctrl+Enter to send | inbox.js:530 | 5 min |
| P3-12 | P3 | No account-switch transition | inbox.js:160 | 15 min |

---

## Recommended Fix Order

**Sprint 1 (1-2 hours, highest ROI)**:
1. P1-1: Fix HTML email rendering (this is the single biggest user-facing bug)
2. P1-3: Sync success feedback (quick win, high visibility)
3. P1-2: Fix "synced just now" staleness
4. P1-4: Remove broken "Flagged" filter option
5. P1-5: Surface init errors

**Sprint 2 (2-3 hours, feature gaps)**:
6. P2-2: Inline search bar
7. P2-6: Show To/CC in detail view
8. P2-5: Simple pagination
9. P2-1: Sent messages view

**Sprint 3 (ongoing polish)**:
10. P3-11: Ctrl+Enter to send (tiny effort, big UX win)
11. P3-4: Unsaved changes guard
12. P3-3: Better hover feedback
13. P3-6: Quoted text in replies
14. P3-9: Filter empty states
15. P3-1 + P3-2: Keyboard + ARIA (batched together)
