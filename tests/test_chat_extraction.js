#!/usr/bin/env node
/**
 * Chat IIFE Extraction -- Pre/Post Verification Tests
 *
 * Tests that the main Chat IIFE ("1. REAL Chat Integration") is correctly
 * extracted from portal-pb-styled.html into static/js/features/chat.js.
 *
 * The Chat IIFE is the BIGGEST inline IIFE (~1076 lines) and handles:
 * - Token/auth management
 * - WebSocket chat connection
 * - Chat history loading
 * - Message rendering with markdown
 * - Agent identity detection
 * - File upload with compression/preview
 * - Optimistic message sending
 *
 * These tests should FAIL before extraction and PASS after.
 *
 * Usage: node tests/test_chat_extraction.js
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const HTML_FILE = path.join(ROOT, 'portal-pb-styled.html');
const CHAT_JS = path.join(ROOT, 'static', 'js', 'features', 'chat.js');

let passed = 0;
let failed = 0;

function assert(condition, msg) {
  if (condition) {
    passed++;
    console.log(`  PASS: ${msg}`);
  } else {
    failed++;
    console.error(`  FAIL: ${msg}`);
  }
}

// =============================================================================
// SECTION 1: Extracted File Exists and Has Correct Structure
// =============================================================================
console.log('\n=== Section 1: Extracted File Structure ===');

assert(fs.existsSync(CHAT_JS), 'static/js/features/chat.js exists');

if (!fs.existsSync(CHAT_JS)) {
  console.error('\nFATAL: chat.js does not exist yet. Remaining tests will reference empty content.');
}

const chatCode = fs.existsSync(CHAT_JS) ? fs.readFileSync(CHAT_JS, 'utf8') : '';

// IIFE wrapper
const trimmed = chatCode.trim();
assert(trimmed.startsWith('(function(){') || trimmed.startsWith('(function () {'),
  'File starts with IIFE opening: (function(){');
assert(trimmed.endsWith('})();'),
  'File ends with IIFE closing: })();');

// 'use strict' directive
assert(chatCode.includes("'use strict'"), "File contains 'use strict' directive");

// =============================================================================
// SECTION 2: Global Exposure (window._portalChat with all methods)
// =============================================================================
console.log('\n=== Section 2: Global Exposure (window._portalChat) ===');

assert(chatCode.includes('window._portalChat'),
  'Extracted file exposes window._portalChat');

// All methods on the _portalChat object
const portalChatMethods = [
  'getToken',
  'setToken',
  'sendMessage',
  'loadHistory',
  'reconnectWS',
  'getCivName',
];

for (const method of portalChatMethods) {
  assert(chatCode.includes(method),
    `window._portalChat exposes ${method}`);
}

// =============================================================================
// SECTION 3: Key Functions Present (12+ internal functions)
// =============================================================================
console.log('\n=== Section 3: Key Functions Present ===');

const requiredFunctions = [
  'safeJson',
  'formatTime',
  'renderMarkdown',
  'addCodeCopyButtons',
  'detectAgentIdentity',
  'buildMessageEl',
  'addMessage',
  'addThinkingIndicator',
  'removeThinkingIndicator',
  'loadChatHistory',
  'connectChatWS',
  'sendMessage',
  'uploadPendingFiles',
  'addFileImageMessage',
];

for (const fn of requiredFunctions) {
  assert(chatCode.includes('function ' + fn),
    `Function ${fn} is defined in extracted file`);
}

// =============================================================================
// SECTION 4: Auth/Token Management
// =============================================================================
console.log('\n=== Section 4: Auth/Token Management ===');

assert(chatCode.includes("localStorage.getItem('portal_token')"),
  'Uses localStorage.getItem(portal_token) for auth');

assert(chatCode.includes("localStorage.setItem('portal_token'"),
  'Uses localStorage.setItem(portal_token) to persist token');

assert(chatCode.includes("'portal-auth'"),
  'Dispatches portal-auth custom event on token set');

assert(chatCode.includes('CustomEvent'),
  'Uses CustomEvent constructor for portal-auth dispatch');

assert(chatCode.includes('function doAuth') || chatCode.includes('function getToken'),
  'Contains auth flow function (doAuth or getToken)');

// URL param token extraction (auto-auth from URL)
assert(chatCode.includes('URLSearchParams'),
  'Reads token from URL query params (auto-auth flow)');

// =============================================================================
// SECTION 5: API Endpoints (4+ endpoints referenced)
// =============================================================================
console.log('\n=== Section 5: API Endpoints ===');

assert(chatCode.includes('/api/chat/history'),
  'References /api/chat/history endpoint');

assert(chatCode.includes('/api/chat/send'),
  'References /api/chat/send endpoint');

assert(chatCode.includes('/api/status'),
  'References /api/status endpoint (auth validation)');

assert(chatCode.includes('/health'),
  'References /health endpoint (civ name detection)');

// =============================================================================
// SECTION 6: WebSocket Code Present
// =============================================================================
console.log('\n=== Section 6: WebSocket Code ===');

assert(chatCode.includes('new WebSocket'),
  'Contains WebSocket constructor call');

assert(chatCode.includes('/ws/chat'),
  'References /ws/chat WebSocket endpoint');

assert(chatCode.includes('onopen'),
  'Has WebSocket onopen handler');

assert(chatCode.includes('onmessage'),
  'Has WebSocket onmessage handler');

assert(chatCode.includes('onclose'),
  'Has WebSocket onclose handler');

// Reconnection logic
assert(chatCode.includes('_wsReconnectDelay'),
  'Contains WebSocket reconnect delay variable');

assert(chatCode.includes('visibilitychange'),
  'Reconnects on tab visibility change (iOS resume)');

// =============================================================================
// SECTION 7: Markdown Rendering
// =============================================================================
console.log('\n=== Section 7: Markdown Rendering ===');

assert(chatCode.includes('escHtml'),
  'References escHtml helper for XSS protection');

assert(chatCode.includes('<strong>'),
  'Renders bold markdown (**text**)');

assert(chatCode.includes('<em>'),
  'Renders italic markdown (*text*)');

assert(chatCode.includes('<code>'),
  'Renders inline code markdown (`code`)');

assert(chatCode.includes('<pre>'),
  'Renders code blocks (```code```)');

assert(chatCode.includes('<h1>') || chatCode.includes('h[1-6]'),
  'Renders heading markdown (# heading)');

// Link security (XSS prevention)
assert(chatCode.includes('javascript:'),
  'Checks for dangerous javascript: scheme in links');

// =============================================================================
// SECTION 8: Chat IIFE Removed from HTML
// =============================================================================
console.log('\n=== Section 8: Chat IIFE Removed from HTML ===');

const html = fs.readFileSync(HTML_FILE, 'utf8');

assert(!html.includes('// 1. REAL Chat Integration'),
  'Section 1 comment marker ("// 1. REAL Chat Integration") removed from HTML');

// Verify key function definitions are gone from inline HTML
// Note: safeJson also exists as a GLOBAL helper (used by referral, client panels etc.)
// so we allow <= 1 occurrence (the global copy). The Chat IIFE's local copy was extracted.
const safeJsonInHtml = (html.match(/function safeJson/g) || []).length;
assert(safeJsonInHtml <= 1,
  'function safeJson not duplicated in HTML (at most 1 global helper remains)');

const renderMarkdownInHtml = (html.match(/function renderMarkdown/g) || []).length;
assert(renderMarkdownInHtml === 0,
  'function renderMarkdown no longer defined inline in HTML');

const connectChatWSInHtml = (html.match(/function connectChatWS/g) || []).length;
assert(connectChatWSInHtml === 0,
  'function connectChatWS no longer defined inline in HTML');

const detectAgentInHtml = (html.match(/function detectAgentIdentity/g) || []).length;
assert(detectAgentInHtml === 0,
  'function detectAgentIdentity no longer defined inline in HTML');

// =============================================================================
// SECTION 9: Script Tag Added to HTML
// =============================================================================
console.log('\n=== Section 9: Script Tag in HTML ===');

assert(html.includes('<script src="/static/js/features/chat.js"></script>'),
  'HTML includes <script src="/static/js/features/chat.js"></script>');

// Verify ordering: chat.js should appear after dock.js (it depends on global helpers)
const dockPos = html.indexOf('features/dock.js');
const chatPos = html.indexOf('features/chat.js');

assert(chatPos > dockPos,
  'chat.js script tag appears after dock.js');

// chat.js should appear before cc-chat.js (cc-chat is a different IIFE)
const ccChatPos = html.indexOf('features/cc-chat.js');
if (ccChatPos > -1) {
  assert(chatPos < ccChatPos,
    'chat.js script tag appears before cc-chat.js');
}

// =============================================================================
// SECTION 10: No Duplicate Globals
// =============================================================================
console.log('\n=== Section 10: No Duplicate Globals ===');

// window._portalChat should appear zero times in the HTML
// (it was moved entirely to chat.js)
const portalChatInHtml = (html.match(/window\._portalChat\s*=/g) || []).length;
assert(portalChatInHtml === 0,
  'window._portalChat assignment does not appear in HTML (fully extracted)');

// It should appear exactly once in the extracted file (the assignment)
const portalChatInJs = (chatCode.match(/window\._portalChat\s*=/g) || []).length;
assert(portalChatInJs === 1,
  'window._portalChat assignment appears exactly once in chat.js');

// =============================================================================
// SECTION 11: Sanity Checks
// =============================================================================
console.log('\n=== Section 11: Sanity Checks ===');

if (chatCode.length > 0) {
  // The IIFE is ~1076 lines (3934-5009). Extracted file should be similar size.
  const lineCount = chatCode.split('\n').length;
  assert(lineCount >= 900, `Extracted file has >= 900 lines (got ${lineCount}; expect ~1076)`);
  // Bound raised 1300 -> 1500: the file has organically grown past the original
  // ~1076-line IIFE (reply linking, drag/drop, paste, and the restored PORTAL_FILE
  // card renderer ~110 lines). 1500 still guards against accidental bloat.
  assert(lineCount <= 1500, `Extracted file has <= 1500 lines (got ${lineCount}; not bloated)`);

  // File should be non-trivial size (the biggest IIFE, expect ~35-50KB)
  assert(chatCode.length >= 25000, `File size >= 25KB (got ${chatCode.length}; not a stub)`);
}

// Adjacent IIFEs should still exist in HTML -- we did NOT accidentally remove them
assert(html.includes('// 2. Poke AI'),
  'Section 2 (Poke AI IIFE) still present in HTML (not accidentally removed)');

assert(html.includes('// 3. Chat Search'),
  'Section 3 (Chat Search IIFE) still present in HTML (not accidentally removed)');

// _loadTopics function is defined OUTSIDE the chat IIFE (at ~line 2267)
// so it should still be in the HTML
assert(html.includes('function _loadTopics'),
  '_loadTopics still defined in HTML (global helper, not part of chat IIFE)');

// Global escHtml should still be defined in HTML (used by chat.js as external dependency)
assert(html.includes('function escHtml'),
  'escHtml still defined in HTML (global helper, not part of chat IIFE)');

// showToast should still be defined in HTML (used by chat.js as external dependency)
assert(html.includes('showToast'),
  'showToast still referenced in HTML (global helper)');

// --- Summary ---
console.log(`\n${'='.repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed, ${passed + failed} total`);
if (failed > 0) {
  process.exit(1);
} else {
  console.log('All tests passed!');
}
