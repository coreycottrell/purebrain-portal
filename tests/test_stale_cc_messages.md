# Bug #4: Frontend knownMsgIds Cleared on Reconnect (chat.js:464)

## Bug Description

`loadChatHistory()` clears `knownMsgIds` before fetching history. If a WebSocket
push arrives between the clear and the history load completing, the pushed message
is rendered. Then when history loads, the same message is in the history response
and renders AGAIN (because knownMsgIds was cleared and no longer remembers the
WS-pushed message).

## Race Condition Timeline

```
T0: loadChatHistory() called (e.g., on WS reconnect)
T1: knownMsgIds.clear()         <-- all dedup memory gone
T2: fetch("/api/chat/history")  <-- async, takes ~200ms
T3: WS pushes msg_id=500        <-- arrives during fetch
T4: msg_id=500 rendered          <-- knownMsgIds is empty, passes dedup
T5: history response arrives with msg_id=500
T6: msg_id=500 rendered AGAIN    <-- duplicate!
```

## What the Fix Should Do

- Option A: Do NOT clear knownMsgIds before fetching. Instead, build a new
  set from the history response and merge with existing.
- Option B: Clear knownMsgIds AFTER history is loaded, not before.
- Option C: Buffer WS messages during history load and reconcile after.

## Test Specification (for future JS test)

### Test: WS push during history reload does not cause duplicate render

**Setup:**
1. Populate knownMsgIds with {400, 401, 402}
2. Mock fetch("/api/chat/history") to return messages [400, 401, 402, 500] with 100ms delay
3. Call loadChatHistory()
4. Before fetch resolves, simulate WS push of msg_id=500

**Assert:**
- msg_id=500 is rendered exactly ONCE in the DOM
- No duplicate DOM nodes for any message

### Test: knownMsgIds survives history reload

**Setup:**
1. Populate knownMsgIds with {400, 401}
2. Call loadChatHistory()
3. After completion, check knownMsgIds

**Assert:**
- knownMsgIds contains all IDs from the previous set AND all IDs from history response
- No IDs were lost during the reload

### Test: Rapid reconnect does not cause duplicates

**Setup:**
1. Trigger loadChatHistory() twice in quick succession (simulating rapid reconnect)
2. Both fetches return overlapping message sets

**Assert:**
- Each message is rendered exactly once
- No DOM duplicates

## Implementation Notes

- Test framework: likely needs Playwright or jsdom for DOM testing
- chat.js location: `/home/aiciv/purebrain_portal/portal-pb-styled.html` (inline JS)
  or separate JS file depending on build
- WS mock: use a mock WebSocket that can push messages on demand
